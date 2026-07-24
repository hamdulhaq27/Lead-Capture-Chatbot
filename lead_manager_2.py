"""
lead_manager_2.py
=================
Manage leads stored in the "Leads_2" Google Sheet worksheet.
Supports the form fields: Lead Name, Email, Contact Number,
Service Interested In, Budget Range, Timeline, Lead Status.

CLI RUN CASES
-------------
# HEADERS = ["Lead Name", "Email", "Contact Number", "Service", "Budget Range", "Timeline", "Lead Status"]
# Fetch every lead in the sheet
python lead_manager_2.py fetch-all

# Fetch only the first N leads
python lead_manager_2.py fetch-n 5

# Fetch leads where a column matches a value (case-insensitive)
python lead_manager_2.py fetch-by "Lead Status" "Hot"
python lead_manager_2.py fetch-by "Email" "abdullah@example.com"
python lead_manager_2.py fetch-lead "+15551234578"

# Add a new lead
python lead_manager_2.py append "Lead Name=Abdullah Siddique" "Email=abdullah@example.com" "Contact Number=+15551234578" "Service=Full Package" "Budget Range=5000+" "Timeline=<1_month" "Lead Status=Hot"

# Update an existing lead by name, email, or phone number
python lead_manager_2.py update "John Doe" "Lead Status=Hot"
python lead_manager_2.py update "john@example.com" "Lead Status=Hot"
python lead_manager_2.py update "+15551234578" "Contact Number=+15559876543"

# Bulk update: change every lead matching a condition
python lead_manager_2.py update-by "Lead Status" "Cold" "Lead Status=Lost"

# Help for any command
python lead_manager_2.py --help
"""

import re
import argparse
from typing import List, Dict, Optional, Any
from google_services import create_service, GoogleSheetsHelper

# Configuration
CLIENT_SECRET_FILE = "Client_Secret.json"
API_NAME = "sheets"
API_VERSION = "v4"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1FfFdcQ1B3M479sw8XpyWoxdZrLDoXoRrMkMV42TUW5o"
SHEET_NAME = "Leads_2"
HEADERS = ["Lead Name", "Email", "Contact Number", "Service", "Budget Range", "Timeline", "Lead Status"]
ALLOWED_STATUSES = ["Warm", "Cold", "Hot"]

ALLOWED_SERVICES = ["SEO", "PPC/Ads", "Social Media", "Web Design", "Branding", "Full Package", "Other"]
ALLOWED_BUDGET_RANGES = ["<500", "500-2000", "2000-5000", "5000+", "not_sure"]
ALLOWED_TIMELINES = ["<1_month", "1-3_months", "3-6_months"]

NAME_REGEX = re.compile(r"^[A-Za-z\s'\-\.]+$")
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Canonical Pakistani mobile number after normalize_phone(): +92 then 10 digits
# starting with 3 (e.g. +923001234567). All PK mobile operators (Jazz, Zong,
# Telenor, Ufone, etc.) use the 03XXXXXXXXX / +923XXXXXXXXX pattern.
PK_MOBILE_REGEX = re.compile(r"^\+923\d{9}$")


class LeadValidationError(Exception):
    """Raised when a lead dict fails validation before being written."""
    pass


_service_cache = None
_sheet_verified = False


def get_service():
    """Return a cached Google Sheets service client.

    Building this client involves OAuth/credential setup and is expensive,
    so we build it once per process and reuse it across all calls.
    """
    global _service_cache
    if _service_cache is None:
        _service_cache = create_service(CLIENT_SECRET_FILE, API_NAME, API_VERSION, SCOPES)
    return _service_cache


