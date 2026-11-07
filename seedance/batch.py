"""Batch analysis of a folder of clips with a vision-capable local model.

The queue is deliberately slow and resumable: one video at a time, a few shots
per request, a pause between calls. Closing the app leaves the rest pending.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import audio, db, media, providers

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg"}
BATCH_SHOTS = 2


class Interrupted(Exception):
    pass


def scan_folder(folder: str) -> list[Path]:
    root = Path(folder)
    if not root.is_dir():
        raise media.MediaError("这不是一个文件夹。")
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXT
    )


def _frame(path: str, at: float) -> bytes:
    out = db.current_root() / "cache" / f"{uuid.uuid4().hex}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        media.extract_thumb(path, at, out)
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


VISION_SYSTEM = """你在做影视拉片。用户会给你同一条视频里连续几个镜头，每个镜头按时间顺序给 5 帧（起、早、中、后、末）。

你**听不到声音**：不要判断音乐、台词、音效，也不要用“声音节拍”解释切点。
你只根据画面判断，看不清就写“不确定”，不要编剧情。

必须比较同一镜头的帧间变化，不能只看第一帧就写“固定”：
- camera_motion：镜头自身怎么动（固定/推/拉/摇/移/跟/环绕/手持/升降/变焦）。看背景位移、透视变化、地平线、边缘遮挡。
- subject_motion：主体从第一帧到最后一帧怎么动（起身/转身/挥剑/跑动/停顿），写明起点和终点。
- blocking：主体与前景、背景、道具、对手的相对位置怎么变（走位、遮挡、前后景关系）。
- micro_expression：眼神、嘴角、眉、呼吸、眨眼、面部紧绷。
- body_language：肩、手、步态、重心、发力方向。
- 如果画面确实不动，才写“固定”；无法判断写“不确定”，并在 evidence 说明哪两帧冲突。

输出 JSON：
{"shots":[{"shot":1,"size":"","move":"","camera_motion":"","subject":"","subject_motion":"","blocking":"","micro_expression":"","body_language":"","dof":"","content":"","emotion":"","rhythm":"","why":"动作接点|视线接点|空间转换|情绪停顿|未知","why_note":"","evidence":"哪两帧的什么变化支持判断","reusable_rule":"","confidence":"high|low"}]}

