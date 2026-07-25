import os
import re
import json
import datetime
from typing import Dict, Any, Optional

from groq import Groq
from dotenv import load_dotenv

from rag_engine import search_knowledge_base, MAX_RESPONSE_TOKENS
from booking_manager import (
    create_booking, cancel_booking, reschedule_booking, find_active_booking_by_email,
    find_booking_by_id, BookingError, find_any_booking_by_email, find_active_booking_by_phone,
)
from support_ticket_manager import (
    create_ticket, find_ticket_by_id, find_tickets_by_email, TicketError, TicketLimitExceededError,
    CATEGORIES_REQUIRING_EXISTING_BOOKING, Category, update_ticket_status, Status,
    escalate_ticket, find_most_recent_ticket_by_email_and_category,
)
from chatbot_lead_manager import capture_lead
from email_service import send_booking_confirmation
from lead_manager_2 import normalize_phone, LeadValidationError

load_dotenv()

GROQ_MODEL_NAME = os.getenv("GROQ_MODEL_NAME", "llama3-groq-70b-8192-tool-use-preview")
if "tool-use" in GROQ_MODEL_NAME:
    GROQ_MODEL_NAME = "llama-3.3-70b-versatile"

_groq_client = None

def get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not found in environment.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client

def load_prompts() -> Dict[str, str]:
    prompts_path = os.path.join(os.path.dirname(__file__), "prompts.md")
    with open(prompts_path, "r", encoding="utf-8") as f:
        content = f.read()
    variables = {}
    try:
        exec(content, globals(), variables)
    except Exception as e:
        print(f"Error parsing prompts.md: {e}")
    return variables

class Session:
    def __init__(self):
        self.history = []
        self.last_active = datetime.datetime.now()
        self.state = "GENERAL"
        self.collected_data = {}

_SESSIONS: Dict[str, Session] = {}
SESSION_IDLE_TIMEOUT = datetime.timedelta(minutes=15)

GREETING_MESSAGE = (
    "Hi! I'm BrightReach's virtual assistant. I can help with services, "
    "pricing, or booking a free discovery call."
)

def get_greeting(session_id: str = None) -> str:
    return GREETING_MESSAGE

def reset_session(session_id: str) -> None:
    if session_id in _SESSIONS:
        del _SESSIONS[session_id]

def get_session(session_id: str) -> Session:
    now = datetime.datetime.now()
    session = _SESSIONS.get(session_id)
    if session:
        if now - session.last_active > SESSION_IDLE_TIMEOUT:
            session.history = []
            session.state = "GENERAL"
            session.collected_data = {}
        session.last_active = now
    else:
        session = Session()
        _SESSIONS[session_id] = session
    return session

def resolve_date(date_json: dict) -> datetime.date:
    now = datetime.datetime.now().date()
    kind = date_json.get("kind")
    if kind == "explicit":
        if date_json.get("day_offset") is not None:
            return now + datetime.timedelta(days=int(date_json["day_offset"]))
        else:
            y = date_json.get("explicit_year") or now.year
            m = date_json.get("explicit_month") or now.month
            d = date_json.get("explicit_day") or now.day
            return datetime.date(int(y), int(m), int(d))
    elif kind == "weekday":
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        wd_str = str(date_json.get("weekday", "")).lower()
        if wd_str not in weekdays:
            raise ValueError("Invalid weekday")
        target_wd = weekdays.index(wd_str)
        current_wd = now.weekday()
        days_ahead = target_wd - current_wd
        if days_ahead <= 0:
            days_ahead += 7
        week_offset = int(date_json.get("week_offset") or 0)
        days_ahead += 7 * week_offset
        return now + datetime.timedelta(days=days_ahead)
    else:
        raise ValueError("unresolvable")

def llm_json_call(system_prompt: str, user_prompt: str) -> dict:
    client = get_groq_client()
    try:
        res = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print(f"LLM JSON Error: {e}")
        return {}

def classify_intent(session: Session, message: str, prompts: dict) -> str:
    sys_prompt = prompts.get("INTENT_SYSTEM_PROMPT", "")
    sys_prompt += f"\n\nCURRENT_STATE: {session.state}"
    history_text = "\n".join([f"{m['role']}: {m['content']}" for m in session.history[-5:]])
    user_prompt = f"CONVERSATION:\n{history_text}\n\nLATEST MESSAGE: {message}"
    data = llm_json_call(sys_prompt, user_prompt)
    return data.get("intent", "general_qa")

def generate_followup(session: Session, missing: list, category: str, prompts: dict) -> str:
    sys_prompt = prompts.get("FOLLOWUP_SYSTEM_PROMPT", "")
    history_text = "\n".join([f"{m['role']}: {m['content']}" for m in session.history[-4:]])
    user_prompt = f"CATEGORY: {category}\nSTILL_NEEDED: {missing}\nCONVERSATION:\n{history_text}"
    data = llm_json_call(sys_prompt, user_prompt)
    fallback_q = f"I still need a few details: {', '.join(missing)}"
    return data.get("question", fallback_q)

def handle_booking_flow(session: Session, message: str, prompts: dict):
    yield {"type": "status", "status": "Processing booking details..."}
    
    # Handle confirmation step if we are already showing preview
    if session.collected_data.get("booking_previewed") and not session.collected_data.get("booking_confirmed"):
        eval_sys = (
            "The user is viewing a booking preview. Categorize their response into one of the following:\n"
            "- 'confirm': The user agrees and wants to proceed.\n"
            "- 'deny': The user wants to start over completely.\n"
            "- 'abandon': The user wants to cancel the booking process.\n"
            "- 'modify': The user wants to change some details (e.g., 'fix the date to 28-08-2026', 'change the email', 'make it 3 PM instead').\n"
            "Output JSON: {'action': 'confirm'|'deny'|'abandon'|'modify'}"
        )
        action_data = llm_json_call(eval_sys, f"LATEST MESSAGE: {message}")
        action = action_data.get("action", "confirm")
        
        if action == "confirm":
            session.collected_data["booking_confirmed"] = True
        elif action == "deny":
            ans = "Okay, let's start over. What time and date would you prefer?"
            session.collected_data.pop("date_phrase", None)
            session.collected_data.pop("time_phrase", None)
            session.collected_data.pop("booking_previewed", None)
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return
        elif action == "abandon":
            ans = "Okay, I've cancelled the booking process."
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return
        elif action == "modify":
            ext_sys = prompts.get("EXTRACTION_SYSTEM_PROMPT", "").replace("{expected_field_hint}", "")
            data = llm_json_call(ext_sys, f"LATEST MESSAGE: {message}")
            for k, v in data.items():
                if v is not None and str(v).lower() != "null" and str(v).strip():
                    session.collected_data[k] = str(v)
            session.collected_data.pop("booking_previewed", None)
            # Let it fall through to re-validate and preview again
        else:
            ans = "I didn't quite catch that. Should I go ahead and confirm the booking?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

    # Extract info if not yet previewed
    if not session.collected_data.get("booking_previewed"):
        ext_sys = prompts.get("EXTRACTION_SYSTEM_PROMPT", "").replace("{expected_field_hint}", "")
        
        user_prompt = f"LATEST MESSAGE: {message}"
        data = llm_json_call(ext_sys, user_prompt)
        
        for k, v in data.items():
            if v is not None and str(v).lower() != "null" and str(v).strip():
                session.collected_data[k] = str(v)
                
        if session.collected_data.get("email"):
            existing = find_any_booking_by_email(session.collected_data["email"])
            if existing:
                if not session.collected_data.get("name") and existing.get("Lead Name"):
                    session.collected_data["name"] = existing["Lead Name"]
                if not session.collected_data.get("phone") and existing.get("Contact Number"):
                    session.collected_data["phone"] = existing["Contact Number"]

    # Determine what's missing, asking for them one at a time.
    missing_one = []
    if not session.collected_data.get("name"):
        missing_one = ["name"]
    elif not session.collected_data.get("email"):
        missing_one = ["email"]
    elif not session.collected_data.get("phone"):
        missing_one = ["phone number"]
    elif not session.collected_data.get("date_phrase"):
        missing_one = ["date (Note: Our business hours are Mon-Fri, 9am to 6pm)"]
    elif not session.collected_data.get("time_phrase"):
        missing_one = ["time (Note: Our business hours are Mon-Fri, 9am to 6pm)"]

    if missing_one:
        q = generate_followup(session, missing_one, "booking", prompts)
        session.history.append({"role": "assistant", "content": q})
        yield {"type": "token", "text": q}
        yield {"type": "done", "answer": q, "state": session.state}
        return

    # Resolve date and time
    date_sys = (
        f"You are an AI date resolver. The current date is {datetime.datetime.now().strftime('%Y-%m-%d, %A')}. "
        "Resolve the user's date phrase into a strict YYYY-MM-DD format. "
        "Take into account phrases like 'next week tuesday' (meaning the tuesday of next week). "
        "Output JSON exactly: {'date': 'YYYY-MM-DD'} or {'error': true} if it cannot be resolved."
    )
    date_user = f"Phrase: {session.collected_data['date_phrase']}"
    date_data = llm_json_call(date_sys, date_user)
    
    try:
        if "date" not in date_data:
            raise ValueError("unresolvable")
        resolved_date = datetime.datetime.strptime(date_data["date"], "%Y-%m-%d").date()
        
        time_res_sys = (
            "Analyze the given time phrase. "
            "If the time clearly indicates AM or PM (e.g., '5 PM', '5am', '15:00', '2 in the afternoon'), output JSON: {'time': 'HH:MM'} in 24-hour format. "
            "If it's just a number without AM/PM context like '3' or '5', output JSON: {'ambiguous': true}. "
            "Do NOT output ambiguous if the user explicitly provided AM, PM, or a 24-hour time."
        )
        time_data = llm_json_call(time_res_sys, f"Phrase: {session.collected_data['time_phrase']}")
        
        if time_data.get("ambiguous"):
            ans = "Did you mean AM or PM?"
            session.collected_data.pop("time_phrase", None)
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return
            
        time_str = time_data.get("time", "09:00")
        
        dt_str = f"{resolved_date.strftime('%Y-%m-%d')} {time_str}"
        start_dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        
        # Preview step
        if not session.collected_data.get("booking_previewed"):
            session.collected_data["booking_previewed"] = True
            
            parsed_time = datetime.datetime.strptime(time_str, "%H:%M")
            time_formatted = parsed_time.strftime("%I:%M %p").lstrip("0")
            
            ans = (f"Please confirm your details:\n\n"
                   f"Name: {session.collected_data['name']}\n"
                   f"Email: {session.collected_data['email']}\n"
                   f"Phone: {session.collected_data['phone']}\n"
                   f"Date: {resolved_date.strftime('%d-%m-%Y')}\n"
                   f"Time: {time_formatted}\n\n"
                   f"Should I proceed with the booking?")
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        # If previewed and confirmed
        if session.collected_data.get("booking_confirmed"):
            yield {"type": "status", "status": "Confirming your booking..."}
            res = create_booking(
                name=session.collected_data["name"],
                email=session.collected_data["email"],
                phone=session.collected_data["phone"],
                start_dt=start_dt,
                company=session.collected_data.get("company", ""),
                note=session.collected_data.get("note", "")
            )
            
            # Append to leadsheet
            try:
                capture_lead({
                    "name": session.collected_data["name"],
                    "email": session.collected_data["email"],
                    "phone": session.collected_data["phone"],
                    "company": session.collected_data.get("company", ""),
                    "note": session.collected_data.get("note", ""),
                    "booking_id": res['booking_id'],
                })
            except Exception as e:
                print(f"Failed to add lead to Chatbot_Leads: {e}")
            
            ans = f"Booking successful! Your ID is {res['booking_id']}."
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            
    except Exception as e:
        ans = f"Sorry, I had trouble finalizing the booking time or date: {str(e)}. Let's try specifying the date and time again."
        session.collected_data.pop("date_phrase", None)
        session.collected_data.pop("time_phrase", None)
        session.collected_data.pop("booking_previewed", None)
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}

