from pydantic import BaseModel
class ChatRequest(BaseModel):
    session_id: str
    user_text: str
    assistant_text: str

class ChatResponse(BaseModel):
    session_id: str
    turn_id: int