每项要具体：
- size：景别
- move：运镜的短标签
- content：画面里发生了什么，一句话
- emotion：情绪
- rhythm：这一镜在节奏里的作用
- why：为什么在这里切；只能从画面推断，推不出写“未知”
- evidence：具体证据（前后帧差异、动作接点、视线方向、空间变化）
"""

SUMMARY_SYSTEM = """你是拉片笔记整理器。输入是一条视频每个镜头的分析 JSON 和可能的转写。输出：
{"summary":"这条视频最值得学的一句话","techniques":["..."],"tags":["..."],"reusable_rules":["..."],"skill_suggestion":"fight|general|其他"}
只基于输入，不要编。没有转写时不要判断台词与声音。"""


def analyze_audio_track(media_id: str, path: str, media_dir) -> dict | None:
    """Audio is a separate track, never inferred from frames."""
    try:
        info = audio.analyze(path, media_dir)
        db.save_media_audio(media_id, info, info.get("waveform", ""))
        onsets = info.get("onsets") or []
        synced = audio.beat_sync(db.list_transitions(media_id), onsets)
        db.mark_beat_sync(media_id, synced)
        return info
    except Exception as error:  # noqa: BLE001 - an audio failure must not stop video
        db.save_media_audio(
            media_id, {"has_audio": False, "error": str(error)[:300]}
        )
        return None


def analyze_media(
    media_id: str,
    provider: dict,
    stop_event=None,
    on_progress=None,
) -> dict:
    item = db.get_media(media_id)
    if not item:
        raise media.MediaError("媒体不存在。")
    if not item["shots"]:
        media_dir = db.current_root() / "media" / media_id
        info = media.pull_apart(item["path"], media_dir)
        db.replace_media_shots(media_id, info["shots"], info.get("transitions"))
        db.set_media_fps(media_id, info.get("fps") or media.probe_fps(item["path"]))
        item = db.get_media(media_id)
    if not item.get("audio"):
        analyze_audio_track(media_id, item["path"], db.current_root() / "media" / media_id)
        item = db.get_media(media_id)

    shots = item["shots"]
    total = len(shots)
    collected: list[dict] = []

    for start in range(0, total, BATCH_SHOTS):
        if stop_event is not None and stop_event.is_set():
            raise Interrupted()
        batch = shots[start : start + BATCH_SHOTS]
        images: list[bytes] = []
        notes: list[str] = []
        transitions = item.get("transitions") or []
        for shot in batch:
            begin = float(shot["start_seconds"])
            finish = float(shot["end_seconds"])
            near = next(
                (
                    item
                    for item in transitions
                    if abs(float(item["at_seconds"]) - begin) < 0.4
                ),
                None,
            )
            if near:
                notes.append(
                    f"第{shot['index_no']}镜入点={near['type']}（{near['confidence']}）"
                )
            for fraction in (0.08, 0.28, 0.50, 0.72, 0.92):
                images.append(_frame(item["path"], begin + (finish - begin) * fraction))
        prompt = f"这是第 {start + 1}–{start + len(batch)} 镜，共 {len(batch)} 镜，每镜五帧，按时间顺序。逐帧比较后再写运镜和主体运动。"
        if notes:
            prompt += "\n切点信息：" + "；".join(notes) + "。转场入点的前几帧可能是混合画面，不要当成新场景。"
        transcript = str((item.get("notes") or {}).get("transcript") or "").strip()
        intent = str((item.get("notes") or {}).get("intent") or "").strip()
        if transcript:
            prompt += f"\n转写（只作背景，不要复述台词）：{transcript[:1500]}"
        if intent:
            prompt += f"\n作者意图：{intent[:400]}"
        prompt += "\n注意：本条没有音频分析，不要判断声音。"
        result = providers.chat(
            provider,
            VISION_SYSTEM,
            prompt,
            images=images,
            timeout=300,
        )
        value = providers.extract_json(result["text"])
        entries = value.get("shots") or []
        for offset, entry in enumerate(entries):
            if offset >= len(batch):
                break
            shot = batch[offset]
            analysis = {
                "size": str(entry.get("size") or ""),
                "move": str(entry.get("move") or ""),
                "camera_motion": str(entry.get("camera_motion") or entry.get("move") or ""),
                "dof": str(entry.get("dof") or ""),
                "subject": str(entry.get("subject") or ""),
                "subject_motion": str(entry.get("subject_motion") or ""),
                "blocking": str(entry.get("blocking") or ""),
                "micro_expression": str(entry.get("micro_expression") or ""),
                "body_language": str(entry.get("body_language") or ""),
                "content": str(entry.get("content") or ""),
                "emotion": str(entry.get("emotion") or ""),
                "rhythm": str(entry.get("rhythm") or ""),
                "why": str(entry.get("why") or "未知"),
                "why_note": str(entry.get("why_note") or ""),
                "evidence": str(entry.get("evidence") or ""),
                "reusable_rule": str(entry.get("reusable_rule") or ""),
                "confidence": str(entry.get("confidence") or "low"),
                "audio": "not_analyzed",
            }
            db.save_shot_analysis(media_id, shot["index_no"], analysis)
            collected.append(analysis)
        if on_progress:
            on_progress(min(total, start + len(batch)), total)
        time.sleep(2)

    summary_text = providers.chat(
        provider,
        SUMMARY_SYSTEM,
        json.dumps(
            {
                "shots": collected,
                "transcript": str((item.get("notes") or {}).get("transcript") or "")[:3000],
                "intent": str((item.get("notes") or {}).get("intent") or "")[:800],
                "audio": "not_analyzed",
            },
            ensure_ascii=False,
        ),
        timeout=180,
    )
    summary = providers.extract_json(summary_text["text"])
    return summary
