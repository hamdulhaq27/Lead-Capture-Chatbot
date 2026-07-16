"""
main.py
========
FastAPI layer (Module H) — the thin HTTP surface over conversation_manager.py.
This is what the HTML/CSS/JS frontend talks to.

Endpoints
---------
POST /api/chat     {"session_id": "...", "message": "..."} -> {"reply": "...", "state": "..."}
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

import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from conversation_manager import handle_message, get_greeting
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