def ensure_sheet_exists(force: bool = False):
    """Verify the 'Leads_2' worksheet and header row exist.

    This does 2-3 API calls, so once verified in a process we skip re-checking
    on every request. Pass force=True to bypass the cache (e.g. in the CLI).
    """
    global _sheet_verified
    if _sheet_verified and not force:
        return

    service = get_service()
    
    # Get existing sheets
    spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    existing_sheets = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]
    
    if SHEET_NAME not in existing_sheets:
        # Create the sheet
        request = {
            "addSheet": {
                "properties": {
                    "title": SHEET_NAME
                }
            }
        }
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [request]}
        ).execute()
        print(f"Created new worksheet: '{SHEET_NAME}'")
    
    # Write headers if the sheet is empty
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:G1"
    ).execute()
    values = result.get("values", [])
    
    if not values or not values[0]:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:G1",
            valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
            body={"majorDimension": "ROWS", "values": [HEADERS]}
        ).execute()
        print(f"Headers written to '{SHEET_NAME}': {HEADERS}")

    _sheet_verified = True


# Normalization
def normalize_phone(phone: str) -> str:
    """Canonicalize any Pakistani mobile input into +92XXXXXXXXXX form.

    Accepts, and treats identically:
      03001234567      (local, with trunk 0)
      3001234567       (local, no trunk 0)
      923001234567     (country code, no +)
      +923001234567    (full E.164)
      0092 300 1234567 (dialing prefix, spaces/dashes)

    Anything that doesn't reduce to a plausible Pakistani mobile number is
    still returned (so validate_phone_number can produce a clear error),
    just without a country code guess for non-PK-shaped input.
    """
    stripped = str(phone).strip()
    digits = re.sub(r"\D", "", stripped)  # drop +, spaces, dashes

    if digits.startswith("0092"):
        digits = digits[4:]
    elif digits.startswith("92") and len(digits) > 10:
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    # else: assume it's already a bare local number (e.g. "3001234567")

    return "+92" + digits


def to_local_pk_format(phone: str) -> str:
    """Convert a canonical +92XXXXXXXXXX number into local 03XXXXXXXXX form.

    Used only when writing/displaying a Contact Number (e.g. into the
    Google Sheet). Internal logic (validation, duplicate checks, and the
    WhatsApp/Twilio send) keeps using normalize_phone()'s +92 canonical
    form, since Twilio requires E.164 — this is purely a storage/display
    convenience layer on top of that.

    Expects `phone` to already be normalize_phone()-canonical (+92...);
    call normalize_phone() first if the input hasn't been normalized yet.
    """
    digits = re.sub(r"\D", "", str(phone).strip())
    if digits.startswith("92"):
        digits = digits[2:]
    return "0" + digits


def normalize_status(status: str) -> str:
    stripped = str(status).strip()
    for allowed in ALLOWED_STATUSES:
        if allowed.lower() == stripped.lower():
            return allowed
    return stripped


