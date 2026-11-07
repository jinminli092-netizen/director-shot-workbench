"""Bridge to the optional heavy audio environment.

The studio server stays standard-library only. If `.venv-audio` exists, these
helpers spawn `audio_engine.py` inside it; otherwise the UI reports "not
installed" instead of breaking.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parents[1]
VENV = db.DATA_DIR / "venv-audio"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SCRIPT = ROOT / "audio_engine.py"
MODELS = db.DATA_DIR / "_models"

TIMEOUTS = {"asr": 1800, "music": 1800, "omni": 3600, "shots": 900}
ENGINE_LABELS = {"asr": "转写", "music": "分离 + 节拍", "omni": "声音描述", "shots": "镜头检测"}


def installed() -> bool:
    return PYTHON.exists() and SCRIPT.exists()


def _env() -> dict:
    env = os.environ.copy()
    env["HF_HOME"] = str(MODELS / "hf")
    env["TORCH_HOME"] = str(MODELS / "torch")
    env["AUDIO_WORK"] = str(db.DATA_DIR / "cache")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(db.DATA_DIR / "_pipshim")
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    torch_lib = VENV / "Lib" / "site-packages" / "torch" / "lib"
    ctranslate2_lib = VENV / "Lib" / "site-packages" / "ctranslate2"
    prefix = [str(torch_lib), str(ctranslate2_lib)]
    env["PATH"] = os.pathsep.join(prefix + [env.get("PATH", "")])
    return env


def check() -> dict:
    if not installed():
        return {
            "ok": False,
            "installed": False,
            "error": "还没有 .venv-audio；先按 README 安装音频环境。",
        }
    try:
        result = subprocess.run(
            [str(PYTHON), str(SCRIPT), "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(),
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "installed": True, "error": str(error)}
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {
            "ok": False,
            "installed": True,
            "error": (result.stdout or result.stderr or "")[-500:],
        }
    value["installed"] = True
    return value


def run(engine: str, path: str) -> dict:
    if engine not in TIMEOUTS:
        return {"ok": False, "error": "未知引擎。"}
    if not installed():
        return {"ok": False, "error": "还没有 .venv-audio；先按 README 安装音频环境。"}
    out = db.DATA_DIR / "cache" / f"engine-{uuid.uuid4().hex}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON),
        str(SCRIPT),
        "--mode",
        engine,
        "--input",
        path,
        "--out",
        str(out),
    ]
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(),
            timeout=TIMEOUTS[engine],
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "引擎超时；长片建议先切段。"}
    payload: dict = {}
    if out.exists():
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        finally:
            out.unlink(missing_ok=True)
    if not payload:
        try:
            payload = json.loads(process.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            payload = {
                "ok": False,
                "error": (process.stderr or process.stdout or "引擎没有输出")[-800:],
            }
    return payload
