"""
email_service.py
=================
Sends booking-confirmation and cancellation-confirmation emails via SMTP
(Gmail). Consumed by booking_manager.py's create_booking() and
cancel_booking() results — this module doesn't touch Sheets or Calendar
at all, it just takes a dict and sends an email.

Design principle: email failure must NEVER roll back a successful
booking/cancellation. The booking is already real (Calendar event +
Sheets row exist) by the time email is attempted — losing the email is
a degraded experience, not a data-integrity problem, so we log and
return a failure flag rather than raising an exception that could
tempt a caller into rolling back real state over a transient SMTP issue.

Gmail setup note: if the account has 2FA enabled (recommended, and
increasingly required by Google), SMTP_PASSWORD must be a 16-character
App Password (myaccount.google.com/apppasswords), NOT the regular
account login password — the regular password will fail SMTP auth.

CLI usage (for manual testing)
-------------------------------
python email_service.py test your-email@example.com
"""

import os
import smtplib
import argparse
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any

from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Our Agency")


class EmailSendError(Exception):
    """Raised when an email genuinely fails to send. Callers should catch
    this, log it, and continue — never let it block/rollback a booking."""
    pass


def _send_email(to_email: str, subject: str, plain_body: str, html_body: str) -> None:
    """Low-level SMTP send. Sends BOTH a plain-text and an HTML version in
    one multipart/alternative message — mail clients that render HTML
    (Gmail, Outlook, Apple Mail, etc.) show the styled version; anything
    that can't render HTML falls back to the plain-text part
    automatically. Sending HTML with no plain-text alternative is itself
    a spam-classifier signal, so both parts are always included together,
    never HTML alone.

    Raises EmailSendError on any failure, with the original exception
    chained for debugging.
    """
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        raise EmailSendError(
            "SMTP_USERNAME or SMTP_PASSWORD not set in environment. "
            "Check your .env file."
        )

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USERNAME}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    # Attach plain text FIRST, then HTML — per the MIME spec, clients that
    # support multiple alternatives render the LAST part they understand,
    # so HTML (the richer version) must come after plain text to be
    # preferred when both are supported.
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, [to_email], msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise EmailSendError(
            "SMTP authentication failed. If using Gmail with 2FA enabled, "
            "make sure SMTP_PASSWORD is an App Password (16 characters, "
            "from myaccount.google.com/apppasswords), not your regular "
            f"account password. Original error: {e}"
        ) from e
    except smtplib.SMTPException as e:
        raise EmailSendError(f"SMTP error while sending to {to_email}: {e}") from e
    except OSError as e:
        # Covers connection/timeout issues (e.g. network down, host blocked).
        raise EmailSendError(f"Could not connect to SMTP server: {e}") from e


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def _format_time_12h(time_24h: str) -> str:
    """Convert 'HH:MM' (24-hour, as stored internally) to '3:30 PM' style
    for display in emails. Mirrors conversation_manager.py's
    _format_time_12h — kept as a separate copy here rather than a shared
    import, since email_service.py is designed to have no dependency on
    conversation_manager.py (avoids a circular import, since
    conversation_manager imports from booking_manager which imports from
    email_service)."""
    try:
        dt = datetime.datetime.strptime(time_24h, "%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return time_24h  # fall back to raw value rather than crash


# ---------------------------------------------------------------------------
# HTML email shell
# ---------------------------------------------------------------------------
# Email HTML has real constraints unlike regular web HTML: many clients
# (notably Outlook) strip <style> blocks or ignore external CSS, so every
# style is inline. Layout uses <table> rather than flexbox/grid, since
# table-based layout is the one thing that renders consistently across
# Gmail, Outlook, and Apple Mail. Kept deliberately simple: one accent
# color, one details table, no images (images can hurt deliverability
# and won't load by default in most clients anyway).
_ACCENT = "#5747E8"
_INK = "#1E2A3A"
_INK_SOFT = "#55606E"
_BORDER = "#E4DFD6"
_BG = "#FAF8F5"


def _html_shell(heading: str, body_html: str) -> str:
    """Wrap templated body content in the shared HTML email shell —
    header bar with the heading, white content card, consistent
    typography. Both templates below call this so they share one visual
    design rather than each hand-rolling their own layout."""
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; background-color:{_BG}; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_BG}; padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px; background-color:#ffffff; border:1px solid {_BORDER}; border-radius:12px; overflow:hidden;">
          <tr>
            <td style="background-color:{_ACCENT}; padding:20px 28px;">
              <span style="color:#ffffff; font-size:17px; font-weight:600;">{heading}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:28px;">
              {body_html}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _html_details_table(rows: list) -> str:
    """Render a clean two-column fact table (Date, Time, Booking ID,
    etc.) — inline-styled so it survives clients that strip <style>
    blocks. `rows` is a list of (label, value) tuples."""
    row_html = "".join(
        f"""<tr>
              <td style="padding:9px 0; border-bottom:1px solid {_BORDER}; color:{_INK_SOFT}; font-size:13.5px; width:38%;">{label}</td>
              <td style="padding:9px 0; border-bottom:1px solid {_BORDER}; color:{_INK}; font-size:13.5px; font-weight:600;">{value}</td>
            </tr>"""
        for label, value in rows
    )
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0;">
      {row_html}
    </table>"""


def _format_booking_confirmation(booking: Dict[str, Any]) -> Dict[str, str]:
    """Returns {"plain": ..., "html": ...} — both versions of the same
    message, kept in sync since they're built from the same data here
    rather than maintained as two separately-written templates."""
    formatted_time = _format_time_12h(booking['time'])

    plain = (
        f"Hi {booking['lead_name']},\n\n"
        f"Thank you for booking a discovery call with us, we are looking "
        f"forward to speaking with you. Here is a summary of your booking "
        f"for your records:\n\n"
        f"You are confirmed for {booking['date']} at {formatted_time} "
        f"(PKT), and we have allocated 30 minutes for the "
        f"conversation. Your booking reference is {booking['booking_id']}, "
        f"should you need to reach us about it.\n\n"
        f"If your plans change and you need to cancel or move the call to "
        f"a different time, simply reply to this email with your booking "
        f"reference ({booking['booking_id']}) and we will be happy to "
        f"assist you, there is no need to submit a new booking.\n\n"
        f"In the meantime, if there is anything specific you would like to "
        f"cover on the call your goals, a particular challenge, or your "
        f"timeline, feel free to share it here, and we will come "
        f"prepared.\n\n"
        f"We look forward to speaking with you soon.\n\n"
        f"Best regards,\n{SMTP_FROM_NAME}"
    )

    body_html = f"""
      <p style="margin:0 0 14px; color:{_INK}; font-size:14.5px; line-height:1.6;">Hi {booking['lead_name']},</p>
      <p style="margin:0 0 14px; color:{_INK}; font-size:14.5px; line-height:1.6;">
        Thank you for booking a discovery call with us, we are looking forward to
        speaking with you. Here is a summary of your booking for your records.
      </p>
      {_html_details_table([
          ("Date", booking['date']),
          ("Time", f"{formatted_time} (PKT)"),
          ("Duration", "30 minutes"),
          ("Booking ID", booking['booking_id']),
      ])}
      <p style="margin:0 0 14px; color:{_INK}; font-size:14.5px; line-height:1.6;">
        If your plans change and you need to cancel or move the call to a different
        time, simply reply to this email with your booking reference
        (<strong>{booking['booking_id']}</strong>) and we will be happy to assist you,
        there is no need to submit a new booking.
      </p>
      <p style="margin:0 0 20px; color:{_INK}; font-size:14.5px; line-height:1.6;">
        In the meantime, if there is anything specific you would like to cover on
        the call your goals, a particular challenge, or your timeline,
        feel free to share it here, and we will come prepared.
      </p>
      <p style="margin:0; color:{_INK}; font-size:14.5px; line-height:1.6;">
        We look forward to speaking with you soon.<br><br>
        Best regards,<br><strong>{SMTP_FROM_NAME}</strong>
      </p>
    """
    html = _html_shell("Your discovery call is confirmed", body_html)

    return {"plain": plain, "html": html}


def _format_cancellation_confirmation(booking: Dict[str, Any]) -> Dict[str, str]:
    """Returns {"plain": ..., "html": ...}, same pairing principle as
    _format_booking_confirmation()."""
    lead_name = booking.get('Lead Name', booking.get('lead_name', ''))
    booking_id = booking.get('Booking ID', booking.get('booking_id', ''))
    date = booking.get('Requested Date', booking.get('date', ''))
    formatted_time = _format_time_12h(booking.get('Requested Time', booking.get('time', '')))

    plain = (
        f"Hi {lead_name},\n\n"
        f"This email confirms that your discovery call, originally "
        f"scheduled for {date} at {formatted_time} (Pakistan time), has "
        f"been cancelled as requested. Your booking reference {booking_id} "
        f"is no longer active.\n\n"
        f"If this cancellation was made in error, or if you would like to "
        f"arrange a more suitable time, we would be glad to continue the "
        f"conversation, please visit the chatbot whenever it is "
        f"convenient for you, and we can arrange a new time.\n\n"
        f"Thank you for letting us know, and we hope to speak with you "
        f"soon.\n\n"
        f"Best regards,\n{SMTP_FROM_NAME}"
    )

    body_html = f"""
      <p style="margin:0 0 14px; color:{_INK}; font-size:14.5px; line-height:1.6;">Hi {lead_name},</p>
      <p style="margin:0 0 14px; color:{_INK}; font-size:14.5px; line-height:1.6;">
        This email confirms that your discovery call, originally scheduled below,
        has been cancelled as requested.
      </p>
      {_html_details_table([
          ("Date", date),
          ("Time", f"{formatted_time} (PKT)"),
          ("Booking ID", f"{booking_id} (no longer active)"),
      ])}
      <p style="margin:0 0 14px; color:{_INK}; font-size:14.5px; line-height:1.6;">
        If this cancellation was made in error, or if you would like to arrange a
        more suitable time, we would be glad to continue the conversation, please
        visit the chatbot whenever it is convenient for you, and we can arrange a
        new time.
      </p>
      <p style="margin:0 0 20px; color:{_INK}; font-size:14.5px; line-height:1.6;">
        Thank you for letting us know, and we hope to speak with you soon.
      </p>
      <p style="margin:0; color:{_INK}; font-size:14.5px; line-height:1.6;">
        Best regards,<br><strong>{SMTP_FROM_NAME}</strong>
      </p>
    """
    html = _html_shell("Your discovery call has been cancelled", body_html)

    return {"plain": plain, "html": html}


# ---------------------------------------------------------------------------
# Public functions — called by booking_manager.py
# ---------------------------------------------------------------------------
def send_booking_confirmation(booking: Dict[str, Any]) -> Dict[str, Any]:
    """Send a booking confirmation email.

    `booking` is the dict returned by booking_manager.create_booking().

    Returns {"sent": True} on success, or {"sent": False, "error": "..."}
    on failure — NEVER raises, so a caller can safely do:

        result = create_booking(...)
        email_result = send_booking_confirmation(result)
        if not email_result["sent"]:
            log_and_flag_for_manual_followup(result, email_result["error"])

    without wrapping every call site in its own try/except.
    """
    try:
        subject = f"Booking Confirmed - {booking['booking_id']}"
        body = _format_booking_confirmation(booking)
        _send_email(booking["email"], subject, body["plain"], body["html"])
        return {"sent": True}
    except EmailSendError as e:
        print(f"[email_service] Booking confirmation email failed: {e}")
        return {"sent": False, "error": str(e)}


def send_cancellation_confirmation(booking: Dict[str, Any]) -> Dict[str, Any]:
    """Send a cancellation confirmation email.

    `booking` is the dict/record returned by booking_manager.cancel_booking()
    (a Sheets row dict with capitalized keys like "Booking ID", "Email").

    Same never-raises contract as send_booking_confirmation().
    """
    try:
        booking_id = booking.get("Booking ID", booking.get("booking_id", ""))
        email = booking.get("Email", booking.get("email", ""))
        if not email:
            return {"sent": False, "error": "No email address on this booking record."}

        subject = f"Booking Cancelled - {booking_id}"
        body = _format_cancellation_confirmation(booking)
        _send_email(email, subject, body["plain"], body["html"])
        return {"sent": True}
    except EmailSendError as e:
        print(f"[email_service] Cancellation confirmation email failed: {e}")
        return {"sent": False, "error": str(e)}


# ---------------------------------------------------------------------------
# CLI (manual SMTP connectivity test)
# ---------------------------------------------------------------------------
def cmd_test(args):
    fake_booking = {
        "booking_id": "BK-TESTEMAIL",
        "lead_name": "Test User",
        "email": args.to_email,
        "date": datetime.date.today().isoformat(),
        "time": "14:30",
        "note": "This is a test email from email_service.py — safe to ignore.",
    }
    result = send_booking_confirmation(fake_booking)
    if result["sent"]:
        print(f"Test email sent successfully to {args.to_email}. Check the inbox.")
    else:
        print(f"Test email FAILED: {result['error']}")


def main():
    parser = argparse.ArgumentParser(description="Email service for booking confirmations.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_test = sub.add_parser("test", help="Send a test booking-confirmation email.")
    p_test.add_argument("to_email", help="Email address to send the test to.")
    p_test.set_defaults(func=cmd_test)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()