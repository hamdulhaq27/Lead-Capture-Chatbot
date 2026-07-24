"""
booking_manager.py
===================
Business-rule layer for discovery-call bookings, sitting on top of
GoogleCalendarHelper (calendar mechanics) and writing to a dedicated
"Bookings" Google Sheet.

Enforces booking_policy.txt:
  - 2-hour minimum / 30-day maximum advance notice
  - one active booking per email at a time (surfaces the existing booking
    instead of creating a duplicate)
  - required fields: name, email, phone (company/note optional)
  - reschedule = cancel + rebook, new booking ID issued
  - a booking is only "final" once the Calendar event AND the Sheets row
    both exist — see create_booking()'s rollback behaviour on partial failure

Booking ID format: "BK-" + 8 random hex chars (e.g. BK-A1B2C3D4).

This module does NOT send email — see email_service.py (Module E). It DOES
return everything email_service.py needs (booking_id, slot, lead info).

CLI usage
---------
python booking_manager.py slots 2026-07-20
python booking_manager.py book "Jane Doe" jane@example.com 03001234567 
    2026-07-20 14:30 --company "Acme Inc" --note "Interested in SEO"
python booking_manager.py cancel BK-A1B2C3D4
python booking_manager.py lookup jane@example.com
"""

#########
# Imports
#########
import os
import re
import secrets
import argparse
import datetime
from typing import Optional, Dict, Any, List

from google_services import create_service, GoogleSheetsHelper, GoogleCalendarHelper

# Reuse validators/normalizers from lead_manager_2.py
from lead_manager_2 import (
    normalize_phone,
    to_local_pk_format,
    validate_phone_number,
    EMAIL_REGEX,
    NAME_REGEX,
    LeadValidationError,
)
from email_service import send_booking_confirmation, send_cancellation_confirmation

# Used only by reschedule_booking() to keep the Chatbot_Leads sheet in
# sync when a reschedule changes name/email/phone — see the call site
# below for why this belongs here rather than in the caller.
import chatbot_lead_manager

###############
# Configuration
###############
CLIENT_SECRET_FILE = "Client_Secret.json"
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

BOOKINGS_SPREADSHEET_ID = os.environ.get("BOOKINGS_SPREADSHEET_ID", "REPLACE_WITH_REAL_SPREADSHEET_ID")
SHEET_NAME = "Bookings"

HEADERS = [
    "Booking ID", "Lead Name", "Email", "Contact Number", "Company",
    "Note", "Requested Date", "Requested Time", "Calendar Event ID",
    "Status", "Created At",
]

MIN_ADVANCE_NOTICE = datetime.timedelta(hours=2)
MAX_ADVANCE_NOTICE = datetime.timedelta(days=30)


def _format_date_for_sheet(iso_date: str) -> str:
    # Convert 'YYYY-MM-DD' (internal storage/logic format) to 'DD-MM-YYYY'
    
    try:
        dt = datetime.datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return iso_date  # fall back to raw value rather than crash


def _format_time_for_sheet(time_24h: str) -> str:
    # Convert 'HH:MM' (24-hour, internal format) to '3:30 PM' style
    
    try:
        dt = datetime.datetime.strptime(time_24h, "%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return time_24h


def _format_datetime_for_sheet(dt: datetime.datetime) -> str:
    # Format a full datetime as 'DD-MM-YYYY 3:30 PM'
    date_part = dt.strftime("%d-%m-%Y")
    time_part = dt.strftime("%I:%M %p").lstrip("0")
    return f"{date_part} {time_part}"


class BookingError(Exception):
    pass

####################
# Service singletons
####################
_sheets_service = None
_calendar_helper = None
_sheet_verified = False


def get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        _sheets_service = create_service(CLIENT_SECRET_FILE, "sheets", "v4", SHEETS_SCOPES)
    return _sheets_service


def get_calendar_helper() -> GoogleCalendarHelper:
    global _calendar_helper
    if _calendar_helper is None:
        service = create_service(CLIENT_SECRET_FILE, "calendar", "v3", CALENDAR_SCOPES)
        _calendar_helper = GoogleCalendarHelper(service)
    return _calendar_helper


def ensure_sheet_exists(force: bool = False):
    
    global _sheet_verified
    if _sheet_verified and not force:
        return

    service = get_sheets_service()
    spreadsheet = service.spreadsheets().get(spreadsheetId=BOOKINGS_SPREADSHEET_ID).execute()
    existing_sheets = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]

    if SHEET_NAME not in existing_sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=BOOKINGS_SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]}
        ).execute()
        print(f"Created new worksheet: '{SHEET_NAME}'")

    last_col = chr(ord('A') + len(HEADERS) - 1)  
    result = service.spreadsheets().values().get(
        spreadsheetId=BOOKINGS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}1"
    ).execute()
    values = result.get("values", [])

    if not values or not values[0]:
        service.spreadsheets().values().update(
            spreadsheetId=BOOKINGS_SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:{last_col}1",
            valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
            body={"majorDimension": "ROWS", "values": [HEADERS]}
        ).execute()
        print(f"Headers written to '{SHEET_NAME}': {HEADERS}")

    _sheet_verified = True

