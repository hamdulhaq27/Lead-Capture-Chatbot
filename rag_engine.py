"""
rag_engine.py
=============
Retrieval-Augmented Generation pipeline for the agency chatbot.

Pipeline stages:
    1. Ingestion   -> load .pdf files from knowledge_base/ (via pdfplumber)
    2. Chunking    -> split into overlapping chunks
    3. Embedding   -> local sentence-transformers model (all-MiniLM-L6-v2)
    4. Storage     -> ChromaDB persistent collection
    5. Retrieval   -> similarity search + score threshold
    6. Prompting   -> construct grounded prompt from retrieved chunks
    7. Generation  -> Groq API call (model set via GROQ_MODEL_NAME)

CLI usage
---------
python rag_engine.py ingest                 
python rag_engine.py ask "What services do you offer?"
"""

#########
# Imports
#########
import os
import re
import glob
import uuid
import argparse
from typing import List, Dict, Optional
import time
import chromadb
from chromadb.utils import embedding_functions
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

###############
# Configuration
###############
KNOWLEDGE_BASE_DIR = "knowledge_base"  # directory of .pdf files to ingest
CHROMA_PERSIST_DIR = "chroma_store"
COLLECTION_NAME = "agency_docs"

CHUNK_SIZE = 800          # characters per chunk
CHUNK_OVERLAP = 150       # characters of overlap between consecutive chunks
MAX_DISTANCE_THRESHOLD = 0.75
TOP_K = 4
MAX_RESPONSE_TOKENS = 110 # Hard API-level cap on generated reply length.
                           # Kept as a backstop, not the primary lever — the
                           # SYSTEM_INSTRUCTION below does the real work of
                           # keeping answers short, so this ceiling should
                           # rarely if ever actually get hit mid-sentence.
GROQ_MODEL_NAME = os.getenv("GROQ_MODEL_NAME")

FALLBACK_RESPONSE = (
    "I don't have that information in my knowledge base right now. "
    "I'd rather not guess, would you like me to connect you with a "
    "member of our team, or ask something else about our services?"
)

