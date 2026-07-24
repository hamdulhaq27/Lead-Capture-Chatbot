"""
support_ticket_manager.py
===========================
Business-rule layer for chatbot support tickets, writing to a dedicated
"Chatbot_Tickets" Google Sheet — same architectural slot as
booking_manager.py (Bookings sheet) and chatbot_lead_manager.py
(Chatbot_Leads sheet), so a ticket is a first-class record next to
bookings and leads rather than a note buried in a chat transcript.

Categories, priority, and escalation implement support_tickets_final.md:
  1. General Inquiry
  2. Service Request
  3. Appointment Support   (only the sub-cases the doc lists — reschedule/
                            cancel themselves are NOT tickets, they're
                            handled directly by booking_manager.py)
  4. Technical Support
  5. General Complaint
  6. Feature Request
  7. Human Assistance
  8. Payment / Invoice     (always High priority — see determine_priority())

No-show deposit enforcement, urgent-priority auto-routing, and plan
upgrade/downgrade are deliberately still NOT categories here —
support_tickets_final.md explicitly defers these until there's a
supporting system (deposit logic, subscription records, etc.) to back
them. Payment/Invoice itself stays lightweight for the same reason:
there's no billing system yet to look anything up against, so it just
logs the report (name/email/description) the way General Complaint
does, and always routes to a human rather than trying to resolve
anything automatically.

Ticket ID format: "TKT-" + 8 uppercase hex chars (e.g. TKT-4F9A21C0),
mirroring booking_manager.py's "BK-" convention.

This module does NOT send email — see email_service.py, which gained
send_ticket_confirmation() / send_escalation_notice() alongside its
existing booking templates. It DOES return everything those need.

CLI usage
---------
python support_ticket_manager.py create "Jane Doe" jane@example.com 03001234567 \\
    --category technical_support --description "Chatbot gave wrong pricing info"
python support_ticket_manager.py status TKT-4F9A21C0
python support_ticket_manager.py list --status Open
python support_ticket_manager.py resolve TKT-4F9A21C0 --notes "Refreshed KB, confirmed fix"
python support_ticket_manager.py escalate TKT-4F9A21C0 --reason "User asked for a manager"
"""

#########
# Imports
#########
import os
import re
import json
import secrets
import argparse
import datetime
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv

from google_services import create_service, GoogleSheetsHelper

from lead_manager_2 import (
    normalize_phone,
    to_local_pk_format,
    validate_phone_number,
    EMAIL_REGEX,
    LeadValidationError,
)
from email_service import send_ticket_confirmation, send_escalation_notice

# Same sibling-module relationship booking_manager.py and this file already
# have architecturally (see module docstring) — booking_manager.py has no
# reason to import support_ticket_manager.py back, so this one-way import
# doesn't create the kind of circular-import problem get_groq_client()
# below avoids with conversation_manager.py. Used only for the
# returning-customer priority bump in create_ticket(): "does this email
# have a previous booking on file" is exactly the question
# find_any_booking_by_email() already answers for conversation_manager.py
# elsewhere, so this reuses that single source of truth instead of
# re-deriving the same answer a second way.
from booking_manager import find_any_booking_by_email

# Load .env here rather than assuming some other module (e.g.
# conversation_manager.py, which calls this in the real app) already did
# it first. booking_manager.py/chatbot_lead_manager.py skip this because
# they're never run standalone in practice — this module IS meant to be
# runnable directly from the CLI for testing (see module docstring), so
# it can't rely on import order to have populated os.environ already.
load_dotenv()

###############
# Configuration
###############
CLIENT_SECRET_FILE = "Client_Secret.json"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Deliberately its own spreadsheet ID/env var, same reasoning
# chatbot_lead_manager.py gives for CHATBOT_LEADS_SPREADSHEET_ID: tickets
# are their own concern and shouldn't be forced into the Bookings or
# Chatbot_Leads sheet's schema just because they happen to share a
# codebase.
CHATBOT_TICKETS_SPREADSHEET_ID = os.environ.get("CHATBOT_TICKETS_SPREADSHEET_ID")
SHEET_NAME = "Chatbot_Tickets"

HEADERS = [
    "Ticket ID", "Category", "Subcategory", "Priority", "Status",
    "Lead Name", "Email", "Contact Number", "Description",
    "Related Booking ID", "Chat Summary", "Escalated To Human",
    "Escalation Reason", "Resolution Notes", "Created At", "Updated At",
]


class TicketError(Exception):
    pass


class TicketLimitExceededError(TicketError):
    """Raised by create_ticket() when this email has already reached
    MAX_TICKETS_PER_EMAIL_CATEGORY tickets in the SAME category.
    Deliberately scoped to (email, category), not email alone — a
    genuinely different issue in another category is a different signal
    and should never be blocked by an unrelated category's history. Kept
    as its own subclass (rather than a plain TicketError) so callers that
    care can catch it specifically and respond differently — there's no
    field to "fix" here the way there is for a validation error, so
    looping back into COLLECTING_TICKET_INFO to re-ask for something
    would make no sense.
    """

    def __init__(self, email: str, category: str, count: int, limit: int):
        self.email = email
        self.category = category
        self.count = count
        self.limit = limit
        category_label = CATEGORY_LABELS.get(category, category)
        ticket_word = "ticket" if count == 1 else "tickets"
        super().__init__(
            f"You've already submitted {count} {category_label} {ticket_word} with this "
            f"email address, which is the most I can log for that category at once."
        )


#####################
# Categories & status
#####################
class Category:
    GENERAL_INQUIRY = "general_inquiry"
    SERVICE_REQUEST = "service_request"
    APPOINTMENT_SUPPORT = "appointment_support"
    TECHNICAL_SUPPORT = "technical_support"
    GENERAL_COMPLAINT = "general_complaint"
    FEATURE_REQUEST = "feature_request"
    HUMAN_ASSISTANCE = "human_assistance"
    PAYMENT_INVOICE = "payment_invoice"


