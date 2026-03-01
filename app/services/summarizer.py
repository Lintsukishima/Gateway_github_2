import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy.orm import Session

from app.db.models import Message, SummaryS4, SummaryS60


logger = logging.getLogger(__name__)
_DEBUG_EVENTS: deque = deque(maxlen=200)


def _push_debug_event(event: Dict[str, Any]) -> None:
    try:
        payload = {"ts": _now().isoformat(), **(event or {})}
        _DEBUG_EVENTS.append(payload)
    except Exception:
        pass


def get_recent_debug_events(session_id: Optional[str] = None, limit: int = 80) -> List[Dict[str, Any]]:
    items = list(_DEBUG_EVENTS)
    if session_id:
        items = [x for x in items if x.get("session_id") in (None, session_id)]
    if limit > 0:
        items = items[-limit:]
    return items

# ===== 在文件顶部 imports 下面（或任意位置）新增 =====

HELP_SEEKING_HINTS = [
    "借钱", "借我", "转账", "打钱", "资助", "赞助", "给我钱", "求助", "救济",
    "能不能给", "能否给", "帮我出", "帮我付", "你出钱", "帮我转", "给点钱"
]

def _has_help_seeking(transcript: str) -> bool:
    t = transcript or ""
    return any(h in t for h in HELP_SEEKING_HINTS)