def normalize_lead(lead: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(lead)
    if "Lead Name" in normalized:
        normalized["Lead Name"] = str(normalized["Lead Name"]).strip().title()
    if "Email" in normalized:
        normalized["Email"] = str(normalized["Email"]).strip().lower()
    if "Contact Number" in normalized:
        normalized["Contact Number"] = normalize_phone(normalized["Contact Number"])
    if "Service" in normalized:
        normalized["Service"] = str(normalized["Service"]).strip()
    if "Lead Status" in normalized:
        normalized["Lead Status"] = normalize_status(normalized["Lead Status"])
    if "Budget Range" in normalized:
        normalized["Budget Range"] = str(normalized["Budget Range"]).strip().lower()
    if "Timeline" in normalized:
        normalized["Timeline"] = str(normalized["Timeline"]).strip().lower()
    return normalized


# Validation
def validate_phone_number(phone: str) -> None:
    """Validate that `phone` is a Pakistani mobile number.

    Expected to be called after normalize_phone(), so `phone` should already
    be in +92XXXXXXXXXX form. Rejects anything else — wrong length, wrong
    country code, or a trunk number that isn't a mobile prefix (must start
    with 3, e.g. 03XX for Jazz/Zong/Telenor/Ufone).
    """
    stripped = str(phone).strip()

    if not PK_MOBILE_REGEX.match(stripped):
        digits = re.sub(r"\D", "", stripped)
        raise LeadValidationError(
            f"Invalid contact number '{phone}': must be a Pakistani mobile number "
            f"in the form 03XXXXXXXXX or +923XXXXXXXXX (11 digits with the trunk "
            f"0, or +92 followed by 10 digits starting with 3) — got {len(digits)} "
            f"digit(s) after normalization."
        )


def validate_lead(lead: Dict[str, Any]) -> None:
    missing = [h for h in HEADERS if h not in lead or str(lead[h]).strip() == ""]
    if missing:
        raise LeadValidationError(f"Missing required field(s): {', '.join(missing)}")

    name = str(lead["Lead Name"]).strip()
    if not NAME_REGEX.match(name):
        raise LeadValidationError(
            f"Invalid Lead Name '{name}': only letters, spaces, apostrophes, "
            "hyphens, and periods are allowed."
        )

    if not EMAIL_REGEX.match(str(lead["Email"]).strip()):
        raise LeadValidationError(f"Invalid email format: '{lead['Email']}'")

    validate_phone_number(lead["Contact Number"])

    service = str(lead["Service"]).strip()
    if service not in ALLOWED_SERVICES:
        raise LeadValidationError(
            f"Invalid Service '{service}'. Must be one of: {', '.join(ALLOWED_SERVICES)}"
        )

    budget = str(lead["Budget Range"]).strip().lower()
    if budget not in ALLOWED_BUDGET_RANGES:
        raise LeadValidationError(
            f"Invalid Budget Range '{budget}'. Must be one of: {', '.join(ALLOWED_BUDGET_RANGES)}"
        )

    timeline = str(lead["Timeline"]).strip().lower()
    if timeline not in ALLOWED_TIMELINES:
        raise LeadValidationError(
            f"Invalid Timeline '{timeline}'. Must be one of: {', '.join(ALLOWED_TIMELINES)}"
        )

    status = str(lead["Lead Status"]).strip()
    if status not in ALLOWED_STATUSES:
        raise LeadValidationError(
            f"Invalid Lead Status '{status}'. Must be one of: {', '.join(ALLOWED_STATUSES)}"
        )


def check_duplicate_in_records(records: List[Dict[str, str]], field: str, value: str,
                                exclude_lead_name: Optional[str] = None) -> None:
    """Check a single field for duplicates against an already-fetched list of records.

    Use this when checking multiple fields (e.g. Email + Contact Number) so the
    sheet only needs to be read once instead of once per field.
    """
    if field == "Contact Number":
        target = normalize_phone(value)
    else:
        target = str(value).strip().lower()

    for r in records:
        if exclude_lead_name and r.get("Lead Name", "").strip().lower() == exclude_lead_name.strip().lower():
            continue

        existing_val = r.get(field, "")
        existing_val = normalize_phone(existing_val) if field == "Contact Number" else existing_val.strip().lower()

        if existing_val == target:
            raise LeadValidationError(
                f"{field} '{value}' is already used by lead '{r.get('Lead Name')}'."
            )


def check_duplicate_field(field: str, value: str, exclude_lead_name: Optional[str] = None,
                           spreadsheet_id: str = SPREADSHEET_ID,
                           sheet_name: str = SHEET_NAME) -> None:
    """Single-field duplicate check that fetches the sheet itself.

    Kept for backwards compatibility (CLI still uses it for one-off checks).
    Prefer check_duplicate_in_records() when checking more than one field,
    to avoid multiple full-sheet reads.
    """
    records = fetch_all_leads(spreadsheet_id, sheet_name)
    check_duplicate_in_records(records, field, value, exclude_lead_name)


# Fetching
def fetch_all_leads(spreadsheet_id: str = SPREADSHEET_ID,
                     sheet_name: str = SHEET_NAME,
                     limit: Optional[int] = None) -> List[Dict[str, str]]:
    """Fetch leads from the sheet. If `limit` is given, only the first
    `limit` data rows are requested from the Sheets API directly."""

    service = get_service()

    if limit is not None:
        data_range = f"{sheet_name}!A1:G{limit + 1}"
    else:
        data_range = f"{sheet_name}!A1:G"

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=data_range
    ).execute()
    values = result.get("values", [])

    if not values:
        return []

    header, *rows = values

    records = []
    for row in rows:
        padded = row + [""] * (len(header) - len(row))
        records.append(dict(zip(header, padded)))
    return records