ALL_CATEGORIES = {
    Category.GENERAL_INQUIRY, Category.SERVICE_REQUEST,
    Category.APPOINTMENT_SUPPORT, Category.TECHNICAL_SUPPORT,
    Category.GENERAL_COMPLAINT, Category.FEATURE_REQUEST,
    Category.HUMAN_ASSISTANCE, Category.PAYMENT_INVOICE,
}

# Display labels for the sheet + user-facing replies — kept separate from
# the internal snake_case category keys so the sheet stays human-readable
# without conversation_manager.py needing to know the mapping.
CATEGORY_LABELS = {
    Category.GENERAL_INQUIRY: "General Inquiry",
    Category.SERVICE_REQUEST: "Service Request",
    Category.APPOINTMENT_SUPPORT: "Appointment Support",
    Category.TECHNICAL_SUPPORT: "Technical Support",
    Category.GENERAL_COMPLAINT: "General Complaint",
    Category.FEATURE_REQUEST: "Feature Request",
    Category.HUMAN_ASSISTANCE: "Human Assistance",
    Category.PAYMENT_INVOICE: "Payment / Invoice",
}

# Subcategories per support_tickets_final.md — used to (a) prompt the
# user with relevant follow-up questions and (b) feed the priority rules
# below. Free-text "other" is always implicitly allowed on top of these.
SUBCATEGORIES = {
    Category.GENERAL_INQUIRY: [
        "unclear_answer", "unsure_which_service", "more_info_request",
    ],
    Category.SERVICE_REQUEST: [
        "seo_consultation", "ppc_consultation", "social_media_consultation",
        "web_design_consultation", "branding_consultation",
        "full_package_consultation", "custom_quote", "sales_callback",
    ],
    Category.APPOINTMENT_SUPPORT: [
        "no_show", "no_booking_confirmation_email",
        "no_cancellation_confirmation_email", "invalid_slot",
        "duplicate_booking", "booking_not_found",
    ],
    Category.TECHNICAL_SUPPORT: [
        "incorrect_or_unclear_info", "booking_submission_error",
        "email_not_received", "website_error","chatbot_not_working",
        "other_technical_issue",
        
    ],
    Category.GENERAL_COMPLAINT: [
        "poor_call_experience", "unsatisfied_with_consultation",
        "slow_response", "poor_service_quality",
        "other_complaint",
    ],
    Category.FEATURE_REQUEST: [
        "chatbot_feature", "new_service_suggestion",
        "website_or_chatbot_improvement",
    ],
    Category.HUMAN_ASSISTANCE: [
        "speak_to_human", "repeated_failed_attempts", "cannot_classify",
    ],
    Category.PAYMENT_INVOICE: [
        "payment_failed", "duplicate_charge", "unrecognized_charge",
        "invoice_discrepancy", "invoice_not_received", "refund_request",
        "payment_method_update",
    ],
} 

# Hard, fixed contract: what MUST be known before create_ticket() can be
# called for each category — beyond the universal name/email/phone/
# description every ticket needs. This does NOT change based on what an
# LLM decides is "enough" — that's the whole point of keeping it a rule.
# The LLM below only decides HOW to ask for these naturally and in what
# order; it can never decide one is optional.
REQUIRED_FIELDS = {
    Category.GENERAL_INQUIRY: [],
    Category.SERVICE_REQUEST: ["which_service"],
    Category.APPOINTMENT_SUPPORT: ["booking_id_or_contact_match"],
    Category.TECHNICAL_SUPPORT: [],
    Category.GENERAL_COMPLAINT: [],
    Category.FEATURE_REQUEST: [],
    Category.HUMAN_ASSISTANCE: [],
    Category.PAYMENT_INVOICE: [],
}

# Categories where the issue being reported presupposes the person has
# actually booked/interacted with the agency before — a "my discovery
# call was disorganized" complaint, or a "the campaign you're running
# for me broke" technical issue, both only make sense for someone who
# already exists in the Bookings sheet. For these categories, the ticket
# flow looks the person up by email instead of asking for name/phone
# directly — name and phone come from their existing booking record, and
# if no record is found at all, the conversation redirects to booking a
# discovery call rather than filing a ticket for someone with no on-file
# relationship to verify against.
#
# Appointment Support is included too, but handled slightly differently:
# a report about an appointment (no-show, missing confirmation email,
# can't book, booking ID not found, etc.) is inherently about an existing
# booking, so there's no real "are you an existing customer?" question to
# ask — the conversation skips straight to requesting their booking ID or
# email instead (see the APPOINTMENT_SUPPORT special-case at the top of
# _handle_customer_verification_flow in conversation_manager.py).
#
# Deliberately EXCLUDES:
# - General Inquiry, Service Request, Feature Request: legitimately open
#   to a brand-new prospect who has never booked anything ("I'd like a
#   quote for SEO", "you should add dark mode") — requiring an existing
#   booking there would incorrectly turn away real leads.
CATEGORIES_REQUIRING_EXISTING_BOOKING = {
    Category.TECHNICAL_SUPPORT,
    Category.GENERAL_COMPLAINT,
    Category.APPOINTMENT_SUPPORT,
}

# Static fallback questions — used ONLY if the LLM call in
# generate_followup_question() fails (no API key, network error,
# malformed response). Never shown to the user in the normal path; this
# exists purely so a Groq outage can't leave the ticket flow stuck with
# no question to ask at all. Each one leads with a brief acknowledgment
# for the same reason the LLM prompt now requires one — a Groq outage
# shouldn't also mean the user's issue goes unacknowledged.
_FALLBACK_FOLLOWUP_QUESTIONS = {
    Category.GENERAL_INQUIRY: "Could you tell me a bit more about what you'd like to know?",
    Category.SERVICE_REQUEST: "Which service is this for: SEO, PPC, Social Media, Web Design, Branding, or a Full Package?",
    Category.APPOINTMENT_SUPPORT: "Do you have your Booking ID? If not, the email and phone number on the booking works too.",
    Category.TECHNICAL_SUPPORT: "What were you trying to do when the issue happened, and what went wrong exactly?",
    Category.GENERAL_COMPLAINT: "Could you describe what happened so I can pass it on accurately?",
    Category.FEATURE_REQUEST: "What would you like to see added or changed?",
    Category.HUMAN_ASSISTANCE: "I'll connect you with our team. Is there anything specific I should let them know up front?",
    Category.PAYMENT_INVOICE: "Could you tell me more about the payment or invoice issue you're running into?",
}

