"""
conversation_manager.py
=========================
The dialogue/session-state layer that turns rag_engine.py, booking_manager.py,
and chatbot_lead_manager.py into one coherent chat experience.

Architecture
------------
Each session (one visitor's chat) has a state machine:

    GENERAL
      -> user wants to book         -> COLLECTING_BOOKING_INFO
      -> user wants to cancel       -> COLLECTING_CANCELLATION_INFO
      -> anything else              -> stays GENERAL, answered via RAG

    COLLECTING_BOOKING_INFO
      -> all fields gathered        -> CONFIRMING_BOOKING
      -> user abandons/changes mind -> back to GENERAL (with confirmation)

    CONFIRMING_BOOKING
      -> user confirms "yes"        -> attempt booking, back to GENERAL
      -> user says "no"/change      -> back to COLLECTING_BOOKING_INFO

    COLLECTING_CANCELLATION_INFO -> CONFIRMING_CANCELLATION -> GENERAL
      (mirrors the booking flow)

Intent classification is LLM-driven (via Groq) rather than keyword-matched,
since real phrasing varies too much for reliable keyword rules ("can we
hop on a call?" has no booking keywords but is clearly a booking intent).

Session state is stored in-memory (a module-level dict keyed by session_id).
This resets on server restart and doesn't scale across multiple processes —
both fine for now, revisit with Redis or a DB if/when you actually need
multi-instance deployment.

This module does NOT expose HTTP endpoints — that's main.py (Module H),
which just calls handle_message(session_id, text) and returns the result.
"""

import os
import re
import json
import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass, field

from groq import Groq
from dotenv import load_dotenv

from rag_engine import generate_answer, generate_answer_stream
from booking_manager import (
    create_booking, cancel_booking, reschedule_booking, find_active_booking_by_email,
    find_booking_by_id, BookingError, GoogleCalendarHelper,
)
from lead_manager_2 import (
    LeadValidationError, EMAIL_REGEX, NAME_REGEX,
    normalize_phone, validate_phone_number,
)
from chatbot_lead_manager import capture_lead, update_lead_booking  # Module B — built alongside this

load_dotenv()

GROQ_MODEL_NAME = os.getenv("GROQ_MODEL_NAME", "llama-3.3-70b-versatile")
_groq_client = None


def get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not found in environment.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


###############
# Session state
###############
class State:
    GENERAL = "GENERAL"
    COLLECTING_BOOKING_INFO = "COLLECTING_BOOKING_INFO"
    CONFIRMING_BOOKING = "CONFIRMING_BOOKING"
    COLLECTING_CANCELLATION_INFO = "COLLECTING_CANCELLATION_INFO"
    CONFIRMING_CANCELLATION = "CONFIRMING_CANCELLATION"
    # Reschedule flow: find the existing booking (same lookup as
    # cancellation), then collect a NEW date/time, then confirm the swap.
    COLLECTING_RESCHEDULE_LOOKUP = "COLLECTING_RESCHEDULE_LOOKUP"
    COLLECTING_RESCHEDULE_NEW_TIME = "COLLECTING_RESCHEDULE_NEW_TIME"
    CONFIRMING_RESCHEDULE = "CONFIRMING_RESCHEDULE"


BOOKING_REQUIRED_FIELDS = ["name", "email", "phone", "date", "time"]
BOOKING_OPTIONAL_FIELDS = ["company", "note"]
SESSION_IDLE_TIMEOUT = datetime.timedelta(minutes=15)

# Fixed (not LLM-generated) greeting shown once at the start of a new session
GREETING_MESSAGE = (
    "Hi! I'm BrightReach's virtual assistant. I can help with services, "
    "pricing, or booking a free discovery call."
)


#############################################
# Progress/status messages (frontend display)
#############################################
# These describe what the APPLICATION is doing at each workflow stage —
# never the model's internal reasoning or chain of thought. They're
# purely cosmetic progress indicators forwarded to the frontend over SSE
# while a request is being processed; they carry no data the handlers
# themselves rely on.
STATUS_UNDERSTANDING = "Understanding your request..."
STATUS_PREPARING_RESPONSE = "Preparing your response..."
STATUS_LOOKING_UP = "Looking up information..."
STATUS_SEARCHING_KB = "Searching our knowledge base..."
STATUS_CHECKING_SLOTS = "Checking available time slots..."
STATUS_CONFIRMING_BOOKING = "Confirming your booking..."
STATUS_SAVING_APPOINTMENT = "Saving your appointment..."
STATUS_FINDING_BOOKING = "Finding your booking..."
STATUS_LOOKING_FOR_SLOT = "Looking for another available time..."
STATUS_UPDATING_APPOINTMENT = "Updating your appointment..."
STATUS_PROCESSING_CANCELLATION = "Processing your cancellation..."
STATUS_GATHERING_INFO = "Gathering your information..."
STATUS_CONNECTING_TEAM = "Connecting you with our team..."

# Per-(state, intent) sequence of statuses to emit BEFORE the handler
# runs, in order. The handler-specific "generating"/"retrieving" statuses
# for the actual RAG call are emitted separately by _handle_general_stream
# itself (since only that path does real Groq streaming) — this map only
# covers the statuses that make sense to show before we even know which
# branch a handler will take.
def _initial_statuses_for(state: str, intent: str) -> list:
    if state == State.GENERAL:
        if intent == "start_booking":
            return [STATUS_UNDERSTANDING, STATUS_CHECKING_SLOTS]
        if intent == "start_cancellation":
            return [STATUS_UNDERSTANDING, STATUS_FINDING_BOOKING]
        if intent == "start_reschedule":
            return [STATUS_UNDERSTANDING, STATUS_FINDING_BOOKING]
        # Falls through to RAG — _handle_general_stream emits its own
        # "retrieving"/"generating" statuses at the right moments, so we
        # only need the initial "understanding" here.
        return [STATUS_UNDERSTANDING]

    if state == State.COLLECTING_BOOKING_INFO:
        return [STATUS_UNDERSTANDING, STATUS_CHECKING_SLOTS]
    if state == State.CONFIRMING_BOOKING:
        return [STATUS_CONFIRMING_BOOKING, STATUS_SAVING_APPOINTMENT]

    if state == State.COLLECTING_CANCELLATION_INFO:
        return [STATUS_FINDING_BOOKING]
    if state == State.CONFIRMING_CANCELLATION:
        return [STATUS_PROCESSING_CANCELLATION]

    if state == State.COLLECTING_RESCHEDULE_LOOKUP:
        return [STATUS_FINDING_BOOKING]
    if state == State.COLLECTING_RESCHEDULE_NEW_TIME:
        return [STATUS_LOOKING_FOR_SLOT]
    if state == State.CONFIRMING_RESCHEDULE:
        return [STATUS_UPDATING_APPOINTMENT]

    return [STATUS_UNDERSTANDING]


@dataclass
class Session:
    state: str = State.GENERAL
    collected: Dict[str, str] = field(default_factory=dict)
    history: list = field(default_factory=list)  # [{"role": ..., "content": ...}, ...]
    last_active: datetime.datetime = field(default_factory=datetime.datetime.now)
    # Set when CONFIRMING_CANCELLATION finds a real booking to confirm against.
    pending_cancellation_booking: Optional[Dict[str, Any]] = None
    # Set when a reschedule flow finds the existing booking to be moved.
    pending_reschedule_booking: Optional[Dict[str, Any]] = None
    # True right after we've asked "did you mean AM or PM?" — the NEXT
    # message is interpreted as answering that specific question, rather
    # than being re-run through general field extraction (a bare "PM"
    # reply has no date/time phrase in it for the LLM to find on its own).
    awaiting_am_pm_clarification: bool = False
    # True once the greeting has been shown for this session — prevents
    # re-greeting on every message, and lets a re-opened idle-timeout
    # session (see get_session()) greet again as if it were new, since
    # a fresh Session() object always starts with greeted=False.
    greeted: bool = False
    # History length (len(session.history) at the time) when the fallback
    # "leave your name and email" CTA was last appended to an ungrounded
    # RAG answer. None means it hasn't been shown yet this session. Used
    # by _should_offer_followup_cta() to enforce a cooldown so the CTA
    # doesn't get re-appended on every single ungrounded reply.
    last_cta_turn: Optional[int] = None
    # Rotates through _FOLLOWUP_CTA_PHRASINGS so repeat CTAs (once the
    # cooldown clears) don't use the exact same wording every time.
    cta_phrasing_index: int = 0
    # Booking ID of the last booking this session has positively
    # identified — via a completed lookup, a freshly created booking, or a
    # completed reschedule — so a LATER request in the same conversation
    # ("I'd like to change my email too") can reuse it instead of asking
    # the user for their booking ID/email all over again. Cleared once the
    # booking is cancelled (no longer something to reuse) or found to be
    # stale (see _get_known_booking).
    last_known_booking_id: Optional[str] = None


_sessions: Dict[str, Session] = {}


def get_session(session_id: str) -> Session:
    """Fetch or create a session, resetting it if it's gone stale (idle
    timeout) — this is our simple handling of 'user abandons the
    conversation' from the original requirements: a stale half-completed
    booking shouldn't silently resume days later with no context."""
    session = _sessions.get(session_id)
    now = datetime.datetime.now()

    if session and (now - session.last_active) > SESSION_IDLE_TIMEOUT:
        session = None  # expired — treat as a new session

    if session is None:
        session = Session()
        _sessions[session_id] = session

    session.last_active = now
    return session


