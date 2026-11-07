"""OpenAI-compatible provider adapter.

Any endpoint that speaks /chat/completions works: DeepSeek, Doubao/ARK,
Moonshot/Kimi, a local vLLM or LM Studio. Nothing provider-specific is
hardcoded — the user owns the base URL, model id and key.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 502, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def extract_json(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(cleaned[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    # Reasoning models sometimes wrap JSON in prose or emit several objects.
    # Return the first balanced object instead of failing on the whole string.
    depth = 0
    in_string = False
    escaped = False
    begin = -1
    for index, char in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                begin = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and begin >= 0:
                try:
                    value = json.loads(cleaned[begin : index + 1])
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    pass
    raise ProviderError("模型返回的不是 JSON。", 502, (text or "")[:800])


def _user_content(user: str, images: list[bytes] | None):
    if not images:
        return user
    content: list[dict] = [{"type": "text", "text": user}]
    for data in images:
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    return content


def chat(
    provider: dict,
    system: str,
    user: str,
    json_mode: bool = True,
    timeout: int = 180,
    images: list[bytes] | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    base = str(provider.get("base_url") or "").strip().rstrip("/")
    model = str(provider.get("model") or "").strip()
    api_key = str(provider.get("api_key") or "").strip()
    if not base:
        raise ProviderError("还没有填 Base URL。", 400)
    if not model:
        raise ProviderError("还没有填模型 ID。", 400)
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    # Kimi K3.1 pins temperature: thinking disabled requires exactly 1.
    temperature = 1.0 if model.lower().startswith("kimi-k3") else 0.4
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _user_content(user, images)},
        ],
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        if error.code == 404:
            message = "接口返回 HTTP 404：模型或接入点不存在。豆包请填 ep-... 接入点 ID，DeepSeek 检查模型 ID。"
        elif error.code == 401:
            message = "接口返回 HTTP 401：Key 无效或没有该模型权限。"
        else:
            message = f"接口返回 HTTP {error.code}。"
        raise ProviderError(message, error.code, body[:800]) from error
    except urllib.error.URLError as error:
        raise ProviderError(f"连不上接口：{error.reason}", 502) from error
    except TimeoutError as error:
        raise ProviderError("接口超时。", 504) from error

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise ProviderError("接口返回的结构不认识。", 502, raw[:800]) from error

    return {
        "text": content or "",
        "latency_ms": int((time.time() - started) * 1000),
        "model": data.get("model") or model,
        "usage": data.get("usage") or {},
    }


def test(provider: dict) -> dict:
    result = chat(
        provider,
        "你是一个连通性测试。只回复 OK。",
        "回复 OK",
        json_mode=False,
        timeout=40,
    )
    return {"ok": True, "latency_ms": result["latency_ms"], "reply": result["text"][:40]}