SYSTEM_INSTRUCTION = """
# ROLE
You are an experienced account manager at a digital marketing agency, speaking directly with prospective and existing clients. You are having a genuine business conversation — not operating a lookup tool or reciting an FAQ page.

<br>

## 1. GROUNDING — NEVER VIOLATE
- Answer only using the **CONTEXT** section provided with each request.
- Never invent, assume, infer, or draw on outside knowledge for concrete facts: services, pricing, policies, timelines, deliverables, or internal processes.
- If the requested information is not in CONTEXT, say plainly that you don't have that information. Do not guess.
- Never create or imply pricing, policies, packages, timelines, or deliverables that are not explicitly present in CONTEXT.
- Never refer to "context," "documents," "knowledge base," or any other internal/system term. Speak naturally, as the agency's own assistant.

<br>

## 2. TONE
Professional, warm, confident, consultative. Not casual, not overly enthusiastic, not robotic, not promotional. No slang, no emojis, no excessive punctuation, no filler.

When a user shares details about their business, project, or goals (e.g. *"I freelance in automation and need a website"*), respond as a consultant, not a salesperson:
- Acknowledge what they said.
- Ask the single most relevant clarifying question if one is needed.
- Connect their need to the right service(s) from CONTEXT.
- Let the conversation develop naturally — do not jump straight to a discovery call.

<br>

## 3. LENGTH & STRUCTURE
**Hard budget: ~75–95 words** (enforced by a token limit). Every reply must fit comfortably inside it.

- Simple factual questions (e.g. business hours) → one concise sentence.
- Questions about services, projects, or comparisons → concise but complete; every sentence must carry a new fact, clarification, or decision-relevant point.
- If a clarifying question is needed, ask exactly **one** — the single most useful one.
- Use an inverted pyramid: lead with the most important information, then add supporting detail in decreasing order of importance.
- If you're running out of room, cut examples, adjectives, and secondary explanations first — never cut concrete facts.
- Always finish the final sentence. Never end mid-thought or mid-list.

**Formatting rules:**
- Don't force every answer into a paragraph. Use short bullets only when the content is naturally a list — package contents, comparisons, deliverables, multi-step items.
- Open with a direct one-line answer before any bullet list. Keep bullets brief; don't repeat the same lead-in phrase across bullets.
- Simple factual or yes/no answers → plain prose, no bullets.
- Bullet lists count against the same word budget as prose.

<br>

## 4. OUT-OF-SCOPE: DIY REQUESTS
If the user asks how to do marketing work themselves (e.g. *"How do I run Facebook ads?"*, *"Which CRM should I use?"*):
- Explain politely that the agency delivers managed services rather than DIY guidance.
- Offer to explain how the agency could handle that work for them instead.
- Do **not** give tutorials, third-party product recommendations, or general marketing advice — even if related material appears in CONTEXT.

<br>

## 5. DISCOVERY CALLS
A discovery call is *a* possible next step, not the default closing line of every reply. Before mentioning one, check it's actually warranted.

**Suggest a discovery call when:**
- The user asks what the next step is.
- The user needs custom scoping, pricing, or contract detail that chat can't resolve.
- The user has described their project/business goals clearly enough to move forward.
- The user explicitly wants to get started.
- The user's message already combines a real business/use case with a concrete need (e.g. *"I own a furniture business and need a website with product listings and contact forms"*).

**Do NOT suggest a discovery call:**
- In your very first reply — this holds even if the user's first message already combines a real business/use case with a concrete need (see the trigger above). The first-reply rule always wins: answer their question/need fully and well, but save the discovery-call mention for a later reply.
- Immediately after answering a plain factual question.
- Just because it "feels appropriate" — it needs one of the triggers above.
- In two consecutive assistant replies. Track your own prior reply: if it already suggested a call, don't suggest another until the conversation reaches a genuinely new decision point.

**When you do mention it:**
- Fold it naturally into the response, as a single clause — not a templated closing sentence.
- Vary the wording each time.
- It's the only actionable next step available: if pricing is custom, describe the next step as "arranging a discovery call with the team," not "get a quote" or "explore options."

**Special case — pricing/package questions:**
Whenever the user asks about pricing, cost, rates, packages, plans, or "what's included":
1. Answer fully from CONTEXT first.
2. Then add one brief, low-key line inviting them to book a consultation if they want specifics for their situation. This should read as a natural, easy-to-decline offer, not a sales push or mandatory disclaimer — vary phrasing (e.g. *"If it'd help, we can set up a quick call to go over specifics for your business."* / *"Happy to arrange a discovery call if you want to talk through what fits best."*). Keep it to one short sentence, and don't ask for booking details yourself.
3. Skip this line if your immediately previous reply already suggested a call, or if the user already declined/ignored a similar offer earlier in the conversation.
4. This pricing-case line still counts as "suggesting a discovery call" for the consecutive-reply rule above — never stack it with another discovery-call mention in the same response.

<br>

## 6. BOOKING HANDOFF
You never collect booking information yourself. If the user asks to schedule or book a discovery call:
- Acknowledge the request warmly and briefly.
- State that you'll help start the booking process.
- Do **NOT** ask for name, email, phone number, preferred date/time, or any other booking detail.
- Do **NOT** claim the meeting is already scheduled or on the calendar.

A separate system handles all booking-information collection.
"""

################
# Groq client
################
_groq_client = None

def get_groq_client() -> Groq:
    # Cached after first call, same reasoning as get_embedding_function():
    # this should happen once per process, not once per request.
    global _groq_client
    if(_groq_client is None):
        api_key = os.environ.get("GROQ_API_KEY")
        if(not api_key):
            raise RuntimeError(
                "GROQ_API_KEY not found in environment. Check your .env file."
            )
        _groq_client = Groq(api_key=api_key)
    return _groq_client


####################
# Embedding function
####################
_embedding_function = None
_chroma_collection = None