def _handle_ticket_limit_exceeded(e) -> str:
    """Shared handling for TicketLimitExceededError, used at every
    create_ticket() call site: per policy, do NOT create another row for
    the same underlying problem once the (email, category) cap is hit —
    instead escalate the most recent EXISTING ticket in that category to
    High priority (escalate_ticket() actually sets Priority to High, not
    just the escalation flag, so "moved to highest priority" below is
    literally true) and notify the team, the same way any other
    escalation does.
    """
    existing_ticket = find_most_recent_ticket_by_email_and_category(e.email, e.category)
    if existing_ticket is not None:
        try:
            escalate_ticket(
                existing_ticket["Ticket ID"],
                reason=f"Limit reached: {e.count} tickets in this category from this email.",
            )
            return (
                f"You've reached the limit of support tickets for this category "
                f"({e.limit}). I've moved your existing issue (ticket "
                f"{existing_ticket['Ticket ID']}) to the highest priority "
                f"expect a response within 4-6 hours."
            )
        except TicketError as inner_e:
            print(f"[conversation_manager] Escalating existing ticket after limit hit failed: {inner_e}")
            return (
                f"You've reached the limit of support tickets for this category. "
                f"I couldn't update the existing ticket automatically ({inner_e}), "
                f"but I've flagged this for our team directly."
            )
    # Defensive fallback — shouldn't happen (the limit itself implies at
    # least MAX_TICKETS_PER_EMAIL_CATEGORY tickets already exist), but
    # never leave the user with no response if the lookup comes up empty.
    return (
        f"You've reached the limit of support tickets for this category. "
        f"I've flagged this for our team directly expect a response within 4-6 hours."
    )


def handle_ticket_flow(session: Session, message: str, prompts: dict):
    """Entry point for support ticket creation.

    Order of operations (each stage is fully LLM-driven — no regex/keyword
    matching anywhere in this pipeline):

    1. **Understand the issue first** (`_handle_issue_capture_flow`) — get
       the category and a clear description of what's actually wrong
       before asking the user anything about their account. Asking "have
       you booked with us before?" before you even know what they're
       reporting reads as robotic and out of order.
    2. **Verify, but only when the category needs it** — only categories
       that presuppose an existing relationship with the agency
       (Technical Support, General Complaint, Appointment Support — see
       CATEGORIES_REQUIRING_EXISTING_BOOKING in support_ticket_manager.py)
       trigger `_handle_customer_verification_flow`. Everything else
       (General Inquiry, Service Request, Feature Request, Human
       Assistance, Payment/Invoice) skips straight to collection — those
       are legitimately open to someone who's never booked anything.
       Appointment Support is a special case within verification itself:
       since a report about an appointment is inherently about an
       existing booking, that flow skips the "are you an existing
       customer?" question and asks directly for booking ID/email.
    3. **Finish collection** (`_handle_ticket_collection_flow`) — if
       verification found a booking, name/email/phone are already loaded
       into `session.collected_data`, so this stage is told exactly what's
       already known and only asks about whatever's genuinely still
       missing. It never re-asks for something already on file.

    The existing sub-flows (resend confirmation, offer-booking-instead) still
    work exactly as before — they're checked first, ahead of all three
    stages above, since they can be entered mid-flow.
    """
    yield {"type": "status", "status": "Processing support ticket..."}
    yield from _route_ticket_flow(session, message, prompts)


def _route_ticket_flow(session: Session, message: str, prompts: dict):
    """The actual staging logic for handle_ticket_flow, split out so later
    stages can hand off to the next one without re-emitting the "Processing
    support ticket..." status event."""
    if session.collected_data.get("ticket_flow_mode") == "resend":
        yield from _handle_resend_confirmation_flow(session, message, prompts)
        return

    if session.collected_data.get("ticket_flow_mode") == "offer_booking_instead":
        yield from _handle_offer_booking_instead_flow(session, message, prompts)
        return

    if session.collected_data.get("ticket_flow_mode") == "service_pitch":
        yield from _handle_service_pitch_flow(session, message, prompts)
        return

    # Stage 1: understand the issue (category + description) before asking
    # anything about the user's booking history.
    if not session.collected_data.get("issue_captured"):
        yield from _handle_issue_capture_flow(session, message, prompts)
        return

    # Stage 1.5: if this is a request for something new, or a change to how
    # an existing feature/process works, get the user's own suggestion for
    # how it should be improved before moving on — makes for a far more
    # actionable ticket than the complaint alone.
    if session.collected_data.get("wants_feature_or_improvement") and not session.collected_data.get("suggestion_captured"):
        yield from _handle_feature_suggestion_flow(session, message, prompts)
        return

    # Stage 2: only verify existing-customer status for categories where
    # the report presupposes a prior relationship with the agency.
    needs_verification = session.collected_data.get("category") in CATEGORIES_REQUIRING_EXISTING_BOOKING
    if needs_verification and session.collected_data.get("verification_stage") != "verified":
        yield from _handle_customer_verification_flow(session, message, prompts)
        return

    # Stage 3: fill in any remaining contact details, preview, confirm, submit.
    yield from _handle_ticket_collection_flow(session, message, prompts)


