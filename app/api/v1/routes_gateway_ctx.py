from __future__ import annotations

import os
import time
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

router = APIRouter()
JSON_UTF8 = "application/json; charset=utf-8"

DIFY_BASE_URL = os.getenv("DIFY_BASE_URL", "https://api.dify.ai").strip()
DIFY_API_KEY = (os.getenv("DIFY_API_KEY") or os.getenv("DIFY_WORKFLOW_API_KEY") or "").strip()
DIFY_WORKFLOW_RUN_URL = os.getenv("DIFY_WORKFLOW_RUN_URL", "https://api.dify.ai/v1/workflows/run").strip()
DIFY_WORKFLOW_ID_ANCHOR = os.getenv("DIFY_WORKFLOW_ID_ANCHOR", "").strip()

# ✅ 默认别用 2025-11-25（你之前就被这个坑过）
DEFAULT_MCP_PROTOCOL_VERSION = os.getenv("MCP_PROTOCOL_VERSION", "2025-06-18").strip()

SUPPORTED_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
}

# 原句截断长度（你可以在 .env 调）
CTX_MAX = int(os.getenv("ANCHOR_SNIP_MAX", "400"))
GATEWAY_CTX_DEBUG = os.getenv("GATEWAY_CTX_DEBUG", "0").strip().lower() in ("1", "true", "yes")
DIFY_TIMEOUT_SECS = float(os.getenv("DIFY_TIMEOUT_SECS", "30"))
RETRIEVAL_TOP_N = int(os.getenv("RETRIEVAL_TOP_N", "3"))

RETRIEVAL_PROFILE_VERSION = os.getenv("RETRIEVAL_PROFILE_VERSION", "v1.0.0").strip() or "v1.0.0"
W_KEYWORD = 0.40
W_VECTOR = 0.40
W_RECENCY = 0.10
W_TYPE = 0.10

# 关键词乱码修复开关：当 keyword 里大部分都是 '?' 时，优先用 text 重新推导中文关键词（而不是直接走撒娇/猫咪兜底）
GARBLED_KW_REPAIR_ENABLED = os.getenv("GARBLED_KW_REPAIR_ENABLED", "1").strip().lower() in ("1", "true", "yes")

# 用于判断 '?' 乱码：只要非空且 '?' 占比高，就视为乱码 keyword
_QMARK = "?"
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
def _looks_garbled_keyword(keyword: str) -> bool:
    kw = (keyword or "").strip()
    if not kw:
        return False
    q = kw.count(_QMARK)
    # 关键：客户端把中文变成 '?' 时，往往会出现 '??' 或 '??,???'
    if q == 0:
        return False
    # 忽略分隔符后的长度
    total = sum(1 for ch in kw if ch not in " ,，;；|/\t\r\n")
    if total <= 0:
        return True
    return (q / total) >= 0.4

# 从 text 推导“中文关键词检索”用的 keyword（仅在 keyword 缺失/乱码时使用）
_STOP_TOKENS = set([
    "哥哥", "哥", "类", "神代", "喵", "猫咪", "小猫咪", "宝宝", "亲", "抱", "mua", "啾", "嘿嘿",
])
def _derive_kw_from_text(text: str, k: int = 2) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    # 1) 先抓中文连续片段
    seqs = _CJK_RE.findall(t)
    cands: list[str] = []
    for s in seqs:
        s = s.strip()
        if not s:
            continue
        # 去掉纯情绪/称呼词
        if s in _STOP_TOKENS:
            continue
        # 过滤太短/太长
        if len(s) < 2:
            continue
        # 常见口语词也别当关键词
        if s in ("就是", "然后", "那个", "这个", "怎么", "为什么", "可以", "不要", "不是"):
            continue
        if s not in cands:
            cands.append(s)
        if len(cands) >= k:
            break
    if not cands:
        return ""
    return ",".join(cands)


# 轻量缓存（同 keyword 短时间重复调用就直接复用）
CACHE_TTL_SECS = float(os.getenv("GATEWAY_CTX_CACHE_TTL", "20"))
MAX_CACHE_SIZE = int(os.getenv("GATEWAY_CTX_CACHE_MAX", "256"))
_cache: Dict[str, Tuple[float, str, Dict[str, Any]]] = {}

_EMO_MARKERS = [
    "哥哥", "类", "喵", "猫咪", "小猫咪", "宝宝", "亲", "抱", "mua", "啾", "嘿嘿",
    "🥺", "😙", "😗", "😽", "😭", "🥰", "💖", "🖤",
]