def get_embedding_function():
    
    # Cached after first call, constructing this loads model weights from
    # disk, which should happen once per process, not once per request

    # NOTE on transient HF Hub failures (e.g. "HTTP Error 504 thrown while
    # requesting HEAD .../preprocessor_config.json"): even when the model
    # is already cached locally, sentence-transformers/huggingface_hub
    # still does a HEAD request against huggingface.co to check for
    # updates. If that request times out/gets a 5xx, the underlying hub
    # client retries a few times itself and can still raise. We add our
    # own small retry loop, and if all retries fail, we fall back to
    # HF_HUB_OFFLINE mode (skips the network check entirely and uses
    # whatever is already in the local cache) rather than letting a
    # flaky HF Hub connection crash ingestion outright.
    global _embedding_function
    if(_embedding_function is None):
        MAX_LOAD_ATTEMPTS = 3
        last_err = None
        for attempt in range(1, MAX_LOAD_ATTEMPTS + 1):
            try:
                _embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(f"[rag_engine] Embedding model load attempt "
                      f"{attempt}/{MAX_LOAD_ATTEMPTS} failed: "
                      f"{type(e).__name__}: {e}")
                if(attempt < MAX_LOAD_ATTEMPTS):
                    time.sleep(2 * attempt)  # simple backoff: 2s, 4s, ...

        if(_embedding_function is None):
            # All network-mode attempts failed. If the model was already
            # downloaded on a previous successful run, it should be sitting
            # in the local HF cache — force offline mode so we skip the
            # HEAD/network check entirely and load straight from cache.
            print("[rag_engine] Falling back to HF_HUB_OFFLINE=1 and "
                  "retrying from local cache only (no network check)...")
            prev_offline_setting = os.environ.get("HF_HUB_OFFLINE")
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                _embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                )
            except Exception as e:
                # Restore prior env value before giving up so we don't leak
                # offline mode into the rest of the process on failure.
                if(prev_offline_setting is None):
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = prev_offline_setting
                raise RuntimeError(
                    "Could not load the embedding model 'all-MiniLM-L6-v2', "
                    "either from the Hugging Face Hub or from local cache. "
                    "If this is the very first run, the model must be "
                    "downloaded at least once with network access before "
                    "offline fallback can work. "
                    f"Original network error: {last_err}"
                ) from e

    return _embedding_function

###################
# Chroma Collection
###################
def get_chroma_collection():
    # Return a persistent Chroma collection, creating it if needed.
    # Cached after first call, for the same reason as get_embedding_function()
    
    global _chroma_collection
    if(_chroma_collection is None):
        client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
        _chroma_collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=get_embedding_function(),
            metadata={"hnsw:space": "cosine"},
        )
        
    return _chroma_collection

#################
# Warmup Function
#################
def warmup():
    
    # Use this to preload embedding model weights 
    # and connect to the Chroma collection
    # at server startup, so the first user request doesn't pay that cost.
    start = time.monotonic()
    retrieve("warmup")  
    elapsed = time.monotonic() - start
    print(f"[rag_engine] Warmup complete in {elapsed:.2f}s "
          f"(model loaded, collection connected).")


############################
# 1. Ingestion + 2. Chunking
############################
def _extract_pdf_text(path: str) -> str:
    """Extract text from a single PDF using pdfplumber, page by page.

    Each page's text is prefixed with a "[Page N]" marker before being
    joined — this keeps page boundaries visible in the raw text, which
    chunk_text() below already splits on blank-line/paragraph boundaries,
    so a page marker naturally starts a new paragraph rather than fusing
    into the previous page's last line.

    Pages that fail to extract (e.g. a scanned/image-only page with no
    text layer) are skipped rather than raising — one bad page in a PDF
    shouldn't sink ingestion of the rest of the document. This does mean
    scanned/image-only PDFs will silently yield no text; OCR is out of
    scope for this pipeline (see pdf-reading skill if that's ever needed).
    """
    import pdfplumber

    page_texts = []
    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            try:
                page_text = page.extract_text()
            except Exception as e:
                print(f"[ingest] Warning: failed to extract page {page_num} "
                      f"of {os.path.basename(path)}: {e}")
                continue

            if page_text and page_text.strip():
                page_texts.append(f"[Page {page_num}]\n{page_text.strip()}")

    return "\n\n".join(page_texts)


