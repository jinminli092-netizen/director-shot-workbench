"""Local clip analysis: shot boundaries and thumbnails via ffmpeg.

Local mode keeps the video on this machine. Deployed mode can route the cut
through a cloud provider (Volcengine) while ffmpeg still cuts the returned
boundaries into local shot clips.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import boundaries, cut_providers, video_shots

CUT_RE = re.compile(r"pts_time:([0-9.]+)")


class MediaError(Exception):
    pass


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def probe_duration(path: str) -> float:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
        ],
        timeout=60,
    )
    if result.returncode != 0:
        raise MediaError("ffprobe 读不了这个文件：" + (result.stderr or "")[:200])
    try:
        return round(float(result.stdout.strip()), 2)
    except ValueError as error:
        raise MediaError("读不到视频时长。") from error


def probe_fps(path: str) -> float:
    result = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=nw=1:nk=1", path,
        ],
        timeout=60,
    )
    raw = (result.stdout or "").strip()
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            value = float(numerator) / float(denominator or 1)
        else:
            value = float(raw)
        return round(value, 3) if value > 1 else 25.0
    except (ValueError, ZeroDivisionError):
        return 25.0


def detect_cuts(path: str, threshold: float = 0.3) -> list[float]:
    result = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-i",
            path,
            "-filter:v",
            f"select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    cuts = sorted({round(float(value), 2) for value in CUT_RE.findall(result.stderr)})
    cleaned: list[float] = []
    for cut in cuts:
        if cut < 0.25:
            continue
        if cleaned and cut - cleaned[-1] < 0.35:
            continue
        cleaned.append(cut)
    return cleaned


def extract_thumb(path: str, at: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            f"{max(0.0, at):.3f}",
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-1",
            str(out),
            "-y",
        ],
        timeout=120,
    )
    if result.returncode != 0 or not out.exists():
        raise MediaError("抽帧失败：" + (result.stderr or "")[:200])


def concat_clips(paths: list[Path], out: Path) -> None:
    """Join already-encoded shot clips in the order the user chose."""
    if not paths:
        raise MediaError("没有可合并的片段。")
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".txt")
    lines = []
    for item in paths:
        escaped = str(item).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    listing.write_text("\n".join(lines), encoding="utf-8")
    try:
        result = _run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", "-movflags", "+faststart", str(out), "-y",
            ],
            timeout=600,
        )
        if result.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
            raise MediaError(result.stderr.strip()[-400:] or "拼接失败。")
    finally:
        listing.unlink(missing_ok=True)


def extract_clip(path: str, start: float, end: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    length = max(0.15, float(end) - float(start))
    result = _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{max(0.0, start):.3f}",
            "-i", path,
            "-t", f"{length:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(out), "-y",
        ],
        timeout=300,
    )
    if result.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
        out.unlink(missing_ok=True)
        raise MediaError("切片段失败：" + (result.stderr or "")[:200])


def pull_apart(
    path: str,
    media_dir: Path,
    max_shots: int = 120,
    extra_cuts: list[float] | None = None,
    remove_cuts: list[float] | None = None,
    mode: str = "model",
    provider: str | None = None,
    provider_config: dict | None = None,
    base_boundaries: list[float] | None = None,
    reuse_shots: list[dict] | None = None,
) -> dict:
    if not Path(path).exists():
        raise MediaError("找不到这个视频文件。")
    media_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(path)
    if base_boundaries is not None:
        automatic = [
            round(float(value), 2)
            for value in base_boundaries
            if 0.2 < float(value) < duration - 0.2
        ]
        automatic = sorted(set(automatic))
        info = {
            "cuts": [{"time": value, "score": 1.0} for value in automatic],
            "hard_boundaries": automatic,
            "transitions": [],
            "boundaries": automatic,
        }
    else:
        selected = (provider or cut_providers.active()).strip().lower()
        if selected == "volcengine":
            try:
                model_scenes = cut_providers.detect_volcengine(path, provider_config)
            except cut_providers.CutProviderError as error:
                detail = f"（{error.detail}）" if error.detail else ""
                raise MediaError(error.message + detail) from error
        else:
            model_scenes = video_shots.detect(path) if mode == "model" else []
        if model_scenes:
            cuts = [
                {"time": scene["start"], "score": scene.get("probability", 1.0)}
                for scene in model_scenes
                if 0.2 < float(scene["start"]) < duration - 0.2
            ]
            info = {
                "cuts": cuts,
                "hard_boundaries": [row["time"] for row in cuts],
                "transitions": [],
                "boundaries": [row["time"] for row in cuts],
            }
            automatic = list(info["boundaries"])
        else:
            info = boundaries.detect(path, duration)
            automatic = list(
                info["hard_boundaries"] if mode != "all" else info["boundaries"]
            )
    for removal in remove_cuts or []:
        automatic = [
            value for value in automatic if abs(float(value) - float(removal)) > 0.35
        ]
    manual = [
        round(float(value), 2)
        for value in (extra_cuts or [])
        if 0.2 < float(value) < duration - 0.2
    ]
    boundaries_list = [0.0] + automatic + manual + [duration]
    boundaries_list = sorted({round(value, 2) for value in boundaries_list})
    reuse = {}
    for shot in reuse_shots or []:
        try:
            key = (round(float(shot["start_seconds"]), 2), round(float(shot["end_seconds"]), 2))
        except (KeyError, TypeError, ValueError):
            continue
        reuse[key] = shot
    clip_counts: dict[str, int] = {}
    for shot in reuse.values():
        clip_key = str(shot.get("clip_path") or "")
        if clip_key:
            clip_counts[clip_key] = clip_counts.get(clip_key, 0) + 1
    shots = []
    for index in range(len(boundaries_list) - 1):
        start = boundaries_list[index]
        end = boundaries_list[index + 1]
        if end - start < 0.25:
            continue
        if len(shots) >= max_shots:
            break
        number = len(shots) + 1
        stamp = int(round(start * 1000))
        old = reuse.get((round(start, 2), round(end, 2)))
        if old and clip_counts.get(str(old.get("clip_path") or ""), 0) > 1:
            old = None
        thumb = None
        if old:
            old_thumb = str(old.get("thumb_path") or "")
            if old_thumb and (media_dir / old_thumb).exists():
                thumb = media_dir / old_thumb
        if thumb is None:
            # Never reuse a fixed shot_NNN name here: a newly split shot would
            # overwrite the clip a later reused shot still points at.
            thumb = media_dir / f"shot_{number:03d}_{stamp:07d}.jpg"
            try:
                extract_thumb(path, (start + end) / 2, thumb)
            except MediaError:
                thumb = None
        clip = None
        if old:
            old_clip = str(old.get("clip_path") or "")
            candidate = media_dir / old_clip if old_clip else None
            if candidate and candidate.exists() and candidate.stat().st_size >= 1024:
                clip = candidate
        if clip is None:
            clip = media_dir / f"shot_{number:03d}_{stamp:07d}.mp4"
            try:
                extract_clip(path, start, end, clip)
            except MediaError:
                clip = None
        if clip is not None and (not clip.exists() or clip.stat().st_size < 1024):
            clip.unlink(missing_ok=True)
            clip = None
        shots.append(
            {
                "index": number,
                "start": round(start, 2),
                "end": round(end, 2),
                "thumb": str(thumb.relative_to(media_dir)) if thumb else "",
                "clip": str(clip.relative_to(media_dir)) if clip else "",
            }
        )
    transitions = [
        {
            "at": transition["mid"],
            "start": transition["start"],
            "end": transition["end"],
            "type": transition["type"],
            "confidence": transition.get("confidence", "candidate"),
            "peak": transition.get("peak", 0),
        }
        for transition in info["transitions"]
    ]
    for cut in info["cuts"]:
        transitions.append(
            {
                "at": cut["time"],
                "start": cut["time"],
                "end": cut["time"],
                "type": "cut",
                "confidence": "high",
                "peak": cut["score"],
            }
        )
    for value in manual:
        transitions.append(
            {
                "at": value,
                "start": value,
                "end": value,
                "type": "cut",
                "confidence": "manual",
                "peak": 1.0,
            }
        )
    transitions.sort(key=lambda item: item["at"])
    return {
        "duration": duration,
        "fps": probe_fps(path),
        "shots": shots,
        "transitions": transitions,
    }