############
# Validation
############
def validate_booking_fields(name: str, email: str, phone: str) -> None:
    
    if(not name or not str(name).strip()):
        raise LeadValidationError("Missing required field: Lead Name")
    
    if(not NAME_REGEX.match(str(name).strip())):
        raise LeadValidationError(
            f"Invalid Lead Name '{name}': only letters, spaces, apostrophes, "
            "hyphens, and periods are allowed."
        )

    if(not email or not EMAIL_REGEX.match(str(email).strip())):
        raise LeadValidationError(f"Invalid email format: '{email}'")

    if(not phone):
        raise LeadValidationError("Missing required field: Contact Number")
    normalized_phone = normalize_phone(phone)
    validate_phone_number(normalized_phone)  


def validate_advance_notice(start_dt: datetime.datetime) -> None:
    
    now = GoogleCalendarHelper.now()
    start_dt = GoogleCalendarHelper.to_local(start_dt)

    if(start_dt <= now):
        raise BookingError(
            "That date/time is in the past. Please choose a future slot."
        )

    if(start_dt - now < MIN_ADVANCE_NOTICE):
        raise BookingError(
            "Bookings must be made at least 2 hours in advance. "
            "Please choose a later slot today, or a slot tomorrow."
        )

    if(start_dt - now > MAX_ADVANCE_NOTICE):
        raise BookingError(
            "Bookings can't be made more than 30 days in advance. "
            "Please choose a closer date."
        )

####################
# Sheet read helpers
####################
def fetch_all_bookings() -> List[Dict[str, str]]:
    ensure_sheet_exists()
    service = get_sheets_service()
    last_col = chr(ord('A') + len(HEADERS) - 1)
    result = service.spreadsheets().values().get(
        spreadsheetId=BOOKINGS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:{last_col}"
    ).execute()
    values = result.get("values", [])
    
    if(not values):
        return []

    header, *rows = values
    records = []
    for idx, row in enumerate(rows, start=2):
        padded = row + [""] * (len(header) - len(row))
        record = dict(zip(header, padded))
        record["row_number"] = idx
        records.append(record)
        
    return records


def find_active_booking_by_email(email: str) -> Optional[Dict[str, Any]]:
    
    email_norm = str(email).strip().lower()
    for record in fetch_all_bookings():
        if (record.get("Email", "").strip().lower() == email_norm
                and record.get("Status", "").strip() == "Confirmed"):
            return record
    return None