def _sanitize_summary(transcript: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    轻量纠偏：当对话里没有明确求助/借钱语句时，禁止 summary 里出现“寻求经济帮助/对方愿意提供帮助”等推断。
    只做保守清理，不做复杂重写，避免引入新幻觉。
    """
    if not isinstance(obj, dict):
        return _default_summary_schema()

    want_help = _has_help_seeking(transcript)

    # 统一字段类型（避免 LLM 给错类型导致 downstream 崩）
    obj.setdefault("goal", "")
    obj.setdefault("state", "")
    obj.setdefault("open_loops", [])
    obj.setdefault("constraints", [])
    obj.setdefault("tone_notes", [])

    if not isinstance(obj["open_loops"], list):
        obj["open_loops"] = [str(obj["open_loops"])]
    if not isinstance(obj["constraints"], list):
        obj["constraints"] = [str(obj["constraints"])]
    if not isinstance(obj["tone_notes"], list):
        obj["tone_notes"] = [str(obj["tone_notes"])]

    # 如果没明确求助，就把“经济帮助/愿意提供帮助”这类推断移除/弱化
    if not want_help:
        bad_goal_phrases = ["寻求经济上的帮助", "寻求经济帮助", "请求经济帮助", "求助对方", "让对方出钱"]
        for p in bad_goal_phrases:
            if p in (obj.get("goal") or ""):
                obj["goal"] = (obj["goal"] or "").replace(p, "").strip("，。;； ")

        bad_state_phrases = ["愿意提供帮助", "表示愿意提供帮助", "同意提供帮助", "已提供帮助", "答应提供帮助"]
        for p in bad_state_phrases:
            if p in (obj.get("state") or ""):
                obj["state"] = (obj["state"] or "").replace(p, "").strip("，。;； ")

        # open_loops 里也清理“需要解决经济困难具体方案”这种强推断措辞
        cleaned_loops = []
        for x in obj["open_loops"]:
            s = str(x)
            s = s.replace("需要解决经济困难的具体方案", "需要明确下一步安排/计划").strip("，。;； ")
            cleaned_loops.append(s)
        obj["open_loops"] = cleaned_loops

    # goal/state 为空时给一个保底（仍然只基于显式内容）
    if not obj["goal"]:
        obj["goal"] = "概括本段对话的显式主题（若无明确目标则写‘闲聊/状态更新’）"
    if not obj["state"]:
        obj["state"] = "概括当前显式进展（若无进展则写‘无明显推进’）"

    return obj


# ========= 基础 =========

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _safe_json_loads(s: Optional[str]) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return {"_raw": s}


def _truncate_preview(value: Any, *, limit: int = 220) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _truncate_hex_bytes(value: Any, *, limit_bytes: int = 120) -> str:
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", errors="replace")[:limit_bytes].hex()


def _summary_debug_snapshot(obj: Dict[str, Any]) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    for field in ("goal", "state", "open_loops", "constraints", "tone_notes"):
        raw = obj.get(field, "")
        if isinstance(raw, list):
            joined = " | ".join(str(x) for x in raw)
        else:
            joined = str(raw)
        preview = _truncate_preview(joined)
        snapshot[f"{field}_preview"] = preview
        snapshot[f"{field}_mojibake_score"] = _mojibake_score(preview)
    return snapshot


def _count_cjk_chars(s: str) -> int:
    return sum(1 for ch in s if "一" <= ch <= "鿿")


def _strip_ctrl(s: str) -> str:
    # 去掉 U+0080..U+009F 控制字符（常见转码噪声）
    return "".join(ch for ch in s if not ("\u0080" <= ch <= "\u009f"))


def _mojibake_score(text: str) -> int:
    if not text:
        return 0
    # 兼容两边：常见“UTF-8 被按 latin-1/cp1252 解码”污染标记 + 控制符噪声
    markers = ("Ã", "Â", "æ", "ä", "å", "ç", "ð", "\u0085", "\u009d", "\u009f")
    return sum(text.count(m) for m in markers)


def _ctrl_char_count(text: str) -> int:
    return sum(1 for ch in text if "\u0080" <= ch <= "\u009f")


def _bad_latin_run_count(text: str) -> int:
    bad_chars = {"æ", "å", "ç", "Ã", "Â"}
    run_count = 0
    in_run = False
    for ch in text:
        if ch in bad_chars:
            if not in_run:
                run_count += 1
                in_run = True
        else:
            in_run = False
    return run_count


def _looks_mojibake_text(text: str) -> bool:
    return _mojibake_score(text) > 0 or _ctrl_char_count(text) > 0


def _try_recode(s: str, src: str) -> Optional[str]:
    byte_candidates = []
    for seed in (s, _strip_ctrl(s)):
        try:
            byte_candidates.append(seed.encode(src, errors="strict"))
        except Exception:
            continue

    for b in byte_candidates:
        for decode_errors in ("strict", "replace"):
            try:
                t = b.decode("utf-8", errors=decode_errors)
                return _strip_ctrl(t)
            except Exception:
                continue
    return None


def _maybe_repair_mojibake_text(text: str) -> str:
    """尝试修复“UTF-8 bytes 被按 latin-1/cp1252 解码后再传输”的文本污染。"""
    if not text:
        return text

    # 如果本来中文就足够、且没有明显 mojibake 标记，就只做控制字符清理
    bad_markers = ("Ã", "Â", "æ", "ä", "å", "ç", "ð")
    if _count_cjk_chars(text) >= 2 and not any(m in text for m in bad_markers):
        return _strip_ctrl(text)

    def metrics(s: str) -> Dict[str, int]:
        return {
            "cjk_count": _count_cjk_chars(s),
            "mojibake_markers": _mojibake_score(s),
            "replacement_count": s.count("�"),
            "ctrl_count": _ctrl_char_count(s),
            "bad_latin_runs": _bad_latin_run_count(s),
        }

    def ordering_key(m: Dict[str, int]) -> tuple[int, int, int, int, int]:
        # 优先级：CJK 更多 > mojibake 更少 > 控制/替换字符更少 > 异常拉丁串更少
        return (
            m["cjk_count"],
            -m["mojibake_markers"],
            -(m["ctrl_count"] + m["replacement_count"]),
            -m["bad_latin_runs"],
            -m["replacement_count"],
        )

    original_score = _mojibake_score(text)
    max_rounds = 2 + (1 if original_score > 2 else 0) + (1 if original_score > 5 else 0)

    best = _strip_ctrl(text)
    best_metrics = metrics(best)
    best_source = "original"

    candidates = {text}
    for round_index in range(max_rounds):
        next_round = set()
        for seed in list(candidates):
            for src in ("latin-1", "cp1252"):
                candidate = _try_recode(seed, src)
                if candidate and candidate not in candidates:
                    next_round.add(candidate)
                    _push_debug_event(
                        {
                            "stage": "mojibake.repair_candidate",
                            "round": round_index + 1,
                            "src": src,
                            "seed_preview": seed[:120],
                            "candidate_preview": candidate[:120],
                            "candidate_metrics": metrics(candidate),
                        }
                    )
        if not next_round:
            break
        candidates.update(next_round)

    for candidate in candidates:
        cleaned = _strip_ctrl(candidate)
        current_metrics = metrics(cleaned)

        # 统一按指标排序，选择最优候选。
        if ordering_key(current_metrics) > ordering_key(best_metrics):
            best = cleaned
            best_metrics = current_metrics
            best_source = "recode" if candidate != text else "original"

    # 若没有获得任何提升，保留原文本（仅清理控制字符）避免过修复
    if not _looks_mojibake_text(text):
        return _strip_ctrl(text)

    _push_debug_event(
        {
            "stage": "mojibake.repair_decision",
            "original_preview": text[:120],
            "selected_preview": best[:120],
            "selected_source": best_source,
            "original_metrics": metrics(_strip_ctrl(text)),
            "selected_metrics": best_metrics,
            "total_candidates": len(candidates),
            "max_rounds": max_rounds,
        }
    )

    return _strip_ctrl(best)


def _repair_mojibake_in_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return _maybe_repair_mojibake_text(obj)
    if isinstance(obj, list):
        return [_repair_mojibake_in_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _repair_mojibake_in_obj(v) for k, v in obj.items()}
    return obj


# ========= LLM（OpenAI-Compatible）=========


def call_llm_json(
    *,
    system: str,
    user: str,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.2,
    timeout_s: int = 45,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 OpenAI-compatible 的 /chat/completions，要求返回 JSON。

    - 兼容 OpenRouter、LiteLLM、各种中转。
    - 失败会抛异常，外层会记录 failed。
    """

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # 强制 JSON（多数兼容实现都支持；不支持也没关系，我们后面会兜底解析）
        "response_format": {"type": "json_object"},
    }

    r = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    r.raise_for_status()

    raw = r.content
    text = raw.decode("utf-8", errors="replace")

    logger.warning(
        "LLM raw response diagnostics status=%s content_type=%s requests_encoding=%s apparent_encoding=%s raw_hex=%s text_preview=%r utf8_preview=%r",
        r.status_code,
        r.headers.get("content-type"),
        getattr(r, "encoding", None),
        getattr(r, "apparent_encoding", None),
        raw[:120].hex(),
        (r.text or "")[:120],
        text[:120],
    )
    _push_debug_event(
        {
            "stage": "call_llm_json.raw_response",
            "content_type": r.headers.get("content-type"),
            "requests_encoding": getattr(r, "encoding", None),
            "apparent_encoding": getattr(r, "apparent_encoding", None),
            "raw_hex_120": raw[:120].hex(),
            "requests_text_120": (r.text or "")[:120],
            "forced_utf8_120": text[:120],
        }
    )

    if any(m in text[:200] for m in ("Ã", "Â", "æ", "ä", "å", "ç", "ð", "\u0085")):
        logger.warning(
            "LLM raw response looks mojibake content_type=%s raw_hex=%s utf8_preview=%r",
            r.headers.get("content-type"),
            raw[:120].hex(),
            text[:120],
        )

    if r.text and text and r.text[:120] != text[:120]:
        logger.warning(
            "LLM response decode mismatch requests_text_preview=%r forced_utf8_preview=%r",
            r.text[:120],
            text[:120],
        )
        _push_debug_event(
            {
                "stage": "call_llm_json.decode_mismatch",
                "requests_text_120": r.text[:120],
                "forced_utf8_120": text[:120],
            }
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(
            "LLM response JSON decode failed status=%s content_type=%s body_preview=%r",
            r.status_code,
            r.headers.get("content-type"),
            text[:200],
        )
        raise RuntimeError("LLM endpoint returned invalid JSON payload") from e

    choices = data.get("choices") if isinstance(data, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    message_obj = first_choice.get("message") if isinstance(first_choice, dict) else {}
    content_raw = message_obj.get("content") if isinstance(message_obj, dict) else ""
    content = content_raw if isinstance(content_raw, str) else str(content_raw or "")
    content = content.strip()

    if content.startswith("```"):
        content = content.strip("`")
        content = content.replace("json\n", "", 1).strip()

    repaired_content = _maybe_repair_mojibake_text(content)
    _push_debug_event(
        {
            "stage": "call_llm_json.message_content",
            "session_id": session_id,
            "content_raw_preview_240": _truncate_preview(content, limit=240),
            "content_raw_hex_120b": _truncate_hex_bytes(content, limit_bytes=120),
            "content_repaired_preview_240": _truncate_preview(repaired_content, limit=240),
            "content_repaired_hex_120b": _truncate_hex_bytes(repaired_content, limit_bytes=120),
            "raw_vs_repaired_changed": repaired_content != content,
        }
    )

    if not content:
        raise RuntimeError(f"LLM empty content: {data}")
    if repaired_content != content:
        logger.warning(
            "LLM content mojibake repaired before JSON parse preview_before=%r preview_after=%r",
            content[:120],
            repaired_content[:120],
        )
    elif _mojibake_score(repaired_content):
        logger.warning(
            "LLM content still contains possible mojibake markers after first-pass repair preview=%r",
            repaired_content[:120],
        )

    try:
        obj = json.loads(repaired_content)
    except Exception as e:
        raise RuntimeError(f"LLM returned non-JSON: {repaired_content[:200]}...") from e

    repaired_obj = _repair_mojibake_in_obj(obj)
    if repaired_obj != obj:
        logger.warning("LLM JSON fields contained mojibake and were repaired")
    return repaired_obj


def _default_summary_schema() -> Dict[str, Any]:
    return {
        "goal": "当前在推进什么",
        "state": "进度到哪/现在什么状态",
        "open_loops": ["未完成事项/待决定点"],
        "constraints": ["硬约束：工具/时间/边界/要求"],
        "tone_notes": ["语气与互动基调提示（很短）"],
    }


def _render_transcript(msgs: List[Message]) -> str:
    lines = []
    for m in msgs:
        role = m.role
        # Telegram 会带很多表情，保留即可
        lines.append(f"{role}: {m.content}")
    return "\n".join(lines)


def _summarize_with_optional_llm(transcript: str, *, level: str) -> Dict[str, Any]:
    """有配置就用 LLM，没配置就返回占位 schema（保证系统仍然跑通）。"""

    base_url = os.getenv("SUMMARIZER_BASE_URL", "").strip()
    api_key = os.getenv("SUMMARIZER_API_KEY", "").strip()
    model = os.getenv("SUMMARIZER_MODEL", "").strip() or "gpt-4o-mini"

    if not base_url or not api_key:
        return _default_summary_schema()

    system = (
        "你是会话总结器。你必须严格基于对话中的『显式文本』提取信息，禁止推断、禁止脑补、禁止编造。"
        "只输出 JSON 对象，不要输出任何多余文字。"
        "字段必须包含：goal, state, open_loops(list), constraints(list), tone_notes(list)。\n\n"
        "硬规则：\n"
        "1) goal：只能写对话里出现过的明确目标/意图；如果用户只是表达情绪或陈述事实，不要写成‘寻求帮助/求助/想让对方做X’，除非用户明确提出请求。\n"
        "2) state：只能写明确发生过的进展；不要写‘对方愿意提供帮助/已确认…’，除非对话里有清晰承诺或确认。\n"
        "3) open_loops/constraints：只列出对话中明确未解决的问题/明确限制；不要新增‘具体方案’这类你自己设定的任务。\n"
        "4) tone_notes：只写非常短的语气标签，如‘关心/轻松/焦急’。\n"
        "5) 如果信息不足，宁可写‘无明显推进/未提及’，不要编造。"
    )

    user = (
        f"请对下面对话做{level}总结，输出 JSON。\n"
        "注意：不要使用‘寻求经济帮助/请求资助/对方愿意帮忙’等推断性措辞，除非原文明确提出。"
        "\n\n--- 对话 ---\n"
        f"{transcript}\n"
        "--- 结束 ---"
    )

    obj = call_llm_json(
        system=system,
        user=user,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.2,
    )

    sanitized_obj = _sanitize_summary(transcript, obj)
    return _repair_mojibake_in_obj(sanitized_obj)


def _summarize_s4_with_debug_events(
    transcript: str,
    *,
    session_id: str,
    to_turn: int,
) -> Dict[str, Any]:
    """S4 专用总结流程，拆分 raw/sanitize/repair 三阶段并记录事件。"""

    base_url = os.getenv("SUMMARIZER_BASE_URL", "").strip()
    api_key = os.getenv("SUMMARIZER_API_KEY", "").strip()
    model = os.getenv("SUMMARIZER_MODEL", "").strip() or "gpt-4o-mini"

    if not base_url or not api_key:
        fallback = _default_summary_schema()
        _push_debug_event(
            {
                "stage": "summary_from_llm_raw",
                "session_id": session_id,
                "to_turn": to_turn,
                **_summary_debug_snapshot(fallback),
            }
        )
        _push_debug_event(
            {
                "stage": "summary_after_sanitize",
                "session_id": session_id,
                "to_turn": to_turn,
                **_summary_debug_snapshot(fallback),
            }
        )
        _push_debug_event(
            {
                "stage": "summary_after_repair",
                "session_id": session_id,
                "to_turn": to_turn,
                **_summary_debug_snapshot(fallback),
            }
        )
        return fallback

    system = (
        "你是会话总结器。你必须严格基于对话中的『显式文本』提取信息，禁止推断、禁止脑补、禁止编造。"
        "只输出 JSON 对象，不要输出任何多余文字。"
        "字段必须包含：goal, state, open_loops(list), constraints(list), tone_notes(list)。\n\n"
        "硬规则：\n"
        "1) goal：只能写对话里出现过的明确目标/意图；如果用户只是表达情绪或陈述事实，不要写成‘寻求帮助/求助/想让对方做X’，除非用户明确提出请求。\n"
        "2) state：只能写明确发生过的进展；不要写‘对方愿意提供帮助/已确认…’，除非对话里有清晰承诺或确认。\n"
        "3) open_loops/constraints：只列出对话中明确未解决的问题/明确限制；不要新增‘具体方案’这类你自己设定的任务。\n"
        "4) tone_notes：只写非常短的语气标签，如‘关心/轻松/焦急’。\n"
        "5) 如果信息不足，宁可写‘无明显推进/未提及’，不要编造。"
    )

    user = (
        "请对下面对话做短期总结，输出 JSON。\n"
        "注意：不要使用‘寻求经济帮助/请求资助/对方愿意帮忙’等推断性措辞，除非原文明确提出。"
        "\n\n--- 对话 ---\n"
        f"{transcript}\n"
        "--- 结束 ---"
    )

    raw_obj = call_llm_json(
        system=system,
        user=user,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.2,
        session_id=session_id,
    )
    _push_debug_event(
        {
            "stage": "summary_from_llm_raw",
            "session_id": session_id,
            "to_turn": to_turn,
            **_summary_debug_snapshot(raw_obj),
        }
    )

    sanitized_obj = _sanitize_summary(transcript, raw_obj)
    _push_debug_event(
        {
            "stage": "summary_after_sanitize",
            "session_id": session_id,
            "to_turn": to_turn,
            **_summary_debug_snapshot(sanitized_obj),
        }
    )

    repaired_obj = _repair_mojibake_in_obj(sanitized_obj)
    _push_debug_event(
        {
            "stage": "summary_after_repair",
            "session_id": session_id,
            "to_turn": to_turn,
            **_summary_debug_snapshot(repaired_obj),
        }
    )

    return repaired_obj


def _build_scope_query(
    db: Session,
    *,
    session_id: str,
    scope_type: str,
    thread_id: Optional[str],
    memory_id: Optional[str],
    agent_id: Optional[str],
):
    q = db.query(Message).filter(Message.session_id == session_id)

    if scope_type == "thread":
        if thread_id is not None:
            q = q.filter(Message.thread_id == thread_id)
    elif scope_type == "memory":
        if memory_id is not None:
            q = q.filter(Message.memory_id == memory_id)
        if agent_id is not None:
            q = q.filter(Message.agent_id == agent_id)

    return q


def _resolve_scope_user_turns(
    db: Session,
    *,
    session_id: str,
    to_user_turn: int,
    window_user_turn: int,
    scope_type: str,
    thread_id: Optional[str],
    memory_id: Optional[str],
    agent_id: Optional[str],
) -> List[int]:
    user_turn_rows = (
        _build_scope_query(
            db,
            session_id=session_id,
            scope_type=scope_type,
            thread_id=thread_id,
            memory_id=memory_id,
            agent_id=agent_id,
        )
        .filter(Message.role == "user")
        .order_by(Message.turn_id.desc())
        .limit(to_user_turn)
        .all()
    )

    ordered_user_turns: List[int] = []
    seen = set()
    for row in reversed(user_turn_rows):
        ut = row.user_turn
        if ut is None or ut in seen:
            continue
        seen.add(ut)
        ordered_user_turns.append(ut)

    if not ordered_user_turns:
        return []

    return ordered_user_turns[-window_user_turn:]


# ========= 对外：S4 / S60 =========


def run_s4(
    db: Session,
    *,
    session_id: str,
    to_user_turn: int,
    window_user_turn: int = 4,
    model_name: str = "summarizer_mvp",
    thread_id: Optional[str] = None,
    memory_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    s4_scope: str = "thread",
    summary_version: int = 2,
) -> Dict[str, Any]:
    """短期总结：按 user_turn 窗口。"""

    effective_scope = (s4_scope or "thread").lower()
    if effective_scope == "auto":
        effective_scope = "thread"
    if effective_scope not in {"thread", "memory"}:
        effective_scope = "thread"

    scoped_user_turns = _resolve_scope_user_turns(
        db,
        session_id=session_id,
        to_user_turn=to_user_turn,
        window_user_turn=window_user_turn,
        scope_type=effective_scope,
        thread_id=thread_id,
        memory_id=memory_id,
        agent_id=agent_id,
    )

    msgs = (
        _build_scope_query(
            db,
            session_id=session_id,
            scope_type=effective_scope,
            thread_id=thread_id,
            memory_id=memory_id,
            agent_id=agent_id,
        )
        .filter(Message.user_turn.in_(scoped_user_turns))
        .order_by(Message.turn_id.asc())
        .all()
        if scoped_user_turns
        else []
    )

    if not msgs:
        return {"skipped": True, "reason": "no messages"}

    from_turn = min(m.turn_id for m in msgs)
    to_turn = max(m.turn_id for m in msgs)

    # 幂等：同一个 to_turn 只写一次
    existed = (
        db.query(SummaryS4)
        .filter(SummaryS4.session_id == session_id)
        .filter(SummaryS4.scope_type == effective_scope)
        .filter(SummaryS4.thread_id == thread_id)
        .filter(SummaryS4.memory_id == memory_id)
        .filter(SummaryS4.agent_id == agent_id)
        .filter(SummaryS4.to_turn == to_turn)
        .first()
    )
    if existed:
        return {"skipped": True, "reason": "exists", "to_turn": to_turn}

    transcript = _render_transcript(msgs)
    summary_obj = _summarize_s4_with_debug_events(
        transcript,
        session_id=session_id,
        to_turn=to_turn,
    )
    logger.debug(
        "S4 summary preview before persist session_id=%s thread_id=%s to_turn=%s summary=%r",
        session_id,
        thread_id,
        to_turn,
        summary_obj,
    )
    _push_debug_event(
        {
            "stage": "run_s4.before_persist",
            "session_id": session_id,
            "to_turn": to_turn,
            "summary_preview": str(summary_obj)[:240],
        }
    )

    first_msg = msgs[0]
    trace_thread_id = thread_id or getattr(first_msg, "thread_id", None)
    trace_memory_id = memory_id or getattr(first_msg, "memory_id", None)
    trace_agent_id = agent_id or getattr(first_msg, "agent_id", None)
    dedupe_key = (
        f"s4:{effective_scope}:{trace_thread_id}:{trace_memory_id}:{trace_agent_id}:"
        f"{to_turn}:v{summary_version}"
    )

    row = SummaryS4(
        session_id=session_id,
        scope_type=effective_scope,
        thread_id=trace_thread_id,
        memory_id=trace_memory_id,
        agent_id=trace_agent_id,
        summary_version=summary_version,
        dedupe_key=dedupe_key,
        from_turn=from_turn,
        to_turn=to_turn,
        summary_json=_safe_json_dumps(summary_obj),
        model=model_name,
        created_at=_now(),
        meta_json=_safe_json_dumps(
            {
                "scope_type": effective_scope,
                "thread_id": trace_thread_id,
                "memory_id": trace_memory_id,
                "agent_id": trace_agent_id,
                "to_user_turn": to_user_turn,
                "window_user_turn": window_user_turn,
                "summary_version": summary_version,
                "dedupe_key": dedupe_key,
            }
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    persisted_summary = _safe_json_loads(row.summary_json)
    if persisted_summary != summary_obj:
        logger.warning(
            "S4 summary changed after DB persist session_id=%s to_turn=%s before=%r after=%r",
            session_id,
            to_turn,
            summary_obj,
            persisted_summary,
        )
        _push_debug_event(
            {
                "stage": "run_s4.after_persist_changed",
                "session_id": session_id,
                "to_turn": to_turn,
                "before_preview": str(summary_obj)[:240],
                "after_preview": str(persisted_summary)[:240],
            }
        )

    return {
        "range": [from_turn, to_turn],
        "summary": persisted_summary or summary_obj,
        "created_at": row.created_at.isoformat(),
        "model": model_name,
    }


def run_s60(
    db: Session,
    *,
    session_id: str,
    to_user_turn: int,
    window_user_turn: int = 30,
    model_name: str = "summarizer_mvp",
    thread_id: Optional[str] = None,
    memory_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    summary_version: int = 1,
) -> Dict[str, Any]:
    """长期总结：你现在要的是 30 轮 user 消息。"""

    scope_type = "memory"
    scoped_user_turns = _resolve_scope_user_turns(
        db,
        session_id=session_id,
        to_user_turn=to_user_turn,
        window_user_turn=window_user_turn,
        scope_type=scope_type,
        thread_id=thread_id,
        memory_id=memory_id,
        agent_id=agent_id,
    )

    msgs = (
        _build_scope_query(
            db,
            session_id=session_id,
            scope_type=scope_type,
            thread_id=thread_id,
            memory_id=memory_id,
            agent_id=agent_id,
        )
        .filter(Message.user_turn.in_(scoped_user_turns))
        .order_by(Message.turn_id.asc())
        .all()
        if scoped_user_turns
        else []
    )

    if not msgs:
        return {"skipped": True, "reason": "no messages"}

    from_turn = min(m.turn_id for m in msgs)
    to_turn = max(m.turn_id for m in msgs)

    existed = (
        db.query(SummaryS60)
        .filter(SummaryS60.session_id == session_id)
        .filter(SummaryS60.scope_type == scope_type)
        .filter(SummaryS60.thread_id == thread_id)
        .filter(SummaryS60.memory_id == memory_id)
        .filter(SummaryS60.agent_id == agent_id)
        .filter(SummaryS60.to_turn == to_turn)
        .first()
    )
    if existed:
        return {"skipped": True, "reason": "exists", "to_turn": to_turn}

    transcript = _render_transcript(msgs)
    summary_obj = _summarize_with_optional_llm(transcript, level="长期")

    first_msg = msgs[0]
    trace_thread_id = thread_id or getattr(first_msg, "thread_id", None)
    trace_memory_id = memory_id or getattr(first_msg, "memory_id", None)
    trace_agent_id = agent_id or getattr(first_msg, "agent_id", None)
    dedupe_key = (
        f"s60:{scope_type}:{trace_thread_id}:{trace_memory_id}:{trace_agent_id}:"
        f"{to_turn}:v{summary_version}"
    )

    row = SummaryS60(
        session_id=session_id,
        scope_type=scope_type,
        thread_id=trace_thread_id,
        memory_id=trace_memory_id,
        agent_id=trace_agent_id,
        summary_version=summary_version,
        dedupe_key=dedupe_key,
        from_turn=from_turn,
        to_turn=to_turn,
        summary_json=_safe_json_dumps(summary_obj),
        model=model_name,
        created_at=_now(),
        meta_json=_safe_json_dumps(
            {
                "scope_type": scope_type,
                "thread_id": trace_thread_id,
                "memory_id": trace_memory_id,
                "agent_id": trace_agent_id,
                "to_user_turn": to_user_turn,
                "window_user_turn": window_user_turn,
                "summary_version": summary_version,
                "dedupe_key": dedupe_key,
            }
        ),
    )
    db.add(row)
    db.commit()

    return {
        "range": [from_turn, to_turn],
        "summary": summary_obj,
        "created_at": row.created_at.isoformat(),
        "model": model_name,
    }
