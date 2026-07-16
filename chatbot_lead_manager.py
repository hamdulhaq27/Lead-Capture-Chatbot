"""
chatbot_lead_manager.py
=========================
Persists leads captured through the chatbot (general conversation or the
booking flow) into a dedicated "Chatbot_Leads" Google Sheet — deliberately
SEPARATE from lead_manager_2.py's "Leads_2" sheet (per project decision),
so chatbot-sourced leads don't mix with whatever Leads_2 is used for
elsewhere.

Reuses validators/normalizers from lead_manager_2.py rather than
duplicating them (normalize_phone, validate_phone_number, EMAIL_REGEX,
NAME_REGEX) — one source of truth for "what is a valid PK phone number".

Schema is intentionally looser than Leads_2's (no Budget Range/Timeline
enums) since chatbot leads come from free-form conversation, not a
structured sales form — company/requirement/service are optional.

CLI usage
---------
python chatbot_lead_manager.py fetch-all
python chatbot_lead_manager.py append "Name=Ali Khan" "Email=ali@example.com" "Phone=03001234567" "Source=chatbot_conversation"
"""

#########
# Imports
#########
import os
import argparse
import datetime
from typing import Dict, Any, List, Optional

from google_services import create_service, GoogleSheetsHelper
from lead_manager_2 import (
    normalize_phone,
    to_local_pk_format,
    validate_phone_number,
    EMAIL_REGEX,
    NAME_REGEX,
    LeadValidationError,
)

# Configuration
CLIENT_SECRET_FILE = "Client_Secret.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CHATBOT_LEADS_SPREADSHEET_ID = os.environ.get("CHATBOT_LEADS_SPREADSHEET_ID")
SHEET_NAME = "Chatbot_Leads"

HEADERS = ["Lead Name", "Email", "Contact Number", "Company",
           "Requirement", "Service Interested In", "Source",
           "Lead Status", "Created At", "Booking ID"]

# Cache service to avoid unnecessary sheet recreation
_service_cache = None
_sheet_verified = False

def get_service():
    global _service_cache
    
    if(_service_cache is None):
        _service_cache = create_service(CLIENT_SECRET_FILE, "sheets", "v4", SCOPES)
    
    return _service_cache


def ensure_sheet_exists(force: bool = False):
    global _sheet_verified
    
    if(_sheet_verified and not force):
        return

    service = get_service()
    spreadsheet = service.spreadsheets().get(spreadsheetId=CHATBOT_LEADS_SPREADSHEET_ID).execute()
    existing_sheets = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]

    if(SHEET_NAME not in existing_sheets):
        service.spreadsheets().batchUpdate(
            spreadsheetId=CHATBOT_LEADS_SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]}
        ).execute()
        print(f"Created new worksheet: '{SHEET_NAME}'")

    last_col = chr(ord('A') + len(HEADERS) - 1)
    result = service.spreadsheets().values().get(
        spreadsheetId=CHATBOT_LEADS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}1"
    ).execute()
    values = result.get("values", [])

    if not values or not values[0]:
        service.spreadsheets().values().update(
            spreadsheetId=CHATBOT_LEADS_SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:{last_col}1",
            valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
            body={"majorDimension": "ROWS", "values": [HEADERS]}
        ).execute()
        print(f"Headers written to '{SHEET_NAME}': {HEADERS}")

    _sheet_verified = True


def fetch_all_leads() -> List[Dict[str, str]]:
    ensure_sheet_exists()
    service = get_service()
    last_col = chr(ord('A') + len(HEADERS) - 1)
    result = service.spreadsheets().values().get(
        spreadsheetId=CHATBOT_LEADS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}"
    ).execute()
    values = result.get("values", [])
    if not values:
        return []

    header, *rows = values
    records = []
    for i, row in enumerate(rows, start=2):  
        padded = row + [""] * (len(header) - len(row))
        record = dict(zip(header, padded))
        record["row_number"] = i
        records.append(record)
        
    return records


def _check_duplicate(records: List[Dict[str, str]], email: str, phone: str) -> Optional[str]:
    
    email_norm = str(email).strip().lower()
    phone_norm = normalize_phone(phone) if phone else None

    for r in records:
        if(r.get("Email", "").strip().lower() == email_norm):
            return f"a lead with email '{email}' already exists"
        
        if(phone_norm and r.get("Contact Number") and normalize_phone(r["Contact Number"]) == phone_norm):
            return f"a lead with phone '{phone}' already exists"
        
    return None


