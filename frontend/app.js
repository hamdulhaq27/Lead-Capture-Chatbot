// app.js
// ======
// Talks to the FastAPI backend (main.py). Handles:
//   - getting/creating a persistent session_id (sessionStorage, so it
//     survives a page refresh within the same tab but not across tabs
//     or browser restarts — appropriate for a support-chat widget)
//   - fetching the one-time greeting on load
//   - sending messages and rendering the bot's reply
//   - a typing indicator while waiting on the backend
//   - revealing the bot's reply gradually (typewriter effect) instead of
//     showing the whole message at once

const API_BASE = window.location.origin; // same host FastAPI serves from

const chatLog = document.getElementById("chat-log");
const typingRow = document.getElementById("typing-row");
const form = document.getElementById("chat-form");
const input = document.getElementById("chat-input");
const sendButton = form.querySelector("button[type='submit']");

// How fast the typewriter effect reveals the bot's reply, in characters
// per second. Using a rate (not a fixed per-character delay) and driving
// it off requestAnimationFrame + real elapsed time means the reveal
// speed stays correct regardless of the browser's actual frame rate or
// any setTimeout throttling — a plain setTimeout-per-character loop can
// get its very short delays clamped/coalesced by the browser (especially
// in a background tab), which can make the "gradual" reveal look like it
// happens all at once.
const TYPEWRITER_CHARS_PER_SECOND = 40;

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
 * Reveal `text` into `bubble` gradually instead of setting it all at
 * once — this is a purely visual/client-side effect: the backend already
 * returned the complete reply, so this just controls how quickly it
 * appears on screen. Driven by requestAnimationFrame and real elapsed
 * time (not a fixed setTimeout-per-character loop), so the pacing stays
 * correct even if individual frames are skipped or delayed, and isn't
 * subject to the browser clamping/coalescing very short setTimeout
 * delays — the failure mode that made the previous version appear to
 * show the whole reply instantly instead of streaming it in.
 */
function typewriterInto(bubble, text) {
  return new Promise((resolve) => {
    const startTime = performance.now();

    function step(now) {
      const elapsedSeconds = (now - startTime) / 1000;
      const charsToShow = Math.min(
        text.length,
        Math.floor(elapsedSeconds * TYPEWRITER_CHARS_PER_SECOND)
      );
      bubble.textContent = text.slice(0, charsToShow);
      chatLog.scrollTop = chatLog.scrollHeight;

      if (charsToShow < text.length) {
        requestAnimationFrame(step);
      } else {
        resolve();
      }
    }

    requestAnimationFrame(step);
  });
}

function setTyping(isTyping) {
  typingRow.hidden = !isTyping;
  if (isTyping) {
    chatLog.scrollTop = chatLog.scrollHeight;
  }
}

function setInputEnabled(enabled) {
  input.disabled = !enabled;
  sendButton.disabled = !enabled;
}

async function sendMessage(sessionId, message) {
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

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    appendBubble(message, "user");
    input.value = "";
    setInputEnabled(false);
    setTyping(true);

    try {
      const { reply } = await sendMessage(sessionId, message);
      setTyping(false);
      // Reveal the reply gradually rather than dumping it on screen all
      // at once — the backend call has already finished by this point,
      // so this is just how quickly the text appears.
      const bubble = appendBubble("", "bot");
      await typewriterInto(bubble, reply);
    } catch (err) {
      setTyping(false);
      appendBubble(
        "Sorry, something went wrong on my end. Please try again in a moment.",
        "error"
      );
      console.error(err);
    } finally {
      setInputEnabled(true);
      input.focus();
    }
  });
}

init();