def _handle_customer_verification_flow(session: Session, message: str, prompts: dict):
    """LLM-driven customer verification before creating a support ticket.
    
    Determines whether the user has previously booked with the agency, collects
    their booking ID or email if they are an existing customer, looks up the
    booking in the Booking sheet, and either proceeds with ticket creation
    (booking found), redirects to booking flow (new customer whose issue is
    about one of the agency's own services), or lets the ticket proceed as a
    new/guest customer (new customer whose issue isn't about an agency
    service — e.g. a general complaint or a request unrelated to booking a
    service). Booking-not-found allows retry with different credentials.

    Appointment Support tickets skip the "are you an existing customer?"
    question entirely — a report about an appointment is inherently about
    an existing booking, so this flow goes straight to asking for their
    booking ID or email instead.
    
    Uses the CUSTOMER_VERIFICATION_PROMPT — no regex, no keyword matching,
    no hardcoded yes/no or service-relatedness checks. The LLM drives the
    entire conversation, including whether a new customer's issue is about
    an agency service.
    """
    verification_prompt = prompts.get("CUSTOMER_VERIFICATION_PROMPT", "")
    if not verification_prompt:
        # Fallback if prompt not found: proceed to ticket without verification
        yield from _route_ticket_flow(session, message, prompts)
        return

    stage = session.collected_data.get("verification_stage")
    current_question = session.collected_data.get("verification_current_question", "")

    # Determine what to do based on the current verification stage
    if stage == "verified":
        # Verification already complete — proceed to ticket collection
        yield from _route_ticket_flow(session, message, prompts)
        return

    # Appointment-related tickets (no-show, missing confirmation email,
    # can't book, booking ID not found, etc.) are inherently about an
    # existing booking — there's no real "are you an existing customer?"
    # question to ask. Skip straight to requesting their booking ID or
    # email instead of the usual ask_existing_customer step. From there
    # on, the normal LLM-driven lookup_booking / retry_credentials flow
    # below takes over exactly as it would for any other category.
    if stage is None and session.collected_data.get("category") == Category.APPOINTMENT_SUPPORT:
        msg = "Sure could you share the email address or booking ID for your appointment so I can pull up your details?"
        session.collected_data["verification_stage"] = "awaiting_credentials"
        session.collected_data["verification_current_question"] = msg
        session.history.append({"role": "assistant", "content": msg})
        yield {"type": "token", "text": msg}
        yield {"type": "done", "answer": msg, "state": session.state}
        return

    # Build input for the LLM
    history_text = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in session.history[-6:]])
    user_prompt = (
        f"CONVERSATION HISTORY:\n{history_text}\n\n"
        f"CURRENT_QUESTION: {current_question}\n\n"
        f"LATEST MESSAGE: {message}"
    )
    data = llm_json_call(verification_prompt, user_prompt)

    action = data.get("action", "clarify")
    follow_up = data.get("follow_up_message", "")
    is_existing = data.get("is_existing_customer")
    booking_id = data.get("booking_id")
    email = data.get("email")

    if action == "ask_existing_customer":
        # First step: ask if they've booked before
        session.collected_data["verification_stage"] = "awaiting_existing_customer"
        session.collected_data["verification_current_question"] = follow_up
        session.history.append({"role": "assistant", "content": follow_up})
        yield {"type": "token", "text": follow_up}
        yield {"type": "done", "answer": follow_up, "state": session.state}
        return

    if action == "clarify":
        # User's response was ambiguous — re-ask naturally
        session.collected_data["verification_current_question"] = follow_up
        session.history.append({"role": "assistant", "content": follow_up})
        yield {"type": "token", "text": follow_up}
        yield {"type": "done", "answer": follow_up, "state": session.state}
        return

    if action == "provide_credentials_prompt":
        # User said they ARE an existing customer — ask for booking ID or email
        session.collected_data["verification_stage"] = "awaiting_credentials"
        session.collected_data["verification_current_question"] = follow_up
        session.history.append({"role": "assistant", "content": follow_up})
        yield {"type": "token", "text": follow_up}
        yield {"type": "done", "answer": follow_up, "state": session.state}
        return

    if action == "lookup_booking":
        # User provided credentials — look them up
        yield {"type": "status", "status": "Looking up your booking..."}
        
        lookup_id = booking_id or ""
        lookup_email = email or ""
        
        booking = None
        if lookup_id:
            booking = find_booking_by_id(lookup_id)
        elif lookup_email:
            booking = find_any_booking_by_email(lookup_email)
        
        if booking:
            # Booking found! Load details into session context
            session.collected_data["verification_stage"] = "verified"
            session.collected_data["verification_booking_id"] = booking.get("Booking ID", "")
            session.collected_data["verification_email"] = booking.get("Email", "")
            session.collected_data["verification_name"] = booking.get("Lead Name", "")
            session.collected_data["verification_phone"] = booking.get("Contact Number", "")
            
            # Pre-fill ticket fields from booking
            session.collected_data["name"] = booking.get("Lead Name", "")
            session.collected_data["email"] = booking.get("Email", "")
            if booking.get("Contact Number"):
                session.collected_data["phone"] = booking.get("Contact Number", "")
            session.collected_data["related_booking_id"] = booking.get("Booking ID", "")
            
            # Now proceed to ticket collection (the actual support ticket creation flow)
            yield from _route_ticket_flow(session, message, prompts)
            return
        else:
            # Booking not found — inform and allow retry
            msg = follow_up if follow_up else (
                f"I checked our records but couldn't find a booking"
                f"{' with ID ' + lookup_id if lookup_id else ''}"
                f"{' under ' + lookup_email if lookup_email else ''}."
                f" Could you double-check and try a different email or booking ID?"
            )
            session.collected_data["verification_stage"] = "awaiting_credentials"
            session.collected_data["verification_current_question"] = msg
            session.collected_data.pop("verification_booking_id", None)
            session.collected_data.pop("verification_email", None)
            session.history.append({"role": "assistant", "content": msg})
            yield {"type": "token", "text": msg}
            yield {"type": "done", "answer": msg, "state": session.state}
            return

    if action == "retry_credentials":
        # Booking lookup failed — allow retry with different credentials
        msg = follow_up or "I wasn't able to find a booking with that information. Could you try a different email or booking ID?"
        session.collected_data["verification_stage"] = "awaiting_credentials"
        session.collected_data["verification_current_question"] = msg
        session.history.append({"role": "assistant", "content": msg})
        yield {"type": "token", "text": msg}
        yield {"type": "done", "answer": msg, "state": session.state}
        return

    if action == "redirect_to_booking":
        # User has never booked, and what they're describing is something one
        # of the agency's services actually solves. Jumping straight to "what
        # name should I book this under?" asks for commitment before the user
        # has been told what they'd be committing to. Instead, hand off to the
        # service-pitch step: explain the relevant service and its pricing
        # (grounded in the knowledge base), THEN offer the discovery call.
        session.collected_data["verification_stage"] = "new_customer"
        yield from _handle_service_pitch_flow(session, message, prompts)
        return

    if action == "proceed_to_ticket":
        # User is verified — proceed to ticket collection
        session.collected_data["verification_stage"] = "verified"
        yield from _route_ticket_flow(session, message, prompts)
        return

    # Fallback: should never reach here, but guard against unexpected actions
    session.collected_data["verification_current_question"] = follow_up or "Could you tell me a bit more about that?"
    session.history.append({"role": "assistant", "content": session.collected_data["verification_current_question"]})
    yield {"type": "token", "text": session.collected_data["verification_current_question"]}
    yield {"type": "done", "answer": session.collected_data["verification_current_question"], "state": session.state}


# The service pitch has to fit "what the service covers" + "what it costs" +
# a closing offer into one message, so it needs a slightly larger budget than
# the 110-token cap used for ordinary Q&A replies. Still small enough to stay
# a chat message rather than a brochure.
MAX_PITCH_TOKENS = 220

# How many follow-up questions about the service we'll answer before simply
# putting the discovery-call offer back on the table plainly, so the loop
# can't run forever.
MAX_SERVICE_PITCH_ROUNDS = 3


def _generate_service_pitch_text(session: Session, prompts: dict, context: str,
                                 topic: str, service_name: str, is_followup: bool) -> Optional[str]:
    """Single grounded generation used by both the initial pitch and any
    follow-up question about the service. Returns None if generation fails,
    so the caller can fall back rather than showing an empty message."""
    sys_prompt = prompts.get("SERVICE_PITCH_PROMPT", "")
    if not sys_prompt:
        return None

    history_text = "\n".join(
        [f"{m['role'].capitalize()}: {m['content']}" for m in session.history[-6:]]
    )
    user_msg = (
        f"CONTEXT:\n{context}\n\n"
        f"MATCHED_SERVICE: {service_name or 'unknown'}\n"
        f"IS_FOLLOW_UP: {is_followup}\n"
        f"USER_NEED: {topic}\n\n"
        f"CONVERSATION HISTORY:\n{history_text}"
    )

    client = get_groq_client()
    try:
        res = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=MAX_PITCH_TOKENS,
        )
        choice = res.choices[0]
        reply = _ensure_complete_reply(
            choice.message.content, getattr(choice, "finish_reason", None)
        )
    except Exception as e:
        print(f"[service_pitch] Generation failed: {e}")
        return None

    return reply.strip() if reply and reply.strip() else None


def _handle_service_pitch_flow(session: Session, message: str, prompts: dict):
    """Sits between "this person has never booked with us" and the booking
    flow itself.

    Reached only from the verification step's `redirect_to_booking` action —
    i.e. a brand-new prospect whose problem is something the agency actually
    sells a service for. Rather than immediately asking for a name and a
    time slot, this stage:

      1. works out WHICH service fits what they described
         (`SERVICE_MATCH_PROMPT` — LLM-driven, no keyword matching),
      2. retrieves that service's own copy from the knowledge base and
         summarises what it covers and what it costs
         (`SERVICE_PITCH_PROMPT`, grounded strictly in the retrieved
         context — pricing is never invented),
      3. only then offers the free discovery call.

    The user's reply is classified into accept / decline / question:
      - accept   -> hand off to the real booking flow,
      - decline  -> resume the normal ticket flow so their issue is still
                    logged for the team rather than dropped,
      - question -> answer it from the knowledge base and re-offer the call
                    (capped at MAX_SERVICE_PITCH_ROUNDS so it can't loop).
    """
    # --- Phase B: the pitch is already on screen; this is their answer to it.
    if session.collected_data.get("service_pitch_delivered"):
        yield from _handle_service_pitch_reply(session, message, prompts)
        return

    # --- Phase A: work out the service, retrieve its copy, deliver the pitch.
    yield {"type": "status", "status": "Finding the right service..."}

    topic = session.collected_data.get("note") or message
    history_text = "\n".join(
        [f"{m['role'].capitalize()}: {m['content']}" for m in session.history[-6:]]
    )

    match_sys = prompts.get("SERVICE_MATCH_PROMPT", "")
    match = llm_json_call(
        match_sys, f"CONVERSATION HISTORY:\n{history_text}\n\nUSER_NEED: {topic}"
    ) if match_sys else {}

    service_name = str(match.get("service_name") or "").strip()
    retrieval_query = str(match.get("retrieval_query") or "").strip()
    if not retrieval_query:
        retrieval_query = f"{service_name or topic} service details, what's included and pricing"

    yield {"type": "status", "status": "Searching knowledge base..."}
    context = search_knowledge_base(retrieval_query)

    yield {"type": "status", "status": "Drafting response..."}
    reply = _generate_service_pitch_text(
        session, prompts, context, topic, service_name, is_followup=False
    )

    if not reply:
        # Generation or retrieval failed — fall back to the old direct
        # hand-off rather than dead-ending the conversation.
        session.state = "BOOKING"
        session.collected_data = {}
        reply = ("No problem at all let's get you started with a free discovery call. "
                 "What name should I book this under?")
        session.history.append({"role": "assistant", "content": reply})
        yield {"type": "token", "text": reply}
        yield {"type": "done", "answer": reply, "state": session.state}
        return

    session.collected_data["ticket_flow_mode"] = "service_pitch"
    session.collected_data["service_pitch_delivered"] = True
    session.collected_data["service_pitch_service"] = service_name
    session.collected_data["service_pitch_rounds"] = 0
    session.history.append({"role": "assistant", "content": reply})
    yield {"type": "token", "text": reply}
    yield {"type": "done", "answer": reply, "state": session.state}