FOLLOWUP_SYSTEM_PROMPT = """
# ROLE
You write the **next follow-up question** a support chatbot should ask, for a digital marketing agency.

<br>

## INPUT
You will be given:
- **CATEGORY** — the type of support ticket being filed
- **STILL_NEEDED** — fields the bot still needs before it can file the ticket
- **CONVERSATION** — the recent chat history

<br>

## OUTPUT FORMAT
Respond with **ONLY** a valid JSON object, nothing else:
```json
{"question": "<the single next question to ask, conversational>"}
```

<br>

## RULES

**No re-apologizing**
- Do NOT open with an apology, "sorry to hear that," "thanks for letting me know," or any other acknowledgment/empathy phrase — the system already shows that ONCE, right when the issue is first reported, before your very first question ever runs.
- Repeating it on every follow-up question reads as over-apologetic and insincere.
- Just ask the next question directly and warmly, the way a person would in the middle of an ongoing conversation rather than re-greeting them each time.

**Stay inside STILL_NEEDED**
- Your question MUST ask about ONE of the fields literally listed in STILL_NEEDED — nothing else.
- Do NOT invent a question about anything not in that list (no re-confirming spelling, no asking about details the schema doesn't track, no tangents), even if it feels like a natural thing to ask.
- If STILL_NEEDED lists more than one field, pick the single most useful one to ask next.

**Don't re-ask what's already known**
- Read CONVERSATION first.
- If the user already stated something in STILL_NEEDED (e.g. they already said which service they want, or already described the problem in detail), do NOT ask for it again — move on to the next missing thing.

**Be specific, not generic**
- Reference specifics the user already mentioned rather than asking a generic question — e.g. if they said *"the confirmation email never came,"* ask about the email address it should have gone to, not a generic *"tell me more."*

**Match tone to category**
- Warm and direct for complaints or appointment issues (without re-apologizing).
- Friendly and practical for service requests, technical issues, or feature suggestions.

**Format constraints**
- Ask exactly ONE question. Never ask two things in one message.
- Never invent facts the user hasn't stated.
- Keep the question under 20 words.
- Never explain your reasoning outside the JSON. Never output markdown.
"""


# Deterministic per-field question templates — the last line of defense
# if the LLM (despite the prompt rule above) still asks about something
# outside STILL_NEEDED. generate_followup_question() checks the model's
# question against these keywords before returning it; if it doesn't
# plausibly address a field that's actually still needed, the template is
# used instead. This is what stops a hallucinated tangent (e.g. "Is that
# your full name?" or "What time was your appointment?" — neither a real
# field in this schema) from ever reaching the user.
_FIELD_QUESTION_TEMPLATES = {
    "name": "Could I get your full name for this?",
    "email": "What's the best email address to reach you at?",
    "phone": "And a phone number, in case we need to reach you quickly?",
    "description": "Could you tell me a bit more about what happened?",
    "which_service": "Which service is this for SEO, PPC, Social Media, Web Design, Branding, or a Full Package?",
    "booking_id_or_contact_match": "Do you have your booking ID? If not, the email or phone number you booked with works too.",
}
_FIELD_KEYWORDS = {
    "name": ["name"],
    "email": ["email", "e-mail"],
    "phone": ["phone", "number", "contact you", "reach you"],
    "description": ["describe", "detail", "happened", "tell me more", "went wrong", "more about", "what happened"],
    "which_service": ["service", "seo", "ppc", "social media", "web design", "branding", "full package"],
    "booking_id_or_contact_match": ["booking id", "booking number", "email", "phone"],
}


def _question_addresses_needed_field(question: str, still_needed: List[str]) -> bool:
    """True if the question plausibly asks about at least one field that's
    actually still needed. Used to catch the model asking about something
    outside STILL_NEEDED (an off-schema tangent) rather than trusting the
    prompt rule alone to hold under every phrasing.
    """
    q = question.lower()
    for field_name in still_needed:
        keywords = _FIELD_KEYWORDS.get(field_name)
        if not keywords:
            # Unknown/uncatalogued field — can't validate it, don't block it.
            return True
        if any(kw in q for kw in keywords):
            return True
    return False


