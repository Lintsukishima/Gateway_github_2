from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as OrmSession
from app.db.session import SessionLocal
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import append_user_and_assistant

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, db: OrmSession = Depends(get_db)):
    result = append_user_and_assistant(
        db=db,
        session_id=req.session_id,
        user_text=req.user_text,
        assistant_text=req.assistant_text,
    )
    return ChatResponse(
        session_id=result.session_id,
        turn_id=result.assistant_message_turn_id,
    )
