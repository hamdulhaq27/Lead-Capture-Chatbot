// app.js
// ======
// Talks to the FastAPI backend (main.py). Handles:
//   - getting/creating a persistent session_id (sessionStorage, so it
//     survives a page refresh within the same tab but not across tabs
//     or browser restarts — appropriate for a support-chat widget)
//   - fetching the one-time greeting on load
//   - streaming a message turn from /api/chat/stream (SSE) and showing
//     LIVE progress copy ("Checking available time slots...",
//     "Confirming your booking...", etc.) as the backend actually moves
//     through those steps — not a generic spinner
//   - revealing the bot's reply as its tokens actually arrive, smoothed
//     by a small typewriter effect so bursts of text don't just pop in
//   - falling back to the older one-shot /api/chat if streaming can't
//     even get started (old browser, proxy strips streaming, etc.)

const API_BASE = window.location.origin; // same host FastAPI serves from

const chatLog = document.getElementById("chat-log");
const typingRow = document.getElementById("typing-row");
const statusText = document.getElementById("status-text");
const form = document.getElementById("chat-form");
const input = document.getElementById("chat-input");
const sendButton = form.querySelector("button[type='submit']");

// How fast revealed text catches up to however much has actually arrived
// from the backend, in characters per second. Using a rate (not a fixed
// per-character delay) and driving it off requestAnimationFrame + real
// elapsed time means the reveal speed stays correct regardless of the
// browser's actual frame rate or any setTimeout throttling — a plain
// setTimeout-per-character loop can get its very short delays clamped/
// coalesced by the browser (especially in a background tab), which can
// make the reveal look like it happens all at once.
const TYPEWRITER_CHARS_PER_SECOND = 60;

/**
 * Get this browser tab's session_id, creating one via the backend if
 * this is the first visit. Stored in sessionStorage (not localStorage)
 * so each new tab gets its own conversation, but a refresh within the
 * same tab keeps the same session — matching conversation_manager.py's
 * in-memory, per-session_id state.
 */
async function getOrCreateSessionId() {
  const existing = sessionStorage.getItem("chatbot_session_id");
  if (existing) return existing;

  try {
    const response = await fetch(`${API_BASE}/api/new-session`);
    const data = await response.json();
    sessionStorage.setItem("chatbot_session_id", data.session_id);
    return data.session_id;
  } catch (err) {
    // Fallback: generate a client-side ID if the backend is unreachable
    // for some reason — better to still let the user try typing than to
    // block the whole widget on this one call failing.
    const fallbackId = `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    sessionStorage.setItem("chatbot_session_id", fallbackId);
    return fallbackId;
  }
}

function appendBubble(text, role) {
  const bubble = document.createElement("div");
  bubble.className = `bubble bubble--${role}`;
  bubble.textContent = text;
  chatLog.appendChild(bubble);
  chatLog.scrollTop = chatLog.scrollHeight;
  return bubble;
}

/**
 * Show/hide the status row (the "bubble" with the pulsing dots + live
 * status copy) that stands in for the bot while a turn is being worked
 * on. Resets the copy back to a neutral default each time it's shown, so
 * the moment a new turn starts the user never sees stale status text
 * from the previous turn while waiting for the first real status event.
 */
function setTyping(isTyping) {
  typingRow.hidden = !isTyping;
  if (isTyping) {
    statusText.textContent = "Thinking…";
    chatLog.scrollTop = chatLog.scrollHeight;
  }
}

/**
 * Update the live status line (e.g. "Checking available time slots...",
 * "Confirming your booking...") with a quick crossfade so one phase
 * doesn't just snap into the next.
 */
function setStatusText(text) {
  if (statusText.textContent === text) return;
  statusText.classList.add("status-text--swap");
  window.setTimeout(() => {
    statusText.textContent = text;
    statusText.classList.remove("status-text--swap");
  }, 150);
}

function setInputEnabled(enabled) {
  input.disabled = !enabled;
  sendButton.disabled = !enabled;
}

/**
 * Reveals text into `bubble` as it actually streams in from the backend,
 * smoothed by a typewriter-style catch-up animation rather than jumping
 * straight to whatever chunk just arrived. `addChunk` can be called
 * repeatedly as more text arrives (extending the target the animation is
 * catching up to); `finish()` resolves once the reveal has fully caught
 * up to everything received so far.
 */
function createStreamingReveal(bubble) {
  let target = "";
  let shown = 0;
  let rafId = null;
  let lastFrameTime = null;
  let doneRequested = false;
  let resolveDone = null;

  function step(now) {
    const elapsedSeconds = lastFrameTime == null ? 0 : (now - lastFrameTime) / 1000;
    lastFrameTime = now;
    shown = Math.min(target.length, shown + elapsedSeconds * TYPEWRITER_CHARS_PER_SECOND);
    bubble.textContent = target.slice(0, Math.floor(shown));
    chatLog.scrollTop = chatLog.scrollHeight;

    if (shown < target.length) {
      rafId = requestAnimationFrame(step);
      return;
    }

    // Caught up to everything received so far.
    rafId = null;
    lastFrameTime = null;
    if (doneRequested && resolveDone) {
      resolveDone();
      resolveDone = null;
    }
  }

  function ensureLoopRunning() {
    if (rafId == null) {
      rafId = requestAnimationFrame(step);
    }
  }

  return {
    addChunk(text) {
      target += text;
      ensureLoopRunning();
    },
    finish() {
      doneRequested = true;
      return new Promise((resolve) => {
        if (shown >= target.length && rafId == null) {
          resolve();
        } else {
          resolveDone = resolve;
        }
      });
    },
  };
}

/**
 * Stream one conversation turn from /api/chat/stream. The backend
 * (conversation_manager.handle_message_stream) yields newline-delimited
 * SSE events shaped like:
 *   {"type": "status", "status": "<user-facing progress text>"}
 *   {"type": "token",  "text": "<incremental reply text>"}
 *   {"type": "done",   "answer": "<full reply>", "state": "<new state>"}
 * which this parses and dispatches to the matching handler.
 */
async function streamChat(sessionId, message, { onStatus, onToken, onDone }) {
  const response = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });

  if (!response.ok || !response.body) {
    throw new Error(`Server returned ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  function handleEventBlock(block) {
    const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
    if (!dataLine) return;
    const jsonStr = dataLine.slice(5).trim();
    if (!jsonStr) return;

    let event;
    try {
      event = JSON.parse(jsonStr);
    } catch (err) {
      console.warn("Could not parse SSE event:", jsonStr, err);
      return;
    }

    if (event.type === "status") onStatus(event.status);
    else if (event.type === "token") onToken(event.text);
    else if (event.type === "done") onDone(event.answer, event.state);
  }

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      handleEventBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
  }

  // Handle a final event that arrived without a trailing blank line.
  if (buffer.trim()) {
    handleEventBlock(buffer);
  }
}