def _handle_service_pitch_reply(session: Session, message: str, prompts: dict):
    """Classifies the user's response to the service pitch and routes it."""
    action_sys = (
        "The user was just given a short overview of one of a digital marketing "
        "agency's services — what it covers and what it costs — and was asked "
        "whether they'd like to book a free discovery call about it. "
        "Classify their latest reply into exactly one of:\n"
        "- 'accept': they want the discovery call (yes, sure, sounds good, "
        "let's do it, book it, when are you free, etc.).\n"
        "- 'decline': they don't want a call right now (no thanks, not yet, "
        "just email me, I'll think about it).\n"
        "- 'question': they're asking something else first — about the service, "
        "the pricing, timelines, what's included, or anything else — rather "
        "than answering yes or no.\n"
        "Output ONLY JSON: {\"action\": \"accept\"|\"decline\"|\"question\"}"
    )
    data = llm_json_call(action_sys, f"LATEST MESSAGE: {message}")
    action = data.get("action", "question")

    if action == "accept":
        # Hand off to the real booking flow, carrying over anything we
        # already know so they're not asked for it twice.
        known_email = session.collected_data.get("email", "")
        session.state = "BOOKING"
        session.collected_data = {"email": known_email} if known_email else {}
        ans = "Great — what name should I book this under?"
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    if action == "decline":
        # Don't drop them: their issue is still worth logging so the team
        # can follow up. Mark verification settled so the ticket flow goes
        # straight to collecting contact details instead of looping back
        # into "have you booked with us before?".
        session.collected_data.pop("ticket_flow_mode", None)
        session.collected_data["verification_stage"] = "verified"
        yield from _route_ticket_flow(session, message, prompts)
        return

    # --- 'question': answer it from the knowledge base, then re-offer.
    rounds = int(session.collected_data.get("service_pitch_rounds") or 0) + 1
    session.collected_data["service_pitch_rounds"] = rounds

    if rounds > MAX_SERVICE_PITCH_ROUNDS:
        ans = ("Happy to go deeper on any of this the quickest way is a free "
               "discovery call where we can look at your setup properly. "
               "Would you like me to book one in?")
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    yield {"type": "status", "status": "Searching knowledge base..."}
    context = search_knowledge_base(message)

    yield {"type": "status", "status": "Drafting response..."}
    reply = _generate_service_pitch_text(
        session, prompts, context, message,
        session.collected_data.get("service_pitch_service", ""),
        is_followup=True,
    )

    if not reply:
        reply = ("I don't have that detail to hand, but it's exactly the kind of "
                 "thing we can cover on a free discovery call. Would you like me "
                 "to book one in?")

    session.history.append({"role": "assistant", "content": reply})
    yield {"type": "token", "text": reply}
    yield {"type": "done", "answer": reply, "state": session.state}


def _handle_issue_capture_flow(session: Session, message: str, prompts: dict):
    """Stage 1 of ticket creation: figure out the category and get a clear
    description of the issue — BEFORE asking anything about the user's
    account or booking history. This runs first specifically so the
    conversation follows a natural order: understand what's wrong, THEN
    (if the category calls for it) check whether they've booked with us
    before, rather than interrogating someone about their account before
    they've even said what the problem is.

    Entirely LLM-driven: category comes from TICKET_CATEGORY_SYSTEM_PROMPT,
    and whether the issue description is clear enough to log comes from
    TICKET_COLLECTION_SYSTEM_PROMPT (now scoped to just the issue, not
    contact info) — no keyword/regex checks.

    Category classification runs AFTER the issue is understood (i.e. once
    we have a real `note`), not off the raw first message. A first message
    like "I'm facing an issue with my website" gives the category
    classifier almost nothing to work with — and because the category is
    cached for the rest of the flow, classifying it that early risks
    permanently locking in a generic category (e.g. general_inquiry) even
    after the user's very next message reveals what's actually wrong. That
    silently skips the existing-customer verification step some categories
    are supposed to trigger, so it has to be classified from the
    understood issue, not the opening message.
    """
    issue_sys = prompts.get("TICKET_COLLECTION_SYSTEM_PROMPT", "")
    is_first_response = not session.collected_data.get("ticket_llm_called")
    session.collected_data["ticket_llm_called"] = True
    history_text = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in session.history[-6:]])
    user_prompt = f"FIRST_RESPONSE: {is_first_response}\nCONVERSATION HISTORY:\n{history_text}"
    data = llm_json_call(issue_sys, user_prompt)

    extracted = data.get("extracted_data", {})
    note = extracted.get("note")
    if note is not None and str(note).lower() != "null" and str(note).strip():
        session.collected_data["note"] = str(note).strip()

    # Track whether this is fundamentally an ask for something new, or a
    # change/improvement to how an existing feature/process works — this
    # drives Stage 1.5 below (feature-suggestion flow), independent of
    # whatever business category the ticket ultimately gets classified
    # into. Once set true on either round, it stays true.
    wants_improvement = data.get("wants_feature_or_improvement")
    if wants_improvement is True:
        session.collected_data["wants_feature_or_improvement"] = True

    # Only ONE clarifying question about the issue itself is ever asked —
    # mirrors the original behavior, where a vague first message ("I'm
    # having an issue with X") earns one follow-up, and whatever the user
    # says next is accepted as the issue description. Without this cap the
    # model can keep chasing more specific detail (exact metrics, root
    # cause, etc.) that isn't actually needed just to log a ticket.
    #
    # Exception: when this is fundamentally a feature/improvement request,
    # always take that one clarifying turn on the FIRST response, even if
    # the LLM judged the issue itself clear — so a bare "the cancellation
    # process is very lengthy" always gets one "what makes it that way?"
    # exchange before Stage 1.5 asks how to improve it, never on the same
    # turn. This is purely about turn sequencing, not about re-deciding
    # whether it's a feature request — that judgment stays with the LLM.
    force_issue_followup = is_first_response and session.collected_data.get("wants_feature_or_improvement")

    if is_first_response and (force_issue_followup or not data.get("issue_clear") or not session.collected_data.get("note")):
        q = data.get("follow_up_question") or "Could you tell me a bit more about what's making that difficult for you?"
        session.history.append({"role": "assistant", "content": q})
        yield {"type": "token", "text": q}
        yield {"type": "done", "answer": q, "state": session.state}
        return

    if not session.collected_data.get("note"):
        # We're past the one allowed clarifying round and still have no
        # usable description (e.g. extraction genuinely failed) — fall
        # back to logging exactly what the user typed rather than asking
        # a third time.
        session.collected_data["note"] = message.strip() if message and message.strip() else "No further detail provided."

    # Issue is understood now — classify its category from the full,
    # synthesized description rather than whatever the user happened to
    # type in this single turn.
    if not session.collected_data.get("category"):
        cat_sys = prompts.get("TICKET_CATEGORY_SYSTEM_PROMPT", "")
        cat_data = llm_json_call(cat_sys, f"LATEST MESSAGE: {session.collected_data['note']}")

        if cat_data.get("missing_confirmation_email"):
            session.collected_data["ticket_flow_mode"] = "resend"
            yield from _handle_resend_confirmation_flow(session, message, prompts)
            return

        session.collected_data["category"] = cat_data.get("category") or "general_inquiry"

    # Issue understood — hand off to the next stage (verification, if the
    # category needs it, otherwise straight to contact-info collection).
    session.collected_data["issue_captured"] = True
    yield from _route_ticket_flow(session, message, prompts)


def _handle_feature_suggestion_flow(session: Session, message: str, prompts: dict):
    """Stage 1.5 of ticket creation: runs only when Stage 1 determined the
    user is asking for something new, or a change to how an existing
    feature/process works (`wants_feature_or_improvement`). Before moving
    on to verification/collection, ask for the user's own idea of how it
    should be improved — a ticket that says "the rescheduling process is
    lengthy" is far less actionable than one that also captures what a
    better version would look like.

    Same one-round pattern as issue capture: FEATURE_SUGGESTION_SYSTEM_PROMPT
    either finds a suggestion the user already gave, or asks for one exactly
    once, then moves on regardless — never a repeated interrogation.
    """
    sys_prompt = prompts.get("FEATURE_SUGGESTION_SYSTEM_PROMPT", "")
    is_first_response = not session.collected_data.get("feature_suggestion_llm_called")
    session.collected_data["feature_suggestion_llm_called"] = True
    history_text = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in session.history[-6:]])
    user_prompt = f"FIRST_RESPONSE: {is_first_response}\nCONVERSATION HISTORY:\n{history_text}"
    data = llm_json_call(sys_prompt, user_prompt)

    # Backstop for a misclassification upstream: if Stage 1 set
    # wants_feature_or_improvement on what is really a fault in a service
    # deliverable (a missing field on the user's site, an underperforming
    # ad), the suggestion prompt flags it here rather than asking the user
    # to design the fix. Clear the flag and hand straight back to routing,
    # which then proceeds to verification/collection like any other issue.
    if data.get("not_a_feature_request"):
        session.collected_data["wants_feature_or_improvement"] = False
        session.collected_data["suggestion_captured"] = True
        yield from _route_ticket_flow(session, message, prompts)
        return

    extracted = data.get("extracted_data", {})
    suggestion = extracted.get("suggestion")
    if suggestion is not None and str(suggestion).lower() != "null" and str(suggestion).strip():
        session.collected_data["suggestion"] = str(suggestion).strip()

    if is_first_response and (not data.get("suggestion_clear") or not session.collected_data.get("suggestion")):
        q = data.get("follow_up_question") or "How would you suggest we improve this?"
        session.history.append({"role": "assistant", "content": q})
        yield {"type": "token", "text": q}
        yield {"type": "done", "answer": q, "state": session.state}
        return

    if not session.collected_data.get("suggestion"):
        session.collected_data["suggestion"] = message.strip() if message and message.strip() else "No specific suggestion provided."

    # Fold the suggestion into the existing `note` field so it flows
    # straight into the ticket without changing the ticket schema.
    if session.collected_data.get("note"):
        session.collected_data["note"] = f"{session.collected_data['note']} Suggested improvement: {session.collected_data['suggestion']}"
    else:
        session.collected_data["note"] = f"Suggested improvement: {session.collected_data['suggestion']}"

    session.collected_data["suggestion_captured"] = True
    yield from _route_ticket_flow(session, message, prompts)


