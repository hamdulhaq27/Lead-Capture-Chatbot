"""
test_conversation_manager_tickets.py
======================================
Tests for the TICKET FLOW state machine in conversation_manager.py —
regex safety nets, the collecting/confirming state transitions, the
MAX_TICKET_FOLLOWUPS cap, abandon handling, and status-check reuse.

These mock out classify_intent(), classify_ticket_category(),
generate_followup_question(), extract_booking_fields(), and
create_ticket()/find_ticket_by_id() so they run WITHOUT live Groq/Google
credentials — they test the state machine wiring, not the LLM's
judgment (that's covered by test_support_ticket_manager.py and the
manual test-case checklist for live behavior).

Run with:
    pip install pytest --break-system-packages
    pytest test_conversation_manager_tickets.py -v
"""
import re
import support_ticket_manager as stm
import conversation_manager as cm


# ---------------------------------------------------------------------------
# Regex safety nets — these need NO mocking, they're pure string matching
# ---------------------------------------------------------------------------
def test_ticket_id_in_message_triggers_status_check():
    assert cm._looks_like_ticket_status_check("any update on TKT-4F9A21C0?") is True


def test_lowercase_ticket_id_still_matches():
    assert cm._looks_like_ticket_status_check("checking on tkt-4f9a21c0") is True


def test_no_ticket_id_does_not_trigger_status_check():
    assert cm._looks_like_ticket_status_check("what's the status of my order") is False


def test_explicit_human_request_triggers_ticket_safety_net():
    assert cm._looks_like_explicit_ticket_request("I want to speak to a manager") is True
    assert cm._looks_like_explicit_ticket_request("can I talk to a human please") is True


def test_factcheck_phrasing_does_not_trigger_ticket_safety_net():
    # Regression test for a real bug: "the chatbot told me X, that doesn't
    # seem right" is a factual claim to VERIFY via RAG, not an automatic
    # ticket trigger — even though it mentions the chatbot being wrong.
    # Only genuinely unambiguous ticket-worthy phrasing (explicit human
    # request, explicit "file a complaint") should hit this safety net.
    assert cm._looks_like_explicit_ticket_request(
        "The chatbot told me you offer video editing, that doesn't seem right?"
    ) is False
    assert cm._looks_like_explicit_ticket_request(
        "the chatbot gave me incorrect info"
    ) is False
    assert cm._looks_like_explicit_ticket_request(
        "is it true you offer same-day turnaround?"
    ) is False


def test_normal_question_does_not_trigger_ticket_safety_net():
    assert cm._looks_like_explicit_ticket_request("what services do you offer") is False
    assert cm._looks_like_explicit_ticket_request("how much does SEO cost") is False


# ---------------------------------------------------------------------------
# Test fixtures / fakes
# ---------------------------------------------------------------------------
class FakeMonkeypatch:
    """Minimal monkeypatch shim for environments without pytest — pytest
    users can ignore this and use the real `monkeypatch` fixture instead
    (just add it as a parameter, same as the other test file)."""
    def __init__(self):
        self._saved = []

    def setattr(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._saved):
            setattr(obj, name, old)


