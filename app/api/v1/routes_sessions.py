import json
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as OrmSession
from app.db.session import SessionLocal
from app.db.models import SummaryS4, SummaryS60
from app.db.models import Session as ChatSession
from app.services.summarizer import get_recent_debug_events

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/sessions/{session_id}/summaries")
def get_summaries(session_id: str, db: OrmSession = Depends(get_db)):
    s4 = (db.query(SummaryS4)
            .filter(SummaryS4.session_id == session_id)
            .order_by(SummaryS4.to_turn.desc())
            .limit(5).all())
    s60 = (db.query(SummaryS60)
            .filter(SummaryS60.session_id == session_id)
            .order_by(SummaryS60.to_turn.desc())
            .limit(2).all())

    return JSONResponse(
        content={
        "s4": [{
            "range": [r.from_turn, r.to_turn],
            "summary": json.loads(r.summary_json),
            "created_at": r.created_at.isoformat()
        } for r in s4],
        "s60": [{
            "range": [r.from_turn, r.to_turn],
            "summary": json.loads(r.summary_json),
            "created_at": r.created_at.isoformat()
        } for r in s60],
        },
        media_type="application/json; charset=utf-8",
    )


@router.get("/sessions/{session_id}/summaries/debug")
def get_summaries_debug(session_id: str, limit: int = 80):
    return JSONResponse(
        content={
            "session_id": session_id,
            "events": get_recent_debug_events(session_id=session_id, limit=limit),
        },
        media_type="application/json; charset=utf-8",
    )

@router.post("/sessions/{session_id}/proactive/enable")
def enable_proactive(session_id: str, db: OrmSession = Depends(get_db)):
    s = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not s:
        return {"ok": False, "error": "session not found"}
    s.proactive_enabled = True
    db.commit()
    return {"ok": True}
