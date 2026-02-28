from __future__ import annotations

import os
import json
import uuid
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, AsyncGenerator, Tuple

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from sqlalchemy.orm import Session as OrmSession

from app.db.session import SessionLocal
from app.db.models import SummaryS4, SummaryS60
from app.services.chat_service import append_user_and_assistant

router = APIRouter()

FORCE_GATEWAY_EVERY_TURN = os.getenv("FORCE_GATEWAY_EVERY_TURN", "1") == "1"
GATEWAY_CTX_USER = os.getenv("GATEWAY_CTX_USER", "rikkahub").strip() or "rikkahub"
ANCHOR_INJECT_ENABLED = os.getenv("ANCHOR_INJECT_ENABLED", "1") == "1"
WRITER_MODE_DEFAULT = (os.getenv("WRITER_MODE", "normal") or "normal").strip().lower()
TOOL_EMPTY_CONTENT_COMPAT = os.getenv("TOOL_EMPTY_CONTENT_COMPAT", "1") == "1"
TOOL_EMPTY_CONTENT_PLACEHOLDER = (os.getenv("TOOL_EMPTY_CONTENT_PLACEHOLDER", "（正在调用工具…）") or "（正在调用工具…）").strip()

LOCAL_MCP_BASE = os.getenv("LOCAL_MCP_BASE", "http://127.0.0.1:8000").rstrip("/")
LOCAL_MCP_GATEWAY_URL = os.getenv(
    "LOCAL_MCP_GATEWAY_URL",
    f"{LOCAL_MCP_BASE}/api/v1/mcp/gateway_ctx"
).strip()
LOCAL_MCP_TIMEOUT = float(os.getenv("LOCAL_MCP_TIMEOUT", "20"))
OPENAI_PROXY_DEBUG_ECHO = os.getenv("OPENAI_PROXY_DEBUG_ECHO", "0") == "1"

# -----------------------------
# DB helper
# -----------------------------
def get_db() -> OrmSession:
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise

# -----------------------------
# Utils
# -----------------------------
def _safe_json_loads(s: str):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s

def _generate_thread_id() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M")
    return f"rk:th:{ts}:{uuid.uuid4().hex[:12]}"