def generate_followup_question(category: str, still_needed: List[str],
                                chat_history: Optional[List[Dict[str, str]]] = None) -> str:
    """LLM-generated next follow-up question for the ticket-collection
    flow. This is the piece that benefits from reading the actual
    conversation — a fixed question per category can't tell whether the
    user already answered it. `still_needed` should list which
    REQUIRED_FIELDS entries (plus "description" if none given yet) are
    still missing; the model uses that as its checklist but decides HOW
    to phrase the ask.

    Falls back to the static _FALLBACK_FOLLOWUP_QUESTIONS on any failure
    (no API key, network error, malformed JSON) — same never-fails
    contract as classify_urgency()/summarize_chat(), so a Groq outage
    degrades the conversation to a slightly more generic question rather
    than breaking the ticket flow entirely.
    """
    client = get_groq_client()
    if client is None:
        return _FALLBACK_FOLLOWUP_QUESTIONS.get(category, "Could you tell me more about the issue?")

    trimmed = (chat_history or [])[-8:]
    transcript = "\n".join(
        f"{turn.get('role', 'user')}: {turn.get('content', '')}" for turn in trimmed
    ) or "(no prior conversation)"

    try:
        model = os.getenv("GROQ_MODEL_NAME")
        user_content = (
            f"CATEGORY: {CATEGORY_LABELS.get(category, category)}\n"
            f"STILL_NEEDED: {', '.join(still_needed) if still_needed else '(nothing required, just need more detail)'}\n"
            f"CONVERSATION:\n{transcript}"
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": FOLLOWUP_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.4,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
        question = str(parsed.get("question", "")).strip()
        if not question:
            return _FALLBACK_FOLLOWUP_QUESTIONS.get(category, "Could you tell me more about the issue?")

        # Safety net: if STILL_NEEDED names actual fields but the model's
        # question doesn't plausibly address any of them (a hallucinated
        # tangent — the exact failure mode that produced questions like
        # "Is that your full name?" or "What time was your appointment?"
        # in production, neither of which is a tracked field), don't pass
        # it through. Fall back to a deterministic, on-schema question for
        # the single most pressing missing field instead.
        if still_needed and not _question_addresses_needed_field(question, still_needed):
            print(f"[support_ticket_manager] Follow-up question '{question}' didn't address "
                  f"any of {still_needed} using deterministic template instead.")
            return _FIELD_QUESTION_TEMPLATES.get(
                still_needed[0], _FALLBACK_FOLLOWUP_QUESTIONS.get(category, "Could you tell me more about the issue?")
            )

        return question
    except Exception as e:
        print(f"[support_ticket_manager] Follow-up question generation failed, "
              f"using fallback: {e}")
        return _FALLBACK_FOLLOWUP_QUESTIONS.get(category, "Could you tell me more about the issue?")


class Status:
    OPEN = "Open"
    IN_PROGRESS = "In Progress"
    RESOLVED = "Resolved"
    CLOSED = "Closed"


ACTIVE_STATUSES = {Status.OPEN, Status.IN_PROGRESS}


class Priority:
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


def bump_priority_one_level(priority: str) -> str:
    """Moves a priority up exactly one level: Low -> Medium, Medium ->
    High. High stays High (nothing above it to escalate to). Used by the
    returning-customer rule in create_ticket() — a deliberate ONE-STEP
    nudge rather than a hard override to High, so a genuinely Low-priority
    issue from a returning customer (e.g. a Feature Request) doesn't get
    treated with the same urgency as a genuinely High-priority issue.
    """
    if priority == Priority.LOW:
        return Priority.MEDIUM
    if priority == Priority.MEDIUM:
        return Priority.HIGH
    return Priority.HIGH


####################
# Priority engine
####################
# Hybrid, not pure rules and not pure LLM — mirrors how conversation_manager.py
# itself splits work: classify_intent() is LLM-driven because phrasing
# varies too much for keywords, but the STATE MACHINE around it (which
# states exist, what "confirm" does in each) is deterministic code, not a
# prompt. Priority follows the same split:
#
#   - category / subcategory / repeated-attempts -> deterministic rules.
#     These are policy decisions ("Human Assistance and Payment/Invoice
#     are always High") that
#     must be auditable and reproducible — a human reviewing the sheet
#     should be able to see WHY something is High without re-running an
#     LLM call, and the same input must always produce the same output.
#
#   - "does this description actually sound urgent/distressed" -> LLM.
#     A keyword list catches "urgent" and "lawyer" but misses "I've been
#     waiting three days and no one has replied" or "I'm about to just
#     take my business elsewhere" — real distress rarely uses a fixed
#     vocabulary. This is exactly the kind of free-form-language judgment
#     classify_intent() already relies on an LLM for elsewhere in this
#     codebase.
#
# The LLM signal has a deliberately CAPPED role: it can only ever raise a
# Medium verdict to High. It can never produce a High on its own, override
# a rule-based High, or push anything to Low. That keeps the floor
# (rules) and ceiling (rules) both fully deterministic — the model only
# adjusts the one ambiguous middle case, and a call failure/timeout just
# leaves the rule-based verdict unchanged (see classify_urgency()'s safe
# fallback below), so this can never crash ticket creation or produce an
# unreviewable decision.
# Whole categories that are always High, regardless of subcategory or
# free-text content — a policy decision, not a judgment call, so it lives
# here as a rule rather than being left to classify_urgency(). Human
# Assistance is always High because the user has explicitly said they
# need a person; Payment/Invoice is always High because money/billing
# issues are reputationally and financially sensitive enough that they
# should never sit at Medium waiting on an LLM's read of "does this
# sound urgent" — see determine_priority().
_ALWAYS_HIGH_PRIORITY_CATEGORIES = {
    Category.HUMAN_ASSISTANCE, Category.PAYMENT_INVOICE,
}
_HIGH_PRIORITY_SUBCATEGORIES = {
    "booking_submission_error", "duplicate_booking", "invalid_slot",
    "booking_not_found", "speak_to_human", "repeated_failed_attempts",
}
_LOW_PRIORITY_SUBCATEGORIES = {
    "chatbot_feature", "new_service_suggestion",
    "website_or_chatbot_improvement", "more_info_request",
}

PRIORITY_SYSTEM_PROMPT = """
# ROLE
You judge the priority of customer support tickets for a digital marketing agency chatbot.

<br>

## OUTPUT FORMAT
Respond with **ONLY** a valid JSON object, nothing else:
```json
{"priority": "<one of: Low, Medium, High>", "reason": "<max 12 words>"}
```

<br>

## PRIORITY RULE
Determine the priority based on the category, subcategory, and the description of the issue.
- **Low**: Feature requests, suggestions, pure informational requests, or very minor issues. Adding a new feature or making a suggestion should **NEVER** be high priority.
- **High**: Payment/invoice issues, human assistance requests, major blocking bugs, or messages conveying genuine distress, anger, a threat to leave/escalate publicly, or a time-critical problem.
- **Medium**: Most other general issues, service requests, technical support issues that aren't critical, or general complaints that aren't overly urgent.

<br>

## EXAMPLES

**priority = High**
- "I've emailed three times and nobody has responded, this is ridiculous."
- "If this isn't fixed today I'm cancelling and telling everyone."
- "I am extremely frustrated, this has completely wasted my afternoon."

**priority = Low**
- "Just wanted to suggest a new feature."
- "Can you add a dark mode?"

**priority = Medium**
- "I didn't get a confirmation email, can you check?"
- "The chatbot gave me a slightly confusing answer about pricing."

<br>

## CONSTRAINTS
- Never explain your reasoning outside the JSON.
- Never output markdown.
"""


def determine_priority(category: str, subcategory: str, description: str,
                        repeated_attempts: bool = False) -> str:
    """LLM-driven priority assignment.
    
    Replaces the hybrid rule-based engine with a pure LLM decision.
    If the LLM fails for any reason, falls back to Priority.MEDIUM.
    """
    client = get_groq_client()
    if client is None:
        return Priority.MEDIUM

    try:
        user_content = (
            f"CATEGORY: {category}\n"
            f"SUBCATEGORY: {subcategory}\n"
            f"REPEATED ATTEMPTS: {repeated_attempts}\n"
            f"DESCRIPTION: {description}"
        )
        model = os.getenv("GROQ_MODEL_NAME")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": PRIORITY_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=40,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
        priority_val = parsed.get("priority", Priority.MEDIUM)
        if priority_val not in [Priority.LOW, Priority.MEDIUM, Priority.HIGH]:
            return Priority.MEDIUM
        return priority_val
    except Exception as e:
        print(f"[support_ticket_manager] Priority classification failed, "
              f"defaulting to Medium: {e}")
        return Priority.MEDIUM


####################
# Escalation engine
####################
# "Escalation" here means: this ticket needs a human to act, not just a
# logged record. Distinct from Priority — a Low-priority feature request
# never needs a human to intervene *right now*, while a Medium-priority
# complaint about a bad call experience still does, just not urgently.
def requires_escalation(category: str, priority: str, repeated_attempts: bool = False) -> bool:
    # Escalation now tracks the LLM-driven priority verdict only: a ticket
    # is routed to a human if and only if it's High priority. Category- and
    # repeated-attempts-based bypasses were removed so this can't escalate
    # a ticket the LLM judged Medium/Low, and doesn't need its own
    # rule/regex logic — it just reads the priority determine_priority()
    # already produced.
    return priority == Priority.HIGH


def escalation_reason(category: str, priority: str, repeated_attempts: bool,
                       previous_booking_bump: bool = False) -> str:
    if repeated_attempts:
        return "User had repeated failed attempts resolving this through the chatbot."
    if previous_booking_bump:
        return "Priority bumped up existing customer with a previous booking on file."
    if category == Category.HUMAN_ASSISTANCE:
        return "User explicitly asked to speak with a human."
    if category == Category.GENERAL_COMPLAINT:
        return "Complaints are routed to a human by policy, regardless of priority."
    if category == Category.PAYMENT_INVOICE:
        return "Payment/invoice issues are always High priority and routed to a human by policy."
    if priority == Priority.HIGH:
        return "Marked High priority."
    return ""


####################
# Service singleton
####################
_sheets_service = None
_sheet_verified = False


def get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        _sheets_service = create_service(CLIENT_SECRET_FILE, "sheets", "v4", SHEETS_SCOPES)
    return _sheets_service


def ensure_sheet_exists(force: bool = False):
    global _sheet_verified
    if _sheet_verified and not force:
        return

    if not CHATBOT_TICKETS_SPREADSHEET_ID:
        raise TicketError(
            "CHATBOT_TICKETS_SPREADSHEET_ID is not set. Add it to your .env "
            "file (in the same folder you're running this from) or set it "
            "as a real environment variable, e.g.:\n"
            "  CHATBOT_TICKETS_SPREADSHEET_ID=your_spreadsheet_id_here\n"
            "Then restart your terminal/shell so it picks up the change."
        )

    service = get_sheets_service()
    spreadsheet = service.spreadsheets().get(spreadsheetId=CHATBOT_TICKETS_SPREADSHEET_ID).execute()
    existing_sheets = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]

    if SHEET_NAME not in existing_sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=CHATBOT_TICKETS_SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]}
        ).execute()
        print(f"Created new worksheet: '{SHEET_NAME}'")

    last_col = chr(ord('A') + len(HEADERS) - 1)
    result = service.spreadsheets().values().get(
        spreadsheetId=CHATBOT_TICKETS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}1"
    ).execute()
    values = result.get("values", [])

    if not values or not values[0]:
        service.spreadsheets().values().update(
            spreadsheetId=CHATBOT_TICKETS_SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:{last_col}1",
            valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
            body={"majorDimension": "ROWS", "values": [HEADERS]}
        ).execute()
        print(f"Headers written to '{SHEET_NAME}': {HEADERS}")

    _sheet_verified = True


