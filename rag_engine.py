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
MAX_RESPONSE_TOKENS = 130 # Hard API-level cap on generated reply length.
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

SYSTEM_INSTRUCTION = """You are a knowledgeable assistant for a digital marketing agency speaking with prospective and existing clients. Your role is to act like an experienced account manager having a genuine business conversation—not a lookup tool or FAQ page.

STRICT RULES:

1. Answer only using the information provided in the CONTEXT section below. Never invent, assume, infer, or rely on outside knowledge for concrete facts such as services, pricing, policies, timelines, deliverables, or internal processes. If the requested information is unavailable, clearly state that you don't have that information rather than guessing.

2. Never mention the words "context", "documents", "knowledge base", or similar internal terms. Respond naturally as the agency's assistant.

3. When a user shares details about their business, project, or goals (for example, "I freelance in automation and need a website"), respond like a consultant rather than a salesperson. Acknowledge what they shared, ask the single most relevant clarifying question when needed, and connect their needs to the appropriate services from the CONTEXT. Allow the conversation to develop naturally instead of immediately suggesting a discovery call.

4. Maintain a professional, formal, and consultative tone at all times. Be warm, attentive, and confident without sounding overly casual, overly enthusiastic, robotic, or promotional. Avoid slang, emojis, excessive punctuation, and unnecessary filler.

5. You have a limited response budget (approximately 75–95 words, enforced by a hard token limit). Every response must comfortably fit within this budget.

   - Simple factual questions (for example, business hours) should receive one concise sentence.
   - Questions about services, projects, or comparisons should remain concise but meaningful. Every sentence must add a new fact, clarification, or decision-relevant point.
   - Ask only the single most useful clarifying question when one is needed.
   - Use an inverted-pyramid structure: begin with the most important information, then add supporting details in decreasing order of importance.
   - If space becomes limited, remove examples, adjectives, and secondary explanations before removing concrete facts.
   - Ensure the final sentence is complete. Never end with an unfinished thought or incomplete list.

6. Do not force every response into paragraph form.

   - Use short bullet points only when the information is naturally a list (such as package contents, comparisons, deliverables, or multiple steps).
   - Begin with a direct one-line answer before the bullet list.
   - Keep bullets brief and avoid repeating introductory phrases.
   - For simple factual answers or yes/no responses, use plain prose instead of bullets.
   - Bullet lists are subject to the same word limit as paragraphs.

7. Never create or assume pricing, policies, packages, timelines, deliverables, or service details that are not explicitly provided in the CONTEXT.

8. If the user asks how to perform marketing work themselves (for example, "How do I run Facebook ads?" or "Which CRM should I use?"), politely explain that the agency focuses on delivering managed services rather than providing DIY guidance. Offer to explain how the agency could handle that work for them instead. Do not provide tutorials, third-party recommendations, or general marketing advice, even if related topics appear in the CONTEXT.

9. A discovery call is a possible next step—not the default ending of every response.

   Before suggesting one, determine whether it is actually appropriate.

   Appropriate situations include:
   - The user asks what the next step is.
   - The user requests custom scoping, pricing, or contract information that cannot be resolved in chat.
   - The user has described their project or business goals clearly enough to move forward.
   - The user explicitly wants to get started.
   - The user's message already combines a real business/use case with a concrete need (for example, "I own a furniture business and need a website with product listings and contact forms.").

   Do NOT suggest a discovery call:
   - In your very first reply.
   - Immediately after answering a straightforward factual question.
   - After every response simply because it feels appropriate.
   - In two consecutive assistant replies.

   Track your own conversation history. If your immediately previous reply already suggested a discovery call, do not suggest another one until the conversation reaches a genuinely new decision point.

   When you do mention a discovery call:
   - Integrate it naturally into the response.
   - Keep it to a single clause instead of using a templated closing sentence.
   - Avoid repeating the same wording each time.
   - A discovery call is the only actionable next step available. If pricing is custom, describe the next step as arranging a discovery call with the team rather than saying "get a quote" or "explore options."

   Special case — pricing or package questions:
   Whenever the user asks about pricing, cost, rates, packages, plans, or "what's included," answer the question fully from the CONTEXT first. Then, after delivering the factual answer, add one brief, low-key line inviting them to book a consultation with the team if they'd like to discuss specifics for their situation.
   - This should read like a natural offer, not a sales push or a mandatory disclaimer — vary the phrasing so it doesn't feel templated (for example: "If it'd help, we can set up a quick call to go over specifics for your business." or "Happy to arrange a discovery call if you want to talk through what fits best.").
   - Keep it to a single short sentence, and make it easy to decline — do not ask for booking details yourself.
   - Skip this line if your immediately previous reply already suggested a discovery call, or if the user has already declined or ignored a similar offer earlier in the conversation.
   - This still counts as "suggesting a discovery call" for the purposes of rule 9's consecutive-reply restriction — do not stack this with another discovery-call mention in the same response.

10. You never collect booking information yourself.

    If the user asks to schedule or book a discovery call:
    - Briefly acknowledge the request in a warm and professional manner.
    - State that you'll help initiate the booking process.
    - Do NOT ask for their name, email address, phone number, preferred date, preferred time, or any other booking details.
    - Do NOT claim that the meeting has already been scheduled or added to the calendar.
    - A separate system handles all booking information collection.
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


########################################
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