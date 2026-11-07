"""Local SQLite store for the standalone Seedance Cut app.

One row per project; the working document (analysis / shots / groups / prompts)
lives in `doc_json` because the UI always saves the whole step. Script versions,
providers and run logs are separate rows so they are auditable without touching
the document.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("SEEDANCE_DATA_DIR") or (ROOT / "data"))
DB_PATH = DATA_DIR / "studio.db"

_CURRENT_DB = contextvars.ContextVar("seedance_cut_current_db", default=None)
_CURRENT_ROOT = contextvars.ContextVar("seedance_cut_current_root", default=None)
_CURRENT_KEY = contextvars.ContextVar("seedance_cut_current_key", default="")
_INITIALIZED: set[str] = set()


def base_dir() -> Path:
    return Path(os.environ.get("SEEDANCE_DATA_DIR") or (ROOT / "data"))


def current_root() -> Path:
    return _CURRENT_ROOT.get() or DATA_DIR


def current_db() -> Path:
    return _CURRENT_DB.get() or DB_PATH


def is_member() -> bool:
    return _CURRENT_ROOT.get() is not None


def is_guest() -> bool:
    """A browser without a site session gets a guest_* root; it may look, not upload."""
    root = _CURRENT_ROOT.get()
    return bool(root and root.name.startswith("guest_"))


def current_key() -> str:
    return _CURRENT_KEY.get() or ""


def set_user(key: str | None) -> None:
    """owner/empty -> the existing single-tenant DB; other keys -> isolated member DB."""
    if not key or key == "owner":
        _CURRENT_ROOT.set(None)
        _CURRENT_DB.set(None)
        _CURRENT_KEY.set(str(key or ""))
        return
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(key))[:64] or "member"
    root = base_dir() / "users" / safe
    _CURRENT_ROOT.set(root)
    _CURRENT_DB.set(root / "studio.db")
    _CURRENT_KEY.set(str(key))
    marker = str(root / "studio.db")
    if marker not in _INITIALIZED:
        init_db()
        _INITIALIZED.add(marker)


def clear_user() -> None:
    _CURRENT_ROOT.set(None)
    _CURRENT_DB.set(None)
    _CURRENT_KEY.set("")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    work_code TEXT NOT NULL,
    work_title TEXT NOT NULL DEFAULT '',
    episode_no INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    doc_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scripts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'original',
    content TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scripts_project ON scripts(project_id, version);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'openai',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS video_references (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    skill_id TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    analysis_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL,
    provider_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    in_chars INTEGER NOT NULL DEFAULT 0,
    out_chars INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    skill_id TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0,
    shot_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_shots (
    id TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    index_no INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    thumb_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_media_shots ON media_shots(media_id, index_no);
CREATE TABLE IF NOT EXISTS media_transitions (
    media_id TEXT NOT NULL,
    at_seconds REAL NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    type TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'candidate',
    peak REAL NOT NULL DEFAULT 0,
    beat_sync INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(media_id, at_seconds)
);
CREATE TABLE IF NOT EXISTS media_audio (
    media_id TEXT PRIMARY KEY,
    analysis_json TEXT NOT NULL DEFAULT '{}',
    waveform_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_analysis (
    media_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    progress INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_merges (
    id TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    start_seconds REAL NOT NULL DEFAULT 0,
    end_seconds REAL NOT NULL DEFAULT 0,
    shot_indices TEXT NOT NULL DEFAULT '[]',
    path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_merges ON media_merges(media_id, created_at);
CREATE TABLE IF NOT EXISTS media_reverse (
    id TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    target TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_reverse ON media_reverse(media_id, created_at);
CREATE TABLE IF NOT EXISTS media_skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cut_settings (
    provider TEXT PRIMARY KEY,
    api_key TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_skills ON media_skills(created_at);
CREATE TABLE IF NOT EXISTS media_shot_analysis (
    media_id TEXT NOT NULL,
    index_no INTEGER NOT NULL,
    analysis_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(media_id, index_no)
);
CREATE TABLE IF NOT EXISTS media_notes (
    media_id TEXT PRIMARY KEY,
    transcript TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shot_reviews (
    media_id TEXT NOT NULL,
    index_no INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(media_id, index_no)
);
CREATE TABLE IF NOT EXISTS media_engine_runs (
    media_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(media_id, engine)
);
"""


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect() -> sqlite3.Connection:
    root = current_root()
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(current_db(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    """Commit on success and always close the file handle.

    sqlite3's connection context manager commits but never closes, which leaks
    handles (and on Windows keeps the database file locked).
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(media_transitions)")
        }
        if "beat_sync" not in columns:
            conn.execute(
                "ALTER TABLE media_transitions ADD COLUMN beat_sync INTEGER NOT NULL DEFAULT 0"
            )
        shot_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(media_shots)")
        }
        if "clip_path" not in shot_columns:
            conn.execute(
                "ALTER TABLE media_shots ADD COLUMN clip_path TEXT NOT NULL DEFAULT ''"
            )
        media_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(media_items)")
        }
        if "fps" not in media_columns:
            conn.execute(
                "ALTER TABLE media_items ADD COLUMN fps REAL NOT NULL DEFAULT 0"
            )
        conn.execute(
            "UPDATE media_items SET duration_seconds=COALESCE("
            "(SELECT MAX(end_seconds) FROM media_shots WHERE media_id=media_items.id),0)"
            " WHERE duration_seconds=0"
        )


def default_doc() -> dict:
    return {
        "analysis": None,
        "shots": [],
        "groups": [],
        "prompts": [],
        "settings": {"seedance_version": "2.5", "provider_id": ""},
    }


def _row_to_project(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "work_code": row["work_code"],
        "work_title": row["work_title"],
        "episode_no": row["episode_no"],
        "title": row["title"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "doc": json.loads(row["doc_json"] or "{}"),
    }


def list_projects() -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_project(row) for row in rows]


def get_project(project_id: str) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return _row_to_project(row) if row else None


def create_project(
    work_code: str,
    work_title: str,
    episode_no: int,
    title: str,
    script_text: str,
) -> dict:
    project_id = new_id("prj")
    stamp = now()
    doc = default_doc()
    with session() as conn:
        conn.execute(
            "INSERT INTO projects(id,work_code,work_title,episode_no,title,status,doc_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                work_code,
                work_title,
                int(episode_no),
                title,
                "draft",
                json.dumps(doc, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO scripts(id,project_id,version,kind,content,sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                new_id("scr"),
                project_id,
                1,
                "original",
                script_text,
                hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
                stamp,
            ),
        )
    return get_project(project_id)  # type: ignore[return-value]


def latest_script(project_id: str) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM scripts WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "version": row["version"],
        "kind": row["kind"],
        "content": row["content"],
        "sha256": row["sha256"],
        "created_at": row["created_at"],
    }


def add_script(project_id: str, content: str, kind: str = "revision") -> dict:
    with session() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM scripts WHERE project_id=?",
            (project_id,),
        ).fetchone()
        version = int(row["next"])
        conn.execute(
            "INSERT INTO scripts(id,project_id,version,kind,content,sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                new_id("scr"),
                project_id,
                version,
                kind,
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                now(),
            ),
        )
        conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now(), project_id)
        )
    return latest_script(project_id)  # type: ignore[return-value]


def list_scripts(project_id: str) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT id,version,kind,sha256,created_at,length(content) AS chars"
            " FROM scripts WHERE project_id=? ORDER BY version",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_doc(project_id: str, doc: dict, status: str | None = None) -> None:
    with session() as conn:
        if status:
            conn.execute(
                "UPDATE projects SET doc_json=?, status=?, updated_at=? WHERE id=?",
                (json.dumps(doc, ensure_ascii=False), status, now(), project_id),
            )
        else:
            conn.execute(
                "UPDATE projects SET doc_json=?, updated_at=? WHERE id=?",
                (json.dumps(doc, ensure_ascii=False), now(), project_id),
            )


def update_project_meta(project_id: str, **fields) -> None:
    allowed = {"title", "episode_no", "work_code", "work_title", "status"}
    pairs = {k: v for k, v in fields.items() if k in allowed}
    if not pairs:
        return
    assignments = ", ".join(f"{key}=?" for key in pairs)
    with session() as conn:
        conn.execute(
            f"UPDATE projects SET {assignments}, updated_at=? WHERE id=?",
            (*pairs.values(), now(), project_id),
        )


def delete_project(project_id: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM scripts WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM runs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


def list_providers() -> list[dict]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM providers ORDER BY created_at").fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "base_url": row["base_url"],
            "model": row["model"],
            "api_key_set": bool(row["api_key"]),
            "api_key_tail": row["api_key"][-4:] if row["api_key"] else "",
        }
        for row in rows
    ]


def get_provider(provider_id: str, with_key: bool = False) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
    if not row:
        return None
    item = {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "base_url": row["base_url"],
        "model": row["model"],
    }
    if with_key:
        item["api_key"] = row["api_key"]
    else:
        item["api_key_set"] = bool(row["api_key"])
        item["api_key_tail"] = row["api_key"][-4:] if row["api_key"] else ""
    return item


def save_provider(
    provider_id: str | None,
    name: str,
    kind: str,
    base_url: str,
    api_key: str,
    model: str,
) -> dict:
    if provider_id:
        with session() as conn:
            current = conn.execute(
                "SELECT api_key FROM providers WHERE id=?", (provider_id,)
            ).fetchone()
            key = api_key if api_key else (current["api_key"] if current else "")
            conn.execute(
                "UPDATE providers SET name=?,kind=?,base_url=?,api_key=?,model=? WHERE id=?",
                (name, kind, base_url.rstrip("/"), key, model, provider_id),
            )
        return get_provider(provider_id) or {}
    provider_id = new_id("prv")
    with session() as conn:
        conn.execute(
            "INSERT INTO providers(id,name,kind,base_url,api_key,model,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                provider_id,
                name,
                kind,
                base_url.rstrip("/"),
                api_key,
                model,
                now(),
            ),
        )
    return get_provider(provider_id) or {}


def delete_provider(provider_id: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def add_run(
    project_id: str,
    agent: str,
    provider_id: str,
    model: str,
    status: str,
    latency_ms: int,
    in_chars: int,
    out_chars: int,
    error: str = "",
) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO runs(id,project_id,agent,provider_id,model,status,latency_ms,in_chars,out_chars,error,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("run"),
                project_id,
                agent,
                provider_id,
                model,
                status,
                int(latency_ms),
                int(in_chars),
                int(out_chars),
                error[:500],
                now(),
            ),
        )


def list_runs(project_id: str, limit: int = 30) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# video references (the living library)
# --------------------------------------------------------------------------


def _reference_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["analysis"] = json.loads(item.pop("analysis_json") or "{}")
    except json.JSONDecodeError:
        item["analysis"] = {}
        item.pop("analysis_json", None)
    return item


def list_references(skill_id: str | None = None) -> list[dict]:
    with session() as conn:
        if skill_id:
            rows = conn.execute(
                "SELECT * FROM video_references WHERE skill_id=? ORDER BY created_at DESC",
                (skill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM video_references ORDER BY created_at DESC"
            ).fetchall()
    return [_reference_row(row) for row in rows]


def get_reference(reference_id: str) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM video_references WHERE id=?", (reference_id,)
        ).fetchone()
    return _reference_row(row) if row else None


def save_reference(
    reference_id: str | None,
    title: str,
    url: str,
    skill_id: str,
    transcript: str,
    notes: str,
) -> dict:
    stamp = now()
    if reference_id:
        with session() as conn:
            conn.execute(
                "UPDATE video_references SET title=?,url=?,skill_id=?,transcript=?,notes=?,updated_at=?"
                " WHERE id=?",
                (title, url, skill_id, transcript, notes, stamp, reference_id),
            )
        return get_reference(reference_id) or {}
    reference_id = new_id("ref")
    with session() as conn:
        conn.execute(
            "INSERT INTO video_references(id,title,url,skill_id,transcript,notes,analysis_json,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'{}','draft',?,?)",
            (reference_id, title, url, skill_id, transcript, notes, stamp, stamp),
        )
    return get_reference(reference_id) or {}


def save_reference_analysis(reference_id: str, analysis: dict, status: str = "analyzed") -> dict:
    with session() as conn:
        conn.execute(
            "UPDATE video_references SET analysis_json=?,status=?,updated_at=? WHERE id=?",
            (json.dumps(analysis, ensure_ascii=False), status, now(), reference_id),
        )
    return get_reference(reference_id) or {}


def delete_reference(reference_id: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM video_references WHERE id=?", (reference_id,))


# --------------------------------------------------------------------------
# local media library (pull-apart clips stay on this machine)
# --------------------------------------------------------------------------


def add_media(
    title: str,
    path: str,
    skill_id: str,
    tags: str,
    duration_seconds: float,
    shots: list[dict],
    media_id: str | None = None,
) -> dict:
    media_id = media_id or new_id("med")
    with session() as conn:
        conn.execute(
            "INSERT INTO media_items(id,title,path,skill_id,tags,duration_seconds,shot_count,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (media_id, title, path, skill_id, tags, duration_seconds, len(shots), now()),
        )
        for shot in shots:
            conn.execute(
                "INSERT INTO media_shots(id,media_id,index_no,start_seconds,end_seconds,thumb_path,clip_path)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("msh"),
                    media_id,
                    shot["index"],
                    shot["start"],
                    shot["end"],
                    shot.get("thumb", ""),
                    shot.get("clip", ""),
                ),
            )
    return get_media(media_id) or {}


def list_media() -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT m.*, a.status AS analysis_status, a.progress AS analysis_progress,"
            " (SELECT thumb_path FROM media_shots s WHERE s.media_id=m.id ORDER BY s.index_no LIMIT 1) AS thumb_path"
            " FROM media_items m LEFT JOIN media_analysis a ON a.media_id=m.id"
            " ORDER BY m.created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_media(media_id: str) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()
        if not row:
            return None
        shots = conn.execute(
            "SELECT index_no,start_seconds,end_seconds,thumb_path,clip_path FROM media_shots"
            " WHERE media_id=? ORDER BY index_no",
            (media_id,),
        ).fetchall()
        analysed = conn.execute(
            "SELECT index_no,analysis_json FROM media_shot_analysis WHERE media_id=?",
            (media_id,),
        ).fetchall()
    item = dict(row)
    by_index = {}
    for entry in analysed:
        try:
            by_index[entry["index_no"]] = json.loads(entry["analysis_json"] or "{}")
        except json.JSONDecodeError:
            by_index[entry["index_no"]] = {}
    item["shots"] = [
        {
            **dict(shot),
            "index": shot["index_no"],
            "analysis": by_index.get(shot["index_no"], {}),
        }
        for shot in shots
    ]
    item["job"] = get_media_job(media_id)
    item["transitions"] = list_transitions(media_id)
    item["audio"] = get_media_audio(media_id)
    item["engines"] = get_engine_runs(media_id)
    reviews = list_shot_reviews(media_id)
    for shot in item["shots"]:
        shot["review"] = reviews.get(shot["index_no"])
    item["notes"] = get_media_notes(media_id)
    item["review_summary"] = {
        "reviewed": len(reviews),
        "good": sum(1 for review in reviews.values() if review["verdict"] == "good"),
        "partial": sum(1 for review in reviews.values() if review["verdict"] == "partial"),
        "wrong": sum(1 for review in reviews.values() if review["verdict"] == "wrong"),
        "total": len(item["shots"]),
    }
    return item


def delete_media(media_id: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM media_shots WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM media_transitions WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM media_analysis WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM media_shot_analysis WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM media_notes WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM shot_reviews WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM media_audio WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM media_engine_runs WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM media_items WHERE id=?", (media_id,))


# --------------------------------------------------------------------------
# folder analysis queue
# --------------------------------------------------------------------------


def media_paths() -> set[str]:
    with session() as conn:
        rows = conn.execute("SELECT path FROM media_items").fetchall()
    return {row["path"] for row in rows}


def replace_media_shots(media_id: str, shots: list[dict], transitions: list[dict] | None = None) -> None:
    with session() as conn:
        conn.execute("DELETE FROM media_shots WHERE media_id=?", (media_id,))
        for shot in shots:
            conn.execute(
                "INSERT INTO media_shots(id,media_id,index_no,start_seconds,end_seconds,thumb_path,clip_path)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("msh"),
                    media_id,
                    shot["index"],
                    shot["start"],
                    shot["end"],
                    shot.get("thumb", ""),
                    shot.get("clip", ""),
                ),
            )
        conn.execute(
            "UPDATE media_items SET shot_count=?, duration_seconds=? WHERE id=?",
            (
                len(shots),
                round(float(shots[-1]["end"]), 2) if shots else 0.0,
                media_id,
            ),
        )
    if transitions is not None:
        save_transitions(media_id, transitions)


def save_transitions(media_id: str, transitions: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM media_transitions WHERE media_id=?", (media_id,))
        for transition in transitions:
            conn.execute(
                "INSERT INTO media_transitions(media_id,at_seconds,start_seconds,end_seconds,type,confidence,peak,beat_sync)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    media_id,
                    transition["at"],
                    transition["start"],
                    transition["end"],
                    transition["type"],
                    transition.get("confidence", "candidate"),
                    float(transition.get("peak") or 0),
                    1 if transition.get("beat_sync") else 0,
                ),
            )


def mark_beat_sync(media_id: str, times: list[float]) -> None:
    with session() as conn:
        conn.execute("UPDATE media_transitions SET beat_sync=0 WHERE media_id=?", (media_id,))
        for at in times:
            conn.execute(
                "UPDATE media_transitions SET beat_sync=1 WHERE media_id=? AND ABS(at_seconds-?)<0.01",
                (media_id, at),
            )


def list_transitions(media_id: str) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM media_transitions WHERE media_id=? ORDER BY at_seconds",
            (media_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_media_audio(media_id: str, analysis: dict, waveform_path: str = "") -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO media_audio(media_id,analysis_json,waveform_path,status,updated_at)"
            " VALUES(?,?,?,'done',?) ON CONFLICT(media_id) DO UPDATE SET"
            " analysis_json=excluded.analysis_json, waveform_path=excluded.waveform_path,"
            " status='done', updated_at=excluded.updated_at",
            (
                media_id,
                json.dumps(analysis, ensure_ascii=False),
                waveform_path,
                now(),
            ),
        )


def get_media_audio(media_id: str) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM media_audio WHERE media_id=?", (media_id,)
        ).fetchone()
    if not row:
        return None
    try:
        analysis = json.loads(row["analysis_json"] or "{}")
    except json.JSONDecodeError:
        analysis = {}
    analysis["waveform_path"] = row["waveform_path"]
    return analysis


def save_engine_run(
    media_id: str,
    engine: str,
    status: str,
    result: dict | None = None,
    error: str = "",
) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO media_engine_runs(media_id,engine,status,result_json,error,updated_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(media_id,engine) DO UPDATE SET"
            " status=excluded.status, result_json=excluded.result_json,"
            " error=excluded.error, updated_at=excluded.updated_at",
            (
                media_id,
                engine,
                status,
                json.dumps(result or {}, ensure_ascii=False),
                error[:600],
                now(),
            ),
        )


def get_engine_runs(media_id: str) -> dict:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM media_engine_runs WHERE media_id=?", (media_id,)
        ).fetchall()
    result = {}
    for row in rows:
        try:
            payload = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        result[row["engine"]] = {
            "status": row["status"],
            "error": row["error"],
            "updated_at": row["updated_at"],
            "result": payload,
        }
    return result


def queue_media(media_id: str, status: str = "pending") -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO media_analysis(media_id,status,progress,error,summary_json,updated_at)"
            " VALUES(?,?,0,'','{}',?) ON CONFLICT(media_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
            (media_id, status, now()),
        )


def next_queued_media() -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT m.* FROM media_items m JOIN media_analysis a ON a.media_id=m.id"
            " WHERE a.status='pending' ORDER BY m.created_at LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def update_media_job(
    media_id: str,
    status: str | None = None,
    progress: int | None = None,
    error: str | None = None,
    summary: dict | None = None,
) -> None:
    fields = ["updated_at=?"]
    values: list = [now()]
    if status is not None:
        fields.append("status=?")
        values.append(status)
    if progress is not None:
        fields.append("progress=?")
        values.append(int(progress))
    if error is not None:
        fields.append("error=?")
        values.append(error[:500])
    if summary is not None:
        fields.append("summary_json=?")
        values.append(json.dumps(summary, ensure_ascii=False))
    values.append(media_id)
    with session() as conn:
        exists = conn.execute("SELECT 1 FROM media_analysis WHERE media_id=?", (media_id,)).fetchone()
        if exists:
            conn.execute(f"UPDATE media_analysis SET {', '.join(fields)} WHERE media_id=?", tuple(values))
        else:
            conn.execute(
                "INSERT INTO media_analysis(media_id,status,progress,error,summary_json,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    media_id,
                    status or "pending",
                    int(progress or 0),
                    (error or "")[:500],
                    json.dumps(summary or {}, ensure_ascii=False),
                    now(),
                ),
            )


def save_shot_analysis(media_id: str, index_no: int, analysis: dict) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO media_shot_analysis(media_id,index_no,analysis_json) VALUES(?,?,?)"
            " ON CONFLICT(media_id,index_no) DO UPDATE SET analysis_json=excluded.analysis_json",
            (media_id, index_no, json.dumps(analysis, ensure_ascii=False)),
        )


def clear_shot_analysis(media_id: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM media_shot_analysis WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM shot_reviews WHERE media_id=?", (media_id,))


def set_shot_clip(media_id: str, index_no: int, clip_path: str) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE media_shots SET clip_path=? WHERE media_id=? AND index_no=?",
            (clip_path, media_id, index_no),
        )


def set_media_fps(media_id: str, fps: float) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE media_items SET fps=? WHERE id=?", (round(float(fps), 3), media_id)
        )


def set_media_duration(media_id: str, seconds: float) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE media_items SET duration_seconds=? WHERE id=?",
            (round(float(seconds), 3), media_id),
        )


def add_merge(media_id: str, start: float, end: float, indices: list[int], path: str) -> dict:
    merge_id = new_id("mrg")
    ordered: list[int] = []
    for value in indices:
        index = int(value)
        if index not in ordered:
            ordered.append(index)
    with session() as conn:
        conn.execute(
            "INSERT INTO media_merges(id,media_id,start_seconds,end_seconds,shot_indices,path,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                merge_id,
                media_id,
                round(float(start), 3),
                round(float(end), 3),
                json.dumps(ordered),
                path,
                now(),
            ),
        )
    return next(item for item in list_merges(media_id) if item["id"] == merge_id)


def list_merges(media_id: str) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM media_merges WHERE media_id=? ORDER BY created_at DESC",
            (media_id,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["shot_indices"] = json.loads(item.get("shot_indices") or "[]")
        except json.JSONDecodeError:
            item["shot_indices"] = []
        items.append(item)
    return items


def delete_merge(media_id: str, merge_id: str) -> bool:
    with session() as conn:
        cursor = conn.execute(
            "DELETE FROM media_merges WHERE media_id=? AND id=?",
            (media_id, merge_id),
        )
    return cursor.rowcount > 0


def save_reverse(media_id: str, target: str, payload: dict) -> dict:
    reverse_id = new_id("rev")
    with session() as conn:
        conn.execute(
            "INSERT INTO media_reverse(id,media_id,target,payload_json,created_at) VALUES(?,?,?,?,?)",
            (reverse_id, media_id, target, json.dumps(payload, ensure_ascii=False), now()),
        )
    return {"id": reverse_id, "target": target, "payload": payload}


def latest_reverse(media_id: str, target: str) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM media_reverse WHERE media_id=? AND target=? ORDER BY created_at DESC LIMIT 1",
            (media_id, target),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
    except json.JSONDecodeError:
        item["payload"] = {}
    return item


def save_media_skill(name: str, payload: dict) -> dict:
    skill_id = new_id("msk")
    with session() as conn:
        conn.execute(
            "INSERT INTO media_skills(id,name,payload_json,created_at) VALUES(?,?,?,?)",
            (skill_id, name, json.dumps(payload, ensure_ascii=False), now()),
        )
    return {"id": skill_id, "name": name, "payload": payload}


def list_media_skills() -> list[dict]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM media_skills ORDER BY created_at DESC").fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        items.append(item)
    return items


def get_media_skill(skill_id: str) -> dict | None:
    return next((item for item in list_media_skills() if item["id"] == skill_id), None)


def delete_media_skill(skill_id: str) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM media_skills WHERE id=?", (skill_id,))
    return cursor.rowcount > 0


def delete_media_skills_for_source(media_id: str, target: str) -> None:
    with session() as conn:
        rows = conn.execute("SELECT id,payload_json FROM media_skills").fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            source = payload.get("source") or {}
            if source.get("media_id") == media_id and source.get("target") == target:
                conn.execute("DELETE FROM media_skills WHERE id=?", (row["id"],))


def save_cut_setting(provider: str, api_key: str, base_url: str = "") -> dict:
    with session() as conn:
        conn.execute(
            "INSERT INTO cut_settings(provider,api_key,base_url,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(provider) DO UPDATE SET api_key=excluded.api_key,"
            " base_url=excluded.base_url, updated_at=excluded.updated_at",
            (provider, api_key, base_url, now()),
        )
    return get_cut_setting(provider) or {}


def get_cut_setting(provider: str) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM cut_settings WHERE provider=?", (provider,)).fetchone()
    return dict(row) if row else None


def delete_cut_setting(provider: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM cut_settings WHERE provider=?", (provider,))


def get_media_job(media_id: str) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM media_analysis WHERE media_id=?", (media_id,)
        ).fetchone()
    return dict(row) if row else None


def save_media_notes(media_id: str, transcript: str, intent: str) -> dict:
    with session() as conn:
        conn.execute(
            "INSERT INTO media_notes(media_id,transcript,intent,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(media_id) DO UPDATE SET transcript=excluded.transcript,"
            " intent=excluded.intent, updated_at=excluded.updated_at",
            (media_id, transcript, intent, now()),
        )
    return {"transcript": transcript, "intent": intent}


def get_media_notes(media_id: str) -> dict:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM media_notes WHERE media_id=?", (media_id,)
        ).fetchone()
    return dict(row) if row else {"transcript": "", "intent": ""}


def save_shot_review(
    media_id: str, index_no: int, verdict: str, reason: str, note: str
) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO shot_reviews(media_id,index_no,verdict,reason,note,updated_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(media_id,index_no) DO UPDATE SET"
            " verdict=excluded.verdict, reason=excluded.reason, note=excluded.note,"
            " updated_at=excluded.updated_at",
            (media_id, index_no, verdict, reason, note, now()),
        )


def list_shot_reviews(media_id: str) -> dict[int, dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM shot_reviews WHERE media_id=?", (media_id,)
        ).fetchall()
    return {row["index_no"]: dict(row) for row in rows}


def media_queue_status() -> dict:
    with session() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM media_analysis GROUP BY status"
        ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    return {
        "counts": counts,
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
    }