def _handle_ticket_collection_flow(session: Session, message: str, prompts: dict):
    """Stage 3 (final stage) of ticket creation: fill in any contact
    details (name, email, phone) still missing, then preview, confirm, and
    submit. By the time this runs, the category and issue description are
    already known (stage 1), and — for categories that required it —
    verification has already run (stage 2), which may have pre-filled
    name/email/phone from an existing booking.

    This stage tells the LLM exactly what's already known via
    ALREADY_KNOWN, so it only ever asks about whatever's genuinely still
    missing — it never re-asks for a name or phone that's already on file,
    even though the user never typed it themselves. That decision is left
    entirely to the LLM (TICKET_CONTACT_INFO_SYSTEM_PROMPT); this function
    just supplies the known state, it doesn't hardcode which fields to
    skip.
    """
    if session.collected_data.get("ticket_previewed"):
        confirm_sys = (
            "The user is viewing a support ticket confirmation preview. Classify "
            "their reply. Output ONLY JSON: {'confirmed': true/false/null} "
            "(null if genuinely ambiguous and needs to be asked again, false if they say no/stop or provide new details)."
        )
        confirm_data = llm_json_call(confirm_sys, f"LATEST MESSAGE: {message}")
        confirmed = confirm_data.get("confirmed")

        if confirmed is False:
            session.collected_data["ticket_previewed"] = False
            ans = "Okay, please let me know what needs to be corrected, or provide the new details."
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        if confirmed is None:
            ans = "Just to confirm should I go ahead and submit your support ticket, yes or no?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

    if not session.collected_data.get("ticket_previewed"):
        # Try to auto-fill phone/name/related_booking_id if email is known —
        # covers guest flows where no explicit verification ran (category
        # didn't require it) but the email the user gave happens to match
        # an existing booking anyway.
        if session.collected_data.get("email"):
            existing_booking = find_any_booking_by_email(session.collected_data["email"])
            if existing_booking:
                if not session.collected_data.get("name") and existing_booking.get("Lead Name"):
                    session.collected_data["name"] = existing_booking["Lead Name"]
                if not session.collected_data.get("phone") and existing_booking.get("Contact Number"):
                    session.collected_data["phone"] = existing_booking["Contact Number"]
                if not session.collected_data.get("related_booking_id"):
                    session.collected_data["related_booking_id"] = existing_booking.get("Booking ID", "")

        if not session.collected_data.get("name") or not session.collected_data.get("email"):
            already_known = {
                "name": session.collected_data.get("name") or None,
                "email": session.collected_data.get("email") or None,
                "phone": session.collected_data.get("phone") or None,
            }
            contact_sys = prompts.get("TICKET_CONTACT_INFO_SYSTEM_PROMPT", "")
            is_first_contact_response = not session.collected_data.get("contact_llm_called")
            session.collected_data["contact_llm_called"] = True
            history_text = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in session.history[-6:]])
            user_prompt = (
                f"FIRST_RESPONSE: {is_first_contact_response}\n"
                f"ALREADY_KNOWN: {json.dumps(already_known)}\n"
                f"CONVERSATION HISTORY:\n{history_text}"
            )
            data = llm_json_call(contact_sys, user_prompt)

            extracted = data.get("extracted_data", {})
            if extracted:
                for k, v in extracted.items():
                    if v is not None and str(v).lower() != "null" and str(v).strip():
                        session.collected_data[k] = str(v)

            if not data.get("ready_to_submit") or not session.collected_data.get("name") or not session.collected_data.get("email"):
                q = data.get("follow_up_question")
                if not q:
                    # Fallback in case LLM forgets to provide a question
                    if not session.collected_data.get("name"):
                        q = "Could you please provide your name so I can log this for you?"
                    elif not session.collected_data.get("email"):
                        q = "Could you please provide your email address so I can log this for you?"

                session.history.append({"role": "assistant", "content": q})
                yield {"type": "token", "text": q}
                yield {"type": "done", "answer": q, "state": session.state}
                return

        session.collected_data["ticket_previewed"] = True
        preview_text = (
            f"Please confirm if these details are correct:\n\n"
            f"Name: {session.collected_data.get('name')}\n"
            f"Email: {session.collected_data.get('email')}\n"
            f"Issue: {session.collected_data.get('note')}\n\n"
            f"Should I go ahead and submit this ticket?"
        )
        session.history.append({"role": "assistant", "content": preview_text})
        yield {"type": "token", "text": preview_text}
        yield {"type": "done", "answer": preview_text, "state": session.state}
        return
        
    yield {"type": "status", "status": "Submitting ticket..."}
    try:
        res = create_ticket(
            name=session.collected_data["name"],
            email=session.collected_data["email"],
            phone=session.collected_data.get("phone", ""),
            category=session.collected_data["category"],
            subcategory=session.collected_data.get("subcategory", ""),
            description=session.collected_data["note"],
            booking_id=session.collected_data.get("related_booking_id", ""),
            chat_history=session.history,
        )
        priority = res.get('Priority', 'Medium')
        time_estimate = "We will get back to you within 24 hours."
        if priority == "High":
            time_estimate = "We will get back to you within 4-6 hours."
        elif priority == "Low":
            time_estimate = "We will get back to you within 48 hours."
            
        ans = f"Ticket created successfully! Your ticket ID is {res['Ticket ID']}. {time_estimate}"
        session.state = "GENERAL"
        session.collected_data = {}
    except TicketLimitExceededError as e:
        ans = _handle_ticket_limit_exceeded(e)
        session.state = "GENERAL"
        session.collected_data = {}
    except TicketError as e:
        ans = f"I couldn't submit that: {str(e)}"
    except Exception as e:
        ans = f"Sorry, there was an error creating your ticket: {str(e)}"
        
    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def _resend_dict_from_booking(booking: Dict[str, Any]) -> Dict[str, Any]:
    """Converts a Sheets-shaped booking record (capitalized keys) into the
    lowercase-key shape send_booking_confirmation() expects."""
    return {
        "booking_id": booking.get("Booking ID", ""),
        "lead_name": booking.get("Lead Name", ""),
        "email": booking.get("Email", ""),
        "date": booking.get("Requested Date", ""),
        "time": booking.get("Requested Time", ""),
    }


def _resolve_booking_for_resend(session: Session, message: str, prompts: dict) -> Optional[Dict[str, Any]]:
    """Tries, in order: an identifier already collected this sub-flow, a
    booking ID in this message, an email in this message, then a phone
    number in this message. Only ACTIVE (Confirmed) bookings count —
    resending a confirmation for an already-cancelled booking makes no
    sense. LLM-driven extraction (no regex) via the same
    EXTRACTION_SYSTEM_PROMPT used everywhere else in this file.
    """
    booking_id = session.collected_data.get("resend_booking_id")
    if booking_id:
        booking = find_booking_by_id(booking_id)
        if booking and booking.get("Status", "").strip() == "Confirmed":
            return booking

    ext_sys = prompts.get("EXTRACTION_SYSTEM_PROMPT", "").replace("{expected_field_hint}", "")
    data = llm_json_call(ext_sys, f"LATEST MESSAGE: {message}")

    candidate_id = data.get("booking_id") or data.get("identifier")
    if candidate_id and str(candidate_id).strip().lower() not in ("null", ""):
        booking = find_booking_by_id(str(candidate_id).strip())
        if booking and booking.get("Status", "").strip() == "Confirmed":
            return booking

    email = data.get("email")
    if email and str(email).strip().lower() not in ("null", ""):
        booking = find_active_booking_by_email(str(email).strip())
        if booking:
            return booking

    phone = data.get("phone")
    if phone and str(phone).strip().lower() not in ("null", ""):
        try:
            booking = find_active_booking_by_phone(normalize_phone(str(phone).strip()))
            if booking:
                return booking
        except LeadValidationError:
            pass

    return None