async function sendMessageFallback(sessionId, message) {
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });

  if (!response.ok) {
    throw new Error(`Server returned ${response.status}`);
  }

  return response.json(); // { reply, state }
}

async function loadGreeting(sessionId) {
  try {
    const response = await fetch(`${API_BASE}/api/greeting?session_id=${encodeURIComponent(sessionId)}`);
    const data = await response.json();
    if (data.greeting) {
      appendBubble(data.greeting, "bot");
    }
  } catch (err) {
    // Non-fatal — the widget still works without a greeting, the visitor
    // can just start typing.
    console.warn("Could not load greeting:", err);
  }
}

async function init() {
  const sessionId = await getOrCreateSessionId();
  await loadGreeting(sessionId);

  // "New Chat" (sidebar) — clears the stored session so the next load
  // gets a fresh session_id + greeting, matching the reference design's
  // sidebar affordance. There's currently no multi-conversation history
  // to switch between, so this simply starts over.
  const newChatButton = document.getElementById("new-chat-button");
  if (newChatButton) {
    newChatButton.addEventListener("click", () => {
      sessionStorage.removeItem("chatbot_session_id");
      window.location.reload();
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    appendBubble(message, "user");
    input.value = "";
    setInputEnabled(false);
    setTyping(true);

    let bubble = null;
    let reveal = null;

    try {
      await streamChat(sessionId, message, {
        onStatus: (text) => setStatusText(text),
        onToken: (text) => {
          if (!bubble) {
            setTyping(false);
            bubble = appendBubble("", "bot");
            reveal = createStreamingReveal(bubble);
          }
          reveal.addChunk(text);
        },
        onDone: async (answer) => {
          if (!bubble) {
            // Defensive fallback: the backend should always send at
            // least one token event before "done", but if it somehow
            // didn't, don't leave the user with nothing.
            setTyping(false);
            bubble = appendBubble("", "bot");
            reveal = createStreamingReveal(bubble);
            reveal.addChunk(answer);
          }
          await reveal.finish();
        },
      });
    } catch (err) {
      // Streaming couldn't even get started (old browser, proxy that
      // strips streaming responses, network hiccup, etc.) — fall back to
      // the plain request/response endpoint so the widget still works.
      console.warn("Streaming failed, falling back to /api/chat:", err);
      try {
        setStatusText("Thinking…");
        const { reply } = await sendMessageFallback(sessionId, message);
        setTyping(false);
        const fallbackBubble = appendBubble("", "bot");
        const fallbackReveal = createStreamingReveal(fallbackBubble);
        fallbackReveal.addChunk(reply);
        await fallbackReveal.finish();
      } catch (fallbackErr) {
        setTyping(false);
        appendBubble(
          "Sorry, something went wrong on my end. Please try again in a moment.",
          "error"
        );
        console.error(fallbackErr);
      }
    } finally {
      setInputEnabled(true);
      input.focus();
    }
  });
}

init();