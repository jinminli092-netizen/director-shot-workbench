"""Pluggable shot-boundary providers.

Local mode keeps TransNetV2/ffmpeg on this machine. Deployed mode can use
Volcengine AI MediaKit: the file is uploaded to a temporary media slot, then
`semantic-segment-pro` returns the shot list. No local model or GPU needed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


class CutProviderError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


def active() -> str:
    value = (os.environ.get("SEEDANCE_CUT_PROVIDER") or "local").strip().lower()
    return value if value in {"local", "volcengine"} else "local"


def _api_key(config: dict | None = None) -> str:
    return str(
        (config or {}).get("api_key")
        or os.environ.get("SEEDANCE_VOLCENGINE_API_KEY")
        or ""
    ).strip()


def _base(config: dict | None = None) -> str:
    return str(
        (config or {}).get("base_url")
        or os.environ.get("SEEDANCE_VOLCENGINE_BASE_URL")
        or "https://mediakit.cn-beijing.volces.com/api/v1"
    ).strip().rstrip("/")


def _json_request(method: str, url: str, body: dict | None = None, timeout: int = 60, config: dict | None = None) -> dict:
    key = _api_key(config)
    if not key:
        raise CutProviderError(
            "还没有配置火山 API Key。",
            "设置 SEEDANCE_VOLCENGINE_API_KEY。",
        )
    data = None
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise CutProviderError(f"火山接口返回 HTTP {error.code}。", detail[:800]) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise CutProviderError("连不上火山接口。", str(error)[:800]) from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CutProviderError("火山接口返回的不是 JSON。", raw[:800]) from error
    if value.get("success") is False:
        raise CutProviderError(
            "火山任务提交失败。", json.dumps(value.get("error") or value, ensure_ascii=False)[:800]
        )
    return value


def _upload(path: str, config: dict | None = None) -> str:
    upload = _json_request("POST", f"{_base(config)}/tools-sync/request-media-upload-url", {}, config=config)
    result = upload.get("result") or {}
    upload_url = result.get("upload_url") or ""
    file_id = result.get("file_id") or ""
    if not upload_url or not file_id:
        raise CutProviderError("火山没有返回上传地址。", json.dumps(upload, ensure_ascii=False)[:800])

    headers = {}
    for item in result.get("upload_headers") or []:
        if isinstance(item, dict) and item.get("key"):
            headers[str(item["key"])] = str(item.get("value") or "")
    headers.setdefault("Content-Type", "video/mp4")
    data = Path(path).read_bytes()
    request = urllib.request.Request(upload_url, data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise CutProviderError(f"上传到火山失败 HTTP {error.code}。", detail[:800]) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise CutProviderError("上传到火山超时或断线。", str(error)[:800]) from error
    return file_id


def _wait(task_id: str, timeout: int = 900, config: dict | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = _json_request("GET", f"{_base(config)}/tasks/{task_id}", config=config)
        status = value.get("status")
        if status == "completed":
            return value.get("result") or {}
        if status == "failed":
            raise CutProviderError(
                "火山切片失败。", json.dumps(value.get("error") or value, ensure_ascii=False)[:800]
            )
        time.sleep(3)
    raise CutProviderError("火山切片超时。", f"task_id={task_id}")


def detect_volcengine(path: str, config: dict | None = None) -> list[dict]:
    """Upload one file and return shot starts, normalized for pull_apart()."""
    file_id = _upload(path, config)
    task = _json_request(
        "POST",
        f"{_base(config)}/tools/semantic-segment-pro",
        {
            "video_url": file_id,
            "strategy": "custom",
            "min_duration": 1,
            "max_duration": 30,
            "enable_scene_detection": True,
            "include_detail_types": ["shot_list"],
            "return_segment_videos": False,
        },
        timeout=60,
        config=config,
    )
    task_id = task.get("task_id") or ""
    if not task_id:
        raise CutProviderError("火山没有返回 task_id。", json.dumps(task, ensure_ascii=False)[:800])
    result = _wait(task_id, config=config)
    scenes = []
    for shot in result.get("shots") or []:
        try:
            start = float(shot.get("start_ms")) / 1000.0
            end = float(shot.get("end_ms")) / 1000.0
        except (TypeError, ValueError):
            continue
        scenes.append({"start": round(start, 2), "end": round(end, 2), "probability": 1.0})
    return scenes
