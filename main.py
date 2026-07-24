"""
main.py
========
FastAPI layer (Module H) — the thin HTTP surface over conversation_manager.py.
This is what the HTML/CSS/JS frontend talks to.

Endpoints
---------
POST /api/chat            {"session_id": "...", "message": "..."} -> {"reply": "...", "state": "..."}
POST /api/chat/stream      {"session_id": "...", "message": "..."} -> text/event-stream of
                            {"type": "status", "status": "..."} | {"type": "token", "text": "..."} |
                            {"type": "done", "answer": "...", "state": "..."} events, one per SSE
                            "data:" line — this is what powers the live "Checking available time
                            slots...", "Confirming your booking..." progress line in the widget,
                            plus token-by-token streaming of the reply itself.
GET  /api/greeting?session_id=...                          -> {"greeting": "..." | null}
GET  /api/health                                            -> {"status": "ok"}

Run locally
-----------
pip install fastapi uvicorn
uvicorn main:app --reload --port 8000

Then open frontend/index.html in a browser (or serve it via any static
file server) — it's configured to call http://localhost:8000.

CORS is enabled for local development so the static HTML file (opened
directly or via a simple file server on a different port) can call this
API without being blocked by the browser.
"""

import json
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from conversation_manager import handle_message, handle_message_stream, get_greeting
from rag_engine import warmup as rag_warmup

app = FastAPI(title="Agency Chatbot API")


@app.on_event("startup")
def on_startup():
    
    start = time.monotonic()
    rag_warmup()
    print(f"[main] Startup warmup finished in {time.monotonic() - start:.2f}s. Ready for requests.")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    state: str


class GreetingResponse(BaseModel):
    greeting: str | None


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):

    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    try:
        result = handle_message(request.session_id, request.message)
    except Exception as e:
        import traceback
        print(f"[main] /api/chat failed for session {request.session_id}: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    return ChatResponse(reply=result["reply"], state=result["state"])


def _sse_event(payload: dict) -> str:
    """Format one event as an SSE 'data:' line. json.dumps (not str()) so
    quotes/newlines/unicode in the reply text survive the trip intact —
    the frontend does a matching JSON.parse per event.
    """
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest):
    """Same conversation turn as /api/chat, but streamed over SSE so the
    widget can show live progress ("Checking available time slots...",
    "Confirming your booking...", etc.) instead of a blank wait, and reveal
    the reply as it's generated rather than all at once.

    Delegates entirely to conversation_manager.handle_message_stream, which
    already yields exactly this shape of event and always ends in exactly
    one "done" event, even on internal failure — see its docstring. The
    try/except here only guards against something failing before that
    generator even starts (e.g. session lookup), so the stream can never
    just hang or die silently instead of reaching the frontend's "done"
    handler.
    """
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    def event_stream():
        try:
            for event in handle_message_stream(request.session_id, request.message):
                yield _sse_event(event)
        except Exception as e:
            import traceback
            print(f"[main] /api/chat/stream failed for session {request.session_id}: {type(e).__name__}: {e}")
            traceback.print_exc()
            fallback = "Sorry, something went wrong on my end. Please try again in a moment."
            yield _sse_event({"type": "token", "text": fallback})
            yield _sse_event({"type": "done", "answer": fallback, "state": "GENERAL"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # Defeat proxy/gateway buffering (nginx et al.) so events reach
            # the browser as they're yielded rather than batched.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/greeting", response_model=GreetingResponse)
def greeting(session_id: str):
    return GreetingResponse(greeting=get_greeting(session_id))


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/new-session")
def new_session():
    return {"session_id": str(uuid.uuid4())}


# Serve the frontend's static files (index.html, style.css, app.js) at
# the root, so the whole thing can be run from a single `uvicorn` command
# without a separate static file server. Visiting http://localhost:8000/
# will load frontend/index.html directly.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")