def _handle_resend_confirmation_flow(session: Session, message: str, prompts: dict):
    """Sub-flow for "I never got my booking confirmation email": resolve
    the booking, actually RESEND the email, ask whether it arrived this
    time, and only file a ticket if the resend itself failed or the user
    says it still didn't come through — never a generic ticket for
    something that has a real, better fix available.
    """
    stage = session.collected_data.get("resend_stage")

    if stage == "awaiting_receipt_confirmation":
        confirm_sys = (
            "The user was just asked whether they received a resent "
            "confirmation email. Classify their reply. "
            "Output ONLY JSON: {'received': true/false/null} "
            "(null if genuinely ambiguous and needs to be asked again)."
        )
        confirm_data = llm_json_call(confirm_sys, f"LATEST MESSAGE: {message}")
        received = confirm_data.get("received")

        if received is True:
            ans = "Great, glad that's sorted! Anything else I can help with?"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        if received is False:
            booking = find_booking_by_id(session.collected_data.get("resend_booking_id", ""))
            yield {"type": "status", "status": "Logging this for our team..."}
            try:
                res = create_ticket(
                    name=booking.get("Lead Name", "") if booking else "",
                    email=booking.get("Email", "") if booking else "",
                    phone=booking.get("Contact Number", "") if booking else "",
                    category=Category.APPOINTMENT_SUPPORT,
                    subcategory="no_booking_confirmation_email",
                    description=(
                        f"Customer never received the booking confirmation email for "
                        f"{session.collected_data.get('resend_booking_id', 'their booking')}. "
                        f"Customer confirmed the resent email still did not arrive."
                    ),
                    booking_id=session.collected_data.get("resend_booking_id", ""),
                    chat_history=session.history,
                )
                ans = (
                    f"I'm sorry that didn't work I've flagged this for our team "
                    f"directly, reference {res['Ticket ID']}."
                )
            except TicketLimitExceededError as e:
                ans = _handle_ticket_limit_exceeded(e)
            except TicketError as e:
                ans = f"I'm sorry that didn't work, and I couldn't log a ticket either: {e}"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        ans = "Just to confirm did the resent confirmation email arrive, yes or no?"
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    # No identifier resolved yet (or first turn) — try to resolve one now.
    yield {"type": "status", "status": "Looking up your booking..."}
    booking = _resolve_booking_for_resend(session, message, prompts)

    if booking is None:
        ans = (
            "I'm sorry you didn't get that let's sort it out. Could you "
            "share the email address or phone number the booking was made "
            "with, so I can resend it?"
        )
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    session.collected_data["resend_booking_id"] = booking.get("Booking ID", "")

    yield {"type": "status", "status": "Resending your confirmation email..."}
    email_result = send_booking_confirmation(_resend_dict_from_booking(booking))

    if email_result.get("sent"):
        session.collected_data["resend_stage"] = "awaiting_receipt_confirmation"
        ans = (
            f"I'm sorry about that I've resent your confirmation email to "
            f"{booking.get('Email', '')}. Please check your spam/junk folder "
            f"as well. Did you receive it this time?"
        )
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    # The resend call itself failed outright (SMTP issue, etc.) — this IS
    # a case that needs a human, so go straight to a ticket rather than
    # asking a confirmation question about an email we know didn't send.
    yield {"type": "status", "status": "Logging this for our team..."}
    try:
        res = create_ticket(
            name=booking.get("Lead Name", ""),
            email=booking.get("Email", ""),
            phone=booking.get("Contact Number", ""),
            category=Category.APPOINTMENT_SUPPORT,
            subcategory="no_booking_confirmation_email",
            description=(
                f"Customer never received the booking confirmation email for "
                f"{booking.get('Booking ID', '')}. Resend attempt failed: "
                f"{email_result.get('error', 'unknown error')}."
            ),
            booking_id=booking.get("Booking ID", ""),
            chat_history=session.history,
        )
        ans = f"I'm sorry that didn't work I've flagged this for our team directly, reference {res['Ticket ID']}."
    except TicketLimitExceededError as e:
        ans = _handle_ticket_limit_exceeded(e)
    except TicketError as e:
        ans = f"I'm sorry, the resend failed and I couldn't log a ticket either: {e}"
    session.state = "GENERAL"
    session.collected_data = {}
    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def _handle_offer_booking_instead_flow(session: Session, message: str, prompts: dict):
    """Sub-flow for when a Technical Support / General Complaint ticket's
    email had no matching booking record. Accept -> hand off to the real
    booking flow (pre-filling the email already given). Decline -> resume
    the normal ticket flow, asking for name directly (email is already
    known) rather than permanently refusing — the person may have booked
    under a different email, or the issue may still be real either way.
    """
    action_sys = (
        "The user was just asked whether they'd like to book a discovery call, "
        "since no booking was found under their email. Classify their reply. "
        "Output ONLY JSON: {'accepted': true/false}"
    )
    action_data = llm_json_call(action_sys, f"LATEST MESSAGE: {message}")

    if action_data.get("accepted"):
        known_email = session.collected_data.get("email", "")
        session.state = "BOOKING"
        session.collected_data = {"email": known_email} if known_email else {}
        ans = "Great — what name should I book this under?"
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    # Declined — resume the ticket, but stop re-triggering this same
    # redirect (the lookup-done flag is already set from before) and
    # clear the sub-flow mode so the normal required-fields check runs.
    session.collected_data.pop("ticket_flow_mode", None)
    ans = "No problem could I get your name so I can log this for our team?"
    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def handle_cancellation_flow(session: Session, message: str, prompts: dict):
    yield {"type": "status", "status": "Processing cancellation..."}

    # "Cancel" is ambiguous between a BOOKING (an appointment) and a
    # TICKET (a support request already filed) — determined once via an
    # LLM call (not a regex), using recent history for context (e.g. if
    # the assistant just listed the user's tickets, "cancel my second
    # one" clearly means a ticket).
    if not session.collected_data.get("cancel_target"):
        history_text = "\n".join(f"{m['role']}: {m['content']}" for m in session.history[-5:])
        target_sys = (
            "The user wants to cancel something. Determine whether they mean "
            "a BOOKING (a discovery call / appointment they scheduled) or a "
            "TICKET (a support request or complaint they previously filed, "
            "often referenced by a ticket ID like 'TKT-XXXXXXXX', or by its "
            "position in a list the assistant just showed, e.g. 'my second "
            "one'). Read the recent conversation for context. "
            "Output ONLY JSON: {'target': 'booking'|'ticket'}"
        )
        target_data = llm_json_call(target_sys, f"RECENT CONVERSATION:\n{history_text}\n\nLATEST MESSAGE: {message}")
        session.collected_data["cancel_target"] = target_data.get("target", "booking")

    if session.collected_data.get("cancel_target") == "ticket":
        yield from _handle_cancel_ticket_flow(session, message, prompts)
        return

    ext_sys = "Extract any booking ID or email address from the message. Output JSON: {'identifier': '...'}"
    data = llm_json_call(ext_sys, f"Message: {message}")
    id_val = data.get("identifier")

    if not session.collected_data.get("identifier") and id_val:
        session.collected_data["identifier"] = id_val

    if not session.collected_data.get("identifier"):
        ans = "Could you please provide your booking ID or the email address you used to book?"
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    # Confirm-before-cancel: once we've found the booking, show its
    # details and ask for an explicit yes before actually cancelling —
    # this matches the same preview-then-confirm pattern the booking flow
    # itself already uses, and avoids an accidental one-shot cancellation.
    if session.collected_data.get("cancel_preview_shown"):
        confirm_sys = (
            "The user is viewing a cancellation confirmation preview. "
            "Classify their reply. Output ONLY JSON: {'confirmed': true/false/null} "
            "(null if genuinely ambiguous and needs to be asked again)."
        )
        confirm_data = llm_json_call(confirm_sys, f"LATEST MESSAGE: {message}")
        confirmed = confirm_data.get("confirmed")

        if confirmed is False:
            ans = "No problem, I've left your booking as is. Anything else I can help with?"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        if confirmed is None:
            ans = "Just to confirm should I go ahead and cancel this booking, yes or no?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        booking_id = session.collected_data.get("cancel_booking_id", "")
        try:
            b = cancel_booking(booking_id)
            ans = (
                f"Booking {b['Booking ID']} has been successfully cancelled.\n\n"
                f"Name: {b.get('Lead Name', '')}\n"
                f"Email: {b.get('Email', '')}"
            )
        except (BookingError, Exception) as e:
            ans = f"Error cancelling booking: {str(e)}"
        session.state = "GENERAL"
        session.collected_data = {}
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    ident = session.collected_data["identifier"]
    try:
        if '@' in ident:
            b = find_active_booking_by_email(ident)
        else:
            b = find_booking_by_id(ident)

        if not b or b.get("Status", "").strip() != "Confirmed":
            ans = f"No active booking found for '{ident}'."
            session.state = "GENERAL"
            session.collected_data = {}
        else:
            session.collected_data["cancel_booking_id"] = b["Booking ID"]
            session.collected_data["cancel_preview_shown"] = True
            ans = (
                f"Just to confirm you'd like to cancel booking {b['Booking ID']} "
                f"for {b.get('Requested Date', '')} at {b.get('Requested Time', '')}. "
                f"Shall I go ahead? (yes/no)"
            )
    except Exception as e:
        ans = f"Error looking up that booking: {str(e)}"
        session.state = "GENERAL"
        session.collected_data = {}

    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def _handle_cancel_ticket_flow(session: Session, message: str, prompts: dict):
    """Withdraw/cancel a previously-filed ticket — a genuinely missing
    feature. Since a ticket represents a reported issue rather than a
    reservation, "cancelling" it means closing it out at the customer's
    own request (distinct from a human resolving it) — recorded as such
    in the Resolution Notes so a reviewer can tell the difference.
    """
    if session.collected_data.get("cancel_ticket_preview_shown"):
        confirm_sys = (
            "The user is viewing a ticket-withdrawal confirmation. Classify "
            "their reply. Output ONLY JSON: {'confirmed': true/false/null} "
            "(null if genuinely ambiguous and needs to be asked again)."
        )
        confirm_data = llm_json_call(confirm_sys, f"LATEST MESSAGE: {message}")
        confirmed = confirm_data.get("confirmed")

        if confirmed is False:
            ans = "No problem, I've left that ticket open. Anything else I can help with?"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        if confirmed is None:
            ans = "Just to confirm should I go ahead and withdraw this ticket, yes or no?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        ticket_id = session.collected_data.get("cancel_ticket_id", "")
        try:
            update_ticket_status(ticket_id, Status.CLOSED)
            ans = f"Done — ticket {ticket_id} has been withdrawn and closed."
        except TicketError as e:
            ans = f"Sorry, I couldn't withdraw that: {e}"
        session.state = "GENERAL"
        session.collected_data = {}
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    ext_sys = (
        "Extract a ticket ID (format TKT-XXXXXXXX) or an email address from "
        "the message, if present. Output JSON: {'ticket_id': '...'|null, 'email': '...'|null}"
    )
    data = llm_json_call(ext_sys, f"LATEST MESSAGE: {message}")
    ticket_id = data.get("ticket_id")
    email = data.get("email")

    if ticket_id and str(ticket_id).strip().lower() != "null":
        session.collected_data["cancel_ticket_id_hint"] = str(ticket_id).strip()
    if email and str(email).strip().lower() != "null":
        session.collected_data["cancel_ticket_email_hint"] = str(email).strip()

    ticket = None
    if session.collected_data.get("cancel_ticket_id_hint"):
        ticket = find_ticket_by_id(session.collected_data["cancel_ticket_id_hint"])
    elif session.collected_data.get("cancel_ticket_email_hint"):
        matches = find_tickets_by_email(session.collected_data["cancel_ticket_email_hint"])
        if len(matches) == 1:
            ticket = matches[0]
        elif len(matches) > 1:
            lines = "\n".join(f"  {t.get('Ticket ID')} — {t.get('Category')} ({t.get('Status')})" for t in matches)
            ans = f"I found a few tickets under that email:\n{lines}\n\nWhich ticket ID would you like to withdraw?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

    if ticket is None:
        ans = "Sure what's the ticket ID you'd like to withdraw? It looks like TKT-XXXXXXXX."
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    if ticket.get("Status", "").strip() in ("Closed", "Resolved"):
        ans = f"Ticket {ticket.get('Ticket ID')} is already {ticket.get('Status')}, so there's nothing to withdraw."
        session.state = "GENERAL"
        session.collected_data = {}
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    session.collected_data["cancel_ticket_id"] = ticket.get("Ticket ID", "")
    session.collected_data["cancel_ticket_preview_shown"] = True
    ans = (
        f"Just to confirm you'd like to withdraw ticket {ticket.get('Ticket ID')} "
        f"({ticket.get('Category')}, currently {ticket.get('Status')}). Shall I go ahead? (yes/no)"
    )
    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def _resolve_date_time_phrase(date_phrase: str, time_phrase: str):
    """Shared date/time resolution — the exact same LLM-driven logic
    handle_booking_flow already uses for a NEW booking, reused here for
    reschedule so both flows behave identically. Returns
    (start_dt, ambiguous_am_pm: bool) — on ambiguity, start_dt is None
    and the caller should re-prompt for AM/PM.
    """
    date_sys = (
        f"You are an AI date resolver. The current date is {datetime.datetime.now().strftime('%Y-%m-%d, %A')}. "
        "Resolve the user's date phrase into a strict YYYY-MM-DD format. "
        "Take into account phrases like 'next week tuesday' (meaning the tuesday of next week). "
        "Output JSON exactly: {'date': 'YYYY-MM-DD'} or {'error': true} if it cannot be resolved."
    )
    date_data = llm_json_call(date_sys, f"Phrase: {date_phrase}")
    if "date" not in date_data:
        raise ValueError("unresolvable date")
    resolved_date = datetime.datetime.strptime(date_data["date"], "%Y-%m-%d").date()

    time_res_sys = (
        "Analyze the given time phrase. "
        "If the time clearly indicates AM or PM (e.g., '5 PM', '5am', '15:00', '2 in the afternoon'), output JSON: {'time': 'HH:MM'} in 24-hour format. "
        "If it's just a number without AM/PM context like '3' or '5', output JSON: {'ambiguous': true}. "
        "Do NOT output ambiguous if the user explicitly provided AM, PM, or a 24-hour time."
    )
    time_data = llm_json_call(time_res_sys, f"Phrase: {time_phrase}")
    if time_data.get("ambiguous"):
        return None, True

    time_str = time_data.get("time", "09:00")
    dt_str = f"{resolved_date.strftime('%Y-%m-%d')} {time_str}"
    return datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M"), False


