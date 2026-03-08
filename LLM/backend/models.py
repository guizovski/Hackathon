from pydantic import BaseModel


class ChatRequest(BaseModel):
    question: str
    session_cookie: str
    student_id: str | None = None  # opcional — auto-detectado da sessão se omitido
    conversation_history: list[dict] = []


class ChatResponse(BaseModel):
    answer: str
    routes_consulted: list[str]
    raw_data_preview: str | None = None
    ics_data: str | None = None  # base64 do ficheiro .ics, quando gerado


class ExportICSRequest(BaseModel):
    session_cookie: str
    student_id: str | None = None


class FetchRequest(BaseModel):
    url: str
    session_cookie: str
