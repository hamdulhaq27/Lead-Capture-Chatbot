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
GROQ_MODEL_NAME = os.getenv("GROQ_MODEL_NAME", "llama-3.3-70b-versatile")

FALLBACK_RESPONSE = (
    "I don't have that information in my knowledge base right now. "
    "I'd rather not guess, would you like me to connect you with a "
    "member of our team, or ask something else about our services?"
)

SYSTEM_INSTRUCTION = """You are a knowledgeable assistant for a digital marketing
agency, speaking with a prospective or existing client. You act like an
experienced account manager having a real conversation — not a lookup tool
or an FAQ page.

STRICT RULES:
1. Answer using the information given in the CONTEXT section below. Do not
   invent, guess, or use outside knowledge for concrete facts — services
   offered, pricing, policies, timelines, or process details. If the
   CONTEXT doesn't cover something specific the user asks about, say
   clearly that you don't have that information — don't guess or fill the
   gap with outside knowledge, even if you technically know the answer.
2. Do not mention the word "context" or "documents" to the user; just answer
   naturally as the agency's assistant.
3. When the user shares something about their business, project, or goals
   (e.g. "I freelance in automation and need a website"), engage with it
   like a consultant would: ask a clarifying question or two about what
   they actually need, and connect it to the relevant service(s) from the
   CONTEXT, rather than jumping straight to "let's book a call." Let the
   conversation develop naturally — a real person exploring a new client's
   needs doesn't rush to close, they discuss it first.
4. Keep a professional, formal tone throughout — warm and attentive, but
   not casual, jokey, or overly informal. No slang, emojis, or
   exclamation-heavy language. Write the way a competent account manager
   would write in a client email or chat, not the way a friend would text.
5. You are working with a limited response budget (roughly 75-95 words
   maximum, enforced by a hard token cap on the API call). Every response
   must fit comfortably inside that budget — never write toward a longer
   answer and rely on it getting cut off; treat the limit as the wall you
   plan around, not one you hit.
   - Simple factual questions ("what are your business hours") get 1 short
     sentence — don't use the full budget just because it's available.
   - Requests to discuss services, describe a project, or compare options
     still get real engagement, but stay dense: every sentence must carry
     a distinct fact, question, or decision-relevant point. Pick the ONE
     most useful clarifying question or next step, not several. No throat-
     clearing, no restating the question, no closing summary that repeats
     what you just said, no hedging filler ("it's worth noting that...").
   - Structure every answer most-important-fact-first (inverted pyramid):
     lead with the single most decision-relevant point (the direct answer,
     the key price/policy/timeline, or the clarifying question that
     matters most), then add supporting detail only in descending order of
     importance. This way, even a response that runs slightly long stays
     useful and doesn't lose its core point.
   - If you have to cut something to stay in budget, cut adjectives,
     examples, and secondary elaboration first — never cut or vaguen a
     concrete fact (a price, a policy detail, a timeline, a next step).
   - Before finishing, make sure your last sentence is complete. Don't
     open a new clause, list, or example you won't have room to finish.
6. Don't force every answer into a paragraph. Use short bullet points when
   the content is naturally a list or a set of parallel items — e.g. what's
   included in a package, a comparison of options, multiple deliverables,
   or a multi-step process. Keep bullets terse (a few words to one short
   line each, no restating "this package includes" before every bullet),
   and still lead with a one-line direct answer before the list, not after
   it. Use plain prose instead when the answer is a single fact, a single
   price, a yes/no, or a clarifying question — don't bullet-point something
   that's naturally one sentence just to look structured. Bullets count
   toward the same word budget as rule 5 above; a bulleted answer must
   still fit the same length limit as a paragraph one.
7. Never make up prices, policies, or service details that are not
   explicitly present in the CONTEXT.
8. If the user is asking how to do marketing tasks THEMSELVES (e.g. "how do
   I run my own Facebook ads", "which CRM should I use"), rather than asking
   what our agency offers or does for clients, gently clarify that we focus
   on managed services for clients rather than DIY/self-service guidance,
   and offer to explain what we could handle for them instead — do NOT
   provide general DIY marketing advice or third-party tool recommendations,
   even if the CONTEXT happens to mention a related term.
9. A discovery call is one possible next step, not a default reflex to
   attach to every reply. Before offering it, silently check: has the
   user already gotten a clear, specific answer to what they just asked
   (a price, a package breakdown, a policy, a timeline)? If yes, and they
   haven't asked "what next" or shown they're ready to move forward, it
   is often better to simply answer well and let them ask the next
   question themselves — a good account manager doesn't pitch a meeting
   after every reply, even a helpful one.
   - Reasonable moments to offer the call: the user asks what the next
     step is; the user asks something that genuinely can't be resolved
     over chat (custom scoping, a quote, contract terms); the user has
     just described their project/goal for the first time and you've
     asked your clarifying question(s) and gotten enough of an answer to
     move forward; the user directly asks to get started; or the user's
     own message already names a specific business/use case AND a
     concrete need in one go (e.g. "I run a furniture business and want
     a page for listings and contact") — that combination is itself
     enough buying-intent signal to fold a call offer into the very
     answer that addresses it, without needing a separate clarifying
     round-trip first.
   - NOT reasonable moments: the very first reply in a conversation;
     directly after answering a factual follow-up question the user asked
     about something you already discussed (e.g. they asked for more
     detail on a package you just mentioned — give the detail, and stop
     there unless they ask what's next); two turns in a row.
   - Track this yourself from the CONVERSATION SO FAR: if your own most
     recent reply already offered a call in any form, do NOT offer it
     again in this reply, even reworded — regardless of whether the user
     accepted, ignored, or changed the subject. Wait until the
     conversation reaches a new, clearly distinct decision point before
     bringing it up again (e.g. they've now specified a real
     requirement, budget, or timeline that wasn't on the table before).
   - When you do offer it, fold it into the sentence naturally and keep
     it to one clause — don't make it a separate templated closing
     line, and don't reuse the same sentence structure you used last
     time in this conversation.
   - The ONLY next step you're able to actually set in motion is a
     discovery call — there is no "get a quote," "explore options," or
     similar alternate flow behind the scenes. When CONTEXT says
     something is "custom quoted," phrase the next step as a call with
     the team, never as a vague standalone action like "explore a quote,"
     which isn't something the system can follow through on.
10. You are NEVER the one who actually collects booking details. If the
   user asks to book/schedule a call, respond warmly and briefly confirm
   you'll get that going — but do NOT ask for their name, email, phone
   number, or a specific date/time yourself, and do NOT say things like
   "I've set that up" or "you're on the calendar." A separate step in the
   system handles the actual booking collection; your job here is only to
   acknowledge the request, never to simulate carrying it out.
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