############
# Validation
############
def validate_ticket_fields(name: str, email: str, phone: str, category: str) -> None:
    if not name or not str(name).strip():
        raise LeadValidationError("Missing required field: name")

    if not email or not EMAIL_REGEX.match(str(email).strip()):
        raise LeadValidationError(f"Invalid email format: '{email}'")

    if phone:
        normalized = normalize_phone(phone)
        validate_phone_number(normalized)

    if category not in ALL_CATEGORIES:
        raise TicketError(
            f"Unknown category '{category}'. Valid categories: "
            f"{', '.join(sorted(ALL_CATEGORIES))}"
        )


####################
# Sheet read helpers
####################
def fetch_all_tickets() -> List[Dict[str, str]]:
    ensure_sheet_exists()
    service = get_sheets_service()
    last_col = chr(ord('A') + len(HEADERS) - 1)
    result = service.spreadsheets().values().get(
        spreadsheetId=CHATBOT_TICKETS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}"
    ).execute()
    values = result.get("values", [])

    if not values:
        return []

    header, *rows = values
    records = []
    for idx, row in enumerate(rows, start=2):
        padded = row + [""] * (len(header) - len(row))
        record = dict(zip(header, padded))
        record["row_number"] = idx
        records.append(record)

    return records


def find_ticket_by_id(ticket_id: str) -> Optional[Dict[str, Any]]:
    ticket_id_norm = str(ticket_id).strip().upper()
    for record in fetch_all_tickets():
        if record.get("Ticket ID", "").strip().upper() == ticket_id_norm:
            return record
    return None


