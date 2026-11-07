"""Shot-boundary and transition candidates without a vision model.

Full-rate ffmpeg scene scores are cheap (CPU only). A hard cut is an isolated
spike; a dissolve/wipe is a sustained run of low scores. Only these candidates
ever need a vision call, so a 10-minute clip costs tens of calls instead of
18,000 frames.
"""

from __future__ import annotations

import re
import subprocess

SCORE_RE = re.compile(r"score:\s*([0-9.]+),\s*lavfi\.scd\.time:\s*([0-9.]+)")


class BoundaryError(Exception):
    pass


def scene_scores(path: str) -> list[tuple[float, float]]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-i",
            path,
            "-vf",
            "scdet=threshold=0,metadata=print:file=-",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    points = [
        (round(float(match.group(2)), 2), round(float(match.group(1)), 4))
        for match in SCORE_RE.finditer(result.stderr)
    ]
    if not points:
        raise BoundaryError("读不到逐帧变化分数：" + (result.stderr or "")[:200])
    points.sort()
    return points


def _max_between(points: list[tuple[float, float]], start: float, end: float) -> float:
    values = [score for time, score in points if start <= time <= end]
    return max(values) if values else 0.0


def detect(path: str, duration: float | None = None) -> dict:
    points = scene_scores(path)
    if not points:
        raise BoundaryError("没有逐帧分数。")
    if duration is None:
        duration = points[-1][0]

    cuts: list[dict] = []
    hard_cut_times: list[float] = []
    for index, (time, score) in enumerate(points):
        if score < 0.30:
            continue
        before = _max_between(points, time - 0.24, time - 0.04)
        after = [value for at, value in points if time + 0.04 <= at <= time + 0.6]
        sustained = len([value for value in after if value >= 0.06])
        # a dissolve starts with a spike and then keeps a low score alive
        if before < 0.14 and sustained < max(3, len(after) // 3):
            if not hard_cut_times or time - hard_cut_times[-1] > 0.3:
                hard_cut_times.append(round(time, 2))
                cuts.append({"time": round(time, 2), "score": score, "type": "cut"})

    # sustained low-score runs = dissolve / wipe / long blend candidates
    transitions: list[dict] = []
    run: list[tuple[float, float]] = []
    last_high = None
    for time, score in points:
        if score >= 0.06:
            if run and last_high is not None and time - last_high > 0.25:
                _close_transition(run, transitions)
                run = []
            run.append((time, score))
            last_high = time
        elif run and last_high is not None and time - last_high > 0.25:
            _close_transition(run, transitions)
            run = []
    if run:
        _close_transition(run, transitions)

    boundaries = list(hard_cut_times)
    for transition in transitions:
        boundaries.append(transition["mid"])
    boundaries = sorted({round(value, 2) for value in boundaries if 0.2 < value < duration - 0.2})

    merged: list[float] = []
    for value in boundaries:
        if not merged or value - merged[-1] > 0.5:
            merged.append(value)

    return {
        "duration": round(duration, 2),
        "cuts": cuts,
        "hard_boundaries": hard_cut_times,
        "transitions": transitions,
        "boundaries": merged,
    }


def _close_transition(run: list[tuple[float, float]], out: list[dict]) -> None:
    if len(run) < 2:
        return
    start, end = run[0][0], run[-1][0]
    span = end - start
    peak = max(score for _, score in run)
    # short spikes are cuts, not transitions; very low runs are motion noise
    if span < 0.35 or peak < 0.10:
        return
    out.append(
        {
            "start": round(start, 2),
            "end": round(end, 2),
            "mid": round((start + end) / 2, 2),
            "peak": round(peak, 3),
            "type": "transition",
            "confidence": "candidate",
        }
    )


def find_cut_near(
    path: str, at: float, window: float = 1.5, min_score: float = 0.08
) -> dict | None:
    """Search the frames around an approximate time for the strongest change."""
    points = scene_scores(path)
    candidates = [
        (time, score) for time, score in points if abs(time - float(at)) <= window
    ]
    if not candidates:
        return None
    time, score = max(candidates, key=lambda item: item[1])
    if score < min_score:
        return None
    return {"time": round(time, 2), "score": round(score, 3)}
