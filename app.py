"""Seedance Cut — a local-only shot splitter and shot analyser.

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
VERSION = "0.1.0"
MAX_UPLOAD = int(os.environ.get("SEEDANCE_MAX_UPLOAD_MB", "4096")) * 1024 * 1024


def provider() -> dict | None:
    base_url = os.environ.get("SEEDANCE_MODEL_BASE_URL", "").strip()
    model = os.environ.get("SEEDANCE_MODEL", "").strip()
    key = os.environ.get("SEEDANCE_API_KEY", "").strip()
    if not (base_url and model and key):
        return None
    return {
        "id": "env",
        "name": os.environ.get("SEEDANCE_MODEL_NAME", "local-vision"),
        "base_url": base_url,
        "model": model,
        "api_key": key,
        "kind": "api",
    }


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def update_job(media_id: str, **kwargs) -> None:
    db.update_media_job(media_id, **kwargs)


def cut_media(media_id: str, mode: str) -> None:
    item = db.get_media(media_id)
    if not item:
        return
    try:
        update_job(media_id, status="running", progress=10, error="")
        media_dir = DATA_DIR / "media" / media_id
        info = media.pull_apart(item["path"], media_dir, mode=mode, provider="local")
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
        update_job(media_id, status="failed", error="未配置视觉模型，请填写 .env。")
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
            self._json(404, {"error": "not_found", "message": "文件不存在。"})
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
            return self._json(200, {"ok": True, "version": VERSION, "model": bool(provider())})
        if path == "/api/media":
            return self._json(200, {"media": list_media_with_state()})
        match = re.fullmatch(r"/api/media/([A-Za-z0-9_]+)", path)
        if match:
            item = db.get_media(match.group(1))
            if not item:
                return self._json(404, {"error": "not_found", "message": "视频不存在。"})
            return self._json(200, {"media": item})
        match = re.fullmatch(r"/media/([A-Za-z0-9_]+)/(.+)", path)
        if match:
            media_id, rel = match.group(1), unquote(match.group(2))
            base = (DATA_DIR / "media" / media_id).resolve()
            target = (base / rel).resolve()
            if base not in target.parents:
                return self._json(403, {"error": "forbidden", "message": "路径无效。"})
            return self._file(target)
        if path in {"", "/"}:
            path = "/index.html"
        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents:
            return self._json(403, {"error": "forbidden", "message": "路径无效。"})
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
                    return self._json(400, {"error": "bad_json", "message": "请求格式无效。"})
            item = db.get_media(media_id)
            if not item:
                return self._json(404, {"error": "not_found", "message": "视频不存在。"})
            if action == "cut":
                mode = str(body.get("mode") or "cuts")
                if mode not in {"cuts", "all", "model"}:
                    return self._json(400, {"error": "bad_mode", "message": "切点模式必须是 cuts / all / model。"})
                update_job(media_id, status="running", progress=5, error="")
                threading.Thread(target=cut_media, args=(media_id, mode), daemon=True).start()
                return self._json(202, {"ok": True, "processing": True})
            if item.get("job") and item["job"].get("status") == "running":
                return self._json(409, {"error": "already_running", "message": "这一条正在分析。"})
            if not provider():
                return self._json(409, {"error": "model_required", "message": "先在 .env 里配置 SEEDANCE_MODEL_BASE_URL / SEEDANCE_MODEL / SEEDANCE_API_KEY。"})
            threading.Thread(target=analyse_media, args=(media_id,), daemon=True).start()
            return self._json(202, {"ok": True, "processing": True})
        return self._json(404, {"error": "not_found", "message": "接口不存在。"})

    def _upload(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._json(400, {"error": "empty", "message": "没有收到视频。"})
        if length > MAX_UPLOAD:
            return self._json(413, {"error": "too_large", "message": f"单个视频不能超过 {MAX_UPLOAD // 1024 // 1024} MB。"})
        name = Path(unquote(self.headers.get("X-Filename") or "video.mp4")).name
        if Path(name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            return self._json(400, {"error": "bad_type", "message": "只支持 mp4 / mov / mkv / webm / avi。"})
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
            return self._json(400, {"error": "short", "message": "上传中断。"})
        try:
            duration = media.probe_duration(str(dest))
        except Exception:
            dest.unlink(missing_ok=True)
            return self._json(400, {"error": "bad_video", "message": "无法读取这个视频，请换一个文件。"})
        media_id = db.new_id("med")
        db.add_media(Path(name).stem, str(dest), "", "", duration, [], media_id=media_id)
        update_job(media_id, status="running", progress=5, error="")
        threading.Thread(target=cut_media, args=(media_id, "cuts"), daemon=True).start()
        return self._json(202, {"media": db.get_media(media_id), "items": list_media_with_state()})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Seedance Cut local server")
    parser.add_argument("--host", default=os.environ.get("SEEDANCE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SEEDANCE_PORT", "8765")))
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    print(f"Seedance Cut {VERSION} · http://{args.host}:{args.port}")
    print(f"data: {DATA_DIR}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