def find_open_tickets_by_email(email: str) -> List[Dict[str, Any]]:
    """Used for the repeated-failed-attempts escalation signal: if this
    email already has an open/in-progress ticket and shows up again with
    a new issue, that's a signal worth surfacing to a human rather than
    quietly logging ticket #3."""
    email_norm = str(email).strip().lower()
    return [
        r for r in fetch_all_tickets()
        if r.get("Email", "").strip().lower() == email_norm
        and r.get("Status", "").strip() in ACTIVE_STATUSES
    ]


def find_tickets_by_email(email: str) -> List[Dict[str, Any]]:
    """ALL tickets for this email, regardless of status, most recently
    created first — used by the "check my ticket status" flow when the
    user doesn't have their ticket ID handy. Deliberately broader than
    find_open_tickets_by_email above: a user asking about status may well
    be asking about a ticket that's already Resolved or Closed, not just
    an active one, so filtering those out here would hide the very
    answer they're looking for.

    Sorted by row_number (append-only sheet, so higher row_number is
    always more recent) rather than parsing the "Created At" display
    string, which is just a formatted date/time and not reliably
    sortable as text.
    """
    email_norm = str(email).strip().lower()
    matches = [r for r in fetch_all_tickets() if r.get("Email", "").strip().lower() == email_norm]
    return sorted(matches, key=lambda r: r.get("row_number", 0), reverse=True)


def count_tickets_by_email_and_category(email: str, category: str) -> int:
    """How many EXISTING tickets this email already has in this SAME
    category, regardless of status. Used in create_ticket() to enforce
    MAX_TICKETS_PER_EMAIL_CATEGORY, the hard cap on how many tickets the
    same email may have in the SAME category. Deliberately category-scoped
    and counts ALL statuses rather than just active ones — even an
    already-resolved ticket in the same category still counts against the
    cap.
    """
    category_label = CATEGORY_LABELS.get(category, category)
    return len([t for t in find_tickets_by_email(email) if t.get("Category", "") == category_label])


# Hard cap on how many tickets the same email can have in the SAME
# category (any status) — enforced in create_ticket() via
# TicketLimitExceededError. Scoped per (email, category), never per email
# overall: someone who's maxed out Billing complaints can still open a
# genuinely new Technical Support ticket without being blocked by an
# unrelated category's history.
#
# Set to 1: only ONE ticket is allowed per (email, category) at all. A
# second attempt in the same category is blocked outright rather than
# logged as a new row — see TicketLimitExceededError and
# _handle_ticket_limit_exceeded() in conversation_manager.py, which
# escalates the existing ticket to High priority instead.
MAX_TICKETS_PER_EMAIL_CATEGORY = 1


#####################
# Ticket ID generation
#####################
def generate_ticket_id(existing_ids: Optional[set] = None) -> str:
    existing_ids = existing_ids or set()
    for _ in range(10):
        candidate = "TKT-" + secrets.token_hex(4).upper()
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("Could not generate a unique ticket ID after 10 attempts.")


def _now_display() -> str:
    now = datetime.datetime.now()
    return now.strftime("%d-%m-%Y") + " " + now.strftime("%I:%M %p").lstrip("0")


########################
# Chat summary generation
########################
# A short LLM-generated summary of the relevant chat history, stored on
# the ticket row so a human picking this up later doesn't have to re-read
# a full transcript (which conversation_manager.py's Session doesn't even
# persist anywhere durable — session.history is in-memory and gone once
# the session expires). This is the one piece of the ticket that
# genuinely benefits from an LLM rather than a rule.
_groq_client = None


def get_groq_client():
    # Local, separate client instance rather than importing
    # conversation_manager.get_groq_client() — same reasoning
    # email_service.py gives for not importing from conversation_manager:
    # avoids a circular import (conversation_manager -> support_ticket_manager
    # -> conversation_manager) and keeps this module usable standalone
    # from the CLI.
    global _groq_client
    if _groq_client is not None:
        return _groq_client

    try:
        from groq import Groq
    except ImportError as e:
        print(f"[support_ticket_manager] groq package not available, "
              f"LLM-based features (chat summary, urgency check) disabled: {e}")
        return None

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    _groq_client = Groq(api_key=api_key)
    return _groq_client


def summarize_chat(history: List[Dict[str, str]], max_turns: int = 12) -> str:
    """Summarize the last `max_turns` messages of a conversation into 2-3
    sentences for a human agent. Falls back to a naive one-line summary
    if Groq isn't configured or the call fails — this must never raise,
    since a missing summary shouldn't block ticket creation."""
    if not history:
        return ""

    trimmed = history[-max_turns:]
    transcript = "\n".join(
        f"{turn.get('role', 'user')}: {turn.get('content', '')}" for turn in trimmed
    )

    client = get_groq_client()
    if client is None:
        last_user = next((t["content"] for t in reversed(trimmed) if t.get("role") == "user"), "")
        return f"(auto-summary unavailable) Last user message: {last_user}"[:300]

    try:
        model = os.getenv("GROQ_MODEL_NAME")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Summarize this customer support chat in 2-3 concise sentences "
                    "for a human support agent who has not seen the conversation. "
                    "Focus on: what the user wants, what's already been tried, and "
                    "any specific facts (dates, IDs, names) mentioned. No preamble."
                )},
                {"role": "user", "content": transcript},
            ],
            temperature=0.3,
            max_tokens=130,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[support_ticket_manager] Chat summary generation failed (non-fatal): {e}")
        last_user = next((t["content"] for t in reversed(trimmed) if t.get("role") == "user"), "")
        return f"(auto-summary failed) Last user message: {last_user}"[:300]


