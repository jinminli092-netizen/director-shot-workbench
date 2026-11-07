"""Shot boundary detection through the optional video environment.

TransNetV2 lives in `.venv-audio`; the studio server stays dependency-free.
If the environment or model is missing this returns an empty list and
`media.pull_apart` falls back to the ffmpeg scene-score detector.
"""

from __future__ import annotations

from . import audio_engines


def detect(path: str) -> list[dict]:
    if not audio_engines.installed():
        return []
    result = audio_engines.run("shots", path)
    if not result.get("ok"):
        return []
    scenes = result.get("scenes") or []
    cleaned = []
    for scene in scenes:
        try:
            start = float(scene["start"])
        except (KeyError, TypeError, ValueError):
            continue
        cleaned.append(
            {
                "start": round(start, 2),
                "end": round(float(scene.get("end") or start), 2),
                "probability": float(scene.get("probability") or 0),
            }
        )
    return cleaned
