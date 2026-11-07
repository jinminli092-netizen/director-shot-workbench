"""Seedance Cut 鈥?a local-only shot splitter and shot analyser.

One process, one data directory, no accounts. Upload a video, cut it into
shots with ffmpeg scene detection, then analyse the shots with any
OpenAI-compatible vision model you configure yourself.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()
os.environ.setdefault("SEEDANCE_DATA_DIR", str(ROOT / "data"))

from seedance import batch, db, media, providers  # noqa: E402

DATA_DIR = Path(os.environ["SEEDANCE_DATA_DIR"]).resolve()
WEB_DIR = ROOT / "web"
VERSION = "0.1.1"
MAX_UPLOAD = int(os.environ.get("SEEDANCE_MAX_UPLOAD_MB", "4096")) * 1024 * 1024

MODEL_PRESETS = {
    "doubao": {
        "label": "璞嗗寘 / 鐏北鏂硅垷",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
    },
    "kimi": {
        "label": "Kimi / Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
    },
    "custom": {
        "label": "鑷畾涔?OpenAI 鍏煎鎺ュ彛",
        "base_url": "",
    },
}


def provider() -> dict | None:
    selected = os.environ.get("SEEDANCE_MODEL_PROVIDER", "custom").strip().lower()
    preset = MODEL_PRESETS.get(selected, MODEL_PRESETS["custom"])
    base_url = (os.environ.get("SEEDANCE_MODEL_BASE_URL") or preset["base_url"]).strip()
    model = os.environ.get("SEEDANCE_MODEL", "").strip()
    key = os.environ.get("SEEDANCE_API_KEY", "").strip()
    if not (base_url and model and key):
        return None
    return {
        "id": selected,
        "name": os.environ.get("SEEDANCE_MODEL_NAME", preset["label"]),
        "provider": selected,
        "base_url": base_url,
        "model": model,
        "api_key": key,
        "kind": "api",
    }


def cut_provider() -> str:
    value = os.environ.get("SEEDANCE_CUT_PROVIDER", "local").strip().lower()
    return value if value in {"local", "volcengine"} else "local"


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def update_job(media_id: str, **kwargs) -> None:
    db.update_media_job(media_id, **kwargs)


def cut_media(media_id: str, mode: str, selected_cut: str = "local") -> None:
    item = db.get_media(media_id)
    if not item:
        return
    try:
        update_job(media_id, status="running", progress=10, error="")
        media_dir = DATA_DIR / "media" / media_id
        info = media.pull_apart(item["path"], media_dir, mode=mode, provider=selected_cut)
        db.replace_media_shots(media_id, info["shots"], info.get("transitions") or [])
        db.set_media_fps(media_id, info.get("fps") or media.probe_fps(item["path"]))
        db.set_media_duration(media_id, info.get("duration") or 0)
        db.clear_shot_analysis(media_id)
        db.queue_media(media_id)
        update_job(media_id, status="pending", progress=70, error="", summary={})
    except Exception as error:  # noqa: BLE001 - report the real ffmpeg/media error
        traceback.print_exc()
        update_job(media_id, status="failed", progress=0, error=str(error)[:500])


def analyse_media(media_id: str) -> None:
    selected = provider()
    if not selected:
        update_job(media_id, status="failed", error="鏈厤缃瑙夋ā鍨嬶紝璇峰～鍐?.env銆?)
        return
    try:
        update_job(media_id, status="running", progress=72, error="")
        summary = batch.analyze_media(
            media_id,
            selected,
            on_progress=lambda done, total: update_job(
                media_id,
                status="running",
                progress=min(99, 70 + int(done / max(1, total) * 29)),
                error="",
            ),
        )
        update_job(media_id, status="done", progress=100, error="", summary=summary or {})
    except Exception as error:  # noqa: BLE001 - surface provider/media errors
        traceback.print_exc()
        update_job(media_id, status="failed", error=str(error)[:500])


def list_media_with_state() -> list[dict]:
    return db.list_media()


class Handler(BaseHTTPRequestHandler):
    server_version = "SeedanceCut/" + VERSION

    def log_message(self, fmt: str, *args) -> None:
        print("[seedance-cut]", fmt % args)

    def _json(self, status: int, value) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._json(404, {"error": "not_found", "message": "鏂囦欢涓嶅瓨鍦ㄣ€?})
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            model = provider()
            return self._json(200, {
                "ok": True,
                "version": VERSION,
                "model": bool(model),
                "model_provider": model.get("provider") if model else "",
                "model_name": model.get("name") if model else "",
                "cut_providers": {
                    "local": True,
                    "volcengine": bool(os.environ.get("SEEDANCE_VOLCENGINE_API_KEY", "").strip()),
                },
            })
        if path == "/api/media":
            return self._json(200, {"media": list_media_with_state()})
        match = re.fullmatch(r"/api/media/([A-Za-z0-9_]+)", path)
        if match:
            item = db.get_media(match.group(1))
            if not item:
                return self._json(404, {"error": "not_found", "message": "瑙嗛涓嶅瓨鍦ㄣ€?})
            return self._json(200, {"media": item})
        match = re.fullmatch(r"/media/([A-Za-z0-9_]+)/(.+)", path)
        if match:
            media_id, rel = match.group(1), unquote(match.group(2))
            base = (DATA_DIR / "media" / media_id).resolve()
            target = (base / rel).resolve()
            if base not in target.parents:
                return self._json(403, {"error": "forbidden", "message": "璺緞鏃犳晥銆?})
            return self._file(target)
        if path in {"", "/"}:
            path = "/index.html"
        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents:
            return self._json(403, {"error": "forbidden", "message": "璺緞鏃犳晥銆?})
        return self._file(target)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/media/upload":
            return self._upload()
        match = re.fullmatch(r"/api/media/([A-Za-z0-9_]+)/(cut|analyze)", path)
        if match:
            media_id, action = match.groups()
            body = {}
            if int(self.headers.get("Content-Length") or 0):
                try:
                    body = json.loads(self._body().decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad_json", "message": "璇锋眰鏍煎紡鏃犳晥銆?})
            item = db.get_media(media_id)
            if not item:
                return self._json(404, {"error": "not_found", "message": "瑙嗛涓嶅瓨鍦ㄣ€?})
            if action == "cut":
                mode = str(body.get("mode") or "cuts")
                if mode not in {"cuts", "all", "model"}:
                    return self._json(400, {"error": "bad_mode", "message": "鍒囩偣妯″紡蹇呴』鏄?cuts / all / model銆?})
                selected_cut = str(body.get("provider") or cut_provider()).strip().lower()
                if selected_cut not in {"local", "volcengine"}:
                    return self._json(400, {"error": "bad_cut_provider", "message": "鍒囬暅鏈嶅姟蹇呴』鏄?local 鎴?volcengine銆?})
                if selected_cut == "volcengine" and not os.environ.get("SEEDANCE_VOLCENGINE_API_KEY", "").strip():
                    return self._json(409, {"error": "cut_key_required", "message": "鍏堝湪 .env 閰嶇疆 SEEDANCE_VOLCENGINE_API_KEY銆?})
                update_job(media_id, status="running", progress=5, error="")
                threading.Thread(target=cut_media, args=(media_id, mode, selected_cut), daemon=True).start()
                return self._json(202, {"ok": True, "processing": True, "provider": selected_cut})
            if item.get("job") and item["job"].get("status") == "running":
                return self._json(409, {"error": "already_running", "message": "杩欎竴鏉℃鍦ㄥ垎鏋愩€?})
            if not provider():
                return self._json(409, {"error": "model_required", "message": "鍏堝湪 .env 閲岄厤缃?SEEDANCE_MODEL_BASE_URL / SEEDANCE_MODEL / SEEDANCE_API_KEY銆?})
            threading.Thread(target=analyse_media, args=(media_id,), daemon=True).start()
            return self._json(202, {"ok": True, "processing": True})
        return self._json(404, {"error": "not_found", "message": "鎺ュ彛涓嶅瓨鍦ㄣ€?})

    def _upload(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._json(400, {"error": "empty", "message": "娌℃湁鏀跺埌瑙嗛銆?})
        if length > MAX_UPLOAD:
            return self._json(413, {"error": "too_large", "message": f"鍗曚釜瑙嗛涓嶈兘瓒呰繃 {MAX_UPLOAD // 1024 // 1024} MB銆?})
        name = Path(unquote(self.headers.get("X-Filename") or "video.mp4")).name
        if Path(name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            return self._json(400, {"error": "bad_type", "message": "鍙敮鎸?mp4 / mov / mkv / webm / avi銆?})
        selected_cut = str(self.headers.get("X-Cut-Provider") or cut_provider()).strip().lower()
        if selected_cut not in {"local", "volcengine"}:
            return self._json(400, {"error": "bad_cut_provider", "message": "鍒囬暅鏈嶅姟蹇呴』鏄?local 鎴?volcengine銆?})
        if selected_cut == "volcengine" and not os.environ.get("SEEDANCE_VOLCENGINE_API_KEY", "").strip():
            return self._json(409, {"error": "cut_key_required", "message": "鍏堝湪 .env 閰嶇疆 SEEDANCE_VOLCENGINE_API_KEY銆?})
        upload_dir = DATA_DIR / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"{time.time_ns()}-{name}"
        remaining = length
        with dest.open("wb") as handle:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                handle.write(chunk)
                remaining -= len(chunk)
        if remaining:
            dest.unlink(missing_ok=True)
            return self._json(400, {"error": "short", "message": "涓婁紶涓柇銆?})
        try:
            duration = media.probe_duration(str(dest))
        except Exception:
            dest.unlink(missing_ok=True)
            return self._json(400, {"error": "bad_video", "message": "鏃犳硶璇诲彇杩欎釜瑙嗛锛岃鎹竴涓枃浠躲€?})
        media_id = db.new_id("med")
        db.add_media(Path(name).stem, str(dest), "", "", duration, [], media_id=media_id)
        update_job(media_id, status="running", progress=5, error="")
        threading.Thread(target=cut_media, args=(media_id, "cuts", selected_cut), daemon=True).start()
        return self._json(202, {"media": db.get_media(media_id), "items": list_media_with_state()})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Seedance Cut local server")
    parser.add_argument("--host", default=os.environ.get("SEEDANCE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SEEDANCE_PORT", "8765")))
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    print(f"Seedance Cut {VERSION} 路 http://{args.host}:{args.port}")
    print(f"data: {DATA_DIR}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