def fetch_by_condition(column: str, value: str,
                       spreadsheet_id: str = SPREADSHEET_ID,
                       sheet_name: str = SHEET_NAME) -> List[Dict[str, str]]:
    records = fetch_all_leads(spreadsheet_id, sheet_name)

    if column == "Contact Number":
        target = normalize_phone(value).lstrip("+")
        return [
            r for r in records
            if normalize_phone(r.get("Contact Number", "")).lstrip("+") == target
        ]

    return [
        r for r in records
        if r.get(column, "").strip().lower() == value.strip().lower()
    ]


def fetch_lead(lead_identifier: str,
               spreadsheet_id: str = SPREADSHEET_ID,
               sheet_name: str = SHEET_NAME) -> Optional[Dict[str, Any]]:
    """Fetch a single lead by Lead Name, Email, or Contact Number (any of the three)."""
    service = get_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1:G"
    ).execute()

    values = result.get("values", [])
    if not values:
        return None

    header, *rows = values

    identifier_norm = lead_identifier.strip().lower()
    # Canonical +92 form of the search input, so lookups match regardless of
    # whether the identifier or the stored value includes the country code.
    search_phone_norm = normalize_phone(lead_identifier)

    for idx, row in enumerate(rows, start=2):
        padded = row + [""] * (len(header) - len(row))
        record = dict(zip(header, padded))

        # Match by Lead Name (case-insensitive)
        if record.get("Lead Name", "").strip().lower() == identifier_norm:
            record["row_number"] = idx
            return record

        # Match by Email (case-insensitive)
        if record.get("Email", "").strip().lower() == identifier_norm:
            record["row_number"] = idx
            return record

        # Match by Contact Number (country-code agnostic)
        contact_value = record.get("Contact Number", "")
        if contact_value and normalize_phone(contact_value) == search_phone_norm:
            record["row_number"] = idx
            return record

    return None


# Storing
def append_lead(lead: Dict[str, Any],
                 spreadsheet_id: str = SPREADSHEET_ID,
                 sheet_name: str = SHEET_NAME) -> Dict[str, Any]:
    lead = normalize_lead(lead)
    validate_lead(lead)

    # Single fetch, both fields checked against it (was 2 full-sheet reads before)
    records = fetch_all_leads(spreadsheet_id, sheet_name)
    check_duplicate_in_records(records, "Email", lead["Email"])
    check_duplicate_in_records(records, "Contact Number", lead["Contact Number"])

    # Validation/dedup above used the +92 canonical form; convert to local
    # 03... form only now, right before writing the row to the sheet.
    lead["Contact Number"] = to_local_pk_format(lead["Contact Number"])

    service = get_service()
    row = [lead.get(h, "") for h in HEADERS]
    return service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1:G1",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        insertDataOption="INSERT_ROWS",
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()


def update_lead(lead_identifier: str, updates: Dict[str, Any],
                 spreadsheet_id: str = SPREADSHEET_ID,
                 sheet_name: str = SHEET_NAME) -> Optional[Dict[str, Any]]:
    """Update a lead found by Lead Name, Email, or Contact Number (any of the three).

    `lead_identifier` can be any of those three fields' current value.
    """
    # Single fetch reused for: finding the record, and both duplicate checks
    # (was 3 full-sheet reads before: fetch_lead + 2x check_duplicate_field)
    records = fetch_all_leads(spreadsheet_id, sheet_name)

    identifier_norm = lead_identifier.strip().lower()
    # Compare via normalize_phone() rather than raw digits, so a lookup by
    # "+923001234567" still matches a stored "03001234567" (or vice versa) —
    # raw digit comparison would miss this whenever the country code differs.
    search_phone_norm = normalize_phone(lead_identifier)
    existing = None
    row_number = None
    match_position = None
    for pos, record in enumerate(records):
        name_match = record.get("Lead Name", "").strip().lower() == identifier_norm
        email_match = record.get("Email", "").strip().lower() == identifier_norm
        contact_value = record.get("Contact Number", "")
        phone_match = bool(contact_value) and normalize_phone(contact_value) == search_phone_norm

        if name_match or email_match or phone_match:
            existing, row_number, match_position = record, pos + 2, pos
            break

    if existing is None:
        return None

    merged = {h: existing.get(h, "") for h in HEADERS}
    merged.update(updates)
    merged = normalize_lead(merged)
    validate_lead(merged)

    # Exclude the matched row itself by position, not by name text — this is
    # correct even when the lookup was done via phone/email rather than name,
    # and even if two leads happen to share the same Lead Name.
    other_records = records[:match_position] + records[match_position + 1:]
    check_duplicate_in_records(other_records, "Email", merged["Email"])
    check_duplicate_in_records(other_records, "Contact Number", merged["Contact Number"])

    # Validation/dedup above used the +92 canonical form; convert to local
    # 03... form only now, right before writing the row back to the sheet.
    merged["Contact Number"] = to_local_pk_format(merged["Contact Number"])

    service = get_service()
    row = [merged[h] for h in HEADERS]
    return service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A{row_number}:G{row_number}",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()