def get_greeting(session_id: str) -> Optional[str]:
    """Return the greeting message if this session hasn't been greeted
    yet, marking it as greeted; otherwise return None.

    Intended to be called once, right when a chat widget/session starts
    (before the visitor has typed anything) — e.g. a frontend calls this
    on page load to show an opening message, separately from
    handle_message(), which only responds to actual user input. Calling
    this repeatedly for the same session is safe: only the first call
    returns the greeting, every call after returns None.
    """
    session = get_session(session_id)
    if session.greeted:
        return None
    session.greeted = True
    return GREETING_MESSAGE


def reset_session(session_id: str) -> None:
    _sessions[session_id] = Session()


####################################
# Intent classification (LLM-driven)
####################################
INTENT_SYSTEM_PROMPT = """You classify a user's message into ONE intent for a
digital marketing agency's chatbot. Respond with ONLY a JSON object, no
other text, in this exact shape:

{"intent": "<one of: general_qa, start_booking, start_cancellation, start_reschedule, confirm, deny, abandon, provide_info>"}

Definitions:
- start_booking: the user has clearly decided they want to schedule a NEW meeting, call, consultation, demo, or discovery call — e.g. "let's book a call", "can we get something on the calendar", "I'd like to schedule a consultation", or an affirmative reply to the assistant having just offered to set one up. Do NOT classify as start_booking just because the user describes their business, project, or goals, or expresses interest in a service — that is general_qa. The assistant should discuss the user's needs first; only move to booking once the user actually asks to schedule something or clearly agrees to a call the assistant offered.
- start_cancellation: user wants to cancel an existing booking outright (not replace it with a new time).
- start_reschedule: user wants to CHANGE/UPDATE any detail of an existing booking — the date, time, OR their name/email/phone number on file for it — this is DIFFERENT from cancellation, since the customer still wants the call, just with something about it corrected or moved. Phrases like "change my booking", "move my appointment", "reschedule", "can we do a different time instead", "change my email", "update my phone number", "I need to correct the name on my booking" all mean this, NOT start_cancellation. This applies even when the user only says "change email"/"update phone"/"change name" with no other context — in this chatbot, updating contact details is only ever done through the booking-update flow, so treat bare requests like that as start_reschedule too.
- confirm: user is affirmatively confirming something just asked of them (yes, correct, sounds good, etc.)
- deny: user is rejecting/correcting something just asked of them (no, that's wrong, actually change it to...)
- abandon: user wants to stop the current booking/cancellation/reschedule process entirely (nevermind, cancel that, stop, forget it).
- provide_info: user is supplying requested information (a name, email, phone, date, time) as part of an ongoing flow.
- general_qa: anything else — general questions about services, pricing, policies, small talk, or the user describing their business/project/goals and exploring whether or how the agency could help.

If the CURRENT_STATE below is not GENERAL, lean toward confirm/deny/abandon/provide_info
over general_qa unless the message is CLEARLY an unrelated new question.
"""


##########################################
# Date-phrase resolution (LLM-driven, but
# constrained to INTERPRETATION only — see
# _parse_date_phrase for why arithmetic is
# never delegated to the model)
##########################################
DATE_RESOLUTION_SYSTEM_PROMPT = """You interpret a natural-language date phrase
for a booking system. You do NOT calculate the final date yourself — you only
identify the STRUCTURE of what the user means, in this exact JSON shape:

{"kind": "explicit" | "weekday" | "unresolvable",
 "explicit_year": <int or null>, "explicit_month": <int 1-12 or null>, "explicit_day": <int 1-31 or null>,
 "weekday": "<monday|tuesday|wednesday|thursday|friday|saturday|sunday> or null",
 "week_offset": <int or null>,
 "day_offset": <int or null>}

Rules for which "kind" to use:
- "explicit": the phrase reduces to either (a) a plain day-count offset from
  today, or (b) a specific calendar date. Two cases:
    (a) Day-count offset — phrases like "today", "tomorrow", "yesterday",
        "day after tomorrow", "in 3 days", "in 2 weeks", "10 days from now",
        "a week from now". Set day_offset to the signed number of days from
        today (today=0, tomorrow=1, yesterday=-1, "in 2 weeks"=14, "a week
        from now"=7, etc.). Leave explicit_year/month/day and weekday null.
    (b) Specific calendar date — phrases like "July 20", "20/7", "20th of
        July 2026", "7-20-2026". Extract explicit_day and explicit_month as
        written (explicit_year only if a year is actually stated, otherwise
        null). Do not compute anything — just read off the numbers/month name
        as given. Leave day_offset and weekday null.
  Do NOT use case (a) for "next month" — a month is not a fixed number of
  days, so guessing a day-count for it would be wrong. Use "unresolvable" for
  "next month" instead (see below).
- "weekday": the phrase names a day of the week (e.g. "Thursday", "next
  Thursday", "next week Thursday", "this coming Monday", "Thursday of next
  week", "the Thursday after next"). Set "weekday" to that day's name
  (lowercase). Set "week_offset" to:
    - 0 for the NORMAL case — this covers a bare weekday name ("Thursday"),
      "next [weekday]" ("next Thursday"), "next week [weekday]" ("next week
      Thursday"), and "this coming [weekday]". ALL of these mean the SAME
      thing: the closest upcoming occurrence of that day, whether that's
      tomorrow or up to 6 days away. Do NOT treat "next" or "next week" as
      a signal to skip an extra week — in everyday scheduling usage, "next
      Tuesday" and "next week Tuesday" both just mean "the Tuesday that's
      coming up," not "skip this Tuesday, go to the one after."
    - 1 ONLY if the phrase is explicit about skipping past the nearest
      occurrence — e.g. "the [weekday] after next", "the week after next on
      [weekday]", "not this Tuesday, the one after", "two Tuesdays from
      now" (which would actually be week_offset 1 relative to the nearest
      Tuesday). If the phrase doesn't clearly signal "skip one," use 0.
    - 2 for phrases that explicitly compound the skip, e.g. "two weeks from
      now on [weekday]" combined with a further "after that" qualifier —
      this should be rare; most real phrases resolve to 0 or 1.
  Leave explicit_year/month/day and day_offset null for this kind.
- "unresolvable": the phrase is too vague to interpret at all (e.g. "soon",
  "later", "whenever"), OR it is "next month" specifically (a month has no
  fixed day-count, so this must be flagged rather than guessed — the caller
  will ask the user for a specific date instead).

Respond with ONLY the JSON object, no other text. Never include commentary,
never explain your reasoning, never wrap the JSON in markdown fences.
"""



# Safety net: LLM classification isn't 100% deterministic, and an API
# failure silently defaults to general_qa (see classify_intent below). If
# the classifier misses an explicit, unambiguous booking request, the
# general-Q&A responder must NOT be the one handling it — it can only
# discuss and improvise, it can't actually collect/store booking details.
# This regex catches clear-cut phrasing as a backstop so a request like
# "I want to book a call" always reaches the real booking flow even if the
# LLM call glitches or the request phrasing was ambiguous enough that the
# classifier hedged toward general_qa.
_EXPLICIT_BOOKING_PATTERN = re.compile(
    r"\b(book|schedule|set\s?up|arrange)\b[^.?!]{0,25}\b"
    r"(a\s+)?(call|meeting|consultation|demo|discovery\s+call|appointment|session)\b",
    re.IGNORECASE,
)


def _looks_like_explicit_booking_request(message: str) -> bool:
    return bool(_EXPLICIT_BOOKING_PATTERN.search(message))


# Same idea as _EXPLICIT_BOOKING_PATTERN above, but for "I want to change/
# update something about my existing booking" requests — including bare
# contact-detail changes like "change my email" or "update phone number"
# that don't mention the word "booking" at all. Since this chatbot only
# ever updates contact details via the reschedule/update flow, catching
# these here means the classifier missing the intent (e.g. misreading
# "change email" as an unrelated general question) doesn't leave the user
# stuck at the RAG fallback.
_EXPLICIT_RESCHEDULE_PATTERN = re.compile(
    r"\b(chang\w*|updat\w*|edit\w*|correct\w*|fix\w*|modif\w*)\b[^.?!]{0,25}\b"
    r"(my\s+)?(email|e-mail|phone|number|phone\s+number|name|date|time|"
    r"booking|appointment|reservation|details?)\b"
    r"|\b(reschedul\w*|mov\w*)\b[^.?!]{0,25}\b(my\s+)?(booking|appointment|call|meeting)\b",
    re.IGNORECASE,
)


def _looks_like_explicit_reschedule_request(message: str) -> bool:
    return bool(_EXPLICIT_RESCHEDULE_PATTERN.search(message))


# Matches the kind of natural-language next-step offer the RAG assistant
# makes per its own system prompt (rag_engine.py rule 8) — e.g. "I can set
# up a quick call with our team", "would you like me to arrange a
# discovery call", "want me to get a call arranged". Also covers looser
# phrasing the model may still occasionally use for the same underlying
# offer (a custom quote, exploring options, discussing further) since
# those are only ever actually fulfilled via a call in this system.
# Deliberately loose: it only needs to catch that SOME next step was
# offered, not the exact wording.
_CALL_OFFER_PATTERN = re.compile(
    r"\b(call|meeting|consultation|demo|discovery\s+call|session)\b[^.?!]{0,40}"
    r"\b(arrange|set\s?up|book|schedule)\b"
    r"|\b(arrange|set\s?up|book|schedule)\b[^.?!]{0,40}"
    r"\b(call|meeting|consultation|demo|discovery\s+call|session)\b"
    r"|\b(would\s+you\s+like|want\s+me\s+to|shall\s+i)\b[^.?!]{0,60}"
    r"\b(custom\s+quote|quote|explore|discuss|connect\s+you|our\s+team|next\s+step)\b",
    re.IGNORECASE,
)

