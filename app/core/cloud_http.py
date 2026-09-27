"""云端适配器的公共 HTTP 工具（**与厂商无关**）。

为什么单独一层
--------------
火山方舟的两个适配器（chat 与录音文件识别）本来就共用同一套请求主干：代理策略、
超时、HTTP 错误映射、JSON 解析；OpenAI 兼容适配器还要再加一条 multipart。
把这些留在某个厂商文件里，就会出现"另一家的适配器 import 火山模块"这种荒唐依赖。

**这里不允许出现任何厂商特有的字段名**——厂商差异只允许留在各自的适配器里。

代理策略
--------
默认**显式绕过系统代理**（`ProxyHandler({})`）：本机环境里 `http_proxy` 会把请求
交给代理，代理再拒绝或改写回环/内网地址（Ollama 路径已实测过一次）。档案里有
`use_system_proxy` 开关，需要时由用户显式打开。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any

from .providers import ProviderError

__all__ = [
    "extract_chat_text",
    "get_ok",
    "http_error",
    "post_json",
    "post_multipart",
    "send",
    "send_multipart",
    "silent_wav_bytes",
]

#: 尽量把厂商侧的排错线索带回来（不同网关用的头名不一样）
_LOGID_HEADERS = ("X-Request-Id", "X-Tt-Logid", "X-Client-Request-Id")


def _opener(use_system_proxy: bool):
    """按档案开关选 opener：默认显式绕过系统代理。"""
    if use_system_proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(
    request: urllib.request.Request, *, timeout: float, use_system_proxy: bool
) -> tuple[bytes, Any]:
    """发一个已构造好的请求，返回 ``(原始字节, 响应头)``；失败抛 :class:`ProviderError`。

    刻意返回**原始字节**：调用方有的要解析 JSON（`post_json`），有的只关心
    "通不通"（连通性自检），不该被"返回体形状"绑住。
    """
    try:
        with _opener(use_system_proxy).open(request, timeout=timeout) as response:
            return response.read(), response.headers
    except urllib.error.HTTPError as exc:
        raise http_error(exc) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"网络不可达：{exc.reason}") from exc
    except OSError as exc:  # 含超时
        raise ProviderError(f"请求失败：{exc}") from exc


def _decode_json(body: bytes) -> dict:
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError(f"返回体不是合法 JSON：{text[:200]}") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"返回体不是 JSON 对象：{text[:200]}")
    return data


def post_json(
    url: str,
    payload: dict,
    headers: dict,
    *,
    timeout: float,
    use_system_proxy: bool,
) -> tuple[dict, Any]:
    """POST 一个 JSON，返回 ``(响应体, 响应头)``；失败抛 :class:`ProviderError`。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    body, response_headers = send(
        request, timeout=timeout, use_system_proxy=use_system_proxy
    )
    return _decode_json(body), response_headers


def _multipart_body(
    fields: dict[str, str], file_field: tuple[str, str, bytes, str]
) -> tuple[bytes, str]:
    """拼一个 multipart/form-data 请求体，返回 ``(字节, Content-Type)``。

    ``file_field`` = ``(字段名, 文件名, 内容, content-type)``。用标准库手搓而不是
    引入 requests：本项目不增运行时依赖（打包体积与"装机就能跑"优先）。
    """
    boundary = f"----drama-splitter-{uuid.uuid4().hex}"
    name, filename, content, content_type = file_field
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode("utf-8")
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_multipart(
    url: str,
    fields: dict[str, str],
    file_field: tuple[str, str, bytes, str],
    headers: dict,
    *,
    timeout: float,
    use_system_proxy: bool,
) -> tuple[bytes, Any]:
    """POST 一个 multipart/form-data，返回 ``(原始字节, 响应头)``（不解析返回体）。"""
    body, content_type = _multipart_body(fields, file_field)
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            **headers,
        },
    )
    return send(request, timeout=timeout, use_system_proxy=use_system_proxy)


def post_multipart(
    url: str,
    fields: dict[str, str],
    file_field: tuple[str, str, bytes, str],
    headers: dict,
    *,
    timeout: float,
    use_system_proxy: bool,
) -> tuple[dict, Any]:
    """POST 一个 multipart/form-data 并解析 JSON 返回体。"""
    body, response_headers = send_multipart(
        url, fields, file_field, headers,
        timeout=timeout, use_system_proxy=use_system_proxy,
    )
    return _decode_json(body), response_headers


def get_ok(url: str, headers: dict, *, timeout: float, use_system_proxy: bool) -> bool:
    """GET 一个地址，只关心"通不通"；失败返回 False（不抛）。

    用于 `/models` 这类**鉴权自检**：各网关是否提供该端点不一致，因此它只能当
    兜底证据，不能当主判据。
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        send(request, timeout=timeout, use_system_proxy=use_system_proxy)
    except ProviderError:
        return False
    return True


def http_error(exc: urllib.error.HTTPError) -> ProviderError:
    """把 HTTP 错误变成带厂商说明与 logid 的 :class:`ProviderError`。

    文案是用户唯一能看到的"为什么"，因此按 401/403、429、5xx 分类给标题；
    厂商说明既认 ``{"error": {"message": …}}``（OpenAI 形态）也认 ``{"message": …}``，
    都不认时退回错误体前 200 字（网关有时直接返回 HTML）。
    """
    raw = ""
    try:
        raw = (exc.read() or b"").decode("utf-8", "replace")
    except OSError:  # pragma: no cover - 读取响应体失败时只保留状态码
        raw = ""
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        if not detail:
            detail = str(payload.get("message") or "")
    if not detail:
        detail = raw.strip()[:200]

    logid = ""
    headers = exc.headers
    for name in _LOGID_HEADERS:
        value = headers.get(name) if headers is not None else None
        if value:
            logid = str(value)
            break

    status = int(exc.code or 0)
    if status in (401, 403):
        headline = "鉴权失败（API Key 或资源权限）"
    elif status == 429:
        headline = "被限流或额度不足"
    elif status >= 500:
        headline = "厂商服务异常"
    else:
        headline = f"请求被拒绝（HTTP {status}）"
    return ProviderError(headline, status=status, vendor_message=detail, logid=logid)


def extract_chat_text(data: dict) -> str:
    """从 ``/chat/completions`` 的返回体里取正文。

    缺 ``choices`` 时优先报厂商错误说明（部分网关用 200 带 error 体），
    否则报返回体形状问题——两种情况都不能静默当成"模型没说话"。
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise ProviderError(
                "厂商返回错误", vendor_message=str(error.get("message")), status=None
            )
        raise ProviderError(
            f"返回体缺少 choices：{json.dumps(data, ensure_ascii=False)[:200]}"
        )
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        # 部分网关按多段内容返回
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


def silent_wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """生成一小段静音 WAV（16k 单声道 16bit）。

    连通性测试用它当"最小请求"：不依赖 ffmpeg、不读用户素材，也不会产生
    任何真实内容的外发。火山对它的返回是 20000003（静音音频）——恰好证明
    Key 与资源权限都是通的。
    """
    frames = int(rate * max(0.1, seconds))
    data = b"\x00\x00" * frames
    header = (
        b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVE"
        + b"fmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")      # PCM
        + (1).to_bytes(2, "little")      # 单声道
        + rate.to_bytes(4, "little")
        + (rate * 2).to_bytes(4, "little")   # 字节率
        + (2).to_bytes(2, "little")      # 块对齐
        + (16).to_bytes(2, "little")     # 位深
        + b"data" + len(data).to_bytes(4, "little")
    )
    return header + data