def update_by_condition(column: str, value: str, updates: Dict[str, Any],
                         spreadsheet_id: str = SPREADSHEET_ID,
                         sheet_name: str = SHEET_NAME) -> int:
    matches = fetch_by_condition(column, value, spreadsheet_id, sheet_name)
    for record in matches:
        update_lead(record["Lead Name"], updates, spreadsheet_id, sheet_name)
    return len(matches)


# -----------------------------------------------------------------------
# Command-line interface
# -----------------------------------------------------------------------
def update_lead_by_row(row_number: int, updates: Dict[str, Any],
                       spreadsheet_id: str = SPREADSHEET_ID,
                       sheet_name: str = SHEET_NAME) -> Optional[Dict[str, Any]]:
    records = fetch_all_leads(spreadsheet_id, sheet_name)
    target_record = None
    for r in records:
        if r.get("row_number") == row_number:
            target_record = r
            break
            
    if not target_record:
        return None
        
    updated_name = (updates.get("name") if updates.get("name") is not None else target_record.get("Lead Name", "")).strip()
    updated_email = (updates.get("email") if updates.get("email") is not None else target_record.get("Email", "")).strip().lower()
    updated_phone = (updates.get("phone") if updates.get("phone") is not None else target_record.get("Contact Number", "")).strip()
    
    if updated_phone:
        try:
            phone_normalized = normalize_phone(updated_phone)
            validate_phone_number(phone_normalized)
            updated_phone = to_local_pk_format(phone_normalized)
        except Exception:
            pass
            
    row = [
        updated_name.title(), updated_email, updated_phone,
        target_record.get("Service", ""), target_record.get("Budget Range", ""),
        target_record.get("Timeline", ""), target_record.get("Lead Status", "")
    ]
    
    service = get_service()
    last_col = chr(ord('A') + len(HEADERS) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A{row_number}:{last_col}{row_number}",
        valueInputOption=GoogleSheetsHelper.Value_Input_Option.raw,
        body={"majorDimension": "ROWS", "values": [row]},
    ).execute()
    
    return dict(zip(HEADERS, row))


def _print_records(records: List[Dict[str, Any]]) -> None:
    if not records:
        print("(no matching leads)")
        return
    for i, r in enumerate(records, start=1):
        print(f"{i}. {r}")


def _parse_kv_pairs(pairs: List[str]) -> Dict[str, str]:
    """Turn ['Lead Status=Hot', 'Email=a@b.com'] into a dict."""
    result: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(
                f"Invalid field assignment '{pair}'. Use the form Field=\"Value\", "
                f"e.g. 'Lead Status=Hot'."
            )
        key, value = pair.split("=", 1)
        key = key.strip()
        if key not in HEADERS:
            raise ValueError(
                f"Unknown field '{key}'. Valid fields are: {', '.join(HEADERS)}"
            )
        result[key] = value
    return result


def cmd_fetch_all(args):
    records = fetch_all_leads()
    _print_records(records)


def cmd_fetch_n(args):
    records = fetch_all_leads(limit=args.count)
    _print_records(records)


def cmd_fetch_by(args):
    records = fetch_by_condition(args.column, args.value)
    _print_records(records)