def _jsonrpc_error(_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": _id, "error": err}


def _jsonrpc_result(_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": _id, "result": result}


def _negotiate_protocol_version(request: Request, params: Dict[str, Any]) -> str:
    pv = str((params or {}).get("protocolVersion") or "").strip()
    if pv and pv in SUPPORTED_VERSIONS:
        return pv

    hv = (request.headers.get("MCP-Protocol-Version") or "").strip()
    if hv and hv in SUPPORTED_VERSIONS:
        return hv

    return DEFAULT_MCP_PROTOCOL_VERSION if DEFAULT_MCP_PROTOCOL_VERSION in SUPPORTED_VERSIONS else "2025-06-18"


def _mcp_wrap_text(res_obj: Dict[str, Any], text_out: str, is_error: bool) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text_out or ""}], "isError": bool(is_error), "data": res_obj}


def _build_evidence_item(
    index: int,
    source_type: str,
    source_id: str,
    text: str,
    keyword_used: str,
    ts: int,
    chunk_id: str = "",
    source_name: str = "anchor_rag",
) -> Dict[str, Any]:
    return {
        "id": f"ev_{index}",
        "source_type": source_type,
        "source_id": source_id,
        "text": text or "",
        "score_raw": 1.0,
        "score_final": 1.0,
        "reason": "keyword_hit" if source_type == "keyword" else "fallback_hit",
        "ts": ts,
        "meta": {
            "source_name": source_name,
            "keyword_used": keyword_used or "",
            "chunk_id": chunk_id or "",
        },
    }


def _build_gateway_evidence(primary_keyword: str, primary_text: str, fallback_keyword: str, fallback_text: str) -> List[Dict[str, Any]]:
    now_ts = int(time.time())
    evidence: List[Dict[str, Any]] = []
    if primary_text:
        evidence.append(
            _build_evidence_item(
                index=len(evidence),
                source_type="keyword",
                source_id=primary_keyword or "",
                text=primary_text,
                keyword_used=primary_keyword,
                ts=now_ts,
            )
        )
    if fallback_text:
        evidence.append(
            _build_evidence_item(
                index=len(evidence),
                source_type="fallback",
                source_id=fallback_keyword or "",
                text=fallback_text,
                keyword_used=fallback_keyword,
                ts=now_ts,
            )
        )
    return evidence


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _type_boost(source_type: str) -> float:
    if source_type == "current_input":
        return 1.3
    if source_type == "s4":
        return 1.2
    if source_type == "s60":
        return 1.1
    if source_type in ("keyword", "vector"):
        return 1.0
    return 0.6


def _source_priority(source_type: str) -> int:
    if source_type == "current_input":
        return 4
    if source_type == "s4":
        return 3
    if source_type == "s60":
        return 2
    return 1


def _parse_iso_ts(created_at: str) -> int:
    try:
        if not created_at:
            return int(time.time())
        return int(datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp())
    except Exception:
        return int(time.time())


def _build_summary_candidates(summaries: Dict[str, Any], text: str) -> List[Dict[str, Any]]:
    now_ts = int(time.time())
    out: List[Dict[str, Any]] = []
    if text:
        out.append(
            {
                "id": "input_0",
                "source_type": "current_input",
                "source_id": "current_input",
                "text": text,
                "chunk_id": "",
                "metadata": {"source_name": "gateway_input"},
                "reason": "当前输入事实优先",
                "ts": now_ts,
                "score_raw": {"keyword": 0.0, "vector": 0.0},
            }
        )

    for source_type in ("s4", "s60"):
        item = (summaries or {}).get(source_type) or {}
        summary = item.get("summary")
        if not summary:
            continue
        out.append(
            {
                "id": f"{source_type}_0",
                "source_type": source_type,
                "source_id": source_type,
                "text": summary if isinstance(summary, str) else str(summary),
                "chunk_id": "",
                "metadata": {
                    "source_name": "memory_summary",
                    "range": item.get("range") or [],
                    "model": item.get("model") or "",
                },
                "reason": f"来自{source_type.upper()}的事实约束",
                "ts": _parse_iso_ts(str(item.get("created_at") or "")),
                "score_raw": {"keyword": 0.0, "vector": 0.0},
            }
        )
    return out