_AFFIRMATIVE_PATTERN = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|sounds?\s+good|please|go\s+ahead|"
    r"let'?s\s+do\s+(it|that)|that\s+works?|works?\s+for\s+me)\b",
    re.IGNORECASE,
)


def _looks_like_call_offer(assistant_message: str) -> bool:
    # Exclude the RAG fallback's own lead-capture nudge ("...would you like
    # to leave your name and email so our team can follow up directly?")
    # — that's a different, simpler ask (just contact info), not an offer
    # to book a call, and shouldn't route a plain "yes" into the full
    # booking flow (which would also ask for a specific date/time).
    if re.search(r"leave\s+your\s+name\s+and\s+email", assistant_message, re.IGNORECASE):
        return False
    return bool(_CALL_OFFER_PATTERN.search(assistant_message))


def _looks_like_affirmative(message: str) -> bool:
    return bool(_AFFIRMATIVE_PATTERN.search(message.strip()))


def classify_intent(message: str, current_state: str,
                     last_assistant_message: Optional[str] = None) -> str:
    """Return one of the intent strings defined in INTENT_SYSTEM_PROMPT.

    `last_assistant_message` gives the classifier the context it needs to
    correctly read a bare "yes"/"sure"/etc. — e.g. distinguishing "yes" as
    agreeing to a call the assistant just offered (-> start_booking) from
    "yes" confirming a booking-info summary (-> confirm). Without this, a
    standalone "yes" is ambiguous even to the classifier itself.

    Falls back to "general_qa" on any classification failure (malformed
    JSON, API error) — the safest default, since worst case the user just
    gets a RAG answer instead of being routed into a flow they didn't ask for.
    """
    intent = "general_qa"
    try:
        client = get_groq_client()
        user_content = f"CURRENT_STATE: {current_state}\n"
        if last_assistant_message:
            user_content += f"ASSISTANT'S LAST MESSAGE: {last_assistant_message}\n"
        user_content += f"MESSAGE: {message}"

        response = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=40,  # tiny JSON object like {"intent": "start_booking"} — this
                            # is a defensive cap, unrelated to reply length (this
                            # output is never shown to the user).
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
        candidate = parsed.get("intent", "general_qa")
        valid_intents = {"general_qa", "start_booking", "start_cancellation", "start_reschedule",
                          "confirm", "deny", "abandon", "provide_info"}
        intent = candidate if candidate in valid_intents else "general_qa"
    except Exception as e:
        print(f"[conversation_manager] Intent classification failed, defaulting to general_qa: {e}")
        intent = "general_qa"

    # Safety net: don't let an explicit, unambiguous booking request slip
    # through to the general-Q&A responder just because the classifier
    # missed it or the API call failed.
    if intent == "general_qa" and current_state == State.GENERAL and _looks_like_explicit_booking_request(message):
        intent = "start_booking"

    # Same safety net, for reschedule/contact-detail-change requests —
    # catches bare phrasing like "change email" that the classifier may
    # read as an unrelated general question since it doesn't mention
    # "booking" explicitly.
    if intent == "general_qa" and current_state == State.GENERAL and _looks_like_explicit_reschedule_request(message):
        intent = "start_reschedule"

    # Second safety net: an affirmative "yes"/"sure"/etc. right after the
    # assistant itself offered to set up a call. Covers the same failure
    # mode as above (classifier miss or API glitch) specifically for the
    # "agreeing to an offered call" case, since that's easy to misclassify
    # as a generic "confirm" with no handler in GENERAL state otherwise.
    if (intent in ("general_qa", "confirm") and current_state == State.GENERAL
            and last_assistant_message and _looks_like_call_offer(last_assistant_message)
            and _looks_like_affirmative(message)):
        intent = "start_booking"

    return intent


##############################################################
# Field extraction (LLM-driven, since users won't fill a form)
##############################################################
EXTRACTION_SYSTEM_PROMPT = """Extract booking-relevant fields from the user's
message. Respond with ONLY a JSON object, no other text:

{{"name": "...", "email": "...", "phone": "...", "date_phrase": "... or null",
 "time_phrase": "... or null", "company": "...", "note": "..."}}

Use null for any field not present in the message.

For date_phrase and time_phrase: extract the user's EXACT words for the date
and time, WITHOUT resolving, calculating, or converting them yourself — e.g.
if they say "next Tuesday at 3pm", return date_phrase: "next Tuesday" and
time_phrase: "3pm", unchanged. This also applies to numeric dates like
"7/13/2026", "13-07-26", or "13/7/26" — copy them exactly as written, do NOT
reformat, reorder, or reinterpret the numbers. A separate, deterministic
step handles all date/time conversion. Your only job here is pulling out
the literal phrase the user used.
{expected_field_hint}
"""


_WEEKDAY_NAME_TO_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _resolve_date_structure_via_llm(phrase: str) -> Optional[Dict[str, Any]]:
    """Ask the LLM to interpret (not calculate) a date phrase, returning
    the structured JSON described in DATE_RESOLUTION_SYSTEM_PROMPT, or
    None on any failure (bad JSON, missing/invalid fields, API error) —
    the caller falls back to dateutil in that case rather than crashing.
    """
    try:
        client = get_groq_client()
        response = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[
                {"role": "system", "content": DATE_RESOLUTION_SYSTEM_PROMPT},
                {"role": "user", "content": phrase},
            ],
            temperature=0,
            max_tokens=80,  # small structured JSON object, never shown to the user
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[conversation_manager] Date-phrase LLM resolution failed, "
              f"falling back to dateutil: {e}")
        return None

    if not isinstance(parsed, dict) or parsed.get("kind") not in ("explicit", "weekday", "unresolvable"):
        return None
    return parsed


def _parse_date_phrase(phrase: str, today: datetime.date) -> Optional[datetime.date]:
    """Resolve a natural-language date phrase to an actual date, anchored
    to `today`.

    Interpretation of the phrase (what does the user mean?) is delegated
    to the LLM via _resolve_date_structure_via_llm(), since covering
    every possible phrasing ("next week Thursday", "Thursday of next
    week", "the Monday after next", etc.) with hand-written patterns is
    fundamentally incomplete — there's always another phrasing a rule
    wasn't written for. This was a real, confirmed bug: "next week
    Thursday" was silently resolved to tomorrow's date, because no
    hand-written pattern covered a weekday name COMBINED with a week
    qualifier, and the fallback parser (dateutil) matched only the
    weekday and silently discarded the "next week" part.

    Critically, the LLM is NEVER asked to compute the final date itself
    — it only classifies the phrase into a small structured shape
    (explicit day-count offset / explicit calendar date / weekday name +
    week offset). ALL actual arithmetic (adding days, finding the Nth
    occurrence of a weekday) happens here in plain deterministic Python,
    using `today` as the anchor. This split exists because of a
    previously confirmed bug where letting an LLM do the arithmetic
    directly ("what date is July 15 plus 2 days") silently produced a
    wrong answer (resolved "July 15" to "July 13") — LLMs are unreliable
    at exact calculation, but reliable at classifying/interpreting
    open-ended natural language, which is exactly the split applied here.

    Falls back to the previous dateutil-based parsing (still fully
    deterministic) if the LLM call itself fails for any reason (API
    outage, malformed response) — see _parse_date_phrase_via_dateutil.

    Returns None if the phrase can't be resolved at all (caller should
    re-ask rather than guess).
    """
    if not phrase:
        return None

    structure = _resolve_date_structure_via_llm(phrase)

    if structure is None:
        # LLM call itself failed (network/API error) — fall back to the
        # deterministic dateutil-only path so a Groq outage doesn't take
        # down date parsing entirely. This fallback still won't handle
        # "next week Thursday"-style compound phrases correctly (that's
        # the whole reason the LLM path exists), but it's better than
        # returning nothing for phrases dateutil CAN handle on its own
        # (plain dates, bare weekday names, "tomorrow", etc.).
        return _parse_date_phrase_via_dateutil(phrase, today)

    kind = structure.get("kind")

    if kind == "unresolvable":
        return None

    if kind == "explicit":
        day_offset = structure.get("day_offset")
        if isinstance(day_offset, (int, float)):
            return today + datetime.timedelta(days=int(day_offset))

        explicit_day = structure.get("explicit_day")
        explicit_month = structure.get("explicit_month")
        if isinstance(explicit_day, int) and isinstance(explicit_month, int):
            explicit_year = structure.get("explicit_year")
            year = explicit_year if isinstance(explicit_year, int) else today.year
            try:
                candidate = datetime.date(year, explicit_month, explicit_day)
            except ValueError:
                return None  # e.g. Feb 30 — genuinely invalid, don't guess
            # No year was stated and the resulting date already passed
            # this year (e.g. today=Dec 1, phrase="March 5") — assume
            # next year, the same anchoring convention the old
            # dateutil-based `default=` behavior used.
            if not isinstance(explicit_year, int) and candidate < today:
                candidate = datetime.date(year + 1, explicit_month, explicit_day)
            return candidate

        return None  # malformed structure — neither offset nor explicit date given

    if kind == "weekday":
        weekday_name = structure.get("weekday")
        week_offset = structure.get("week_offset")
        if not isinstance(weekday_name, str) or not isinstance(week_offset, int):
            return None
        target_weekday = _WEEKDAY_NAME_TO_INDEX.get(weekday_name.strip().lower())
        if target_weekday is None:
            return None

        # Deterministic arithmetic: find the next occurrence of
        # target_weekday on/after today, then add week_offset additional
        # weeks. days_until is 0-6 (0 if today itself is that weekday).
        days_until = (target_weekday - today.weekday()) % 7
        return today + datetime.timedelta(days=days_until + 7 * week_offset)

    return None