def load_documents(directory: str = KNOWLEDGE_BASE_DIR) -> List[Dict[str, str]]:
    # Load every .pdf file in `directory` into memory, extracting text via
    # pdfplumber (better layout/table handling than raw pypdf, per the
    # pdf-reading skill's guidance for text-heavy documents).

    # Empty knowledge base
    if(not os.path.isdir(directory)):
        return []

    # Sort so the ingestion order is deterministic
    paths = sorted(glob.glob(os.path.join(directory, "*.pdf")))

    # Skip files with no extractable text, but keep the filename for metadata
    docs = []
    for path in paths:
        try:
            text = _extract_pdf_text(path).strip()
        except Exception as e:
            # A genuinely broken/encrypted/corrupt PDF shouldn't stop the
            # rest of the knowledge base from ingesting.
            print(f"[ingest] Warning: could not read {os.path.basename(path)}: {e}")
            continue

        if text:
            docs.append({"filename": os.path.basename(path), "text": text})
        else:
            print(f"[ingest] Warning: no extractable text in "
                  f"{os.path.basename(path)} (scanned/image-only PDF? "
                  f"OCR is not handled by this pipeline).")

    return docs


def _split_oversized_paragraph(paragraph: str, chunk_size: int, overlap: int) -> List[str]:
    # Fall back to plain character-count splitting, but only for a single
    # paragraph that's too big for chunk size
    
    chunks = []
    start = 0
    while(start < len(paragraph)):
        end = start + chunk_size
        chunks.append(paragraph[start:end])
        
        if(end >= len(paragraph)):
            break
        
        start = end - overlap
    
    return chunks


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> List[str]:
    
    # Entire document less than chunk size no splitting
    if(len(text) <= chunk_size):
        return [text]

    # Split text into paragraphs
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    
    # No paragraphs fallback to character based splitting
    if(not paragraphs):
        return _split_oversized_paragraph(text, chunk_size, overlap)

    chunks = []
    current_paragraphs: List[str] = []
    current_length = 0

    # If chunk full join its paragraphs together
    def flush():
        if(current_paragraphs):
            chunks.append("\n\n".join(current_paragraphs))

    for paragraph in paragraphs:
        if(len(paragraph) > chunk_size):
            # save previous chunks
            flush()
            current_paragraphs = []
            current_length = 0
            
            # split larger paragraph into smaller chunks
            # use sperate chunk for storing it
            chunks.extend(_split_oversized_paragraph(paragraph, chunk_size, overlap))
            continue

        added_length = len(paragraph) + (2 if current_paragraphs else 0) 
        if(current_length + added_length > chunk_size and current_paragraphs):
            flush()
            carry_over = current_paragraphs[-1] if len(current_paragraphs[-1]) <= overlap * 2 else None
            current_paragraphs = [carry_over] if carry_over else []
            current_length = len(carry_over) if carry_over else 0

        current_paragraphs.append(paragraph)
        current_length += added_length

    flush()
    
    return chunks


def ingest_documents(directory: str = KNOWLEDGE_BASE_DIR) -> int:
    # Full ingestion pipeline: load -> chunk -> embed -> store
    # Wipes and rebuilds the collection each run.
    # Invalidates the cached collection uses new one.
    
    global _chroma_collection
    docs = load_documents(directory)
    
    # Empty knowledge base no collection
    if(not docs):
        print(f"[ingest] No documents found in '{directory}'. Nothing to ingest.")
        return 0

    # Recreate delete previous chunks
    client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)

    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass 

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )
    
    # Refresh cache with new collection
    _chroma_collection = collection 

    all_chunks, all_ids, all_metadatas = [], [], []
    for doc in docs:
        chunks = chunk_text(doc["text"])
        # Derive a human-readable topic hint from the filename (e.g.
        # "faq_and_policies.txt" -> "faq and policies"). Prefixing each
        # chunk with this before embedding gives the model a small but
        # real topic signal
        topic_hint = os.path.splitext(doc["filename"])[0].replace("_", " ").replace("-", " ")
        for i, chunk in enumerate(chunks):
            prefixed_chunk = f"[{topic_hint}]\n{chunk}"
            all_chunks.append(prefixed_chunk)
            all_ids.append(str(uuid.uuid4()))
            all_metadatas.append({"source": doc["filename"], "chunk_index": i})

    collection.add(documents=all_chunks, ids=all_ids, metadatas=all_metadatas)
    print(f"[ingest] Stored {len(all_chunks)} chunks from {len(docs)} document(s).")
    return len(all_chunks)