def _recency_score(ts: int) -> float:
    if not ts:
        return 0.0
    age = max(0, int(time.time()) - int(ts))
    day = 86400
    if age <= day:
        return 1.0
    if age <= 7 * day:
        return 0.8
    if age <= 30 * day:
        return 0.6
    return 0.3


def _adapt_keyword_candidates(raw_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unified: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for idx, item in enumerate(raw_candidates):
        score = _safe_float(item.get("score"), 1.0)
        unified.append(
            {
                "id": item.get("id") or f"kw_{idx}",
                "source_type": "keyword",
                "source_id": item.get("source_id") or item.get("keyword") or "",
                "text": item.get("text") or "",
                "chunk_id": item.get("chunk_id") or "",
                "metadata": item.get("metadata") or {},
                "reason": item.get("reason") or "keyword_hit",
                "ts": int(item.get("ts") or now_ts),
                "score_raw": {
                    "keyword": score,
                    "vector": 0.0,
                },
            }
        )
    return unified


def _adapt_vector_candidates(raw_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unified: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for idx, item in enumerate(raw_candidates):
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("doc_id") or item.get("document_id") or item.get("id") or "")
        chunk_id = str(item.get("chunk_id") or item.get("segment_id") or "")
        text = str(item.get("text") or item.get("content") or "")
        score = _safe_float(item.get("score"), 0.0)
        unified.append(
            {
                "id": f"vec_{idx}",
                "source_type": "vector",
                "source_id": doc_id,
                "text": text,
                "chunk_id": chunk_id,
                "metadata": item.get("metadata") or {},
                "reason": item.get("reason") or "vector_hit",
                "ts": int(item.get("ts") or now_ts),
                "score_raw": {
                    "keyword": 0.0,
                    "vector": score,
                },
            }
        )
    return unified


def _score_and_rank_candidates(candidates: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    for idx, item in enumerate(candidates):
        raw = dict(item.get("score_raw") or {})
        raw["keyword"] = _safe_float(raw.get("keyword"), 0.0)
        raw["vector"] = _safe_float(raw.get("vector"), 0.0)
        raw["recency"] = _recency_score(int(item.get("ts") or 0))
        raw["type_boost"] = _type_boost(str(item.get("source_type") or ""))

        score_final = (
            (W_KEYWORD * raw["keyword"])
            + (W_VECTOR * raw["vector"])
            + (W_RECENCY * raw["recency"])
            + (W_TYPE * raw["type_boost"])
        )

        out = {
            "id": item.get("id") or f"ev_{idx}",
            "source_type": item.get("source_type") or "unknown",
            "source_id": item.get("source_id") or "",
            "text": item.get("text") or "",
            "score_raw": raw,
            "score_final": round(score_final, 6),
            "reason": item.get("reason") or "",
            "ts": int(item.get("ts") or 0),
            "meta": {
                "source_name": "anchor_rag",
                "chunk_id": item.get("chunk_id") or "",
                "source_priority": _source_priority(str(item.get("source_type") or "")),
                **(item.get("metadata") or {}),
            },
        }
        scored.append(out)

    scored.sort(
        key=lambda x: (
            x.get("score_final", 0.0),
            _source_priority(str(x.get("source_type") or "")),
            _safe_float((x.get("score_raw") or {}).get("recency"), 0.0),
        ),
        reverse=True,
    )
    n = max(1, int(top_n or RETRIEVAL_TOP_N))
    return _postprocess_candidates(scored, top_n=n)


def _normalize_text_for_dedupe(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return ""
    t = re.sub(r"[\W_]+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _tokenize_for_jaccard(text: str) -> set[str]:
    nt = _normalize_text_for_dedupe(text)
    if not nt:
        return set()
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", nt)
    if not tokens:
        return set(nt.split())
    return set(tokens)


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union <= 0:
        return 0.0
    return inter / union


def _dup_payload(ev: Dict[str, Any]) -> Dict[str, Any]:
    meta = ev.get("meta") or {}
    return {
        "id": ev.get("id") or "",
        "source_type": ev.get("source_type") or "",
        "source_id": ev.get("source_id") or "",
        "chunk_id": meta.get("chunk_id") or "",
        "score_final": ev.get("score_final") or 0.0,
        "reason": ev.get("reason") or "",
    }


def _merge_duplicate(kept: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    k_score = _safe_float(kept.get("score_final"), 0.0)
    i_score = _safe_float(incoming.get("score_final"), 0.0)

    keeper = kept
    dup = incoming
    if i_score > k_score:
        keeper, dup = incoming, kept

    keeper_meta = dict(keeper.get("meta") or {})
    keeper_dups = list(keeper_meta.get("duplicates") or [])
    keeper_dups.append(_dup_payload(dup))
    keeper_meta["duplicates"] = keeper_dups
    keeper["meta"] = keeper_meta
    return keeper


def _postprocess_candidates(scored: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    # Step-1: 按 source_id + chunk_id 去重，保留 score_final 更高者
    by_source_chunk: List[Dict[str, Any]] = []
    key_index: Dict[Tuple[str, str], int] = {}
    for ev in scored:
        ev2 = dict(ev)
        ev2["meta"] = dict(ev2.get("meta") or {})
        ev2["meta"].setdefault("duplicates", [])

        key = (str(ev2.get("source_id") or ""), str(ev2["meta"].get("chunk_id") or ""))
        if key not in key_index:
            key_index[key] = len(by_source_chunk)
            by_source_chunk.append(ev2)
            continue

        idx = key_index[key]
        merged = _merge_duplicate(by_source_chunk[idx], ev2)
        by_source_chunk[idx] = merged

    # Step-2: 文本归一化 + token Jaccard 近似去重
    deduped: List[Dict[str, Any]] = []
    token_sets: List[set[str]] = []
    for ev in by_source_chunk:
        cur_tokens = _tokenize_for_jaccard(str(ev.get("text") or ""))
        duplicate_idx = None
        for i, seen_tokens in enumerate(token_sets):
            if _jaccard_similarity(cur_tokens, seen_tokens) > 0.9:
                duplicate_idx = i
                break

        if duplicate_idx is None:
            deduped.append(ev)
            token_sets.append(cur_tokens)
            continue

        merged = _merge_duplicate(deduped[duplicate_idx], ev)
        deduped[duplicate_idx] = merged
        token_sets[duplicate_idx] = _tokenize_for_jaccard(str(merged.get("text") or ""))

    deduped.sort(key=lambda x: x.get("score_final", 0.0), reverse=True)
    return deduped[:top_n]


def _extract_vector_candidates_safe(outs: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        vc = outs.get("vector_candidates") if isinstance(outs, dict) else []
        if isinstance(vc, list):
            return vc
        return []
    except Exception as e:
        print(f"[gateway_ctx] vector_retrieval_degrade err={e}")
        return []


def _is_emo_chitchat(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return any(m in t for m in _EMO_MARKERS)


def _truncate_ctx(text: str) -> str:
    t = (text or "").strip().replace("\r", "")
    if not t:
        return ""
    if len(t) <= CTX_MAX:
        return t
    return t[:CTX_MAX].rstrip() + "…"


def _compute_grounding_mode(evidence: List[Dict[str, Any]]) -> str:
    if not evidence:
        return "none"
    top1 = _safe_float(evidence[0].get("score_final"), 0.0)
    valid_evidence_count = sum(1 for ev in evidence if str(ev.get("text") or "").strip())
    if top1 < 0.45 and valid_evidence_count < 2:
        return "weak"
    return "strong"


def _debug_fields(
    *,
    cache_hit: bool,
    cache_miss_reason: str,
    keyword_primary: str,
    keyword_used: str,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    ev = evidence if isinstance(evidence, list) else []
    return {
        "cache_hit": bool(cache_hit),
        "cache_miss_reason": cache_miss_reason,
        "keyword_primary": keyword_primary or "",
        "keyword_used": keyword_used or "",
        "grounding_mode": _compute_grounding_mode(ev),
    }

def _normalize_kw(keyword: str) -> str:
    """Normalize keyword string to stabilize caching."""
    kw = (keyword or "").strip()
    if not kw:
        return ""
    # unify separators
    kw = kw.replace("，", ",").replace(";", ",").replace("；", ",")
    parts = [p.strip() for p in kw.split(",") if p.strip()]
    # de-dup while preserving order
    seen = set()
    uniq = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return ",".join(uniq)


def _has_cache_for_other_profile(user: str, keyword: str, profile_version: str) -> bool:
    prefix = f"{user}||{keyword}||"
    legacy_key = f"{user}||{keyword}"
    for key in _cache.keys():
        if key == legacy_key:
            return True
        if key.startswith(prefix):
            other_profile = key[len(prefix):]
            if other_profile and other_profile != profile_version:
                return True
    return False



async def _call_dify_anchor(keyword: str, user: str = "mcp") -> Dict[str, Any]:
    if not DIFY_API_KEY:
        raise RuntimeError("Missing env DIFY_API_KEY (or DIFY_WORKFLOW_API_KEY)")

    url = DIFY_WORKFLOW_RUN_URL or f"{DIFY_BASE_URL.rstrip('/')}/v1/workflows/run"
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}

    payload: Dict[str, Any] = {
        "inputs": {"keyword": keyword},
        "response_mode": "blocking",
        "user": user,
    }
    if DIFY_WORKFLOW_ID_ANCHOR:
        payload["workflow_id"] = DIFY_WORKFLOW_ID_ANCHOR

    async with httpx.AsyncClient(timeout=DIFY_TIMEOUT_SECS) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


def _extract_outputs(dify_resp: Dict[str, Any]) -> Dict[str, Any]:
    outputs: Dict[str, Any] = {}
    if isinstance(dify_resp, dict):
        if isinstance(dify_resp.get("data"), dict) and isinstance(dify_resp["data"].get("outputs"), dict):
            outputs = dify_resp["data"]["outputs"]
        elif isinstance(dify_resp.get("outputs"), dict):
            outputs = dify_resp["outputs"]

    result = ""
    chat_text = ""
    vector_candidates: List[Dict[str, Any]] = []
    if isinstance(outputs, dict):
        result = str(outputs.get("result") or "")
        chat_text = str(outputs.get("chat_text") or "")
        if isinstance(outputs.get("vector_candidates"), list):
            vector_candidates = outputs.get("vector_candidates") or []
    return {"result": result, "chat_text": chat_text, "vector_candidates": vector_candidates}


async def _handle_jsonrpc(request: Request, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _id = msg.get("id", None)
    method = msg.get("method", "")
    params = (msg.get("params", {}) or {}) if isinstance(msg, dict) else {}
    is_notification = isinstance(msg, dict) and ("id" not in msg)

    pv = _negotiate_protocol_version(request, params)
    request.state.mcp_pv = pv

    if method == "initialize":
        result = {
            "protocolVersion": pv,
            "serverInfo": {"name": "gateway_ctx", "version": "2.3"},
            "capabilities": {"tools": {}},
        }
        return None if is_notification else _jsonrpc_result(_id, result)

    if method == "tools/list":
        tools = [{
            "name": "gateway_ctx",
            "description": "Unified gateway context builder: keyword + Anchor RAG snippet. Returns MCP content[].text + debug data.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "search keywords"},
                    "text": {"type": "string", "description": "optional raw user message"},
                    "user": {"type": "string", "description": "optional user/session id"},
                    "summaries": {
                        "type": "object",
                        "description": "optional session summaries, support s4/s60 as fact constraints",
                    },
                },
                "required": ["keyword"],
            },
        }]
        return None if is_notification else _jsonrpc_result(_id, {"tools": tools})

    if method != "tools/call":
        return None if is_notification else _jsonrpc_error(_id, -32601, f"Method not found: {method}")

    name = params.get("name")
    arguments = params.get("arguments", {}) or {}
    if name != "gateway_ctx":
        return None if is_notification else _jsonrpc_error(_id, -32601, f"Unknown tool: {name}")

    keyword = str(arguments.get("keyword") or "").strip()
    text = str(arguments.get("text") or "").strip()
    user = str(arguments.get("user") or "mcp").strip()
    summaries = arguments.get("summaries") if isinstance(arguments.get("summaries"), dict) else {}

        # 1) 先确定 primary keyword（优先使用上游抽取结果；仅在缺失/乱码时，才用 text 推导中文关键词）
    primary_keyword_raw = keyword

    # 1.1 缺失 / 乱码 -> 用 text 推导中文关键词（尽量保持“中文关键词检索”，不要直接掉到撒娇/猫咪兜底）
    if (not keyword) or (GARBLED_KW_REPAIR_ENABLED and _looks_garbled_keyword(keyword)):
        derived = _derive_kw_from_text(text)
        if derived:
            if GARBLED_KW_REPAIR_ENABLED and _looks_garbled_keyword(primary_keyword_raw):
                print(f"[gateway_ctx] repair_garbled_kw from={primary_keyword_raw!r} to={derived!r}")
            keyword = derived
        else:
            keyword = ""

    # 1.2 可选：垃圾 keyword -> 也尝试用 text 推导
    try:
        if keyword and "_is_garbage_kw" in globals() and _is_garbage_kw(keyword):
            derived = _derive_kw_from_text(text)
            keyword = derived or ""
    except Exception:
        pass

    # 1.3 如果最终仍然没有 keyword（例如 text 也抽不到），才用情绪兜底 keyword
    if not keyword:
        keyword = "哥哥,小猫咪" if _is_emo_chitchat(text) else "哥哥,撒娇"

    # 2) 再生成 cache_key（必须在 keyword 最终确定之后）
    keyword = _normalize_kw(keyword)
    primary_keyword = keyword
    cache_key = f"{user}||{primary_keyword}||{RETRIEVAL_PROFILE_VERSION}"
    t0 = time.perf_counter()
    if GATEWAY_CTX_DEBUG:
        print(f"[gateway_ctx] pid={os.getpid()} cache_size={len(_cache)} kw={keyword!r}")
        print(f"[gateway_ctx] user={user!r} cache_key={cache_key!r} ttl={CACHE_TTL_SECS}")

    # cache hit?
    now = time.time()
    hit = _cache.get(cache_key)
    cache_miss_reason = "profile_changed" if _has_cache_for_other_profile(user, primary_keyword, RETRIEVAL_PROFILE_VERSION) else "not_found"
    if hit:
        cache_miss_reason = "expired" if (now - hit[0] > CACHE_TTL_SECS) else "bypassed"

    if hit and (now - hit[0] <= CACHE_TTL_SECS):
        ctx, res_obj = hit[1], hit[2]
        evidence_cached = res_obj.get("evidence") if isinstance(res_obj, dict) else []
        debug = _debug_fields(
            cache_hit=True,
            cache_miss_reason=cache_miss_reason,
            keyword_primary=primary_keyword,
            keyword_used=str((res_obj or {}).get("keyword") or primary_keyword),
            evidence=evidence_cached,
        )
        res_obj["retrieval_profile_version"] = RETRIEVAL_PROFILE_VERSION
        res_obj.update(debug)
        dt = (time.perf_counter() - t0) * 1000
        print(f"[gateway_ctx] cache_hit kw={keyword!r} ms={dt:.1f} len={len(ctx)}")
        return None if is_notification else _jsonrpc_result(_id, _mcp_wrap_text(res_obj, ctx, is_error=False))

    # cache miss -> call dify
    try:
        t1 = time.perf_counter()
        dify = await _call_dify_anchor(keyword=keyword, user=user)
        ms_dify = (time.perf_counter() - t1) * 1000

        outs = _extract_outputs(dify)
        picked = (outs.get("result") or "").strip() or (outs.get("chat_text") or "").strip()
        ctx = _truncate_ctx(picked)

        used_keyword = primary_keyword
        ms_dify_primary = ms_dify
        ms_dify_used = ms_dify
        primary_hit_text = ctx
        fallback_keyword = ""
        fallback_hit_text = ""

        # 3.1 如果 primary keyword 没命中（ctx 为空），再按“撒娇程度”路由到亲密兜底 keyword，并重试一次
        if not ctx:
            fallback_keyword = _normalize_kw("哥哥,小猫咪" if _is_emo_chitchat(text) else "哥哥,撒娇")
            # 避免 primary 本来就是兜底 keyword 时重复调用
            if fallback_keyword and fallback_keyword != primary_keyword:
                if GATEWAY_CTX_DEBUG:
                    print(f"[gateway_ctx] primary_miss kw={primary_keyword!r} -> fallback={fallback_keyword!r}")
                t2 = time.perf_counter()
                dify2 = await _call_dify_anchor(keyword=fallback_keyword, user=user)
                ms_dify2 = (time.perf_counter() - t2) * 1000
                outs2 = _extract_outputs(dify2)
                picked2 = (outs2.get("result") or "").strip() or (outs2.get("chat_text") or "").strip()
                ctx2 = _truncate_ctx(picked2)
                if ctx2:
                    fallback_hit_text = ctx2
                    used_keyword = fallback_keyword
                    ctx = ctx2
                    outs = outs2
                    ms_dify_used = ms_dify2

        keyword_candidates = _build_gateway_evidence(
            primary_keyword=primary_keyword,
            primary_text=primary_hit_text,
            fallback_keyword=fallback_keyword,
            fallback_text=fallback_hit_text,
        )
        keyword_unified = _adapt_keyword_candidates(keyword_candidates)
        try:
            vector_candidates_raw = _extract_vector_candidates_safe(outs)
            vector_unified = _adapt_vector_candidates(vector_candidates_raw)
        except Exception as e:
            print(f"[gateway_ctx] vector_retrieval_degrade err={e}")
            vector_unified = []
        summary_unified = _build_summary_candidates(summaries=summaries, text=text)
        evidence = _score_and_rank_candidates(keyword_unified + vector_unified + summary_unified, top_n=RETRIEVAL_TOP_N)

        used_evidence_ids = [ev.get("id") for ev in evidence if ev.get("id")]

        res_obj = {
            "keyword": used_keyword,
            "keyword_primary": primary_keyword,
            "keyword_used": used_keyword,
            "ctx": ctx,
            "raw": outs,
            "evidence": evidence,
            "used_evidence_ids": used_evidence_ids,
            "retrieval_profile_version": RETRIEVAL_PROFILE_VERSION,
            "ms_dify_primary": round(ms_dify_primary, 1),
            "ms_dify_used": round(ms_dify_used, 1),
        }
        res_obj.update(
            _debug_fields(
                cache_hit=False,
                cache_miss_reason=cache_miss_reason,
                keyword_primary=primary_keyword,
                keyword_used=used_keyword,
                evidence=evidence,
            )
        )

        # ✅ 写入缓存时用最新 now（更符合 TTL 语义）
        _cache[cache_key] = (time.time(), ctx, res_obj)
        # simple eviction (oldest-first) to cap memory
        if len(_cache) > MAX_CACHE_SIZE:
            oldest_key = min(_cache.items(), key=lambda kv: kv[1][0])[0]
            _cache.pop(oldest_key, None)

        ms_all = (time.perf_counter() - t0) * 1000
        print(f"[gateway_ctx] miss kw={primary_keyword!r} used={res_obj.get('keyword')!r} ms_all={ms_all:.1f} ms_dify={ms_dify:.1f} len={len(ctx)}")
        return None if is_notification else _jsonrpc_result(_id, _mcp_wrap_text(res_obj, ctx, is_error=False))

    except Exception as e:
        ms_all = (time.perf_counter() - t0) * 1000
        print(f"[gateway_ctx] ERROR kw={keyword!r} ms_all={ms_all:.1f} err={e}")
        res_obj = {
            "keyword": keyword,
            "keyword_primary": primary_keyword,
            "keyword_used": primary_keyword,
            "retrieval_profile_version": RETRIEVAL_PROFILE_VERSION,
            "error": str(e),
        }
        res_obj.update(
            _debug_fields(
                cache_hit=False,
                cache_miss_reason=cache_miss_reason,
                keyword_primary=primary_keyword,
                keyword_used=primary_keyword,
                evidence=[],
            )
        )
        return None if is_notification else _jsonrpc_result(_id, _mcp_wrap_text(res_obj, str(e), is_error=True))


@router.api_route("/gateway_ctx", methods=["GET", "POST", "OPTIONS"])
async def gateway_ctx_mcp(request: Request):
    default_pv = DEFAULT_MCP_PROTOCOL_VERSION if DEFAULT_MCP_PROTOCOL_VERSION in SUPPORTED_VERSIONS else "2025-06-18"

    if request.method in ("GET", "OPTIONS"):
        return JSONResponse(
            {"ok": True, "name": "gateway_ctx", "mcp": True},
            headers={"MCP-Protocol-Version": default_pv},
            media_type=JSON_UTF8,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"), headers={"MCP-Protocol-Version": default_pv}, media_type=JSON_UTF8)

    # batch?
    if isinstance(body, list):
        results = []
        for msg in body:
            if isinstance(msg, dict):
                r = await _handle_jsonrpc(request, msg)
                if r is not None:
                    results.append(r)
        pv = getattr(request.state, "mcp_pv", default_pv)
        return JSONResponse(results, headers={"MCP-Protocol-Version": pv}, media_type=JSON_UTF8)

    resp = await _handle_jsonrpc(request, body if isinstance(body, dict) else {})
    pv = getattr(request.state, "mcp_pv", default_pv)
    if resp is None:
        return Response(status_code=204, headers={"MCP-Protocol-Version": pv})
    return JSONResponse(resp, headers={"MCP-Protocol-Version": pv}, media_type=JSON_UTF8)
