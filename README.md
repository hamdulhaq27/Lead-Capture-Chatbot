# BrightReach Virtual Assistant

An AI-powered chatbot for BrightReach, a digital marketing agency — answers questions about services and pricing using a RAG pipeline over the agency's own docs, and handles the full booking lifecycle (book, reschedule, cancel a free discovery call) end-to-end through Google Calendar and Google Sheets, with automatic lead capture along the way.

## Features

- **Grounded Q&A** — retrieval-augmented answers about services, pricing, and policies, sourced only from the agency's own knowledge base (no hallucinated claims).
- **Conversational booking flow** — book, reschedule, or cancel a discovery call entirely in natural language, with slot/business-hours validation and AM/PM disambiguation.
- **Google Calendar + Sheets integration** — every booking is a real calendar event, backed by a Sheets row as the single source of truth (with rollback if either half fails).
- **Automatic lead capture** — leads from bookings and general conversation are logged to a dedicated Chatbot_Leads sheet, kept in sync as booking details change.
- **Stateful sessions** — a lightweight state machine per session remembers context (e.g. an already-identified booking) so users aren't re-asked for the same info mid-conversation.
- **FastAPI backend** — a thin REST/streaming API in front of the conversation engine, serving a static HTML/CSS/JS frontend.

## Architecture

```
frontend (HTML/CSS/JS)
        │
        ▼
   main.py  (FastAPI — /api/chat, /api/greeting, /api/health)
        │
        ▼
conversation_manager.py  (state machine, intent classification, orchestration)
        │
   ┌────┼─────────────┬──────────────────┐
   ▼    ▼             ▼                  ▼
rag_engine.py   booking_manager.py   chatbot_lead_manager.py
(RAG Q&A)       (bookings, via       (lead capture, via
                 google_services.py) google_services.py)
```

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app — HTTP/SSE surface for the frontend |
| `conversation_manager.py` | Session state machine, intent classification, field extraction, flow orchestration |
| `rag_engine.py` | Ingests the knowledge base into ChromaDB, retrieves + generates grounded answers via Groq |
| `booking_manager.py` | Booking business rules — slot validation, create/reschedule/cancel, keeps Calendar + Sheets in sync |
| `chatbot_lead_manager.py` | Persists chatbot-sourced leads to a dedicated Sheet, kept in sync with booking updates |
| `google_services.py` | Shared Google API auth/service helpers (Sheets, Calendar) |

## Tech stack

- **Backend:** FastAPI, Python
- **LLM:** Groq API (Llama 3.3 70B)
- **RAG:** ChromaDB + sentence-transformers (`all-MiniLM-L6-v2`), PDF ingestion via pdfplumber
- **Storage:** Google Sheets (leads, bookings), Google Calendar (scheduling)

## Setup

1. **Install dependencies**
   ```bash
   pip install fastapi uvicorn groq chromadb sentence-transformers pdfplumber \
               python-dotenv python-dateutil google-auth-oauthlib google-api-python-client
   ```

2. **Configure environment variables** (e.g. in a `.env` file)
   ```bash
   GROQ_API_KEY=your_groq_api_key
   GROQ_MODEL_NAME=llama-3.3-70b-versatile   # optional, this is the default
   BOOKINGS_SPREADSHEET_ID=your_bookings_sheet_id
   CHATBOT_LEADS_SPREADSHEET_ID=your_chatbot_leads_sheet_id
   ```

3. **Google API credentials** — place your OAuth `Client_Secret.json` in the project root. The first run of any Google-backed module opens a browser consent flow and caches the resulting token under `token files/`.

4. **Build the knowledge base** — drop your `.pdf` docs into `knowledge_base/`, then run:
   ```bash
   python rag_engine.py ingest
   ```

5. **Run the server**
   ```bash
   uvicorn main:app --reload --port 8000
   ```
   Open `http://localhost:8000` to chat via the bundled frontend.

## CLI usage

Each module is independently scriptable for testing/admin tasks:

```bash
python rag_engine.py ask "What services do you offer?"
python booking_manager.py slots 2026-07-20
python booking_manager.py book "Jane Doe" jane@example.com 03001234567 2026-07-20 14:30
python chatbot_lead_manager.py fetch-all
```

## Project status

Actively developed — booking, rescheduling, cancellation, and lead capture are functional; the RAG knowledge base and frontend are expected to grow alongside the agency's real content.