def handle_reschedule_flow(session: Session, message: str, prompts: dict):
    yield {"type": "status", "status": "Processing reschedule..."}

    # Step 1: identify the existing booking, if not already known.
    if not session.collected_data.get("reschedule_booking_id"):
        ext_sys = "Extract any booking ID or email address from the message. Output JSON: {'identifier': '...'}"
        data = llm_json_call(ext_sys, f"Message: {message}")
        id_val = data.get("identifier")
        if id_val and str(id_val).strip().lower() != "null":
            session.collected_data["reschedule_identifier"] = str(id_val).strip()

        ident = session.collected_data.get("reschedule_identifier")
        if not ident:
            ans = "Sure, I can help reschedule. Could you give me your booking ID or the email address you booked with?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        booking = find_active_booking_by_email(ident) if "@" in ident else find_booking_by_id(ident)
        if not booking or booking.get("Status", "").strip() != "Confirmed":
            ans = f"I couldn't find an active booking for '{ident}'. Could you double check it?"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        session.collected_data["reschedule_booking_id"] = booking["Booking ID"]
        session.collected_data["reschedule_old_date"] = booking.get("Requested Date", "")
        session.collected_data["reschedule_old_time"] = booking.get("Requested Time", "")
        ans = (
            f"Found booking {booking['Booking ID']} for {booking.get('Requested Date', '')} "
            f"at {booking.get('Requested Time', '')}. What new date and time would you like instead?"
        )
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    # Step 2: preview + confirm, once we have both a booking and a
    # proposed new date/time.
    if session.collected_data.get("reschedule_previewed"):
        confirm_sys = (
            "The user is viewing a reschedule confirmation preview. Classify "
            "their reply. Output ONLY JSON: {'confirmed': true/false/null} "
            "(null if genuinely ambiguous and needs to be asked again)."
        )
        confirm_data = llm_json_call(confirm_sys, f"LATEST MESSAGE: {message}")
        confirmed = confirm_data.get("confirmed")

        if confirmed is False:
            ans = "No problem, I've left your original booking as is. Anything else I can help with?"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        if confirmed is None:
            ans = "Just to confirm should I go ahead and update your booking, yes or no?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        yield {"type": "status", "status": "Updating your booking..."}
        try:
            new_dt = datetime.datetime.strptime(
                f"{session.collected_data['reschedule_new_date']} {session.collected_data['reschedule_new_time']}",
                "%Y-%m-%d %H:%M",
            )
            res = reschedule_booking(session.collected_data["reschedule_booking_id"], new_dt)
            ans = (
                f"Done! Booking {res['booking_id']} is now set for "
                f"{res['date']} at {_format_time_display(res['time'])}."
            )
        except (BookingError, Exception) as e:
            ans = f"Sorry, I couldn't reschedule that: {str(e)}"
        session.state = "GENERAL"
        session.collected_data = {}
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    # Step 3: resolve the new date/time phrase the user just gave.
    ext_sys = prompts.get("EXTRACTION_SYSTEM_PROMPT", "").replace("{expected_field_hint}", "")
    data = llm_json_call(ext_sys, f"LATEST MESSAGE: {message}")
    for k, v in data.items():
        if v is not None and str(v).lower() != "null" and str(v).strip() and k in ("date_phrase", "time_phrase"):
            session.collected_data[k] = str(v)

    if not session.collected_data.get("date_phrase") or not session.collected_data.get("time_phrase"):
        missing = []
        if not session.collected_data.get("date_phrase"):
            missing.append("date")
        if not session.collected_data.get("time_phrase"):
            missing.append("time")
        q = generate_followup(session, missing, "reschedule", prompts)
        session.history.append({"role": "assistant", "content": q})
        yield {"type": "token", "text": q}
        yield {"type": "done", "answer": q, "state": session.state}
        return

    try:
        new_dt, ambiguous = _resolve_date_time_phrase(
            session.collected_data["date_phrase"], session.collected_data["time_phrase"]
        )
    except Exception as e:
        ans = f"Sorry, I had trouble understanding that date or time: {str(e)}. Could you try again?"
        session.collected_data.pop("date_phrase", None)
        session.collected_data.pop("time_phrase", None)
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    if ambiguous:
        ans = "Did you mean AM or PM?"
        session.collected_data.pop("time_phrase", None)
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    session.collected_data["reschedule_new_date"] = new_dt.strftime("%Y-%m-%d")
    session.collected_data["reschedule_new_time"] = new_dt.strftime("%H:%M")
    session.collected_data["reschedule_previewed"] = True
    ans = (
        f"Just to confirm moving booking {session.collected_data['reschedule_booking_id']} from "
        f"{session.collected_data['reschedule_old_date']} {session.collected_data['reschedule_old_time']} to "
        f"{new_dt.strftime('%d-%m-%Y')} {_format_time_display(new_dt.strftime('%H:%M'))}. Shall I go ahead? (yes/no)"
    )
    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def _format_time_display(time_24h: str) -> str:
    try:
        return datetime.datetime.strptime(time_24h, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except Exception:
        return time_24h



def handle_change_details_flow(session: Session, message: str, prompts: dict):
    yield {"type": "status", "status": "Updating booking details..."}

    # Step 1: identify the existing booking, if not already known.
    if not session.collected_data.get("update_booking_id"):
        ext_sys = "Extract any booking ID or email address from the message. Output JSON: {'identifier': '...'}"
        data = llm_json_call(ext_sys, f"Message: {message}")
        id_val = data.get("identifier")
        if id_val and str(id_val).strip().lower() != "null":
            session.collected_data["update_identifier"] = str(id_val).strip()

        ident = session.collected_data.get("update_identifier")
        if not ident:
            ans = "Sure, I can help update your booking details. Could you give me your booking ID or the email address you booked with?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        booking = find_active_booking_by_email(ident) if "@" in ident else find_booking_by_id(ident)
        if not booking or booking.get("Status", "").strip() != "Confirmed":
            ans = f"I couldn't find an active booking for '{ident}'. Could you double check it?"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        session.collected_data["update_booking_id"] = booking["Booking ID"]
        session.collected_data["update_current_date"] = booking.get("Requested Date", "")
        session.collected_data["update_current_time"] = booking.get("Requested Time", "")
        # Fall through to Step 3 so the LLM can extract any details already provided in the initial message.

    # Step 2: preview + confirm, once we have all the requested updates.
    if session.collected_data.get("update_previewed"):
        confirm_sys = (
            "The user is viewing an update confirmation preview. Classify "
            "their reply. Output ONLY JSON: {'confirmed': true/false/null} "
            "(null if genuinely ambiguous and needs to be asked again)."
        )
        confirm_data = llm_json_call(confirm_sys, f"LATEST MESSAGE: {message}")
        confirmed = confirm_data.get("confirmed")

        if confirmed is False:
            ans = "No problem, I've left your original booking as is. Anything else I can help with?"
            session.state = "GENERAL"
            session.collected_data = {}
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        if confirmed is None:
            ans = "Just to confirm should I go ahead and update your booking details, yes or no?"
            session.history.append({"role": "assistant", "content": ans})
            yield {"type": "token", "text": ans}
            yield {"type": "done", "answer": ans, "state": session.state}
            return

        yield {"type": "status", "status": "Applying updates..."}
        try:
            current_dt_str = f"{session.collected_data['update_current_date']} {session.collected_data['update_current_time']}"
            current_dt = datetime.datetime.strptime(current_dt_str, "%d-%m-%Y %I:%M %p")
            
            updates = session.collected_data.get("update_details_pending", {})
            
            # Fetch the old booking details to pass to the cascade function
            booking = find_booking_by_id(session.collected_data["update_booking_id"])
            old_details = {
                "name": booking.get("Lead Name"),
                "email": booking.get("Email"),
                "phone": booking.get("Contact Number")
            } if booking else {}
            
            res = reschedule_booking(
                booking_id=session.collected_data["update_booking_id"],
                new_start_dt=current_dt,
                name=updates.get("name"),
                email=updates.get("email"),
                phone=updates.get("phone")
            )
            
            cascade_updates_llm_driven(old_details, updates, prompts)
            
            ans = f"Done! Booking {res['booking_id']} has been successfully updated, and your details have been synced across our systems."
        except (BookingError, Exception) as e:
            ans = f"Sorry, I couldn't update that: {str(e)}"
        
        session.state = "GENERAL"
        session.collected_data = {}
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return

    # Step 3: LLM figures out what the user wants to change and if we have the new values.
    # Pass the entire conversation history context.
    chat_history_text = "\\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in session.history[-6:]])
    
    update_sys = prompts.get("UPDATE_DETAILS_SYSTEM_PROMPT", "")
    llm_prompt = f"CONVERSATION HISTORY:\\n{chat_history_text}\\n\\nLATEST MESSAGE: {message}"
    data = llm_json_call(update_sys, llm_prompt)
    
    ready = data.get("ready_to_preview", False)
    follow_up = data.get("follow_up_question")
    updates = data.get("updates", {})
    
    if not ready or follow_up:
        ans = follow_up if follow_up else "Could you provide the new details you'd like to update?"
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return
        
    # Filter out nulls from updates
    clean_updates = {k: v for k, v in updates.items() if v is not None and str(v).lower() != "null" and str(v).strip()}
    if not clean_updates:
        ans = "I'm sorry, I couldn't understand what details you want to change. Could you clarify?"
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return
        
    session.collected_data["update_details_pending"] = clean_updates
    session.collected_data["update_previewed"] = True
    
    changes_str = ", ".join([f"{k.capitalize()} to '{v}'" for k, v in clean_updates.items()])
    ans = (
        f"Just to confirm, you'd like to update booking {session.collected_data['update_booking_id']}. "
        f"Changes: {changes_str}. Shall I go ahead? (yes/no)"
    )
    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def handle_check_ticket_status(session: Session, message: str, prompts: dict):
    yield {"type": "status", "status": "Checking ticket status..."}
    ext_sys = "Extract ticket ID or email. Output JSON: {'identifier': '...'}"
    data = llm_json_call(ext_sys, f"Message: {message}")
    ident = data.get("identifier")
    
    if not ident:
        ans = "Please provide your ticket ID or email address to check the status."
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return
        
    try:
        if '@' in ident:
            res = find_tickets_by_email(ident)
            if not res:
                ans = f"I couldn't find any tickets under {ident}."
            elif len(res) == 1:
                t = res[0]
                ans = f"Ticket {t.get('Ticket ID')} ({t.get('Category')}) is currently: {t.get('Status', 'Unknown')}."
            else:
                lines = "\n".join(
                    f"  {t.get('Ticket ID')} ({t.get('Category')}) — {t.get('Status', 'Unknown')}"
                    for t in res
                )
                ans = f"I found {len(res)} tickets under {ident}:\n{lines}"
        else:
            res = find_ticket_by_id(ident)
            if res:
                ans = f"Ticket {res.get('Ticket ID')} ({res.get('Category')}) is currently: {res.get('Status', 'Unknown')}."
            else:
                ans = f"I couldn't find a ticket with ID '{ident}'. Could you double check it?"
    except Exception as e:
        ans = f"Error finding ticket: {str(e)}"
        
    session.state = "GENERAL"
    session.collected_data = {}
    session.history.append({"role": "assistant", "content": ans})
    yield {"type": "token", "text": ans}
    yield {"type": "done", "answer": ans, "state": session.state}


def _ensure_complete_reply(reply: str, finish_reason: Optional[str]) -> str:
    """Guard against a reply being left mid-sentence if generation is ever
    cut off by the MAX_RESPONSE_TOKENS hard cap.

    The system prompt's word budget is calibrated to sit comfortably under
    that cap, so this should rarely trigger — but if the model ever does
    write right up to the limit (finish_reason == "length"), trim back to
    the last fully-finished sentence rather than showing a response that
    ends mid-word or mid-thought. If no sentence boundary is found (e.g. a
    single very long clause), the reply is returned as-is rather than
    discarding it outright, since a slightly-long-but-intact reply is
    preferable to losing the information entirely.
    """
    if not reply:
        return reply

    if finish_reason != "length":
        return reply

    # Find the last sentence-ending punctuation followed by a boundary.
    matches = list(re.finditer(r'[.!?](?:["\')\]]?)(?=\s|$)', reply))
    if not matches:
        return reply

    last_end = matches[-1].end()
    trimmed = reply[:last_end].rstrip()

    # Only use the trimmed version if it didn't throw away most of the
    # content (e.g. a stray period very early in the text).
    if len(trimmed) >= 0.5 * len(reply):
        return trimmed
    return reply


def handle_general_qa(session: Session, message: str, prompts: dict):
    yield {"type": "status", "status": "Searching knowledge base..."}
    context = search_knowledge_base(message)
    
    sys_prompt = prompts.get("SYSTEM_INSTRUCTION", "")
    user_msg_with_context = f"CONTEXT:\n{context}\n\nUSER MESSAGE: {message}"
    
    messages = [{"role": "system", "content": sys_prompt}]
    for m in session.history[:-1]:
        messages.append({"role": m["role"], "content": m["content"]})
        
    messages.append({"role": "user", "content": user_msg_with_context})
    
    yield {"type": "status", "status": "Drafting response..."}
    client = get_groq_client()
    try:
        res = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=messages,
            temperature=0.3,
            max_tokens=MAX_RESPONSE_TOKENS,
        )
        choice = res.choices[0]
        reply = choice.message.content
        # Belt-and-suspenders: if the model ever writes right up to the
        # 130-token cap and gets hard-cut by the API, trim back to the
        # last complete sentence instead of showing a response that ends
        # mid-word or mid-thought.
        reply = _ensure_complete_reply(reply, getattr(choice, "finish_reason", None))
    except Exception as e:
        reply = "Sorry, I ran into an issue processing that. Could you try again?"
        
    session.history.append({"role": "assistant", "content": reply})
    yield {"type": "token", "text": reply}
    yield {"type": "done", "answer": reply, "state": session.state}