##############
# 3. Retrieval
##############
def retrieve(query: str, top_k: int = TOP_K,
             max_distance: float = MAX_DISTANCE_THRESHOLD) -> List[Dict]:
    # Retrieve the top-k most relevant chunks for query
    # Filters out chunks whose distance exceeds max_distance
    
    # Reject only a fully empty query — an empty string can't be embedded
    # meaningfully. No minimum-length gate beyond that: short queries like
    # "yes" or "ok" are now handled upstream by intent classification
    # (see conversation_manager.py), so they shouldn't reach here as a
    # RAG query in the first place — but if one does, let it through and
    # let the distance threshold decide relevance rather than a hardcoded
    # length cutoff.
    stripped = query.strip()
    if(not stripped):
        return []

    collection = get_chroma_collection()
    # Empty collection
    if(collection.count() == 0):
        return [] 

    # Query the collection for the top-k most similar chunks
    results = collection.query(query_texts=[stripped], n_results=top_k)

    # Filter out chunks that are too far away (distance > max_distance)
    hits = []
    docs = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    for doc, meta, dist in zip(docs, metadatas, distances):
        if(dist <= max_distance):
            hits.append({"text": doc, "source": meta.get("source", "unknown"), "distance": dist})

    return hits


def search_knowledge_base(query: str) -> str:
    """Retrieves the most relevant chunks from the knowledge base for a given query.

    Used by conversation_manager.py's handle_general_qa: this only does
    retrieval, it does NOT call the LLM to generate a final answer —
    conversation_manager.py owns that step itself, using its own
    SYSTEM_INSTRUCTION (loaded from prompts.md) and MAX_RESPONSE_TOKENS.
    generate_answer()/generate_answer_stream() below are a separate,
    self-contained retrieve+generate pipeline kept for any other caller
    (e.g. the CLI or a standalone endpoint) and are unaffected by this
    function.
    """
    try:
        chunks = retrieve(query)
    except Exception as e:
        import traceback
        print(f"[rag_engine] Retrieval failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return f"Error retrieving from knowledge base: {e}"

    if not chunks:
        return "No relevant information found in the knowledge base."

    context_block = "\n\n---\n\n".join(c["text"] for c in chunks)
    sources = sorted(set(c["source"] for c in chunks))
    sources_block = "\nSources: " + ", ".join(sources)
    return context_block + sources_block



# 4. Prompt construction + 5. Generation
########################################
def _build_prompt(query: str, chunks: List[Dict], history: Optional[List[Dict]] = None) -> str:
    
    # Build a prompt for the Groq model, including the retrieved context and optionally the conversation history.
    context_block = "\n\n---\n\n".join(c["text"] for c in chunks)

    history_block = ""
    if(history):
        turns = [f"{turn['role']}: {turn['content']}" for turn in history[-6:]]
        history_block = "CONVERSATION SO FAR:\n" + "\n".join(turns) + "\n\n"

    return (
        f"{history_block}"
        f"CONTEXT:\n{context_block}\n\n"
        f"QUESTION: {query}\n\n"
        f"Answer the question using only the CONTEXT above."
    )


def generate_answer(query: str, history: Optional[List[Dict]] = None) -> Dict:
    # Full RAG call: retrieve -> (short-circuit or) generate -> return.

    # Retrieve relevant chunks from the knowledge base. Wrapped separately
    # from generation so a Chroma/embedding failure here fails safe (falls
    # back to FALLBACK_RESPONSE) instead of bubbling up as an uncaught 500.
    try:
        chunks = retrieve(query)
    except Exception as e:
        import traceback
        print(f"[rag_engine] Retrieval failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {
            "answer": FALLBACK_RESPONSE,
            "grounded": False,
            "sources": [],
            "error": str(e),
        }

    if(not chunks):
        return {
            "answer": FALLBACK_RESPONSE,
            "grounded": False,
            "sources": [],
        }

    # Build the prompt for the Groq model, including the retrieved context and optionally the conversation history.
    prompt = _build_prompt(query, chunks, history)

    try:
        client = get_groq_client()
        response = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=MAX_RESPONSE_TOKENS,
        )
        answer_text = response.choices[0].message.content.strip()
    except Exception as e:
        # Groq API failure (rate limit, network, invalid/missing key,
        # etc.) — fail safe rather than crashing the whole chat turn.
        import traceback
        print(f"[rag_engine] Groq generation failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {
            "answer": (
                "I'm having trouble generating a response right now. "
                "Please try again in a moment."
            ),
            "grounded": False,
            "sources": [],
            "error": str(e),
        }

    return {
        "answer": answer_text,
        "grounded": True,
        "sources": sorted(set(c["source"] for c in chunks)),
    }


