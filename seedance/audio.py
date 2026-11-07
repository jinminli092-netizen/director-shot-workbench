"""Separate audio track: loudness, silence, onsets, tempo, sections.

No music-tagging model and no downloads: pure signal analysis on the decoded
PCM. It can't name the instrument or say "this is sad", but it can tell you
where the beats are, where it goes quiet, and whether a cut lands on a beat.
"""

from __future__ import annotations

import array
import json
import math
import subprocess
from pathlib import Path

HOP = 0.025
MIN_ONSET_GAP = 0.08


class AudioError(Exception):
    pass


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def probe(path: str) -> dict:
    result = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,channels,sample_rate",
            "-of", "json", path,
        ],
        timeout=60,
    )
    try:
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except json.JSONDecodeError:
        streams = []
    if not streams:
        return {"has_audio": False, "codec": "", "channels": 0, "sample_rate": 0}
    stream = streams[0]
    return {
        "has_audio": True,
        "codec": str(stream.get("codec_name") or ""),
        "channels": int(stream.get("channels") or 0),
        "sample_rate": int(stream.get("sample_rate") or 0),
    }


def _extract_wav(path: str, out: Path) -> bool:
    result = _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
            str(out), "-y",
        ]
    )
    return result.returncode == 0 and out.exists()


def _waveform(path: str, out: Path) -> bool:
    result = _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", path, "-filter_complex",
            "[0:a]showwavespic=s=900x140:colors=#e0a458",
            "-frames:v", "1", str(out), "-y",
        ]
    )
    return result.returncode == 0 and out.exists()


def _rms(values: array.array, start: int, end: int) -> float:
    if end <= start:
        return 0.0
    total = 0
    for index in range(start, end):
        value = values[index]
        total += value * value
    return math.sqrt(total / (end - start))


def analyze(path: str, media_dir: Path) -> dict:
    info = probe(path)
    if not info["has_audio"]:
        return {"has_audio": False, "note": "视频没有音轨"}

    wav = media_dir / "audio.wav"
    if not _extract_wav(path, wav):
        raise AudioError("音频抽取失败。")
    with open(wav, "rb") as handle:
        import wave

        with wave.open(handle, "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
            raw = reader.readframes(frames)
    wav.unlink(missing_ok=True)

    samples = array.array("h")
    samples.frombytes(raw)
    if rate != 16000:
        rate = 16000  # the extractor resamples; this is a guard only
    window = max(1, int(rate * HOP))
    rms_values = [
        _rms(samples, start, min(start + window, len(samples)))
        for start in range(0, len(samples), window)
    ]
    peak = max(rms_values) if rms_values else 1.0
    normalized = [value / peak if peak else 0.0 for value in rms_values]
    loudness = 20 * math.log10(max(peak, 1e-6) / 32768.0)
    duration = round(len(samples) / rate, 2)

    if peak < 100:  # quieter than about -50 dBFS: treat as a silent track
        waveform = media_dir / "waveform.png"
        has_wave = _waveform(path, waveform)
        return {
            "has_audio": True,
            "silent": True,
            "codec": info["codec"],
            "channels": info["channels"],
            "sample_rate": info["sample_rate"],
            "duration": duration,
            "loudness_db": round(loudness, 1),
            "bpm": None,
            "onset_count": 0,
            "onsets": [],
            "silence": [[0.0, duration]],
            "sections": [{"start": 0.0, "end": duration, "level": "silent"}],
            "energy_curve": [0.0] * 200,
            "waveform": "waveform.png" if has_wave else "",
            "note": "这条音轨几乎是静音（-50 dBFS 以下），没有可测的节拍或台词。",
        }

    flux = [0.0]
    for index in range(1, len(normalized)):
        flux.append(max(0.0, normalized[index] - normalized[index - 1]))
    mean = sum(flux) / max(1, len(flux))
    variance = sum((value - mean) ** 2 for value in flux) / max(1, len(flux))
    deviation = math.sqrt(variance)

    onsets: list[float] = []
    for index in range(1, len(flux) - 1):
        if flux[index] < mean + deviation * 1.1:
            continue
        if flux[index] < flux[index - 1] or flux[index] < flux[index + 1]:
            continue
        time = index * HOP
        if onsets and time - onsets[-1] < MIN_ONSET_GAP:
            continue
        onsets.append(round(time, 2))

    bpm = _tempo(flux)

    silence: list[list[float]] = []
    start = None
    for index, value in enumerate(normalized):
        if value < 0.03:
            if start is None:
                start = index
        elif start is not None:
            if (index - start) * HOP >= 0.4:
                silence.append([round(start * HOP, 2), round(index * HOP, 2)])
            start = None
    if start is not None and (len(normalized) - start) * HOP >= 0.4:
        silence.append([round(start * HOP, 2), round(len(normalized) * HOP, 2)])

    sections = _sections(normalized)

    waveform = media_dir / "waveform.png"
    has_wave = _waveform(path, waveform)

    points = 200
    step = max(1, len(normalized) // points)
    curve = [round(normalized[index], 3) for index in range(0, len(normalized), step)][:points]

    return {
        "has_audio": True,
        "codec": info["codec"],
        "channels": info["channels"],
        "sample_rate": info["sample_rate"],
        "duration": round(len(samples) / rate, 2),
        "loudness_db": round(loudness, 1),
        "bpm": bpm,
        "onset_count": len(onsets),
        "onsets": onsets[:400],
        "silence": silence,
        "sections": sections,
        "energy_curve": curve,
        "waveform": "waveform.png" if has_wave else "",
        "note": "纯信号分析：不识别曲名、乐器、歌词或情绪标签。",
    }


def _tempo(flux: list[float]) -> float | None:
    """Autocorrelation over 0.33–1.0 秒 lags (60–180 BPM)."""
    if len(flux) < 40:
        return None
    best_lag = 0
    best_score = 0.0
    for lag in range(12, 41):
        score = 0.0
        for index in range(lag, len(flux)):
            score += flux[index] * flux[index - lag]
        if score > best_score:
            best_score = score
            best_lag = lag
    if not best_lag:
        return None
    bpm = 60.0 / (best_lag * HOP)
    return round(bpm, 1)


def _sections(normalized: list[float]) -> list[dict]:
    if not normalized:
        return []
    bucket = max(1, int(5.0 / HOP))
    levels = []
    for start in range(0, len(normalized), bucket):
        chunk = normalized[start : start + bucket]
        levels.append(round(sum(chunk) / max(1, len(chunk)), 3))
    median = sorted(levels)[len(levels) // 2] if levels else 0
    sections: list[dict] = []
    for index, level in enumerate(levels):
        name = "high" if level > median * 1.35 else "low" if level < median * 0.65 else "mid"
        start = round(index * 5.0, 1)
        end = round(min((index + 1) * 5.0, len(normalized) * HOP), 1)
        if sections and sections[-1]["level"] == name:
            sections[-1]["end"] = end
        else:
            sections.append({"start": start, "end": end, "level": name})
    return sections


def beat_sync(transitions: list[dict], onsets: list[float], tolerance: float = 0.09) -> list[float]:
    synced = []
    for transition in transitions:
        at = float(transition.get("at") or 0)
        if any(abs(at - onset) <= tolerance for onset in onsets):
            synced.append(round(at, 2))
    return synced