########################
# Returning-customer check
########################
def _has_previous_booking(email: str) -> bool:
    """Whether this email has ANY existing booking on file (any status) —
    i.e. whether this person is a returning customer rather than a brand
    new prospect. Reuses booking_manager.find_any_booking_by_email(), the
    same data lookup conversation_manager.py already relies on to answer
    this exact question elsewhere (e.g. pre-filling name/phone during
    ticket collection), so "existing customer" is decided one way,
    against the real Bookings record, everywhere in the codebase rather
    than being guessed from the conversation text.

    Never raises: a Sheets/network hiccup on this lookup is a reason to
    fall back to treating the person as a new customer (no priority
    bump), not a reason to fail ticket creation outright — same
    never-fails contract as summarize_chat() above.
    """
    try:
        return bool(find_any_booking_by_email(email))
    except Exception as e:
        print(f"[support_ticket_manager] Previous-booking lookup failed, "
              f"treating as new customer: {e}")
        return False


#########################
# Core ticket operations
#########################
def create_ticket(name: str, email: str, phone: str, category: str,
                   description: str, subcategory: str = "",
                   booking_id: str = "", chat_history: Optional[List[Dict[str, str]]] = None
                   ) -> Dict[str, Any]:
    """Create a new support ticket: validate, assign priority + escalation,
    write to Sheets, return the full record (including whether this
    ticket needs human follow-up so conversation_manager.py can phrase
    its reply accordingly)."""

    validate_ticket_fields(name, email, phone, category)
    normalized_email = str(email).strip().lower()
    name = str(name).strip().title()
    phone_display = ""
    if phone:
        phone_display = to_local_pk_format(normalize_phone(phone))

    # Same-category ticket cap: computed early and checked BEFORE any of
    # the more expensive work below (priority/escalation logic, LLM chat
    # summarization, Sheets writes) so a rejected submission doesn't pay
    # for work that's about to be thrown away. A 2nd ticket from the same
    # email in the SAME category is blocked outright rather than just
    # bumping priority — see MAX_TICKETS_PER_EMAIL_CATEGORY and
    # TicketLimitExceededError. Scoped per (email, category): the same
    # email can still open tickets in a different category without limit.
    same_category_count = count_tickets_by_email_and_category(normalized_email, category)
    if same_category_count >= MAX_TICKETS_PER_EMAIL_CATEGORY:
        raise TicketLimitExceededError(
            normalized_email, category, same_category_count, MAX_TICKETS_PER_EMAIL_CATEGORY
        )

    open_tickets = find_open_tickets_by_email(normalized_email)
    repeated_attempts = len(open_tickets) >= 2

    priority = determine_priority(category, subcategory, description, repeated_attempts)

    # Returning-customer bump: someone with an existing booking on file
    # (any status, any category — see _has_previous_booking()) is a known,
    # verified relationship rather than a brand-new prospect, so their
    # issue gets nudged up exactly one priority step (Low->Medium,
    # Medium->High) via the same one-step nudge bump_priority_one_level()
    # has always used, rather than a hard override to High. A genuinely
    # new customer with no booking on file keeps whatever priority
    # determine_priority() already assigned.
    has_previous_booking = _has_previous_booking(normalized_email)
    if has_previous_booking:
        priority = bump_priority_one_level(priority)

    escalate = requires_escalation(category, priority, repeated_attempts)
    reason = escalation_reason(category, priority, repeated_attempts,
                                previous_booking_bump=has_previous_booking) if escalate else ""

    chat_summary = summarize_chat(chat_history or [])

    existing_ids = {r.get("Ticket ID", "") for r in fetch_all_tickets()}
    ticket_id = generate_ticket_id(existing_ids)

    ensure_sheet_exists()
    service = get_sheets_service()
    now_display = _now_display()
    row = [
        ticket_id,
        CATEGORY_LABELS.get(category, category),
        subcategory,
        priority,
        Status.OPEN,
        name,
        normalized_email,
        phone_display,
        str(description).strip(),
        booking_id,
        chat_summary,
        "Yes" if escalate else "No",
        reason,
        "",  # Resolution Notes
        now_display,
        now_display,
    ]
    last_col = chr(ord('A') + len(HEADERS) - 1)
    service.spreadsheets().values().append(
        spreadsheetId=CHATBOT_TICKETS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}1",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        insertDataOption="INSERT_ROWS",
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()

    result = dict(zip(HEADERS, row))

    # Email is best-effort and must never roll back a ticket that's
    # already real in Sheets — same principle booking_manager.py follows
    # for send_booking_confirmation(). Both sends are independent: a
    # failed customer confirmation shouldn't skip notifying the team, and
    # vice versa.
    email_result = send_ticket_confirmation(result)
    result["email_sent"] = email_result["sent"]
    if not email_result["sent"]:
        result["email_error"] = email_result.get("error", "")

    if escalate:
        escalation_result = send_escalation_notice(result)
        result["escalation_email_sent"] = escalation_result["sent"]
        if not escalation_result["sent"]:
            result["escalation_email_error"] = escalation_result.get("error", "")

    return result