def _install_ticket_flow_fakes(monkeypatch, fake_sheet):
    """Shared fakes for a full ticket-flow test: deterministic intent
    classification, a fixed category, a stub follow-up question, a
    regex-based (not LLM-based) field extractor, and an in-memory
    create_ticket()/find_ticket_by_id() pair backed by `fake_sheet`."""

    def fake_classify_intent(message, current_state, last_assistant_message=None):
        m = message.lower()
        if "video editing" in m:
            return "start_ticket"
        if "never mind" in m:
            return "abandon"
        if current_state == cm.State.CONFIRMING_TICKET:
            if "yes" in m:
                return "confirm"
            if "no" in m:
                return "deny"
        if current_state == cm.State.COLLECTING_TICKET_INFO:
            return "provide_info"
        # A literal ticket ID is the strongest signal regardless of
        # phrasing — mirrors the real classify_intent()'s
        # _looks_like_ticket_status_check() safety net, which this fake
        # bypasses by fully replacing classify_intent, so it must be
        # reproduced here to stay a faithful stand-in.
        if re.search(r"\bTKT-[A-Z0-9]{8}\b", message, re.IGNORECASE):
            return "check_ticket_status"
        if "status" in m and "ticket" in m:
            return "check_ticket_status"
        return "general_qa"

    def fake_extract_booking_fields(message):
        result = {}
        email_m = re.search(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", message)
        if email_m:
            result["email"] = email_m.group(0)
        phone_m = re.search(r"\b0\d{10}\b", message)
        if phone_m:
            result["phone"] = phone_m.group(0)
        name_m = re.search(r"name is ([A-Za-z ]+?)(?:,|\.|\bemail\b|$)", message)
        if name_m:
            result["name"] = name_m.group(1).strip()
        return result

    def fake_create_ticket(name, email, phone, category, description,
                            subcategory="", booking_id="", chat_history=None):
        tid = f"TKT-TEST{len(fake_sheet) + 1:04d}"
        record = {
            "Ticket ID": tid,
            "Category": stm.CATEGORY_LABELS.get(category, category),
            "Priority": "Medium",
            "Status": "Open",
            "Lead Name": name, "Email": email, "Contact Number": phone,
            "Description": description, "Escalated To Human": "No",
            "Resolution Notes": "",
        }
        fake_sheet.append(record)
        return record

    monkeypatch.setattr(cm, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(cm, "classify_ticket_category",
                         lambda message, history=None: stm.Category.TECHNICAL_SUPPORT)
    monkeypatch.setattr(cm, "generate_followup_question",
                         lambda category, missing, chat_history=None: f"[stub, still need: {missing}]")
    monkeypatch.setattr(cm, "extract_booking_fields", fake_extract_booking_fields)
    monkeypatch.setattr(cm, "create_ticket", fake_create_ticket)
    monkeypatch.setattr(cm, "find_ticket_by_id",
                         lambda tid: next((r for r in fake_sheet if r["Ticket ID"] == tid), None))


# ---------------------------------------------------------------------------
# Full flow: opening message -> collecting -> confirming -> created
# ---------------------------------------------------------------------------
def test_full_ticket_creation_flow(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    session_id = "test-full-flow"
    r1 = cm.handle_message(session_id, "The chatbot told me you offer video editing, that seems wrong")
    assert r1["state"] == cm.State.COLLECTING_TICKET_INFO

    r2 = cm.handle_message(session_id, "My name is Ayesha Raza, email ayesha@example.com, phone 03001234567")
    assert r2["state"] == cm.State.CONFIRMING_TICKET
    assert "ayesha@example.com" in r2["reply"]

    r3 = cm.handle_message(session_id, "yes please submit it")
    assert r3["state"] == cm.State.GENERAL
    assert "TKT-" in r3["reply"]
    assert len(fake_sheet) == 1


def test_ticket_status_check_reuses_last_known_id(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    session_id = "test-status-reuse"
    cm.handle_message(session_id, "The chatbot told me you offer video editing, that seems wrong")
    cm.handle_message(session_id, "My name is Ayesha Raza, email ayesha@example.com, phone 03001234567")
    created = cm.handle_message(session_id, "yes please submit it")
    ticket_id = created["reply"].split("ticket ")[1].split(".")[0]

    status_reply = cm.handle_message(session_id, "what is the status of my ticket")
    assert ticket_id in status_reply["reply"]
    assert "Open" in status_reply["reply"]


def test_ticket_status_check_with_explicit_id_not_in_session(monkeypatch):
    # A user checking on a DIFFERENT ticket than the one this session
    # created should get that ticket's info, not last_known_ticket_id's.
    fake_sheet = [{
        "Ticket ID": "TKT-OTHER001", "Category": "General Inquiry", "Status": "Resolved",
        "Resolution Notes": "Confirmed and closed out.",
    }]
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    session_id = "test-explicit-id"
    result = cm.handle_message(session_id, "any update on TKT-OTHER001?")
    assert "TKT-OTHER001" in result["reply"]
    assert "Resolved" in result["reply"]


def test_ticket_status_check_unknown_id(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    session_id = "test-unknown-id"
    # Correctly-formatted ID (TKT- + 8 hex chars) that just isn't in the
    # sheet — an invalid-length/format ID wouldn't even match the ticket-ID
    # regex, so it's not a fair test of "not found" handling.
    result = cm.handle_message(session_id, "any update on TKT-00000000?")
    assert "couldn't find" in result["reply"].lower()


def test_ticket_status_check_with_no_id_and_no_history_asks_for_one(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    session_id = "test-no-id-no-history"
    result = cm.handle_message(session_id, "what is the status of my ticket")
    assert "TKT-" in result["reply"] or "ticket id" in result["reply"].lower()
    assert result["state"] == cm.State.GENERAL


# ---------------------------------------------------------------------------
# Abandon path
# ---------------------------------------------------------------------------
def test_abandon_mid_ticket_flow_resets_session(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    session_id = "test-abandon"
    r1 = cm.handle_message(session_id, "The chatbot told me you offer video editing, that seems wrong")
    assert r1["state"] == cm.State.COLLECTING_TICKET_INFO

    r2 = cm.handle_message(session_id, "never mind")
    assert r2["state"] == cm.State.GENERAL

    session = cm.get_session(session_id)
    assert session.ticket_category is None
    assert session.collected == {}
    assert len(fake_sheet) == 0, "abandoning must not create a ticket"


def test_deny_at_confirmation_returns_to_collecting(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    session_id = "test-deny-confirm"
    cm.handle_message(session_id, "The chatbot told me you offer video editing, that seems wrong")
    r2 = cm.handle_message(session_id, "My name is Ayesha Raza, email ayesha@example.com, phone 03001234567")
    assert r2["state"] == cm.State.CONFIRMING_TICKET

    r3 = cm.handle_message(session_id, "no wait, let me fix something")
    assert r3["state"] == cm.State.COLLECTING_TICKET_INFO
    assert len(fake_sheet) == 0, "denying at confirmation must not create a ticket"


# ---------------------------------------------------------------------------
# MAX_TICKET_FOLLOWUPS hard cap
# ---------------------------------------------------------------------------
def test_max_followups_cap_bails_out_gracefully(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)
    # Force extraction to NEVER succeed, so the collecting state can never
    # naturally progress to confirming — this is what the cap protects
    # against (an infinite follow-up loop).
    monkeypatch.setattr(cm, "extract_booking_fields", lambda message: {})

    session_id = "test-cap"
    r = cm.handle_message(session_id, "The chatbot told me you offer video editing, that seems wrong")
    assert r["state"] == cm.State.COLLECTING_TICKET_INFO

    last_state = r["state"]
    for _ in range(cm.MAX_TICKET_FOLLOWUPS + 2):
        r = cm.handle_message(session_id, "still no useful info here")
        last_state = r["state"]
        if last_state != cm.State.COLLECTING_TICKET_INFO:
            break

    assert last_state == cm.State.GENERAL, "should bail out rather than loop forever"
    assert len(fake_sheet) == 0, "should not create an incomplete ticket without name+email"


def test_max_followups_cap_submits_with_partial_info_if_name_and_email_known(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    # Extraction succeeds on the FIRST follow-up (name+email), then never
    # again — simulates a user who gave contact info but won't answer a
    # category-specific follow-up (e.g. which_service for Service Request).
    call_count = {"n": 0}
    def flaky_extract(message):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"name": "Test User", "email": "test@example.com", "phone": "03001112222"}
        return {}
    monkeypatch.setattr(cm, "extract_booking_fields", flaky_extract)
    monkeypatch.setattr(cm, "classify_ticket_category",
                         lambda message, history=None: stm.Category.SERVICE_REQUEST)  # has a REQUIRED_FIELDS entry

    session_id = "test-cap-partial"
    r = cm.handle_message(session_id, "The chatbot told me you offer video editing, that seems wrong")
    last_state = r["state"]
    for _ in range(cm.MAX_TICKET_FOLLOWUPS + 2):
        r = cm.handle_message(session_id, "I don't know, just fix it")
        last_state = r["state"]
        if last_state != cm.State.COLLECTING_TICKET_INFO:
            break

    # Should reach CONFIRMING_TICKET (name+email known) rather than bailing
    # to GENERAL, even though which_service was never provided.
    assert last_state == cm.State.CONFIRMING_TICKET


# ---------------------------------------------------------------------------
# Category stays fixed once set (doesn't drift mid-flow)
# ---------------------------------------------------------------------------
def test_ticket_category_does_not_drift_mid_collection(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)
    # If classify_ticket_category were called again mid-flow and returned
    # something different, this would catch it — session.ticket_category
    # must only be set once, at start_ticket entry.
    calls = []
    original = stm.Category.TECHNICAL_SUPPORT
    def tracking_classify(message, history=None):
        calls.append(message)
        return original
    monkeypatch.setattr(cm, "classify_ticket_category", tracking_classify)

    session_id = "test-no-drift"
    cm.handle_message(session_id, "The chatbot told me you offer video editing, that seems wrong")
    cm.handle_message(session_id, "My name is Ayesha Raza, email ayesha@example.com, phone 03001234567")

    assert len(calls) == 1, "category should be classified once, not re-classified per turn"


# ---------------------------------------------------------------------------
# Lead-capture CTA acceptance — the lightweight path for "the chatbot said
# X, is that right?" style questions that RAG couldn't answer confidently.
# Regression coverage for a real bug: this used to force a 5-turn
# one-field-per-message interrogation through the full ticket flow instead
# of a single combined ask.
# ---------------------------------------------------------------------------
def _install_leadcapture_fakes(monkeypatch, fake_sheet):
    def fake_classify_intent(message, current_state, last_assistant_message=None):
        if current_state == cm.State.COLLECTING_CONTACT_ONLY:
            return "provide_info"
        if (last_assistant_message and cm._looks_like_leadcapture_cta(last_assistant_message)
                and cm._looks_like_affirmative(message)):
            return "accept_leadcapture"
        return "general_qa"

    def fake_extract_booking_fields(message):
        result = {}
        email_m = re.search(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", message)
        if email_m:
            result["email"] = email_m.group(0)
        phone_m = re.search(r"\b0\d{10}\b", message)
        if phone_m:
            result["phone"] = phone_m.group(0)
        name_m = re.search(r"^([A-Za-z ]+?),", message)
        if name_m:
            result["name"] = name_m.group(1).strip()
        return result

    def fake_create_ticket(name, email, phone, category, description,
                            subcategory="", booking_id="", chat_history=None):
        tid = f"TKT-TEST{len(fake_sheet) + 1:04d}"
        record = {
            "Ticket ID": tid, "Category": stm.CATEGORY_LABELS.get(category, category),
            "Lead Name": name, "Email": email, "Contact Number": phone,
            "Description": description,
        }
        fake_sheet.append(record)
        return record

    monkeypatch.setattr(cm, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(cm, "extract_booking_fields", fake_extract_booking_fields)
    monkeypatch.setattr(cm, "create_ticket", fake_create_ticket)


def test_leadcapture_cta_asks_for_all_three_fields_in_one_message(monkeypatch):
    fake_sheet = []
    _install_leadcapture_fakes(monkeypatch, fake_sheet)

    session_id = "test-leadcapture-single-ask"
    session = cm.get_session(session_id)
    session.history.append({"role": "user", "content": "Do you offer video editing?"})
    session.history.append({"role": "assistant", "content":
        "I couldn't confirm that. Would you like to leave your name and email so our team can follow up?"})

    r1 = cm.handle_message(session_id, "yes please")
    assert r1["state"] == cm.State.COLLECTING_CONTACT_ONLY
    # Must ask for all three at once, not one at a time.
    assert "name" in r1["reply"].lower()
    assert "email" in r1["reply"].lower()
    assert "phone" in r1["reply"].lower()


def test_leadcapture_flow_completes_in_two_turns_total(monkeypatch):
    # Regression test: the original bug took 5 turns (name, email, phone,
    # issue, "what service were you expecting") before giving up entirely.
    # The fix should complete in exactly 2: accept the CTA, then one
    # combined reply with all contact info.
    fake_sheet = []
    _install_leadcapture_fakes(monkeypatch, fake_sheet)

    session_id = "test-leadcapture-two-turns"
    session = cm.get_session(session_id)
    session.history.append({"role": "user", "content":
        "The chatbot told me you offer video editing, that doesn't seem right?"})
    session.history.append({"role": "assistant", "content":
        "I couldn't confirm that in our services. Would you like to leave your name and email so our team can follow up directly?"})

    cm.handle_message(session_id, "yes please")
    final = cm.handle_message(session_id, "Hamd, hamdulhaq946@gmail.com, 03115041955")

    assert final["state"] == cm.State.GENERAL
    assert "TKT-" in final["reply"]
    assert len(fake_sheet) == 1


def test_leadcapture_description_is_the_original_question_not_the_contact_reply(monkeypatch):
    # Regression test for the exact bug: the ticket description must be
    # the user's ORIGINAL question, captured at the moment the CTA is
    # accepted — not overwritten by the "Hamd, email, phone" message that
    # comes later once that message becomes "the most recent user turn".
    fake_sheet = []
    _install_leadcapture_fakes(monkeypatch, fake_sheet)

    session_id = "test-leadcapture-description"
    session = cm.get_session(session_id)
    original_question = "The chatbot told me you offer video editing, that doesn't seem right?"
    session.history.append({"role": "user", "content": original_question})
    session.history.append({"role": "assistant", "content":
        "I couldn't confirm that. Would you like to leave your name and email so our team can follow up?"})

    cm.handle_message(session_id, "yes please")
    cm.handle_message(session_id, "Hamd, hamdulhaq946@gmail.com, 03115041955")

    assert len(fake_sheet) == 1
    assert fake_sheet[0]["Description"] == original_question
    assert "hamdulhaq946" not in fake_sheet[0]["Description"], \
        "description must not be overwritten by the contact-info reply"


def test_leadcapture_partial_info_asks_only_for_whats_missing(monkeypatch):
    fake_sheet = []
    _install_leadcapture_fakes(monkeypatch, fake_sheet)

    session_id = "test-leadcapture-partial"
    session = cm.get_session(session_id)
    session.history.append({"role": "user", "content": "Do you offer video editing?"})
    session.history.append({"role": "assistant", "content":
        "Would you like to leave your name and email so our team can follow up?"})

    cm.handle_message(session_id, "yes")
    # Give only email+phone, no name — should ask just for what's missing,
    # not repeat the full three-field ask.
    r2 = cm.handle_message(session_id, "my email is test@example.com and phone 03001234567")
    assert r2["state"] == cm.State.COLLECTING_CONTACT_ONLY
    assert "name" in r2["reply"].lower()
    assert "email" not in r2["reply"].lower()
    assert "phone" not in r2["reply"].lower()


def test_leadcapture_abandon_resets_cleanly(monkeypatch):
    fake_sheet = []
    _install_leadcapture_fakes(monkeypatch, fake_sheet)

    def classify_with_abandon(message, current_state, last_assistant_message=None):
        if "never mind" in message.lower():
            return "abandon"
        if current_state == cm.State.COLLECTING_CONTACT_ONLY:
            return "provide_info"
        if (last_assistant_message and cm._looks_like_leadcapture_cta(last_assistant_message)
                and cm._looks_like_affirmative(message)):
            return "accept_leadcapture"
        return "general_qa"
    monkeypatch.setattr(cm, "classify_intent", classify_with_abandon)

    session_id = "test-leadcapture-abandon"
    session = cm.get_session(session_id)
    session.history.append({"role": "user", "content": "Do you offer video editing?"})
    session.history.append({"role": "assistant", "content":
        "Would you like to leave your name and email so our team can follow up?"})

    cm.handle_message(session_id, "yes")
    r2 = cm.handle_message(session_id, "never mind")
    assert r2["state"] == cm.State.GENERAL
    assert len(fake_sheet) == 0


def test_leadcapture_cta_detection_excludes_call_booking_offers():
    # _looks_like_leadcapture_cta and _looks_like_call_offer must stay
    # mutually exclusive — a "yes" after a call-booking offer should never
    # be misread as accepting the lead-capture CTA instead.
    call_offer = "Would you like me to set up a discovery call to go over this?"
    assert cm._looks_like_leadcapture_cta(call_offer) is False
    assert cm._looks_like_call_offer(call_offer) is True

    leadcapture_offer = "Would you like to leave your name and email so our team can follow up?"
    assert cm._looks_like_leadcapture_cta(leadcapture_offer) is True
    assert cm._looks_like_call_offer(leadcapture_offer) is False


# ---------------------------------------------------------------------------
# Regression: extract_booking_fields() must reject the literal string
# "null"/"none"/"n/a" from a misbehaving LLM response as if the field were
# absent, not accept it as a real value (this was surfacing as literal
# "Name: null" in ticket summaries).
# ---------------------------------------------------------------------------
def test_extract_booking_fields_rejects_literal_null_string(monkeypatch):
    class FakeChoice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})()

    class FakeCompletions:
        def create(self, **kwargs):
            import json
            return type("R", (), {"choices": [FakeChoice(json.dumps({
                "name": "null", "email": "None", "phone": "N/A",
                "company": None, "note": None,
                "date_phrase": None, "time_phrase": None,
            }))]})()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(cm, "get_groq_client", lambda: FakeClient())

    result = cm.extract_booking_fields("some message")
    assert result["name"] is None, f"expected None, got {result['name']!r}"
    assert result["email"] is None, f"expected None, got {result['email']!r}"
    assert result["phone"] is None, f"expected None, got {result['phone']!r}"


def test_extract_booking_fields_still_accepts_real_values(monkeypatch):
    # Guard against overcorrecting — a real name/email/phone must still
    # come through untouched.
    class FakeChoice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})()

    class FakeCompletions:
        def create(self, **kwargs):
            import json
            return type("R", (), {"choices": [FakeChoice(json.dumps({
                "name": "Hamd", "email": "hamdulhaq946@gmail.com", "phone": "03115041955",
                "company": None, "note": None,
                "date_phrase": None, "time_phrase": None,
            }))]})()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(cm, "get_groq_client", lambda: FakeClient())

    result = cm.extract_booking_fields("some message")
    assert result["name"] == "Hamd"
    assert result["email"] == "hamdulhaq946@gmail.com"
    assert result["phone"] == "03115041955"


# ---------------------------------------------------------------------------
# Regression: a ticket filed after an earlier booking in the SAME session
# must reuse that booking's name/email/phone (via _get_known_booking),
# not restart from scratch and ask again — this was surfacing as
# "Name: null / Email: null / Phone: null" in the confirmation summary
# even though the session had just completed a booking with all three.
# ---------------------------------------------------------------------------
def test_ticket_reuses_identity_from_known_booking_in_same_session(monkeypatch):
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    def classify_start_ticket_on_confirmation(message, current_state, last_assistant_message=None):
        if "i did" in message.lower():
            return "start_ticket"
        return "general_qa"
    monkeypatch.setattr(cm, "classify_intent", classify_start_ticket_on_confirmation)
    monkeypatch.setattr(cm, "extract_booking_fields", lambda message: {})

    known_booking = {
        "Booking ID": "BK-2646BDF4", "Lead Name": "Hamd", "Email": "hamdulhaq946@gmail.com",
        "Contact Number": "03115041955", "Status": "Confirmed",
    }
    monkeypatch.setattr(cm, "find_booking_by_id",
                         lambda bid: known_booking if bid == "BK-2646BDF4" else None)

    session_id = "test-identity-reuse"
    session = cm.get_session(session_id)
    session.last_known_booking_id = "BK-2646BDF4"
    session.history.append({"role": "user", "content": "I never got my booking confirmation email"})
    session.history.append({"role": "assistant", "content": "Can you check spam for it?"})

    result = cm.handle_message(session_id, "i did")

    assert result["state"] == cm.State.CONFIRMING_TICKET
    assert "null" not in result["reply"].lower(), "REGRESSION: null leaked into the ticket summary"
    assert "Hamd" in result["reply"]
    assert "hamdulhaq946@gmail.com" in result["reply"]
    assert "03115041955" in result["reply"]
    assert "BK-2646BDF4" in result["reply"], "should link the ticket to the related booking"


def test_ticket_description_seeded_from_earlier_message_not_just_trigger(monkeypatch):
    # Regression: start_ticket often fires on a short confirmatory reply
    # ("i did") a turn or two after the real issue was described — the
    # description must capture the actual issue, not just "i did".
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)

    def classify_start_ticket_on_confirmation(message, current_state, last_assistant_message=None):
        if "i did" in message.lower():
            return "start_ticket"
        return "general_qa"
    monkeypatch.setattr(cm, "classify_intent", classify_start_ticket_on_confirmation)
    monkeypatch.setattr(cm, "extract_booking_fields",
                         lambda message: {"name": "Hamd", "email": "hamdulhaq946@gmail.com", "phone": "03115041955"})
    monkeypatch.setattr(cm, "find_booking_by_id", lambda bid: None)  # no known booking this time

    session_id = "test-description-seed"
    session = cm.get_session(session_id)
    session.history.append({"role": "user", "content": "I never got my booking confirmation email"})
    session.history.append({"role": "assistant", "content": "Can you check spam for it?"})

    result = cm.handle_message(session_id, "i did")

    assert result["state"] == cm.State.CONFIRMING_TICKET, f"expected CONFIRMING_TICKET, got {result['state']}"
    assert "i did i did" not in result["reply"], "REGRESSION: triggering message duplicated in description"
    assert "I never got my booking confirmation email" in result["reply"]


def test_ticket_does_not_reuse_stale_or_cancelled_booking(monkeypatch):
    # _get_known_booking() already clears last_known_booking_id if the
    # booking is no longer Confirmed — confirm the ticket flow respects
    # that and falls back to asking for contact info instead of reusing
    # stale data.
    fake_sheet = []
    _install_ticket_flow_fakes(monkeypatch, fake_sheet)
    monkeypatch.setattr(cm, "classify_intent", lambda message, current_state, last_assistant_message=None: "start_ticket")
    monkeypatch.setattr(cm, "extract_booking_fields", lambda message: {})

    cancelled_booking = {
        "Booking ID": "BK-STALE0001", "Lead Name": "Someone", "Email": "someone@example.com",
        "Contact Number": "03009998888", "Status": "Cancelled",
    }
    monkeypatch.setattr(cm, "find_booking_by_id",
                         lambda bid: cancelled_booking if bid == "BK-STALE0001" else None)

    session_id = "test-stale-booking"
    session = cm.get_session(session_id)
    session.last_known_booking_id = "BK-STALE0001"

    result = cm.handle_message(session_id, "the chatbot told me something wrong")
    assert "someone@example.com" not in result["reply"], "must not reuse a cancelled booking's contact info"
    assert session.last_known_booking_id is None, "_get_known_booking should have cleared the stale reference"