def find_any_booking_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Most recent booking record for this email REGARDLESS of status
    (Confirmed, Cancelled, or anything else) — unlike
    find_active_booking_by_email above, which is specifically "does this
    person have a LIVE booking right now" (used for reschedule/cancel).
    This one answers a different question: "has this person ever made a
    booking with us at all", used to verify an existing-customer
    relationship before filing certain support tickets. A cancelled
    booking still counts — cancelling doesn't erase that they were once
    a real lead/customer.

    Returns the most recent match (highest row_number, since the sheet
    is append-only) if there are several.
    """
    email_norm = str(email).strip().lower()
    matches = [
        record for record in fetch_all_bookings()
        if record.get("Email", "").strip().lower() == email_norm
    ]
    if not matches:
        return None
    return max(matches, key=lambda r: r.get("row_number", 0))


def find_active_booking_by_phone(normalized_phone: str) -> Optional[Dict[str, Any]]:
    """Same idea as find_active_booking_by_email but keyed on phone
    number. Compares normalized phone forms so formatting differences
    (spaces, dashes, +92 vs 0 prefix, etc.) don't cause a false miss —
    both sides of the comparison go through normalize_phone() first.
    """
    phone_norm = normalize_phone(normalized_phone)
    for record in fetch_all_bookings():
        existing_phone = record.get("Contact Number", "")
        try:
            existing_phone_norm = normalize_phone(existing_phone) if existing_phone else ""
        except LeadValidationError:
            # A malformed/legacy phone value already in the sheet
            # shouldn't crash this lookup — just treat it as non-matching.
            existing_phone_norm = None
        if (existing_phone_norm == phone_norm
                and record.get("Status", "").strip() == "Confirmed"):
            return record
    return None


def find_booking_by_id(booking_id: str) -> Optional[Dict[str, Any]]:
    booking_id_norm = str(booking_id).strip().upper()
    for record in fetch_all_bookings():
        if record.get("Booking ID", "").strip().upper() == booking_id_norm:
            return record
    return None

#######################
# Booking ID generation
#######################
def generate_booking_id(existing_ids: Optional[set] = None) -> str:
    # Generate a BK-XXXXXXXX booking ID (8 uppercase hex chars).

    existing_ids = existing_ids or set()
    for _ in range(10):  
        candidate = "BK-" + secrets.token_hex(4).upper()
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("Could not generate a unique booking ID after 10 attempts.")


#########################
# Core booking operations
#########################
def create_booking(name: str, email: str, phone: str, start_dt: datetime.datetime,
                    company: str = "", note: str = "") -> Dict[str, Any]:
    
    validate_booking_fields(name, email, phone)
    normalized_phone = normalize_phone(phone)
    normalized_email = str(email).strip().lower()
    name = str(name).strip().title()

    validate_advance_notice(start_dt)

    # Duplicate-booking check per booking_policy.txt: "Only one active
    # discovery call booking is allowed per email address at a time."
    existing = find_active_booking_by_email(normalized_email)
    if existing:
        raise BookingError(
            f"You already have an active booking (ID: {existing.get('Booking ID')}) "
            f"for {existing.get('Requested Date')} at {existing.get('Requested Time')}. "
            f"Please cancel or reschedule that booking first if you'd like a "
            f"different time."
        )

    cal = get_calendar_helper()
    local_start = GoogleCalendarHelper.to_local(start_dt)

    if not cal.is_slot_available(local_start):
        
        if local_start.weekday() not in cal.BUSINESS_DAYS:
            day_name = local_start.strftime("%A")
            raise BookingError(
                f"{local_start.strftime('%Y-%m-%d')} is a {day_name}, and "
                f"we're only open Monday to Friday. Please choose a weekday."
            )
        if not cal.is_within_business_hours(local_start):
            raise BookingError(
                f"That time is outside business hours (9 AM-6 PM PKT, "
                f"Mon-Fri). Please choose a time in that window."
            )
        if not cal.is_aligned_to_slot_grid(local_start):
            raise BookingError(
                "Our call slots start on the hour or half-hour (e.g. 2:00 "
                "or 2:30). Please choose a time aligned to that."
            )
        raise BookingError(
            "That slot is already booked. Please choose a different time."
        )

    existing_ids = {r.get("Booking ID", "") for r in fetch_all_bookings()}
    booking_id = generate_booking_id(existing_ids)

    # 1. Create the Calendar event first.
    event = cal.create_event(
        start_dt=local_start,
        lead_name=name,
        email=normalized_email,
        phone=to_local_pk_format(normalized_phone),
        note=note,
        booking_id=booking_id,
    )
    event_id = event["id"]

    # Sanity-check log: echoes back exactly what Google Calendar confirmed
    # as the event's start — including its UTC offset. If this ever prints
    # anything other than "+05:00" for a PKT booking, that's a real data
    # bug in the create_event() call. If it correctly prints "+05:00" but
    # the event still LOOKS wrong in the Calendar UI/app, the event data is
    # fine and the mismatch is the *viewing* Google account's Calendar
    # timezone setting (Settings > General > Time zone), not this code.
    print(f"[booking_manager] Calendar confirms event {event_id} "
          f"start={event.get('start', {}).get('dateTime')} "
          f"(requested {local_start.isoformat()})")

    # 2. Write the Sheets row. If this fails, roll back the Calendar event
    # so we don't leave an orphaned, un-cancellable booking behind.
    try:
        ensure_sheet_exists()
        service = get_sheets_service()
        row = [
            booking_id,
            name,
            normalized_email,
            to_local_pk_format(normalized_phone),
            str(company).strip(),
            str(note).strip(),
            _format_date_for_sheet(local_start.strftime("%Y-%m-%d")),
            _format_time_for_sheet(local_start.strftime("%H:%M")),
            event_id,
            "Confirmed",
            _format_datetime_for_sheet(GoogleCalendarHelper.now()),
        ]
        last_col = chr(ord('A') + len(HEADERS) - 1)
        service.spreadsheets().values().append(
            spreadsheetId=BOOKINGS_SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:{last_col}1",
            valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
            insertDataOption="INSERT_ROWS",
            body={"majorDimension": "ROWS", "values": [row]},
        ).execute()
    except Exception:
        # Roll back: don't leave a phantom, un-trackable Calendar booking.
        try:
            cal.delete_event(event_id)
        except Exception:
            pass  # best-effort rollback; original exception is what matters
        raise

    result = {
        "booking_id": booking_id,
        "event_id": event_id,
        "lead_name": name,
        "email": normalized_email,
        "phone": to_local_pk_format(normalized_phone),
        "company": company,
        "note": note,
        "start_dt": local_start,
        "date": local_start.strftime("%Y-%m-%d"),
        "time": local_start.strftime("%H:%M"),
    }

    email_result = send_booking_confirmation(result)
    result["email_sent"] = email_result["sent"]
    if not email_result["sent"]:
        result["email_error"] = email_result["error"]

    return result


def cancel_booking(booking_id: str) -> Dict[str, Any]:
    
    record = find_booking_by_id(booking_id)
    if record is None:
        raise BookingError(f"No booking found with ID '{booking_id}'.")

    if record.get("Status", "").strip() != "Confirmed":
        raise BookingError(
            f"Booking '{booking_id}' is already {record.get('Status', 'not active')} "
            f"and cannot be cancelled again."
        )

    cal = get_calendar_helper()
    event_id = record.get("Calendar Event ID", "")
    if event_id:
        cal.delete_event(event_id)  

    service = get_sheets_service()
    row_number = record["row_number"]
    status_col_index = HEADERS.index("Status")  # 0-based
    status_col_letter = chr(ord('A') + status_col_index)
    service.spreadsheets().values().update(
        spreadsheetId=BOOKINGS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!{status_col_letter}{row_number}",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        body={"majorDimension": "ROWS", "values": [["Cancelled"]]},
    ).execute()

    email_result = send_cancellation_confirmation(record)
    record["email_sent"] = email_result["sent"]
    if not email_result["sent"]:
        record["email_error"] = email_result["error"]

    return record


def reschedule_booking(booking_id: str, new_start_dt: datetime.datetime,
                        name: Optional[str] = None, email: Optional[str] = None,
                        phone: Optional[str] = None, company: Optional[str] = None,
                        note: Optional[str] = None) -> Dict[str, Any]:
    
    record = find_booking_by_id(booking_id)
    if record is None:
        raise BookingError(f"No booking found with ID '{booking_id}'.")
    
    if record.get("Status", "").strip() != "Confirmed":
        raise BookingError(f"Booking '{booking_id}' is not active and cannot be updated.")

    cal = get_calendar_helper()
    new_start_local = GoogleCalendarHelper.to_local(new_start_dt)

    try:
        current_start_naive = datetime.datetime.strptime(
            f"{record.get('Requested Date', '')} {record.get('Requested Time', '')}",
            "%d-%m-%Y %I:%M %p",
        )
        current_start_local = GoogleCalendarHelper.to_local(current_start_naive)
    except ValueError:
        current_start_local = None

    time_changed = current_start_local is None or current_start_local != new_start_local

    if time_changed:
        validate_advance_notice(new_start_dt)

    updated_name = (name if name is not None else record.get("Lead Name", "")).strip()
    updated_email = (email if email is not None else record.get("Email", "")).strip().lower()
    updated_phone_raw = phone if phone is not None else record.get("Contact Number", "")
    updated_company = (company if company is not None else record.get("Company", "")).strip()
    updated_note = (note if note is not None else record.get("Note", "")).strip()

    # Captured BEFORE any of the updated_* values are written anywhere —
    # this booking's contact info as it stood prior to this reschedule,
    # used below as a fallback matcher when syncing the change to
    # Chatbot_Leads (see update_lead_booking()'s own docstring: some lead
    # rows never got a Booking ID recorded, so matching on the OLD
    # contact info is what finds them).
    old_email = record.get("Email", "")
    old_phone = record.get("Contact Number", "")

    validate_booking_fields(updated_name, updated_email, updated_phone_raw)
    updated_phone_normalized = normalize_phone(updated_phone_raw)

    # Reject an email/phone that already belongs to a DIFFERENT active
    # booking. Without this, rescheduling could silently attach someone
    # else's live booking's contact info to this one (or collide with
    # it), since validate_booking_fields only checks FORMAT, never
    # uniqueness — that's the gap create_booking() already closes for
    # brand-new bookings via find_active_booking_by_email(), but
    # reschedule_booking() never had an equivalent check.
    #
    # Excludes this booking's own record from the collision check —
    # keeping (or re-entering) your own existing email/phone unchanged
    # must still be allowed; the point is to block a MATCH ON SOMEONE
    # ELSE'S booking, not to force every reschedule to use brand-new
    # contact info.
    this_booking_id_norm = str(booking_id).strip().upper()

    email_collision = find_active_booking_by_email(updated_email)
    if email_collision and str(email_collision.get("Booking ID", "")).strip().upper() != this_booking_id_norm:
        raise BookingError(
            f"That email address is already tied to a different active "
            f"booking ID. Please "
            f"use a different email, or cancel/reschedule that other "
            f"booking first."
        )

    phone_collision = find_active_booking_by_phone(updated_phone_normalized)
    if phone_collision and str(phone_collision.get("Booking ID", "")).strip().upper() != this_booking_id_norm:
        raise BookingError(
            f"That contact number is already tied to a different active "
            f"booking ID. Please "
            f"use a different number, or cancel/reschedule that other "
            f"booking first."
        )

    event_id = record.get("Calendar Event ID", "")
    if not event_id:
        raise BookingError(
            f"Booking '{booking_id}' has no linked calendar event cannot update it safely."
        )

    try:
        updated_event = cal.update_event(
            event_id=event_id,
            start_dt=new_start_local,
            lead_name=updated_name,
            email=updated_email,
            phone=to_local_pk_format(updated_phone_normalized),
            note=updated_note,
            booking_id=booking_id,
            time_changed=time_changed,
        )
        print(f"[booking_manager] Calendar confirms rescheduled event {event_id} "
              f"start={updated_event.get('start', {}).get('dateTime')} "
              f"(requested {new_start_local.isoformat()})")
    except RuntimeError as e:
      
        raise BookingError(str(e))

    service = get_sheets_service()
    row_number = record["row_number"]
    row = [
        booking_id,
        updated_name.title(),
        updated_email,
        to_local_pk_format(updated_phone_normalized),
        updated_company,
        updated_note,
        _format_date_for_sheet(new_start_local.strftime("%Y-%m-%d")),
        _format_time_for_sheet(new_start_local.strftime("%H:%M")),
        event_id,
        "Confirmed",
        record.get("Created At", ""), 
    ]
    last_col = chr(ord('A') + len(HEADERS) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=BOOKINGS_SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A{row_number}:{last_col}{row_number}",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()

    result = {
        "booking_id": booking_id,  
        "event_id": event_id,
        "lead_name": updated_name,
        "email": updated_email,
        "phone": to_local_pk_format(updated_phone_normalized),
        "company": updated_company,
        "note": updated_note,
        "start_dt": new_start_local,
        "date": new_start_local.strftime("%Y-%m-%d"),
        "time": new_start_local.strftime("%H:%M"),
    }

    # Keep Chatbot_Leads in sync with this reschedule — if the reschedule
    # changed name/email/phone, the lead row for this same booking should
    # reflect it too, rather than silently going stale. Best-effort and
    # non-blocking, same principle as the confirmation email right below:
    # a failure to update the lead sheet must never undo or fail the
    # reschedule itself, which has already fully succeeded by this point
    # (Calendar + Bookings sheet are both already updated above).
    try:
        lead_sync_result = chatbot_lead_manager.update_lead_booking(
            old_booking_id=booking_id,
            new_booking_id=booking_id,  # reschedule keeps the same booking ID
            name=updated_name,
            email=updated_email,
            phone=to_local_pk_format(updated_phone_normalized),
            company=updated_company,
            old_email=old_email,
            old_phone=old_phone,
        )
        result["lead_sync_updated"] = lead_sync_result is not None
    except Exception as e:
        print(f"[booking_manager] Failed to sync updated contact info to Chatbot_Leads: {e}")
        result["lead_sync_updated"] = False
        result["lead_sync_error"] = str(e)

    # Send an update confirmation (reuses the booking-confirmation email
    # template — the content is the same shape: here's your booking ID
    # and the current date/time — since this is functionally "here's your
    # updated booking", not a brand-new one).
    email_result = send_booking_confirmation(result)
    result["email_sent"] = email_result["sent"]
    if not email_result["sent"]:
        result["email_error"] = email_result["error"]

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_slots(args):
    cal = get_calendar_helper()
    date = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
    slots = cal.list_available_slots(date)
    if not slots:
        print(f"No available slots on {date}.")
        return
    for s in slots:
        print(s.strftime("%Y-%m-%d %H:%M %Z"))


def cmd_book(args):
    try:
        dt = datetime.datetime.strptime(f"{args.date} {args.time}", "%Y-%m-%d %H:%M")
        result = create_booking(
            name=args.name, email=args.email, phone=args.phone, start_dt=dt,
            company=args.company or "", note=args.note or "",
        )
        print("Booking confirmed!")
        print(result)
    except (LeadValidationError, BookingError) as e:
        print(f"Booking failed: {e}")


def cmd_cancel(args):
    try:
        result = cancel_booking(args.booking_id)
        print("Booking cancelled.")
        print(result)
    except BookingError as e:
        print(f"Cancellation failed: {e}")


def cmd_lookup(args):
    result = find_active_booking_by_email(args.email)
    if result is None:
        print(f"No active booking found for '{args.email}'.")
    else:
        print(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booking_manager.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_slots = sub.add_parser("slots", help="List available slots for a date.")
    p_slots.add_argument("date", help="YYYY-MM-DD")
    p_slots.set_defaults(func=cmd_slots)

    p_book = sub.add_parser("book", help="Create a new booking.")
    p_book.add_argument("name")
    p_book.add_argument("email")
    p_book.add_argument("phone")
    p_book.add_argument("date", help="YYYY-MM-DD")
    p_book.add_argument("time", help="HH:MM (24-hour, PKT)")
    p_book.add_argument("--company", default="")
    p_book.add_argument("--note", default="")
    p_book.set_defaults(func=cmd_book)

    p_cancel = sub.add_parser("cancel", help="Cancel a booking by ID.")
    p_cancel.add_argument("booking_id")
    p_cancel.set_defaults(func=cmd_cancel)

    p_lookup = sub.add_parser("lookup", help="Find an active booking by email.")
    p_lookup.add_argument("email")
    p_lookup.set_defaults(func=cmd_lookup)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()