def _update_row(ticket_id: str, updates: Dict[str, str]) -> Dict[str, Any]:
    """Shared helper: patch specific columns on an existing ticket row by
    ID, always bumping Updated At. Used by update_ticket_status(),
    resolve_ticket(), and escalate_ticket() so there's one place that
    knows how to locate+rewrite a row."""
    record = find_ticket_by_id(ticket_id)
    if record is None:
        raise TicketError(f"No ticket found with ID '{ticket_id}'.")

    updates["Updated At"] = _now_display()
    merged = {**record, **updates}
    row = [merged.get(h, "") for h in HEADERS]

    service = get_sheets_service()
    row_number = record["row_number"]
    last_col = chr(ord('A') + len(HEADERS) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=CHATBOT_TICKETS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A{row_number}:{last_col}{row_number}",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()

    return dict(zip(HEADERS, row))


def update_ticket_contact_info(ticket_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update contact information (Name, Email, Phone) on an existing ticket."""
    clean_updates = {}
    if updates.get("name"):
        clean_updates["Lead Name"] = updates["name"].strip().title()
    if updates.get("email"):
        clean_updates["Email"] = updates["email"].strip().lower()
    if updates.get("phone"):
        try:
            phone_normalized = normalize_phone(updates["phone"])
            validate_phone_number(phone_normalized)
            clean_updates["Contact Number"] = to_local_pk_format(phone_normalized)
        except Exception:
            clean_updates["Contact Number"] = updates["phone"].strip()

    if not clean_updates:
        return find_ticket_by_id(ticket_id)
        
    return _update_row(ticket_id, clean_updates)


def update_ticket_status(ticket_id: str, new_status: str) -> Dict[str, Any]:
    valid_statuses = {Status.OPEN, Status.IN_PROGRESS, Status.RESOLVED, Status.CLOSED}
    if new_status not in valid_statuses:
        raise TicketError(f"Invalid status '{new_status}'. Valid: {', '.join(valid_statuses)}")
    return _update_row(ticket_id, {"Status": new_status})


def resolve_ticket(ticket_id: str, resolution_notes: str = "") -> Dict[str, Any]:
    return _update_row(ticket_id, {
        "Status": Status.RESOLVED,
        "Resolution Notes": resolution_notes,
    })


def close_ticket(ticket_id: str) -> Dict[str, Any]:
    return _update_row(ticket_id, {"Status": Status.CLOSED})


def escalate_ticket(ticket_id: str, reason: str = "") -> Dict[str, Any]:
    """Manually escalate a ticket that wasn't auto-escalated at creation
    time — e.g. the user got increasingly frustrated a few messages
    later, or a category-4 issue turned out to need a manager after all.

    Also bumps Priority to High: escalating a ticket is a statement that
    it needs urgent human attention, so the priority column should
    actually reflect that rather than leaving it at whatever level it
    was assigned at creation — otherwise "this has been escalated" and
    "this is High priority" could silently disagree with each other on
    the same row.
    """
    record = find_ticket_by_id(ticket_id)
    if record is None:
        raise TicketError(f"No ticket found with ID '{ticket_id}'.")

    was_already_escalated = str(record.get("Escalated To Human", "")).strip().lower() == "yes"

    updated = _update_row(ticket_id, {
        "Escalated To Human": "Yes",
        "Escalation Reason": reason or record.get("Escalation Reason", "") or "Manually escalated.",
        "Priority": Priority.HIGH,
        "Status": Status.IN_PROGRESS if record.get("Status") == Status.OPEN else record.get("Status"),
    })

    # Only send the notice on the transition INTO escalated, not on every
    # subsequent call (e.g. re-running escalate_ticket to update the
    # reason text shouldn't re-notify the team from scratch each time).
    if not was_already_escalated:
        escalation_result = send_escalation_notice(updated)
        updated["escalation_email_sent"] = escalation_result["sent"]
        if not escalation_result["sent"]:
            updated["escalation_email_error"] = escalation_result.get("error", "")

    return updated


def find_most_recent_ticket_by_email_and_category(email: str, category: str) -> Optional[Dict[str, Any]]:
    """The single most recently created ticket this email has in this
    SAME category, regardless of status — used when someone hits
    MAX_TICKETS_PER_EMAIL_CATEGORY and the right move is to escalate
    their existing, most current issue rather than create yet another
    row for the same underlying problem.
    """
    category_label = CATEGORY_LABELS.get(category, category)
    matches = [t for t in find_tickets_by_email(email) if t.get("Category", "") == category_label]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_create(args):
    try:
        result = create_ticket(
            name=args.name, email=args.email, phone=args.phone,
            category=args.category, description=args.description,
            subcategory=args.subcategory or "", booking_id=args.booking_id or "",
        )
        print("Ticket created.")
        print(result)
    except (LeadValidationError, TicketError) as e:
        print(f"Ticket creation failed: {e}")


def cmd_status(args):
    result = find_ticket_by_id(args.ticket_id)
    if result is None:
        print(f"No ticket found with ID '{args.ticket_id}'.")
    else:
        print(result)


def cmd_list(args):
    records = fetch_all_tickets()
    if args.status:
        records = [r for r in records if r.get("Status", "") == args.status]
    if not records:
        print("(no matching tickets)")
        return
    for r in records:
        print(r)


def cmd_resolve(args):
    try:
        result = resolve_ticket(args.ticket_id, args.notes or "")
        print("Ticket resolved.")
        print(result)
    except TicketError as e:
        print(f"Failed: {e}")


def cmd_escalate(args):
    try:
        result = escalate_ticket(args.ticket_id, args.reason or "")
        print("Ticket escalated.")
        print(result)
    except TicketError as e:
        print(f"Failed: {e}")


def build_parser():
    parser = argparse.ArgumentParser(prog="support_ticket_manager.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("name")
    p_create.add_argument("email")
    p_create.add_argument("phone")
    p_create.add_argument("--category", required=True, choices=sorted(ALL_CATEGORIES))
    p_create.add_argument("--subcategory", default="")
    p_create.add_argument("--description", required=True)
    p_create.add_argument("--booking-id", default="")
    p_create.set_defaults(func=cmd_create)

    p_status = sub.add_parser("status")
    p_status.add_argument("ticket_id")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.set_defaults(func=cmd_list)

    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("ticket_id")
    p_resolve.add_argument("--notes", default="")
    p_resolve.set_defaults(func=cmd_resolve)

    p_escalate = sub.add_parser("escalate")
    p_escalate.add_argument("ticket_id")
    p_escalate.add_argument("--reason", default="")
    p_escalate.set_defaults(func=cmd_escalate)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()