def capture_lead(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    
    name = str(lead.get("name") or lead.get("Lead Name") or "").strip()
    email = str(lead.get("email") or lead.get("Email") or "").strip().lower()
    phone = str(lead.get("phone") or lead.get("Contact Number") or "").strip()
    company = str(lead.get("company") or lead.get("Company") or "").strip()
    requirement = str(lead.get("requirement") or lead.get("Requirement") or "").strip()
    service = str(lead.get("service") or lead.get("Service Interested In") or "").strip()
    source = str(lead.get("source") or lead.get("Source") or "chatbot_conversation").strip()
    booking_id = str(lead.get("booking_id") or lead.get("Booking ID") or "").strip()

    if(not name):
        raise LeadValidationError("Missing required field: name")
    
    if(not email or not EMAIL_REGEX.match(email)):
        raise LeadValidationError(f"Invalid email format: '{email}'")
    
    if(phone): 
        phone_normalized = normalize_phone(phone)
        validate_phone_number(phone_normalized)
        phone = to_local_pk_format(phone_normalized)

    records = fetch_all_leads()
    conflict = _check_duplicate(records, email, phone)
    
    if(conflict):
        print(f"[chatbot_lead_manager] Skipping duplicate lead: {conflict}")
        return None

    service_client = get_service()
    now = datetime.datetime.now()
    created_at_display = now.strftime("%d-%m-%Y") + " " + now.strftime("%I:%M %p").lstrip("0")
    row = [
        name.title(), email, phone, company, requirement, service, source,
        "New",  # Lead Status starts as "New" for chatbot leads
        created_at_display,
        booking_id,
    ]
    last_col = chr(ord('A') + len(HEADERS) - 1)
    service_client.spreadsheets().values().append(
        spreadsheetId=CHATBOT_LEADS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}1",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        insertDataOption="INSERT_ROWS",
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()

    return dict(zip(HEADERS, row))


def update_lead_booking(old_booking_id: str, new_booking_id: str,
                         name: Optional[str] = None, email: Optional[str] = None,
                         phone: Optional[str] = None, company: Optional[str] = None,
                         requirement: Optional[str] = None,
                         old_email: Optional[str] = None,
                         old_phone: Optional[str] = None) -> Optional[Dict[str, Any]]:
    
    records = fetch_all_leads()
    target_row_number = None
    target_record = None

    old_booking_id_norm = str(old_booking_id).strip().upper()
    for record in records:
        if record.get("Booking ID", "").strip().upper() == old_booking_id_norm:
            target_row_number = record["row_number"]
            target_record = record
            break

    # Fallback: some Chatbot_Leads rows never got a Booking ID written to
    # them in the first place — e.g. leads created by the passive
    # capture_lead_if_present() background scan (which only sees an email
    # mentioned mid-conversation, with no booking context at all), as
    # opposed to capture_lead() being called directly from the booking
    # flow with booking_id set. For those rows the Booking ID match above
    # will never succeed even though the row is clearly "this person's"
    # lead. Fall back to matching on the caller-supplied pre-update
    # email/phone (the contact info the booking had BEFORE this edit,
    # which is what such a row would still contain) so the update isn't
    # silently dropped.
    if target_row_number is None and (old_email or old_phone):
        old_email_norm = str(old_email).strip().lower() if old_email else None
        old_phone_norm = normalize_phone(old_phone) if old_phone else None

        for record in records:
            rec_email = record.get("Email", "").strip().lower()
            rec_phone_raw = record.get("Contact Number", "")
            try:
                rec_phone_norm = normalize_phone(rec_phone_raw) if rec_phone_raw else None
            except LeadValidationError:
                rec_phone_norm = None

            if (old_email_norm and rec_email == old_email_norm) or \
               (old_phone_norm and rec_phone_norm == old_phone_norm):
                target_row_number = record["row_number"]
                target_record = record
                print(f"[chatbot_lead_manager] Matched lead row by contact info "
                      f"(no Booking ID was stored on the row) for booking "
                      f"'{old_booking_id}' — backfilling Booking ID.")
                break

    if target_row_number is None:
        print(f"[chatbot_lead_manager] No lead row found for booking ID '{old_booking_id}' "
              f"or the given contact info — nothing to update.")
        return None

    updated_name = (name if name is not None else target_record.get("Lead Name", "")).strip()
    updated_email = (email if email is not None else target_record.get("Email", "")).strip().lower()
    updated_phone = (phone if phone is not None else target_record.get("Contact Number", "")).strip()
    updated_company = (company if company is not None else target_record.get("Company", "")).strip()
    updated_requirement = (requirement if requirement is not None else target_record.get("Requirement", "")).strip()

    if updated_phone:
        phone_normalized = normalize_phone(updated_phone)
        validate_phone_number(phone_normalized)
        updated_phone = to_local_pk_format(phone_normalized)

    row = [
        updated_name.title(), updated_email, updated_phone, updated_company,
        updated_requirement,
        target_record.get("Service Interested In", ""),
        target_record.get("Source", ""),
        target_record.get("Lead Status", "New"),
        target_record.get("Created At", ""),  # keep original creation timestamp
        new_booking_id,
    ]

    service_client = get_service()
    last_col = chr(ord('A') + len(HEADERS) - 1)
    service_client.spreadsheets().values().update(
        spreadsheetId=CHATBOT_LEADS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A{target_row_number}:{last_col}{target_row_number}",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()

    return dict(zip(HEADERS, row))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_kv_pairs(pairs: List[str]) -> Dict[str, str]:
    result = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid field assignment '{pair}'. Use Field=Value.")
        key, value = pair.split("=", 1)
        result[key.strip()] = value
    return result


def cmd_fetch_all(args):
    records = fetch_all_leads()
    if not records:
        print("(no leads yet)")
        return
    for i, r in enumerate(records, start=1):
        print(f"{i}. {r}")


def cmd_append(args):
    try:
        fields = _parse_kv_pairs(args.fields)
        result = capture_lead(fields)
        if result is None:
            print("Lead was a duplicate — skipped.")
        else:
            print("Lead added successfully.")
            print(result)
    except (LeadValidationError, ValueError) as e:
        print(f"Failed: {e}")


def build_parser():
    parser = argparse.ArgumentParser(prog="chatbot_lead_manager.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("fetch-all")
    p_all.set_defaults(func=cmd_fetch_all)

    p_append = sub.add_parser("append")
    p_append.add_argument("fields", nargs="+")
    p_append.set_defaults(func=cmd_append)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()