def _parse_date_phrase_via_dateutil(phrase: str, today: datetime.date) -> Optional[datetime.date]:
    """Deterministic fallback used only when the LLM-based interpretation
    in _parse_date_phrase can't be reached (API failure). Handles plain
    explicit dates, bare weekday names, and simple relative words
    ("today"/"tomorrow"/"yesterday") via dateutil — the same behavior
    this codebase has always relied on for those simpler cases. Does NOT
    attempt to handle compound relative-duration or weekday+qualifier
    phrases ("next week Thursday", "in 2 weeks") correctly; those are
    exactly the cases the LLM path exists to cover properly.
    """
    normalized = phrase.strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)

    if normalized in ("today", "tonight"):
        return today
    if normalized == "tomorrow":
        return today + datetime.timedelta(days=1)
    if normalized == "yesterday":
        return today - datetime.timedelta(days=1)
    if normalized in ("day after tomorrow", "the day after tomorrow"):
        return today + datetime.timedelta(days=2)

    try:
        from dateutil import parser as dateutil_parser
        parsed = dateutil_parser.parse(
            phrase,
            default=datetime.datetime(today.year, today.month, today.day),
            dayfirst=True,
            fuzzy=True,
        )
        return parsed.date()
    except (ValueError, OverflowError):
        return None




def _parse_time_phrase(phrase: str) -> Dict[str, Any]:
    
    if not phrase:
        return {"time": None, "ambiguous_am_pm": False}

    phrase = phrase.strip()

    # Detect whether an AM/PM marker is present in the raw phrase BEFORE
    # parsing, since dateutil will happily resolve "3:30" to 03:30 without
    # telling us it had to guess.
    has_meridiem = bool(re.search(r'\b(am|pm|a\.m\.|p\.m\.)\b', phrase, re.IGNORECASE))

    # Bare hour only ("7", "7pm", "07", "19") — no colon/minutes. dateutil's
    # fuzzy parser is unreliable for this specific shape: given nothing but
    # a lone number it tends to read it as a DAY-OF-MONTH instead of an
    # hour (e.g. "7" parses to today's date with the day replaced by 7,
    # time left at the 00:00 default) — which then silently produces a
    # wrong "did you mean 12:00 AM or PM?" prompt instead of reflecting the
    # hour the user actually typed. Handle this shape directly rather than
    # routing it through dateutil at all.
    bare_hour_match = re.fullmatch(r'(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)?', phrase, re.IGNORECASE)
    if bare_hour_match:
        hour_value = int(bare_hour_match.group(1))
        meridiem = bare_hour_match.group(2)
        if 0 <= hour_value <= 23:
            if meridiem:
                is_pm = meridiem.lower().startswith("p")
                hour_24 = hour_value % 12
                if is_pm:
                    hour_24 += 12
                return {"time": f"{hour_24:02d}:00", "ambiguous_am_pm": False}
            if hour_value == 0 or hour_value > 12:
                # Unambiguous on a 12-hour clock (midnight, or 13-23).
                return {"time": f"{hour_value:02d}:00", "ambiguous_am_pm": False}
            # 1-12 with no meridiem given — genuinely ambiguous.
            return {"time": f"{hour_value:02d}:00", "ambiguous_am_pm": True}

    # An hour of 1-12 written as "H:MM" is ALWAYS ambiguous without a
    # meridiem marker — "3:30" could mean 3:30 AM or 3:30 PM regardless of
    # the colon format used. Only an explicit hour of 13-23 (or "0" for
    # midnight-hour) is unambiguous, since those values are impossible on
    # a 12-hour clock. (Bug fix: an earlier version treated ANY "H:MM"
    # pattern as unambiguous 24-hour style, which incorrectly let "3:30"
    # slip through as if it were unambiguous.)
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\b', phrase)
    hour_unambiguous = False
    if match:
        hour_value = int(match.group(1))
        hour_unambiguous = hour_value == 0 or hour_value > 12

    try:
        from dateutil import parser as dateutil_parser
        parsed = dateutil_parser.parse(phrase, fuzzy=True)
        time_str = parsed.strftime("%H:%M")
    except (ValueError, OverflowError):
        return {"time": None, "ambiguous_am_pm": False}

    ambiguous = not has_meridiem and not hour_unambiguous
    return {"time": time_str, "ambiguous_am_pm": ambiguous}


_EXPECTED_FIELD_HINTS = {
    "name": "\nContext: the bot just asked for the customer's NAME. If the "
             "message is short and doesn't obviously look like an email, "
             "phone, date, or time, treat it as the name.",
    "email": "\nContext: the bot just asked for the customer's EMAIL address.",
    "phone": "\nContext: the bot just asked for the customer's CONTACT NUMBER.",
    "date": "\nContext: the bot just asked what DATE the customer wants to "
            "book. A short reply like \"tomorrow\", \"Monday\", \"July 20\", "
            "or a bare number/date should be treated as date_phrase, even "
            "if it's the entire message with nothing else in it.",
    "time": "\nContext: the bot just asked what TIME the customer wants. A "
            "short reply like \"2pm\", \"14:30\", or \"morning\" should be "
            "treated as time_phrase, even if it's the entire message.",
}