def handle_message(session_id: str, message: str) -> Dict[str, Any]:
    reply = ""
    state = "GENERAL"
    for event in handle_message_stream(session_id, message):
        if event["type"] == "done":
            reply = event["answer"]
            state = event["state"]
    return {"reply": reply, "state": state}

def handle_message_stream(session_id: str, message: str):
    session = get_session(session_id)
    prompts = load_prompts()
    
    session.history.append({"role": "user", "content": message})
    yield {"type": "status", "status": "Understanding your request..."}
    
    intent = classify_intent(session, message, prompts)
    
    if intent == "abandon":
        session.state = "GENERAL"
        session.collected_data = {}
        ans = "Okay, I've cancelled that process. How else can I help?"
        session.history.append({"role": "assistant", "content": ans})
        yield {"type": "token", "text": ans}
        yield {"type": "done", "answer": ans, "state": session.state}
        return
        
    intent_map = {
        "start_booking": "BOOKING",
        "start_cancellation": "CANCELLATION",
        "start_reschedule": "RESCHEDULE",
        "change_booking_details": "UPDATE_DETAILS",
        "start_ticket": "TICKET",
        "check_ticket_status": "CHECK_TICKET"
    }
    
    if intent in intent_map:
        new_state = intent_map[intent]
        if session.state != new_state:
            session.state = new_state
            session.collected_data = {}
            
    if session.state == "BOOKING":
        yield from handle_booking_flow(session, message, prompts)
    elif session.state == "CANCELLATION":
        yield from handle_cancellation_flow(session, message, prompts)
    elif session.state == "RESCHEDULE":
        yield from handle_reschedule_flow(session, message, prompts)
    elif session.state == "UPDATE_DETAILS":
        yield from handle_change_details_flow(session, message, prompts)
    elif session.state == "TICKET":
        yield from handle_ticket_flow(session, message, prompts)
    elif session.state == "CHECK_TICKET":
        yield from handle_check_ticket_status(session, message, prompts)
    else:
        yield from handle_general_qa(session, message, prompts)


def cascade_updates_llm_driven(old_details: dict, new_details: dict, prompts: dict):
    import chatbot_lead_manager
    import lead_manager_2
    import support_ticket_manager
    
    chatbot_leads = chatbot_lead_manager.fetch_all_leads()
    main_leads = lead_manager_2.fetch_all_leads()
    tickets = support_ticket_manager.fetch_all_tickets()
    
    cl_simple = [{"row": c.get("row_number"), "name": c.get("Lead Name"), "email": c.get("Email"), "phone": c.get("Contact Number")} for c in chatbot_leads]
    ml_simple = [{"row": l.get("row_number"), "name": l.get("Lead Name"), "email": l.get("Email"), "phone": l.get("Contact Number")} for l in main_leads]
    tk_simple = [{"id": t.get("Ticket ID"), "name": t.get("Lead Name"), "email": t.get("Email"), "phone": t.get("Contact Number")} for t in tickets]
    
    llm_sys = prompts.get("CASCADE_UPDATES_SYSTEM_PROMPT", "")
    if not llm_sys:
        return
        
    prompt = f"OLD DETAILS: {old_details}\\nNEW DETAILS: {new_details}\\n\\nCHATBOT LEADS:\\n{cl_simple}\\n\\nMAIN LEADS:\\n{ml_simple}\\n\\nTICKETS:\\n{tk_simple}"
    
    data = llm_json_call(llm_sys, prompt)
    
    for row in data.get("chatbot_leads_rows", []):
        try:
            chatbot_lead_manager.update_lead_by_row(int(row), new_details)
        except Exception as e:
            print(f"[cascade] Error updating chatbot lead row {row}: {e}")
            
    for row in data.get("main_leads_rows", []):
        try:
            lead_manager_2.update_lead_by_row(int(row), new_details)
        except Exception as e:
            print(f"[cascade] Error updating main lead row {row}: {e}")
            
    for tk_id in data.get("ticket_ids", []):
        try:
            support_ticket_manager.update_ticket_contact_info(str(tk_id), new_details)
        except Exception as e:
            print(f"[cascade] Error updating ticket {tk_id}: {e}")