def _resolve_identity(payload: Dict[str, Any], req: Request) -> Dict[str, str]:
    metadata = payload.get("metadata") if isinstance(payload, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}

    def _pick_str(*values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    thread_id = _pick_str(
        req.headers.get("x-thread-id"),
        metadata.get("thread_id"),
        req.headers.get("x-session-id"),
    ) or _generate_thread_id()

    memory_id = _pick_str(
        req.headers.get("x-memory-id"),
        metadata.get("memory_id"),
        os.getenv("MEMORY_ID_DEFAULT", ""),
    ) or thread_id

    agent_id = _pick_str(
        metadata.get("agent_id"),
        os.getenv("AGENT_ID_DEFAULT", ""),
    ) or ""

    raw_s4_scope = _pick_str(metadata.get("s4_scope"))
    s4_scope = raw_s4_scope.lower() if raw_s4_scope else "thread"
    if s4_scope not in {"thread", "memory", "auto"}:
        s4_scope = "thread"
    effective_s4_scope = "thread" if s4_scope == "auto" else s4_scope

    return {
        "thread_id": thread_id,
        "memory_id": memory_id,
        "agent_id": agent_id,
        "s4_scope": effective_s4_scope,
    }

def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return ""

def _compact_summary_block(s4: Optional[Dict[str, Any]], s60: Optional[Dict[str, Any]]) -> str:
    parts = []
    if s4 and s4.get("summary"):
        parts.append("S4 (recent): " + json.dumps(s4["summary"], ensure_ascii=False))
    if s60 and s60.get("summary"):
        parts.append("S60 (long): " + json.dumps(s60["summary"], ensure_ascii=False))
    if not parts:
        return ""
    return (
        "【Internal Memory事实约束（仅用于事实一致性，不可作为语气模板；不要在回复中提到“摘要/记忆/系统”）】\n"
        + "\n".join(parts)
        + "\n【End】"
    )

def _fetch_latest_summaries(db: OrmSession, session_id: str) -> Dict[str, Any]:
    s4_row = (
        db.query(SummaryS4)
        .filter(SummaryS4.session_id == session_id)
        .order_by(SummaryS4.to_turn.desc())
        .first()
    )
    s60_row = (
        db.query(SummaryS60)
        .filter(SummaryS60.session_id == session_id)
        .order_by(SummaryS60.to_turn.desc())
        .first()
    )
    out = {"s4": None, "s60": None}
    if s4_row:
        out["s4"] = {
            "range": [s4_row.from_turn, s4_row.to_turn],
            "summary": _safe_json_loads(s4_row.summary_json),
            "created_at": s4_row.created_at.isoformat() if s4_row.created_at else None,
            "model": s4_row.model,
        }
    if s60_row:
        out["s60"] = {
            "range": [s60_row.from_turn, s60_row.to_turn],
            "summary": _safe_json_loads(s60_row.summary_json),
            "created_at": s60_row.created_at.isoformat() if s60_row.created_at else None,
            "model": s60_row.model,
        }
    return out

def _inject_system(messages: List[Dict[str, Any]], system_blocks: List[str]) -> List[Dict[str, Any]]:
    blocks = [b for b in (system_blocks or []) if b and b.strip()]
    if not blocks:
        return messages
    injected = [{"role": "system", "content": "\n\n".join(blocks)}]
    injected.extend(messages or [])
    return injected

def _build_upstream_url(upstream_base: str) -> str:
    base = (upstream_base or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com"
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"

def _build_upstream_headers() -> Dict[str, str]:
    upstream_key = os.getenv("UPSTREAM_API_KEY", "").strip()
    if not upstream_key:
        raise RuntimeError("UPSTREAM_API_KEY is empty")
    headers = {
        "Authorization": f"Bearer {upstream_key}",
        "Content-Type": "application/json",
    }
    referer = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
    title = os.getenv("OPENROUTER_X_TITLE", "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def _build_debug_headers(user_text: str, kw: str) -> Dict[str, str]:
    if not OPENAI_PROXY_DEBUG_ECHO:
        return {}

    preview = (user_text or "")[:120]
    preview_hex = preview.encode("utf-8", errors="replace")[:120].hex()
    keyword_preview = (kw or "")[:120]
    return {
        "X-Debug-User-Text-Preview": preview,
        "X-Debug-User-Text-Hex": preview_hex,
        "X-Debug-Keyword": keyword_preview,
    }

def _parse_stream_flag(body: Dict[str, Any]) -> bool:
    sv = body.get("stream", False)
    if sv is True:
        return True
    if sv is False or sv is None:
        return False
    return str(sv).lower() == "true"

# -----------------------------
# sanitize ONLY broken tool traces (avoid upstream 400)
# -----------------------------
def _sanitize_messages_for_upstream(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    pending: set[str] = set()

    def strip_last_assistant_tool_fields():
        for i in range(len(cleaned) - 1, -1, -1):
            if cleaned[i].get("role") == "assistant":
                m2 = dict(cleaned[i])
                m2.pop("tool_calls", None)
                m2.pop("function_call", None)
                cleaned[i] = m2
                return

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip()

        if role == "tool":
            tcid = (m.get("tool_call_id") or "").strip()
            if tcid and tcid in pending:
                cleaned.append(m)
                pending.discard(tcid)
            else:
                continue
            continue

        if pending and role != "tool":
            strip_last_assistant_tool_fields()
            pending.clear()

        if role == "assistant" and m.get("tool_calls") is not None:
            tc = m.get("tool_calls") or []
            ids: List[str] = []
            if isinstance(tc, list):
                for t in tc:
                    if isinstance(t, dict):
                        _id = (t.get("id") or "").strip()
                        if _id:
                            ids.append(_id)
            pending = set(ids)
            cleaned.append(m)
            continue

        if role == "assistant" and m.get("function_call") is not None:
            content = m.get("content")
            if content is None or (isinstance(content, str) and not content.strip()):
                continue
            cleaned.append(m)
            continue

        cleaned.append(m)

    if pending:
        strip_last_assistant_tool_fields()
    return cleaned

# -----------------------------
# Keyword extraction (keep for now)
# -----------------------------
_STOPWORDS = {
    "我","你","他","她","它","我们","你们","他们","她们",
    "的","了","啊","呀","呢","吧","吗","喵","哥哥","小猫咪","小命",
    "就是","但是","然后","所以","因为","如果","能不能","怎么",
    "这个","那个","现在","今天","明天","刚才","感觉","有点",
    "接着","拿起","提前","给","当是","好啦","嗯","唉呀","唔",
}
_EMO_PAT = re.compile(r"[😂🤣😭🥺😙😗😸😺😿😽💦💖💕❤️✨🎭🖤]+")
_TECH_PAT = re.compile(r"(uvicorn|python|notion|dify|mcp|rag|api|http|db|sql|error|bug|traceback|token|stream|openrouter|rikkahub|telegram)", re.I)

def _is_smalltalk_emotion(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if not t:
        return True
    if _TECH_PAT.search(t):
        return False
    if len(t) <= 18 and any(x in t for x in ["哥哥", "猫咪", "小猫咪", "小命", "宝宝", "在吗", "早安", "晚安", "嘿嘿", "喵"]):
        return True
    emo_hits = len(_EMO_PAT.findall(t))
    if emo_hits >= 2:
        return True
    if t.count("~") >= 2 or t.count("…") >= 2:
        return True
    if t.count("喵") >= 2 or t.count("嘿嘿") >= 2:
        return True
    if any(x in t for x in ["想你", "抱抱", "亲亲", "贴贴", "陪我", "我回来啦", "我来啦", "我走啦", "加油", "辛苦啦"]):
        return True
    return False

def _split_long_cn(seq: str) -> list[str]:
    seps = ["，","。","！","？","…","～","—","(",")","（","）"," ", "\n",
            "又","接着","拿起","就当","当是","今天","提前","给","好啦","于是","然后","所以","但是","因为","不过"]
    s = seq
    for sp in seps:
        s = s.replace(sp, "|")
    parts = [p for p in s.split("|") if p]
    return parts

def _extract_keywords(text: str, k: int = 2) -> str:
    if not text:
        return "猫咪,哥哥"
    if _is_smalltalk_emotion(text):
        return "撒娇,哥哥"

    cn_seqs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    cand: list[str] = []
    for seq in cn_seqs:
        parts = _split_long_cn(seq) if len(seq) > 6 else [seq]
        for p in parts:
            p = p.strip()
            if not p or p in _STOPWORDS:
                continue
            if 2 <= len(p) <= 6:
                cand.append(p)

    if not cand:
        return "猫咪,哥哥"

    seen = set()
    uniq = []
    for t in cand:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    picked = uniq[:k]
    if "猫咪" not in picked and k >= 2:
        picked = picked[:k-1] + ["猫咪"]
    return ",".join(picked)

# -----------------------------
# NEW: call local MCP gateway_ctx
# -----------------------------
async def _call_local_gateway_ctx(
    keyword: str,
    text: str,
    user: str,
    summaries: Optional[Dict[str, Any]] = None,
) -> str:
    req_id = uuid.uuid4().hex[:8]
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {
            "name": "gateway_ctx",
            "arguments": {
                "keyword": keyword,
                "text": text,
                "user": user,
                "summaries": summaries or {},
            }
        }
    }

    headers = {
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "1",
        # 让本机 gateway_ctx 按你现在兼容的版本返回
        "MCP-Protocol-Version": os.getenv("MCP_PROTOCOL_VERSION", "2025-06-18"),
    }

    async with httpx.AsyncClient(timeout=LOCAL_MCP_TIMEOUT) as client:
        r = await client.post(LOCAL_MCP_GATEWAY_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    # data: {"jsonrpc":"2.0","id":...,"result":{"content":[{"type":"text","text":"..."}],...}}
    try:
        res = data.get("result", {})
        content = res.get("content", []) or []
        if isinstance(content, list) and content:
            t = content[0].get("text", "")
            return str(t or "").strip()
    except Exception:
        pass
    return ""

def _build_anchor_system_block(snippet: str) -> str:
    snippet = (snippet or "").strip()
    if not snippet:
        return ""
    return (
        "【Persona Anchor（仅用于你在心里模仿语气与节奏，不要在回复中提到“锚点/检索/工具/系统”）】\n"
        "规则：下面内容是【学习素材】。你绝对不可以逐句复述或引用其中任何一句原话；只能学习称呼、语气、节奏、动作描写方式，用你自己的话回答。\n"
        f"{snippet}\n"
        "【End】"
    )




def _apply_tool_empty_content_compat(data: Dict[str, Any]) -> Dict[str, Any]:
    if not TOOL_EMPTY_CONTENT_COMPAT:
        return data
    if not isinstance(data, dict):
        return data
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return data
    c0 = choices[0] if isinstance(choices[0], dict) else {}
    msg = c0.get("message") if isinstance(c0.get("message"), dict) else None
    if not msg:
        return data

    finish_reason = c0.get("finish_reason")
    has_tool_calls = bool(msg.get("tool_calls"))
    content = msg.get("content")

    if finish_reason == "tool_calls" and has_tool_calls and isinstance(content, str) and not content.strip():
        out = dict(data)
        out_choices = list(choices)
        out_c0 = dict(c0)
        out_msg = dict(msg)
        out_msg["content"] = TOOL_EMPTY_CONTENT_PLACEHOLDER
        out_c0["message"] = out_msg
        out_choices[0] = out_c0
        out["choices"] = out_choices
        return out
    return data

def _resolve_writer_mode(payload: Dict[str, Any]) -> str:
    metadata = payload.get("metadata") if isinstance(payload, dict) else {}
    if isinstance(metadata, dict):
        mode = metadata.get("writer_mode") or metadata.get("mode")
        if isinstance(mode, str) and mode.strip():
            return mode.strip().lower()
    return WRITER_MODE_DEFAULT


def _build_writer_constraint_block(writer_mode: str) -> str:
    base = (
        "【Writer Constraint】\n"
        "你可以自然发挥、保持表达灵活与有温度。\n"
        "禁止语气迁移：S4/S60 仅可作为事实约束，严禁把其中原话或语气当作措辞模板。"
    )
    if writer_mode == "weak":
        return (
            base
            + "\n"
            + "当前为 weak 模式：禁止编造明确事实（如时间、地点、人物身份、事件经过、数据、引用来源）。"
            + "若事实不确定，请明确说明不确定，或用条件句/建议式表达。\n"
            + "【End】"
        )
    return base + "\n【End】"

# -----------------------------
# Streaming proxy: single stream + collect + store
# -----------------------------
async def _proxy_stream_and_store(
    upstream_url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    session_id: str,
    user_text: str,
    model_name: str,
) -> AsyncGenerator[bytes, None]:
    full_parts: List[str] = []
    done = False

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", upstream_url, headers=headers, json=body) as r:
            if r.status_code >= 400:
                raw = await r.aread()
                try:
                    j = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
                    msg = j.get("error", {}).get("message") or j.get("message") or raw.decode("utf-8", errors="ignore")
                except Exception:
                    msg = raw.decode("utf-8", errors="ignore")
                err = {"error": {"message": msg, "type": "upstream_error", "status": r.status_code}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                return

            async for line in r.aiter_lines():
                if line is None:
                    continue
                if line == "":
                    yield b"\n"
                    continue

                yield (line + "\n").encode("utf-8")

                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    done = True
                    break

                try:
                    j = json.loads(data)
                    delta = (j.get("choices") or [{}])[0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        full_parts.append(piece)
                except Exception:
                    continue

    full_text = "".join(full_parts).strip()

    if full_text:
        db2 = get_db()
        try:
            append_user_and_assistant(
                db2,
                session_id=session_id,
                user_text=user_text,
                assistant_text=full_text,
                model_name=model_name,
                s4_every_user_turns=int(os.getenv("S4_EVERY_USER_TURNS", "4")),
                s60_every_user_turns=int(os.getenv("S60_EVERY_USER_TURNS", "30")),
                s4_window_user_turns=int(os.getenv("S4_WINDOW_USER_TURNS", "4")),
                s60_window_user_turns=int(os.getenv("S60_WINDOW_USER_TURNS", "30")),
            )
        finally:
            db2.close()

    if not done:
        yield b"\ndata: [DONE]\n\n"

# -----------------------------
# Main route: OpenAI compatible
# -----------------------------
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload: Dict[str, Any] = await request.json()
    identity = _resolve_identity(payload, request)
    thread_id = identity["thread_id"]
    memory_id = identity["memory_id"]
    agent_id = identity["agent_id"]
    s4_scope = identity["s4_scope"]
    session_id = thread_id

    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return JSONResponse({"error": {"message": "messages must be a list"}}, status_code=400)

    # 当请求显式携带 tools/functions 时，保留完整工具轮次，
    # 避免把客户端（如 RikkaHub）传入的 tool 历史意外清掉，导致“工具执行后空回”。
    has_tools = bool(payload.get("tools") or payload.get("functions"))
    if not has_tools:
        messages = _sanitize_messages_for_upstream(messages)
    user_text = _last_user_text(messages)

    # summaries
    db = get_db()
    try:
        sums = _fetch_latest_summaries(db, session_id=session_id)
    finally:
        db.close()

    s_block = _compact_summary_block(sums.get("s4"), sums.get("s60"))

    # ✅ 统一入口：每轮强制走本机 MCP gateway_ctx，proxy 不再直连 Dify
    anchor_block = ""
    kw = ""
    if ANCHOR_INJECT_ENABLED and FORCE_GATEWAY_EVERY_TURN:
        kw = _extract_keywords(user_text, k=2)
        # 使用稳定的会话标识，避免每次请求的 user 变化
        metadata = payload.get("metadata", {})
        stable_user = (metadata.get("gateway_user") or payload.get("user") or GATEWAY_CTX_USER)
        ctx = await _call_local_gateway_ctx(
            keyword=kw,
            text=user_text,
            user=stable_user,
            summaries={"s4": sums.get("s4"), "s60": sums.get("s60")},
        )
        anchor_block = _build_anchor_system_block(ctx)

    system_blocks = []
    if s_block:
        system_blocks.append(s_block)
    if anchor_block:
        system_blocks.append(anchor_block)
    writer_mode = _resolve_writer_mode(payload)
    system_blocks.append(_build_writer_constraint_block(writer_mode))

    messages2 = _inject_system(messages, system_blocks)

    upstream_base = os.getenv("UPSTREAM_BASE_URL", "https://openrouter.ai/api/v1")
    try:
        headers = _build_upstream_headers()
    except RuntimeError as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)

    upstream_url = _build_upstream_url(upstream_base)

    body = dict(payload)
    body["messages"] = messages2

    stream = _parse_stream_flag(body)
    model_name = str(body.get("model") or "unknown")

    # 给你加个可观测：返回上游地址
    if stream:
        debug_headers = _build_debug_headers(user_text, kw if ANCHOR_INJECT_ENABLED and FORCE_GATEWAY_EVERY_TURN else "")
        return StreamingResponse(
            _proxy_stream_and_store(
                upstream_url,
                headers,
                body,
                session_id=session_id,
                user_text=user_text,
                model_name=model_name,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Upstream-URL": upstream_url,
                "X-Thread-Id": thread_id,
                "X-Memory-Id": memory_id,
                "X-Agent-Id": agent_id,
                "X-S4-Scope": s4_scope,
                "X-Session-Id": session_id,
                **debug_headers,
            },
        )

    async with httpx.AsyncClient(timeout=None) as client:
        r = await client.post(upstream_url, headers=headers, json=body)
        if r.status_code >= 400:
            ct = r.headers.get("content-type", "")
            if ct.startswith("application/json"):
                resp = JSONResponse(r.json(), status_code=r.status_code)
            else:
                resp = JSONResponse({"error": {"message": r.text}}, status_code=r.status_code)
            resp.headers["x-upstream-url"] = upstream_url
            resp.headers["x-thread-id"] = thread_id
            resp.headers["x-memory-id"] = memory_id
            resp.headers["x-agent-id"] = agent_id
            resp.headers["x-s4-scope"] = s4_scope
            resp.headers["x-session-id"] = session_id
            for k, v in _build_debug_headers(user_text, kw if ANCHOR_INJECT_ENABLED and FORCE_GATEWAY_EVERY_TURN else "").items():
                resp.headers[k] = v
            return resp

        data = r.json()

    data = _apply_tool_empty_content_compat(data)

    assistant_text = ""
    try:
        assistant_text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception:
        assistant_text = ""

    if assistant_text:
        db2 = get_db()
        try:
            append_user_and_assistant(
                db2,
                session_id=session_id,
                user_text=user_text,
                assistant_text=assistant_text,
                model_name=model_name,
                s4_every_user_turns=int(os.getenv("S4_EVERY_USER_TURNS", "4")),
                s60_every_user_turns=int(os.getenv("S60_EVERY_USER_TURNS", "30")),
                s4_window_user_turns=int(os.getenv("S4_WINDOW_USER_TURNS", "4")),
                s60_window_user_turns=int(os.getenv("S60_WINDOW_USER_TURNS", "30")),
            )
        finally:
            db2.close()

    resp = JSONResponse(data)
    resp.headers["x-upstream-url"] = upstream_url
    resp.headers["x-thread-id"] = thread_id
    resp.headers["x-memory-id"] = memory_id
    resp.headers["x-agent-id"] = agent_id
    resp.headers["x-s4-scope"] = s4_scope
    resp.headers["x-session-id"] = session_id
    for k, v in _build_debug_headers(user_text, kw if ANCHOR_INJECT_ENABLED and FORCE_GATEWAY_EVERY_TURN else "").items():
        resp.headers[k] = v
    return resp