def cmd_fetch_lead(args):
    record = fetch_lead(args.lead_name)
    if record is None:
        print(f"No lead found with name '{args.lead_name}'.")
    else:
        print(record)


def cmd_append(args):
    try:
        fields = _parse_kv_pairs(args.fields)
        result = append_lead(fields)
        print("Lead added successfully.")
        print(result)
    except (LeadValidationError, ValueError) as e:
        print(f"Failed: {e}")


def cmd_update(args):
    try:
        updates = _parse_kv_pairs(args.fields)
        result = update_lead(args.lead_identifier, updates)
        if result is None:
            print(f"No lead found with name/email/phone '{args.lead_identifier}'.")
        else:
            print("Lead updated successfully.")
            print(result)
    except (LeadValidationError, ValueError) as e:
        print(f"Failed: {e}")


def cmd_update_by(args):
    try:
        updates = _parse_kv_pairs(args.fields)
        count = update_by_condition(args.column, args.value, updates)
        print(f"{count} row(s) updated.")
    except (LeadValidationError, ValueError) as e:
        print(f"Failed: {e}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lead_manager_2.py",
        description="Manage leads stored in the Leads_2 Google Sheet worksheet.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch-all
    p_all = sub.add_parser("fetch-all", help="Fetch every lead in the sheet.")
    p_all.set_defaults(func=cmd_fetch_all)

    # fetch-n
    p_n = sub.add_parser("fetch-n", help="Fetch the first N leads (in sheet order).")
    p_n.add_argument("count", type=int, help="Number of rows to fetch.")
    p_n.set_defaults(func=cmd_fetch_n)

    # fetch-by
    p_by = sub.add_parser("fetch-by", help="Fetch leads where a column matches a value.")
    p_by.add_argument("column", help=f"Column name, one of: {', '.join(HEADERS)}")
    p_by.add_argument("value", help="Value to match (case-insensitive).")
    p_by.set_defaults(func=cmd_fetch_by)

    # fetch-lead
    p_one = sub.add_parser("fetch-lead", help="Fetch a single lead by exact name.")
    p_one.add_argument("lead_name", help="Lead Name to look up.")
    p_one.set_defaults(func=cmd_fetch_lead)

    # append
    p_append = sub.add_parser(
        "append",
        help="Add a new lead. Pass each field as Field=\"Value\".",
        description=(
            "Example:\n"
            '  python lead_manager_2.py append "Lead Name=Abdullah Siddique" '
            '"Email=abdullah@example.com" "Contact Number=+15551234578" '
            '"Service=Full Package" "Budget Range=5000+" "Timeline=<1_month" "Lead Status=Hot"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_append.add_argument("fields", nargs="+", help='Field assignments, e.g. "Email=a@b.com"')
    p_append.set_defaults(func=cmd_append)

    # update
    p_update = sub.add_parser(
        "update",
        help="Update an existing lead by name, email, or phone number. Pass changed fields as Field=\"Value\".",
        description=(
            "Example:\n"
            '  python lead_manager_2.py update "John Doe" "Lead Status=Hot" "Email=new@example.com"\n'
            '  python lead_manager_2.py update "john@example.com" "Lead Status=Hot"\n'
            '  python lead_manager_2.py update "+15551234578" "Lead Status=Hot"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_update.add_argument("lead_identifier", help="Lead Name, Email, or Contact Number of the record to update.")
    p_update.add_argument("fields", nargs="+", help='Field assignments, e.g. "Lead Status=Hot"')
    p_update.set_defaults(func=cmd_update)

    # update-by
    p_update_by = sub.add_parser(
        "update-by",
        help="Bulk-update every lead where a column matches a value.",
        description=(
            "Example:\n"
            '  python lead_manager_2.py update-by "Lead Status" "Cold" "Lead Status=Lost"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_update_by.add_argument("column", help=f"Column name, one of: {', '.join(HEADERS)}")
    p_update_by.add_argument("value", help="Value to match (case-insensitive).")
    p_update_by.add_argument("fields", nargs="+", help='Field assignments to apply, e.g. "Lead Status=Lost"')
    p_update_by.set_defaults(func=cmd_update_by)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()