def generate_answer_stream(query: str, history: Optional[List[Dict]] = None):
    """Streaming twin of generate_answer(), used by the SSE chat endpoint.

    A generator that yields dicts describing what's happening, in order:

        {"type": "status", "status": "retrieving"}
        {"type": "status", "status": "generating"}       (only if grounded)
        {"type": "token", "text": "..."}                  (repeated, real
                                                            Groq stream deltas)
        {"type": "done", "answer": "...", "grounded": bool, "sources": [...]}

    or, on any failure / ungrounded short-circuit:

        {"type": "status", "status": "retrieving"}
        {"type": "done", "answer": FALLBACK_RESPONSE, "grounded": False, ...}

    This mirrors generate_answer()'s exact retrieval/fallback/error
    handling — the only difference is real token-by-token streaming from
    Groq's API for the actual generation call, and status events emitted
    at each stage so the caller (conversation_manager.handle_message) can
    forward them to the frontend as progress indicators. No fake/fabricated
    typing: every "token" event is a real incremental delta from the
    provider's streaming response, never a replayed complete string.
    """
    yield {"type": "status", "status": "retrieving"}

    try:
        chunks = retrieve(query)
    except Exception as e:
        import traceback
        print(f"[rag_engine] Retrieval failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        yield {
            "type": "done",
            "answer": FALLBACK_RESPONSE,
            "grounded": False,
            "sources": [],
            "error": str(e),
        }
        return

    if not chunks:
        yield {
            "type": "done",
            "answer": FALLBACK_RESPONSE,
            "grounded": False,
            "sources": [],
        }
        return

    prompt = _build_prompt(query, chunks, history)
    yield {"type": "status", "status": "generating"}

    try:
        client = get_groq_client()
        stream = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=MAX_RESPONSE_TOKENS,
            stream=True,
        )

        full_text_parts = []
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                full_text_parts.append(delta)
                yield {"type": "token", "text": delta}

        answer_text = "".join(full_text_parts).strip()
    except Exception as e:
        import traceback
        print(f"[rag_engine] Groq streaming generation failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        yield {
            "type": "done",
            "answer": (
                "I'm having trouble generating a response right now. "
                "Please try again in a moment."
            ),
            "grounded": False,
            "sources": [],
            "error": str(e),
        }
        return

    yield {
        "type": "done",
        "answer": answer_text,
        "grounded": True,
        "sources": sorted(set(c["source"] for c in chunks)),
    }

##############
# CLI Commands
##############
def main():
    parser = argparse.ArgumentParser(description="RAG engine for the agency chatbot.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="(Re)build the vector store from knowledge_base/.")

    p_ask = sub.add_parser("ask", help="Ask a question against the knowledge base.")
    p_ask.add_argument("query", help="The question to ask.")

    args = parser.parse_args()

    if args.command == "ingest":
        ingest_documents()
    elif args.command == "ask":
        result = generate_answer(args.query)
        print("\nAnswer:", result["answer"])
        print("Grounded:", result["grounded"])
        if result["sources"]:
            print("Sources:", ", ".join(result["sources"]))


if __name__ == "__main__":
    main()