def extract_booking_fields(message: str, expected_field: Optional[str] = None) -> Dict[str, Optional[str]]:
   
    today = datetime.date.today()
    empty = {f: None for f in BOOKING_REQUIRED_FIELDS + BOOKING_OPTIONAL_FIELDS}
    empty["ambiguous_am_pm"] = False

    hint = _EXPECTED_FIELD_HINTS.get(expected_field, "") if expected_field else ""
    system_prompt = EXTRACTION_SYSTEM_PROMPT.format(expected_field_hint=hint)

    try:
        client = get_groq_client()
        response = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=150,  # small JSON object of extracted fields — this is a
                             # defensive cap, unrelated to reply length (this
                             # output is never shown to the user).
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
    except Exception as e:
        import traceback
        print(f"[conversation_manager] Field extraction failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return empty

    result = dict(empty)
    for key in ["name", "email", "phone", "company", "note"]:
        if parsed.get(key):
            result[key] = parsed[key]

    date_phrase = parsed.get("date_phrase")
    if date_phrase:
        resolved_date = _parse_date_phrase(date_phrase, today)
        if resolved_date:
            result["date"] = resolved_date.isoformat()

    time_phrase = parsed.get("time_phrase")
    if time_phrase:
        time_result = _parse_time_phrase(time_phrase)
        if time_result["time"]:
            result["time"] = time_result["time"]
            result["ambiguous_am_pm"] = time_result["ambiguous_am_pm"]

   
    is_short_message = len(message.strip().split()) <= 4
    if expected_field == "date" and not result.get("date") and is_short_message:
        fallback_date = _parse_date_phrase(message.strip(), today)
        if fallback_date:
            result["date"] = fallback_date.isoformat()
    elif expected_field == "time" and not result.get("time") and is_short_message:
        fallback_time = _parse_time_phrase(message.strip())
        if fallback_time["time"]:
            result["time"] = fallback_time["time"]
            result["ambiguous_am_pm"] = fallback_time["ambiguous_am_pm"]

    return result


def missing_required_fields(collected: Dict[str, str]) -> list:
    return [f for f in BOOKING_REQUIRED_FIELDS if not collected.get(f)]


def _validate_single_field(field_key: str, value: str) -> Optional[str]:
    
    if field_key == "name":
        if not NAME_REGEX.match(value.strip()):
            return (
                f"'{value}' doesn't look like a valid name (only letters, "
                f"spaces, apostrophes, hyphens, and periods are allowed). "
                f"Could you re-enter it?"
            )

    elif field_key == "email":
        if not EMAIL_REGEX.match(value.strip()):
            return f"'{value}' doesn't look like a valid email address. Could you re-enter it?"

    elif field_key == "phone":
        try:
            normalized = normalize_phone(value)
            validate_phone_number(normalized)
        except LeadValidationError as e:
            return str(e)

    elif field_key == "date":
        try:
            parsed_date = datetime.datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return "I couldn't understand that date. Could you try again? (e.g. \"tomorrow\", \"July 20\")"

        today = GoogleCalendarHelper.now().date()
        if parsed_date < today:
            return (
                f"{_format_date_display(value)} is in the past. "
                f"Could you give me a future date instead?"
            )
        if (parsed_date - today).days > 30:
            return (
                f"{_format_date_display(value)} is more than 30 days out, "
                f"and we can only book up to 30 days in advance. Could you "
                f"choose a closer date?"
            )
        if parsed_date.weekday() not in GoogleCalendarHelper.BUSINESS_DAYS:
            day_name = parsed_date.strftime("%A")
            return (
                f"{_format_date_display(value)} is a {day_name}, and "
                f"we're only open Monday to Friday. Could you choose a weekday?"
            )

    elif field_key == "time":
        try:
            parsed_time = datetime.datetime.strptime(value, "%H:%M").time()
        except (ValueError, TypeError):
            return "I couldn't understand that time. Could you try again? (e.g. \"2pm\", \"14:30\")"

        hour_minute = parsed_time.hour + parsed_time.minute / 60
        start = GoogleCalendarHelper.BUSINESS_START_HOUR
        end = GoogleCalendarHelper.BUSINESS_END_HOUR
        if not (start <= hour_minute < end):
            return (
                f"That time is outside business hours (9 AM-6 PM PKT, "
                f"Mon-Fri). Could you choose a time in that window?"
            )
        if parsed_time.minute % GoogleCalendarHelper.SLOT_DURATION_MINUTES != 0:
            return (
                "Our call slots start on the hour or half-hour (e.g. 2:00 "
                "or 2:30). Could you adjust to that?"
            )

    return None


def _validate_and_apply_field(session: Session, field_key: str, value: str) -> Optional[str]:
    
    error = _validate_single_field(field_key, value)
    if error:
        return error
    session.collected[field_key] = value
    return None

###############
# Flow handlers
###############
def _handle_general(session: Session, message: str, intent: str) -> str:
    if intent == "start_booking":
        session.state = State.COLLECTING_BOOKING_INFO
        session.collected = {}
        extracted = extract_booking_fields(message)
        session.collected.update({k: v for k, v in extracted.items() if v})
        
        return _ask_for_next_booking_field(session)

    if intent == "start_cancellation":
        session.collected = {}
        booking = _get_known_booking(session)
        if booking is not None:
            session.pending_cancellation_booking = booking
            session.state = State.CONFIRMING_CANCELLATION
            return (
                f"I have your booking on file:\n"
                f"  Booking ID: {booking.get('Booking ID')}\n"
                f"  Date: {_format_date_display(booking.get('Requested Date'))}\n"
                f"  Time: {booking.get('Requested Time', '')}\n\n"
                f"Should I go ahead and cancel it? (yes/no)"
            )
        session.state = State.COLLECTING_CANCELLATION_INFO
        return (
            "Sure I can help you cancel a booking. Could you give me "
            "either your booking ID (e.g. BK-XXXXXXXX) or the email "
            "address you booked with?"
        )

    if intent == "start_reschedule":
        session.collected = {}
        booking = _get_known_booking(session)
        if booking is not None:
            session.pending_reschedule_booking = booking
            session.collected = _prefill_from_booking(booking)
            session.state = State.COLLECTING_RESCHEDULE_NEW_TIME
            return _found_booking_reply(booking)
        session.state = State.COLLECTING_RESCHEDULE_LOOKUP
        return (
            "No problem — I can update your booking. Could "
            "you give me your booking ID (e.g. BK-XXXXXXXX) or the email "
            "address you booked with?"
        )

    # Default: general Q&A via RAG.
    result = generate_answer(message, history=session.history)
    if not result["grounded"] and _should_offer_followup_cta(session):
        result["answer"] += "\n\n" + _next_followup_cta_phrasing(session)
        session.last_cta_turn = len(session.history)
    return result["answer"]


def _handle_general_stream(session: Session, message: str, intent: str):
    """Generator twin of _handle_general(), used only for the streaming
    entry point (handle_message_stream). The one real token-by-token
    generation path here is the RAG branch, via generate_answer_stream();
    the three early-exit intents below return static/templated text with
    nothing to actually stream, so they're each yielded as a single
    "token" event to keep a uniform event shape for callers either way.
    """
    if intent == "start_booking":
        session.state = State.COLLECTING_BOOKING_INFO
        session.collected = {}
        extracted = extract_booking_fields(message)
        session.collected.update({k: v for k, v in extracted.items() if v})

        reply = _ask_for_next_booking_field(session)
        yield {"type": "token", "text": reply}
        yield {"type": "done", "answer": reply}
        return

    if intent == "start_cancellation":
        session.collected = {}
        booking = _get_known_booking(session)
        if booking is not None:
            session.pending_cancellation_booking = booking
            session.state = State.CONFIRMING_CANCELLATION
            reply = (
                f"I have your booking on file:\n"
                f"  Booking ID: {booking.get('Booking ID')}\n"
                f"  Date: {_format_date_display(booking.get('Requested Date'))}\n"
                f"  Time: {booking.get('Requested Time', '')}\n\n"
                f"Should I go ahead and cancel it? (yes/no)"
            )
        else:
            session.state = State.COLLECTING_CANCELLATION_INFO
            reply = (
                "Sure I can help you cancel a booking. Could you give me "
                "either your booking ID (e.g. BK-XXXXXXXX) or the email "
                "address you booked with?"
            )
        yield {"type": "token", "text": reply}
        yield {"type": "done", "answer": reply}
        return

    if intent == "start_reschedule":
        session.collected = {}
        booking = _get_known_booking(session)
        if booking is not None:
            session.pending_reschedule_booking = booking
            session.collected = _prefill_from_booking(booking)
            session.state = State.COLLECTING_RESCHEDULE_NEW_TIME
            reply = _found_booking_reply(booking)
        else:
            session.state = State.COLLECTING_RESCHEDULE_LOOKUP
            reply = (
                "No problem I can update your booking. Could "
                "you give me your booking ID (e.g. BK-XXXXXXXX) or the email "
                "address you booked with?"
            )
        yield {"type": "token", "text": reply}
        yield {"type": "done", "answer": reply}
        return

    # Default: general Q&A via RAG — the one real streaming path, backed
    # by Groq's actual streaming API (see generate_answer_stream).
    full_answer = ""
    grounded = False
    for event in generate_answer_stream(message, history=session.history):
        if event["type"] == "status":
            # Translate rag_engine's internal status keys into the
            # user-facing copy defined near the top of this file, per
            # the workflow-mapping spec ("Knowledge Lookup" section).
            status_text = {
                "retrieving": STATUS_SEARCHING_KB,
                "generating": STATUS_PREPARING_RESPONSE,
            }.get(event["status"])
            if status_text:
                yield {"type": "status", "status": status_text}
        elif event["type"] == "token":
            full_answer += event["text"]
            yield {"type": "token", "text": event["text"]}
        elif event["type"] == "done":
            grounded = event["grounded"]
            # The ungrounded/fallback short-circuit path in
            # generate_answer_stream emits "done" directly without ever
            # emitting a "token" event first — make sure the frontend
            # still gets the fallback text as at least one chunk.
            if not full_answer:
                full_answer = event["answer"]
                yield {"type": "token", "text": full_answer}

    if not grounded and _should_offer_followup_cta(session):
        cta = _next_followup_cta_phrasing(session)
        full_answer += "\n\n" + cta
        yield {"type": "token", "text": "\n\n" + cta}
        session.last_cta_turn = len(session.history)

    yield {"type": "done", "answer": full_answer}


# Minimum number of turns that must pass since we last appended the
# "leave your name and email" fallback CTA before we're willing to show
# it again. Without this, every ungrounded RAG answer in a row would
# re-append the same line, which is what made the bot feel spammy/robotic
# (see bug report: "asking again and again ... should be natural flow").
FOLLOWUP_CTA_COOLDOWN_TURNS = 4

_FOLLOWUP_CTA_PHRASINGS = [
    "In the meantime, would you like to leave your name and email so our "
    "team can follow up directly?",
    "If it'd help, I can pass your name and email to our team so they can "
    "follow up with the details.",
    "Want me to grab your name and email so someone from the team can "
    "get back to you on this?",
]


def _should_offer_followup_cta(session: "Session") -> bool:
    """Only offer the passive lead-capture CTA if we haven't already
    offered it within the last FOLLOWUP_CTA_COOLDOWN_TURNS turns, and not
    on the very first reply of a session."""
    last_turn = getattr(session, "last_cta_turn", None)
    current_turn = len(session.history)

    if current_turn == 0:
        return False
    if last_turn is not None and (current_turn - last_turn) < FOLLOWUP_CTA_COOLDOWN_TURNS:
        return False
    return True


def _next_followup_cta_phrasing(session: "Session") -> str:
    """Rotate phrasing so the fallback CTA doesn't read as a copy-pasted
    line every time it does show up."""
    count = getattr(session, "cta_phrasing_index", 0)
    phrasing = _FOLLOWUP_CTA_PHRASINGS[count % len(_FOLLOWUP_CTA_PHRASINGS)]
    session.cta_phrasing_index = count + 1
    return phrasing


def _ask_for_next_booking_field(session: Session) -> str:
    if session.collected.get("ambiguous_am_pm"):
        # A time was given with no AM/PM marker (e.g. "3:30") and no other
        # way to disambiguate it — asking rather than guessing avoids
        # silently booking someone 12 hours off from what they meant.
        session.collected["ambiguous_am_pm"] = False  
        session.awaiting_am_pm_clarification = True
        pending_time = session.collected.get("time", "")
        try:
            hour, minute = pending_time.split(":")
            readable = f"{int(hour) % 12 or 12}:{minute}"
        except (ValueError, AttributeError):
            readable = pending_time
        return f"Just to confirm did you mean {readable} AM or PM?"

    missing = missing_required_fields(session.collected)
    if not missing:
        session.state = State.CONFIRMING_BOOKING
        return _build_booking_confirmation_prompt(session.collected)

    prompts = {
        "name": "What name should I book this under?",
        "email": "Kindly provide an email address to send your confirmation to?",
        "phone": "And a contact number? (e.g. +1 234 567 8900 or your local format)",
        "date": "What date would you like to book? (e.g. \"tomorrow\", \"July 20\")",
        "time": "What time works for you? (business hours are Mon-Fri, 9 AM-6 PM PKT)",
    }
    
    return prompts[missing[0]]


def _format_time_12h(time_24h: str) -> str:
    try:
        dt = datetime.datetime.strptime(time_24h, "%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return time_24h  


# Every date shown to the user, anywhere in the conversation, should look
# the same — otherwise a booking confirmation might show "2026-07-20"
# while a cancellation summary a few turns later shows "20-07-2026" for
# the exact same date, which reads as inconsistent/buggy even though
# both are "correct" in their own internal representation. This is the
# ONE function responsible for how a date is displayed to a user; every
# user-facing message renders dates through this rather than interpolating
# a raw stored string directly.
#
# Internally, dates live in two different raw formats depending on where
# they came from (session.collected["date"] / booking results use ISO
# "YYYY-MM-DD"; the Google Sheet's "Requested Date" column uses
# "DD-MM-YYYY") — this formatter accepts either and normalizes both to
# the same "DD Month YYYY" display form, e.g. "20 July 2026".
_DISPLAY_DATE_INPUT_FORMATS = ("%Y-%m-%d", "%d-%m-%Y")


def _format_date_display(raw_date: Optional[str]) -> str:
    if not raw_date:
        return ""
    stripped = raw_date.strip()
    for fmt in _DISPLAY_DATE_INPUT_FORMATS:
        try:
            dt = datetime.datetime.strptime(stripped, fmt)
            # Always include the weekday name (e.g. "28 July 2026
            # (Tuesday)"), not just the date. This matters especially for
            # relative-date phrases like "next Tuesday" or "next week
            # Tuesday" — those are genuinely ambiguous in everyday
            # English, and an LLM resolving them can occasionally land on
            # the "wrong" Tuesday relative to what the user meant. Showing
            # the weekday alongside the date lets the user catch a
            # misinterpretation immediately at the confirmation step,
            # instead of only discovering it after the booking is made.
            return dt.strftime("%d %B %Y (%A)")  # e.g. "28 July 2026 (Tuesday)"
        except ValueError:
            continue
    # Unrecognized format (shouldn't normally happen) — show the raw
    # value rather than silently hiding it, so the user still sees
    # SOMETHING rather than a blank field.
    return stripped


def _parse_sheet_date_to_iso(display_date: str) -> str:
    
    try:
        dt = datetime.datetime.strptime(display_date.strip(), "%d-%m-%Y")
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return display_date


def _parse_sheet_time_to_24h(display_time: str) -> str:
    
    try:
        dt = datetime.datetime.strptime(display_time.strip(), "%I:%M %p")
        return dt.strftime("%H:%M")
    except (ValueError, AttributeError):
        return display_time


def _build_booking_confirmation_prompt(collected: Dict[str, str]) -> str:
    lines = [
        "Let's confirm your discovery call details:",
        f"  Name:  {collected.get('name')}",
        f"  Email: {collected.get('email')}",
        f"  Phone: {collected.get('phone')}",
        f"  Date:  {_format_date_display(collected.get('date'))}",
        f"  Time:  {_format_time_12h(collected.get('time', ''))}",
    ]
    if collected.get("company"):
        lines.append(f"  Company: {collected['company']}")
    if collected.get("note"):
        lines.append(f"  Note: {collected['note']}")
    lines.append("\nShall I go ahead and book this? (yes/no)")
    return "\n".join(lines)


def _handle_collecting_booking_info(session: Session, message: str, intent: str) -> str:
    if intent == "abandon":
        session.state = State.GENERAL
        session.collected = {}
        session.awaiting_am_pm_clarification = False
        return "No problem, I've cancelled that booking request. Anything else I can help with?"

    if session.awaiting_am_pm_clarification:
        session.awaiting_am_pm_clarification = False
        is_pm = bool(re.search(r'\bpm\b|\bp\.m\.?\b|evening|afternoon|night', message, re.IGNORECASE))
        is_am = bool(re.search(r'\bam\b|\ba\.m\.?\b|morning', message, re.IGNORECASE))

        if not is_pm and not is_am:
            # Still unclear — ask again rather than guess either way.
            session.awaiting_am_pm_clarification = True
            return "Sorry, could you just reply with AM or PM?"

        current_time = session.collected.get("time", "")
        try:
            hour, minute = current_time.split(":")
            hour = int(hour) % 12  # normalize to 0-11
            if is_pm:
                hour += 12
            resolved_time = f"{hour:02d}:{minute}"
        except (ValueError, AttributeError):
            resolved_time = current_time  # fall through, re-ask for time normally below

        # Now that AM/PM is resolved, validate the FULL time (business
        # hours, slot grid) immediately — this was previously skipped,
        # letting an out-of-hours time slip through until final booking.
        error = _validate_single_field("time", resolved_time)
        if error:
            session.collected.pop("time", None)
            return f"{error}\n\nWhat time works for you?"
        session.collected["time"] = resolved_time
        return _ask_for_next_booking_field(session)

    missing_now = missing_required_fields(session.collected)
    expected = missing_now[0] if missing_now else None
    extracted = extract_booking_fields(message, expected_field=expected)

    if extracted.get("ambiguous_am_pm") and extracted.get("time"):
        # Store fields other than time normally, but hold the time back
        # from validation/storage until AM/PM is confirmed.
        for field_key in ["name", "email", "phone"]:
            value = extracted.get(field_key)
            if not value:
                continue
            error = _validate_single_field(field_key, value)
            if error:
                return error
            session.collected[field_key] = value
        if extracted.get("date"):
            date_error = _validate_single_field("date", extracted["date"])
            if date_error:
                return date_error
            session.collected["date"] = extracted["date"]

        session.collected["time"] = extracted["time"]  # unvalidated guess, held pending confirmation
        session.awaiting_am_pm_clarification = True
        pending_time = extracted["time"]
        try:
            hour, minute = pending_time.split(":")
            readable = f"{int(hour) % 12 or 12}:{minute}"
        except (ValueError, AttributeError):
            readable = pending_time
        return f"Just to confirm did you mean {readable} AM or PM?"

    for field_key in ["name", "email", "phone", "date", "time"]:
        value = extracted.get(field_key)
        if not value:
            continue
        error = _validate_single_field(field_key, value)
        if error:
            return f"{error}"
        session.collected[field_key] = value

    for field_key in ["company", "note"]:
        if extracted.get(field_key):
            session.collected[field_key] = extracted[field_key]

    return _ask_for_next_booking_field(session)


def _handle_confirming_booking(session: Session, message: str, intent: str) -> str:
    if intent == "deny" or intent == "abandon":
        # Per original requirement "user changing booking details midway":
        # go back to collecting rather than fully resetting, so they don't
        # have to re-type everything — just correct what changed.
        session.state = State.COLLECTING_BOOKING_INFO
        return (
            "No problem what would you like to change? "
            "(You can also say \"start over\" to begin again.)"
        )

    if intent == "confirm":
        c = session.collected
        try:
            dt = datetime.datetime.strptime(f"{c['date']} {c['time']}", "%Y-%m-%d %H:%M")
            booking = create_booking(
                name=c["name"], email=c["email"], phone=c["phone"], start_dt=dt,
                company=c.get("company", "") or "", note=c.get("note", "") or "",
            )
            session.state = State.GENERAL
            session.collected = {}
            session.last_known_booking_id = booking["booking_id"]

            email_note = "" if booking.get("email_sent") else (
                "\n(I couldn't send the confirmation email, but your booking "
                "is confirmed, please save your booking ID.)"
            )

            # A completed booking is always a real lead — capture it
            # directly here rather than relying on the passive background
            # scan (capture_lead_if_present), which only fires on GENERAL
            # messages containing an '@' and would otherwise miss leads
            # whose entire conversation stayed inside the booking flow.
            try:
                capture_lead({
                    "name": c["name"],
                    "email": c["email"],
                    "phone": c["phone"],
                    "company": c.get("company") or "",
                    "requirement": c.get("note") or "",
                    "source": "booking_flow",
                    "booking_id": booking["booking_id"],
                })
            except Exception as e:
                print(f"[conversation_manager] Lead capture after booking failed (non-fatal): {e}")

            return (
                f"You're all booked! Your booking ID is {booking['booking_id']}, "
                f"for {_format_date_display(booking['date'])} at {_format_time_12h(booking['time'])} PKT.{email_note}"
            )
        except (LeadValidationError, BookingError) as e:
            # Stay in COLLECTING state so the user can fix the offending
            # field without losing everything else they already gave.
            session.state = State.COLLECTING_BOOKING_INFO
            return f"I couldn't complete that booking: {e}\n\nWhat would you like to change?"

    # Anything else (e.g. they tried to change a field directly instead of
    # saying yes/no) — treat as new info and re-extract.
    extracted = extract_booking_fields(message)
    if any(extracted.values()):
        session.collected.update({k: v for k, v in extracted.items() if v})
        session.state = State.COLLECTING_BOOKING_INFO
        return _ask_for_next_booking_field(session)

    return "Sorry, should I go ahead and book this? (yes/no)"


def _find_booking_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    """Shared lookup used by both cancellation and reschedule flows: try as
    a booking ID first (format BK-XXXXXXXX), then as an email."""
    identifier = identifier.strip()
    if re.match(r"^BK-[A-F0-9]{8}$", identifier, re.IGNORECASE):
        return find_booking_by_id(identifier)
    elif "@" in identifier:
        return find_active_booking_by_email(identifier)
    return None


def _get_known_booking(session: Session) -> Optional[Dict[str, Any]]:
    """If this session has already identified an active booking earlier in
    the conversation — via a completed lookup, a booking just created, or
    a completed reschedule — reuse it instead of asking the user for their
    booking ID/email again for a follow-up change.

    Always re-fetches fresh from the sheet rather than trusting a cached
    dict, since the record may have changed (or been cancelled elsewhere)
    since we last saw it. Returns None — and clears the stale reference —
    if it's no longer a valid, active booking.
    """
    if not session.last_known_booking_id:
        return None
    booking = find_booking_by_id(session.last_known_booking_id)
    if booking is None or booking.get("Status", "").strip() != "Confirmed":
        session.last_known_booking_id = None
        return None
    return booking


def _handle_collecting_cancellation_info(session: Session, message: str, intent: str) -> str:
    if intent == "abandon":
        session.state = State.GENERAL
        return "No problem, I've stopped the cancellation. Anything else I can help with?"

    booking = _find_booking_by_identifier(message)

    if booking is None:
        return (
            "I couldn't find an active booking with that information. "
            "Could you double check your booking ID (format BK-XXXXXXXX) "
            "or the email you used?"
        )

    session.pending_cancellation_booking = booking
    session.state = State.CONFIRMING_CANCELLATION
    session.last_known_booking_id = booking.get("Booking ID")
    return (
        f"I found this booking:\n"
        f"  Booking ID: {booking.get('Booking ID')}\n"
        f"  Date: {_format_date_display(booking.get('Requested Date'))}\n"
        f"  Time: {booking.get('Requested Time', '')}\n\n"
        f"Should I go ahead and cancel it? (yes/no)"
    )


def _handle_confirming_cancellation(session: Session, message: str, intent: str) -> str:
    if intent == "confirm":
        booking = session.pending_cancellation_booking
        try:
            cancelled = cancel_booking(booking["Booking ID"])
            session.state = State.GENERAL
            session.pending_cancellation_booking = None
            session.last_known_booking_id = None

            email_note = "" if cancelled.get("email_sent") else (
                " (I couldn't send the cancellation email, but it's confirmed cancelled.)"
            )
            return f"Your booking ({booking['Booking ID']}) has been cancelled.{email_note}"
        except BookingError as e:
            session.state = State.GENERAL
            session.pending_cancellation_booking = None
            return f"I couldn't cancel that booking: {e}"

    if intent == "deny":
        # This is exactly the gap that caused the original bug: someone
        # says "no, I want to change/reschedule it" instead of just "no".
        # Route them into the reschedule flow with the booking we ALREADY
        # found, rather than dropping back to GENERAL with nothing to do.
        booking = session.pending_cancellation_booking
        session.pending_cancellation_booking = None
        session.pending_reschedule_booking = booking
        session.state = State.COLLECTING_RESCHEDULE_NEW_TIME
        session.collected = {}
        return (
            f"Got it, let's reschedule booking {booking.get('Booking ID')} "
            f"instead of cancelling it. What new date and time would you like?"
        )

    session.state = State.GENERAL
    session.pending_cancellation_booking = None
    return "No problem, I've left that booking as is. Anything else I can help with?"


#################
# Reschedule flow
#################
_SHEET_TO_FIELD = {
    "Lead Name": "name", "Email": "email", "Contact Number": "phone",
    "Company": "company", "Note": "note",
}


def _prefill_from_booking(booking: Dict[str, Any]) -> Dict[str, Any]:
    
    collected = {}
    for sheet_key, field_key in _SHEET_TO_FIELD.items():
        if booking.get(sheet_key):
            collected[field_key] = booking[sheet_key]
    collected["date"] = _parse_sheet_date_to_iso(booking.get("Requested Date", ""))
    collected["time"] = _parse_sheet_time_to_24h(booking.get("Requested Time", ""))
    return collected


def _build_reschedule_summary(original: Dict[str, Any], collected: Dict[str, Any]) -> str:
    
    field_labels = [
        ("name", "Name", lambda b: b.get("Lead Name", "")),
        ("email", "Email", lambda b: b.get("Email", "")),
        ("phone", "Phone", lambda b: b.get("Contact Number", "")),
        ("date", "Date", lambda b: _parse_sheet_date_to_iso(b.get("Requested Date", ""))),
        ("time", "Time", lambda b: _parse_sheet_time_to_24h(b.get("Requested Time", ""))),
    ]
    display_formatters = {
        "date": _format_date_display,
        "time": _format_time_12h,
    }
    lines = ["Here's what will change:"]
    any_change = False
    for key, label, get_original in field_labels:
        old_value = get_original(original)  # now normalized to internal format
        new_value = collected.get(key, old_value)
        formatter = display_formatters.get(key, lambda v: v)
        if str(new_value).strip() != str(old_value).strip():
            lines.append(f"  {label}: {formatter(old_value)} -> {formatter(new_value)}")
            any_change = True

    if not any_change:
        lines = ["No changes detected from the original booking."]
    lines.append("\nShall I go ahead and update this booking? (yes/no)")
    return "\n".join(lines)


def _found_booking_reply(booking: Dict[str, Any]) -> str:
    return (
        f"Found it, booking {booking.get('Booking ID')}:\n"
        f"  Name: {booking.get('Lead Name')}\n"
        f"  Email: {booking.get('Email')}\n"
        f"  Phone: {booking.get('Contact Number')}\n"
        f"  Date: {_format_date_display(booking.get('Requested Date'))}\n"
        f"  Time: {booking.get('Requested Time', '')}\n\n"
        f"What would you like to change? (You can update the date, time, "
        f"email, phone, or name mention anything you'd like to update, "
        f"or say \"that's everything\" once you're done.)"
    )


def _handle_collecting_reschedule_lookup(session: Session, message: str, intent: str) -> str:
    if intent == "abandon":
        session.state = State.GENERAL
        return "No problem, I've stopped the update. Anything else I can help with?"

    booking = _find_booking_by_identifier(message)
    if booking is None:
        return (
            "I couldn't find an active booking with that information. "
            "Could you double check your booking ID (format BK-XXXXXXXX) "
            "or the email you used?"
        )

    session.pending_reschedule_booking = booking
    session.collected = _prefill_from_booking(booking)
    session.state = State.COLLECTING_RESCHEDULE_NEW_TIME
    session.last_known_booking_id = booking.get("Booking ID")
    return _found_booking_reply(booking)


def _handle_collecting_reschedule_new_time(session: Session, message: str, intent: str) -> str:
    if intent == "abandon":
        session.state = State.GENERAL
        session.pending_reschedule_booking = None
        session.collected = {}
        return "No problem, I've stopped the update. Anything else I can help with?"

    if session.awaiting_am_pm_clarification:
        session.awaiting_am_pm_clarification = False
        is_pm = bool(re.search(r'\bpm\b|\bp\.m\.?\b|evening|afternoon|night', message, re.IGNORECASE))
        is_am = bool(re.search(r'\bam\b|\ba\.m\.?\b|morning', message, re.IGNORECASE))
        if not is_pm and not is_am:
            session.awaiting_am_pm_clarification = True
            return "Sorry, could you just reply with AM or PM?"

        current_time = session.collected.get("time", "")
        try:
            hour, minute = current_time.split(":")
            hour = int(hour) % 12
            if is_pm:
                hour += 12
            session.collected["time"] = f"{hour:02d}:{minute}"
        except (ValueError, AttributeError):
            pass
    elif intent != "confirm":
        # Only re-extract if this doesn't look like "I'm done" — otherwise
        # a plain "yes"/"that's everything" could get misread as new field
        # content by the extractor.
        extracted = extract_booking_fields(message)
        for key in ["name", "email", "phone", "company", "note"]:
            if extracted.get(key):
                session.collected[key] = extracted[key]
        if extracted.get("date"):
            session.collected["date"] = extracted["date"]
        if extracted.get("time"):
            session.collected["time"] = extracted["time"]
            session.collected["ambiguous_am_pm"] = extracted.get("ambiguous_am_pm", False)

        if session.collected.get("ambiguous_am_pm"):
            session.collected["ambiguous_am_pm"] = False
            session.awaiting_am_pm_clarification = True
            pending_time = session.collected.get("time", "")
            try:
                hour, minute = pending_time.split(":")
                readable = f"{int(hour) % 12 or 12}:{minute}"
            except (ValueError, AttributeError):
                readable = pending_time
            return f"Just to confirm did you mean {readable} AM or PM?"

    # User can say they're done in many ways ("that's everything", "just
    # that", "confirm", etc.) — the intent classifier's "confirm" catches
    # this, OR we fall through to asking if they want to add anything else,
    # rather than forcing an exact phrase.
    if intent == "confirm":
        session.state = State.CONFIRMING_RESCHEDULE
        return _build_reschedule_summary(session.pending_reschedule_booking, session.collected)

    return "Anything else you'd like to change, or should I go ahead with the update?"


def _handle_confirming_reschedule(session: Session, message: str, intent: str) -> str:
    if intent == "confirm":
        booking = session.pending_reschedule_booking
        c = session.collected
        try:
            dt = datetime.datetime.strptime(f"{c['date']} {c['time']}", "%Y-%m-%d %H:%M")
            result = reschedule_booking(
                booking["Booking ID"], dt,
                name=c.get("name"), email=c.get("email"), phone=c.get("phone"),
                company=c.get("company"), note=c.get("note"),
            )
            session.state = State.GENERAL
            session.pending_reschedule_booking = None
            session.collected = {}
            session.last_known_booking_id = result["booking_id"]

            email_note = "" if result.get("email_sent") else (
                "\n(I couldn't send the confirmation email, but your "
                "updated booking is confirmed.)"
            )

            # Keep the Chatbot_Leads row's contact info in sync with the
            # update. The booking ID itself never changes now (this is an
            # in-place update, not cancel+recreate), so old_booking_id and
            # new_booking_id are the same — this call just refreshes
            # name/email/phone/company/requirement on the existing row.
            #
            # IMPORTANT: this uses `result` (reschedule_booking's actual,
            # fully-merged outcome), NOT `c` (session.collected). `c` only
            # contains the fields the user explicitly mentioned THIS turn
            # — e.g. if they only changed the date/time and didn't restate
            # their email, c.get("email") is None, not their existing
            # email. Passing that through would have wiped/left-stale the
            # Chatbot_Leads row's other fields instead of reflecting the
            # real updated booking. `result` already has every field
            # correctly resolved (old value kept where unchanged, new
            # value applied where changed) inside reschedule_booking()
            # itself, so it's the correct single source of truth here.
            try:
                update_lead_booking(
                    old_booking_id=booking["Booking ID"],
                    new_booking_id=result["booking_id"],
                    name=result.get("lead_name"), email=result.get("email"),
                    phone=result.get("phone"), company=result.get("company"),
                    requirement=result.get("note"),
                    # Fallback identifiers in case the Chatbot_Leads row was
                    # created by the passive capture_lead_if_present() scan
                    # and never had a Booking ID written to it — see the
                    # matching logic in update_lead_booking() for why this
                    # is necessary. `booking` here is the record as it was
                    # BEFORE this edit, so its Email/Contact Number are the
                    # values a pre-existing lead row would still have.
                    old_email=booking.get("Email"),
                    old_phone=booking.get("Contact Number"),
                )
            except Exception as e:
                print(f"[conversation_manager] Lead update after booking edit failed (non-fatal): {e}")

            return (
                f"Done! Your booking ({result['booking_id']}) has been "
                f"updated now set for {_format_date_display(result['date'])} at "
                f"{_format_time_12h(result['time'])} PKT.{email_note}"
            )
        except (LeadValidationError, BookingError) as e:
            # The ORIGINAL booking is untouched if this fails — this is a
            # true in-place update, so a failed update doesn't cancel or
            # lose anything. Send them back to editing rather than fully
            # resetting, so they don't lose everything they'd already
            # specified.
            session.state = State.COLLECTING_RESCHEDULE_NEW_TIME
            return f"I couldn't complete that update: {e}\n\nYour original booking is unchanged. What would you like to change?"

    if intent == "deny":
        session.state = State.COLLECTING_RESCHEDULE_NEW_TIME
        return "No problem what would you like to change instead?"

    session.state = State.GENERAL
    session.pending_reschedule_booking = None
    session.collected = {}
    return "No problem, I've left your original booking as is. Anything else I can help with?"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

# Handlers that support a streaming variant (only GENERAL, since it's the
# only state whose reply can involve real token-by-token generation via
# the RAG path). Every other state's reply is already a fully-formed
# string built synchronously (booking prompts, confirmations, etc.) — for
# those, handle_message_stream below yields the whole reply as one token
# event, since there's nothing to stream progressively.
_NON_STREAMING_HANDLERS = {
    State.COLLECTING_BOOKING_INFO: _handle_collecting_booking_info,
    State.CONFIRMING_BOOKING: _handle_confirming_booking,
    State.COLLECTING_CANCELLATION_INFO: _handle_collecting_cancellation_info,
    State.CONFIRMING_CANCELLATION: _handle_confirming_cancellation,
    State.COLLECTING_RESCHEDULE_LOOKUP: _handle_collecting_reschedule_lookup,
    State.COLLECTING_RESCHEDULE_NEW_TIME: _handle_collecting_reschedule_new_time,
    State.CONFIRMING_RESCHEDULE: _handle_confirming_reschedule,
}


def _prepare_turn(session_id: str, message: str):
    """Shared setup for both handle_message() and handle_message_stream():
    fetch the session, classify intent using the prior assistant turn as
    context, and append the user's message to history. Returns
    (session, intent) — the caller is responsible for appending the
    assistant's reply to history afterward, since streaming callers only
    know the full reply once the stream finishes.
    """
    session = get_session(session_id)

    last_assistant_message = None
    if session.history and session.history[-1]["role"] == "assistant":
        last_assistant_message = session.history[-1]["content"]

    intent = classify_intent(message, session.state, last_assistant_message)
    session.history.append({"role": "user", "content": message})
    return session, intent


def _finish_turn(session: Session, message: str, reply: str) -> None:
    """Shared teardown: record the assistant's reply and run best-effort
    passive lead capture. Shared by both entry points so streaming and
    non-streaming callers behave identically once a reply is complete."""
    session.history.append({"role": "assistant", "content": reply})

    if session.state == State.GENERAL:
        try:
            capture_lead_if_present(message)
        except Exception as e:
            print(f"[conversation_manager] Passive lead capture failed (non-fatal): {e}")


def handle_message(session_id: str, message: str) -> Dict[str, Any]:
    """Main non-streaming entry point — process one user message and
    return a complete response.

    Returns {"reply": str, "state": str} so main.py (FastAPI layer) can
    log/inspect state if useful, without every caller needing to know the
    internal state machine.

    Kept for any caller that doesn't need streaming (e.g. tests, non-HTTP
    callers) — internally it just drains handle_message_stream() and
    reassembles the full text, so the two entry points can never drift in
    behavior.
    """
    reply = ""
    state = None
    for event in handle_message_stream(session_id, message):
        if event["type"] == "done":
            reply = event["answer"]
            state = event["state"]
    return {"reply": reply, "state": state}


def handle_message_stream(session_id: str, message: str):
    """Streaming entry point — a generator yielding progress/status and
    token events as one user message is processed, for the /api/chat/stream
    SSE endpoint in main.py.

    Yields dicts of the form:
        {"type": "status", "status": "<user-facing status text>"}
        {"type": "token",  "text": "<incremental text chunk>"}
        {"type": "done",   "answer": "<full reply text>", "state": "<new state>"}

    Exactly one "done" event is always yielded last, even on error —
    callers (the SSE endpoint) can rely on "done" as the terminal event
    for a turn. Status events are informational only and never change
    handler behavior; they exist purely to give the frontend something
    to display instead of a blank wait.
    """
    session, intent = _prepare_turn(session_id, message)

    for status_text in _initial_statuses_for(session.state, intent):
        yield {"type": "status", "status": status_text}

    reply = ""
    try:
        if session.state == State.GENERAL:
            # The only state with a real streaming (token-by-token) path.
            for event in _handle_general_stream(session, message, intent):
                if event["type"] == "status":
                    yield event
                elif event["type"] == "token":
                    yield event
                elif event["type"] == "done":
                    reply = event["answer"]
        else:
            # Every other state already returns a complete string
            # synchronously — yield it as a single token event so the
            # frontend's rendering path doesn't need two separate code
            # paths for "streamed" vs "instant" replies.
            handler = _NON_STREAMING_HANDLERS[session.state]
            reply = handler(session, message, intent)
            yield {"type": "token", "text": reply}
    except Exception as e:
        # Last line of defense: whatever state we were in, and whatever
        # broke (RAG generation, retrieval, booking/calendar calls,
        # Sheets/Twilio/SMTP failures, etc.), the user must always get a
        # normal chat reply back — never a raw crash or a hung stream.
        # Full detail goes to the terminal for debugging.
        import traceback
        print(f"[conversation_manager] Handler crashed in state "
              f"{session.state} for session {session_id}: {type(e).__name__}: {e}")
        traceback.print_exc()
        reply = (
            "Sorry, I ran into an issue processing that. Could you try "
            "rephrasing, or let me know if you'd like me to connect you "
            "with our team directly?"
        )
        yield {"type": "token", "text": reply}

    _finish_turn(session, message, reply)
    yield {"type": "done", "answer": reply, "state": session.state}


def capture_lead_if_present(message: str) -> None:
    """Best-effort: if a message contains what looks like an email address,
    try to extract a lead from it and log it. Silently does nothing if no
    email is found — this is a passive net, not a required step."""
    email_match = re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", message)
    if not email_match:
        return

    extracted = extract_booking_fields(message)
    if extracted.get("email"):
        capture_lead({
            "name": extracted.get("name") or "Unknown",
            "email": extracted["email"],
            "phone": extracted.get("phone") or "",
            "company": extracted.get("company") or "",
            "requirement": extracted.get("note") or "",
            "source": "chatbot_conversation",
        })