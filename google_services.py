import pickle
import os
import datetime
from collections import namedtuple
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request


def create_service(client_secret_file, api_name, api_version, *scopes, prefix=''):
	CLIENT_SECRET_FILE = client_secret_file
	API_SERVICE_NAME = api_name
	API_VERSION = api_version
	SCOPES = [scope for scope in scopes[0]]
	
	cred = None
	working_dir = os.getcwd()
	token_dir = 'token files'
	pickle_file = f'token_{API_SERVICE_NAME}_{API_VERSION}{prefix}.pickle'

	### Check if token dir exists first, if not, create the folder
	if not os.path.exists(os.path.join(working_dir, token_dir)):
		os.mkdir(os.path.join(working_dir, token_dir))

	if os.path.exists(os.path.join(working_dir, token_dir, pickle_file)):
		with open(os.path.join(working_dir, token_dir, pickle_file), 'rb') as token:
			cred = pickle.load(token)

	if not cred or not cred.valid:
		if cred and cred.expired and cred.refresh_token:
			cred.refresh(Request())
		else:
			flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
			cred = flow.run_local_server()

		with open(os.path.join(working_dir, token_dir, pickle_file), 'wb') as token:
			pickle.dump(cred, token)

	try:
		service = build(API_SERVICE_NAME, API_VERSION, credentials=cred)
		print(API_SERVICE_NAME, API_VERSION, 'service created successfully')
		return service
	except Exception as e:
		print(e)
		print(f'Failed to create service instance for {API_SERVICE_NAME}')
		os.remove(os.path.join(working_dir, token_dir, pickle_file))
		return None

def convert_to_RFC_datetime(year=1900, month=1, day=1, hour=0, minute=0):
	dt = datetime.datetime(year, month, day, hour, minute, 0).isoformat() + 'Z'
	return dt

class GoogleSheetsHelper:
	# --> spreadsheets().batchUpdate()
	Paste_Type = namedtuple('_Paste_Type', 
					('normal', 'value', 'format', 'without_borders', 
					 'formula', 'date_validation', 'conditional_formatting')
					)('PASTE_NORMAL', 'PASTE_VALUES', 'PASTE_FORMAT', 'PASTE_NO_BORDERS', 
					  'PASTE_FORMULA', 'PASTE_DATA_VALIDATION', 'PASTE_CONDITIONAL_FORMATTING')

	Paste_Orientation = namedtuple('_Paste_Orientation', ('normal', 'transpose'))('NORMAL', 'TRANSPOSE')

	Merge_Type = namedtuple('_Merge_Type', ('merge_all', 'merge_columns', 'merge_rows')
					)('MERGE_ALL', 'MERGE_COLUMNS', 'MERGE_ROWS')

	Delimiter_Type = namedtuple('_Delimiter_Type', ('comma', 'semicolon', 'period', 'space', 'custom', 'auto_detect')
						)('COMMA', 'SEMICOLON', 'PERIOD', 'SPACE', 'CUSTOM', 'AUTODETECT')

	# --> Types
	Dimension = namedtuple('_Dimension', ('rows', 'columns'))('ROWS', 'COLUMNS')

	Value_Input_Option = namedtuple('_Value_Input_Option', ('raw', 'user_entered'))('RAW', 'USER_ENTERED')

	Value_Render_Option = namedtuple('_Value_Render_Option',["formatted", "unformatted", "formula"]
							)("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
                            
	@staticmethod
	def define_cell_range(
		sheet_id, 
		start_row_number=1, end_row_number=0, 
		start_column_number=None, end_column_number=0):
		"""GridRange object"""
		json_body = {
			'sheetId': sheet_id,
			'startRowIndex': start_row_number - 1,
			'endRowIndex': end_row_number,
			'startColumnIndex': start_column_number - 1,
			'endColumnIndex': end_column_number
		}
		return json_body

	@staticmethod
	def define_dimension_range(sheet_id, dimension, start_index, end_index):
		json_body = {
			'sheetId': sheet_id,
			'dimension': dimension,
			'startIndex': start_index,
			'endIndex': end_index
		}
		return json_body



class GoogleCalendarHelper:
	"""Calendar mechanics for the discovery-call booking flow.

	This class owns ONLY calendar-level concerns: business hours, slot
	duration, timezone conversion, availability checks, and event
	CRUD. Business rules like "book at least 2 hours in advance",
	"one booking per email", or "reschedule = cancel + rebook" belong
	in booking_manager.py (Module C), which calls into this helper —
	keeping this class reusable even if those policies change later.

	All wall-clock times in and out of this class are Asia/Karachi
	(Pakistan Standard Time, UTC+5, no DST) unless explicitly noted.
	"""

	TIMEZONE = 'Asia/Karachi'
	SLOT_DURATION_MINUTES = 30
	BUSINESS_START_HOUR = 9   # 9 AM
	BUSINESS_END_HOUR = 18    # 6 PM
	# Monday=0 ... Sunday=6. Business days per booking_policy.txt: Mon-Fri.
	BUSINESS_DAYS = {0, 1, 2, 3, 4}

	CALENDAR_SUMMARY = 'Agency Discovery Calls'

	def __init__(self, service, calendar_id=None):
		"""
		service: an authenticated Calendar API service object, e.g.
			create_service('Client_Secret.json', 'calendar', 'v3',
			                ['https://www.googleapis.com/auth/calendar'])
		calendar_id: an existing calendar's ID to use directly. If None,
			get_or_create_booking_calendar() will find-or-create the
			dedicated "Agency Discovery Calls" calendar and cache its ID
			on self.calendar_id.
		"""
		self.service = service
		self.calendar_id = calendar_id

	# ------------------------------------------------------------------
	# Timezone helpers
	# ------------------------------------------------------------------
	@classmethod
	def _tz(cls):
		# Imported lazily so this module doesn't hard-fail on Python <3.9
		# environments before this class is even touched.
		from zoneinfo import ZoneInfo
		return ZoneInfo(cls.TIMEZONE)

	@classmethod
	def now(cls):
		"""Current time, timezone-aware, in Asia/Karachi."""
		return datetime.datetime.now(cls._tz())

	@classmethod
	def to_local(cls, naive_or_aware_dt):
		"""Attach/convert a datetime to Asia/Karachi.

		A naive datetime (no tzinfo) is ASSUMED to already represent local
		PKT wall-clock time (e.g. from a booking form) and is just tagged
		with the timezone. An aware datetime is properly converted.
		"""
		tz = cls._tz()
		if naive_or_aware_dt.tzinfo is None:
			return naive_or_aware_dt.replace(tzinfo=tz)
		return naive_or_aware_dt.astimezone(tz)

	# ------------------------------------------------------------------
	# Business-hours / slot validation
	# ------------------------------------------------------------------
	@classmethod
	def is_within_business_hours(cls, dt):
		"""True if `dt` (assumed/converted to PKT) falls on a business day
		and its slot [dt, dt+30min) fits entirely within 9AM-6PM."""
		local_dt = cls.to_local(dt)

		if local_dt.weekday() not in cls.BUSINESS_DAYS:
			return False

		slot_end = local_dt + datetime.timedelta(minutes=cls.SLOT_DURATION_MINUTES)
		day_start = local_dt.replace(hour=cls.BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)
		day_end = local_dt.replace(hour=cls.BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)

		return day_start <= local_dt and slot_end <= day_end

	@classmethod
	def is_aligned_to_slot_grid(cls, dt):
		"""True if `dt` falls exactly on a 30-minute grid line (e.g. :00 or
		:30), so we don't create overlapping/staggered slots like 9:07."""
		local_dt = cls.to_local(dt)
		return local_dt.minute % cls.SLOT_DURATION_MINUTES == 0 and local_dt.second == 0

	@classmethod
	def generate_business_day_slots(cls, date):
		"""Return every possible 30-min slot start time (as PKT-aware
		datetimes) for the given calendar date, ignoring current
		availability — callers filter this against get_busy_slots()."""
		local_date = cls.to_local(
			datetime.datetime(date.year, date.month, date.day)
		)
		if local_date.weekday() not in cls.BUSINESS_DAYS:
			return []

		slots = []
		cursor = local_date.replace(hour=cls.BUSINESS_START_HOUR, minute=0)
		day_end = local_date.replace(hour=cls.BUSINESS_END_HOUR, minute=0)
		step = datetime.timedelta(minutes=cls.SLOT_DURATION_MINUTES)

		while cursor + step <= day_end:
			slots.append(cursor)
			cursor += step

		return slots

	# ------------------------------------------------------------------
	# Calendar setup
	# ------------------------------------------------------------------
	def get_or_create_booking_calendar(self):
		"""Find the dedicated 'Agency Discovery Calls' calendar, or create
		it if it doesn't exist yet. Caches and returns the calendar ID.

		Using a dedicated calendar (rather than 'primary') keeps client
		bookings visually and programmatically separate from personal
		events, and makes it trivial to share just this calendar with
		other team members later.
		"""
		if self.calendar_id:
			return self.calendar_id

		calendar_list = self.service.calendarList().list().execute()
		for cal in calendar_list.get('items', []):
			if cal.get('summary') == self.CALENDAR_SUMMARY:
				self.calendar_id = cal['id']
				return self.calendar_id

		created = self.service.calendars().insert(body={
			'summary': self.CALENDAR_SUMMARY,
			'timeZone': self.TIMEZONE,
		}).execute()
		self.calendar_id = created['id']
		return self.calendar_id

	# ------------------------------------------------------------------
	# Availability
	# ------------------------------------------------------------------
	def get_busy_slots(self, date):
		"""Return a list of (start, end) PKT-aware datetime tuples for
		every existing (non-cancelled) event on the booking calendar for
		the given date. Used to filter generate_business_day_slots().
		"""
		calendar_id = self.get_or_create_booking_calendar()
		tz = self._tz()

		day_start = datetime.datetime(date.year, date.month, date.day, 0, 0, tzinfo=tz)
		day_end = day_start + datetime.timedelta(days=1)

		events_result = self.service.events().list(
			calendarId=calendar_id,
			timeMin=day_start.isoformat(),
			timeMax=day_end.isoformat(),
			singleEvents=True,
			orderBy='startTime',
		).execute()

		busy = []
		for event in events_result.get('items', []):
			if event.get('status') == 'cancelled':
				continue
			start = event.get('start', {}).get('dateTime')
			end = event.get('end', {}).get('dateTime')
			if not start or not end:
				continue  # skip all-day events with no dateTime
			busy.append((
				datetime.datetime.fromisoformat(start),
				datetime.datetime.fromisoformat(end),
			))
		return busy

	def is_slot_available(self, start_dt):
		"""True if the 30-min slot starting at `start_dt` (PKT) is within
		business hours, grid-aligned, and not already booked.

		Does NOT check the 2-hour/30-day advance-notice window — that's a
		booking-policy rule enforced by booking_manager.py, not a calendar
		mechanics rule.
		"""
		start_dt = self.to_local(start_dt)

		if not self.is_aligned_to_slot_grid(start_dt):
			return False
		if not self.is_within_business_hours(start_dt):
			return False

		end_dt = start_dt + datetime.timedelta(minutes=self.SLOT_DURATION_MINUTES)
		busy = self.get_busy_slots(start_dt.date())

		for busy_start, busy_end in busy:
			# Overlap check: two intervals overlap unless one ends before
			# the other starts.
			if start_dt < busy_end and end_dt > busy_start:
				return False

		return True

	def list_available_slots(self, date):
		"""Return every open 30-min slot (as PKT-aware datetimes) for a
		given date — the full day's grid minus busy slots. Convenience
		wrapper for surfacing options to the user in the chat flow."""
		all_slots = self.generate_business_day_slots(date)
		busy = self.get_busy_slots(date)

		def overlaps_any_busy(slot_start):
			slot_end = slot_start + datetime.timedelta(minutes=self.SLOT_DURATION_MINUTES)
			return any(slot_start < b_end and slot_end > b_start for b_start, b_end in busy)

		return [s for s in all_slots if not overlaps_any_busy(s)]

	# ------------------------------------------------------------------
	# Event CRUD
	# ------------------------------------------------------------------
	def create_event(self, start_dt, lead_name, email, phone, note='', booking_id=''):
		"""Create a 30-min discovery-call event on the booking calendar.

		Returns the created event dict (includes 'id', which the caller
		MUST persist as Calendar Event ID in the Bookings sheet — it's the
		join key needed later for cancellation/reschedule).

		Raises RuntimeError if the slot isn't actually available at the
		moment of creation (defends against a race between an earlier
		is_slot_available() check and this call — see edge-case note in
		booking_manager.py's design).
		"""
		start_dt = self.to_local(start_dt)

		if not self.is_slot_available(start_dt):
			raise RuntimeError(
				f"Slot {start_dt.isoformat()} is no longer available "
				f"(business hours, grid alignment, or already booked)."
			)

		end_dt = start_dt + datetime.timedelta(minutes=self.SLOT_DURATION_MINUTES)
		calendar_id = self.get_or_create_booking_calendar()

		description_lines = [
			f"Booking ID: {booking_id}" if booking_id else None,
			f"Phone: {phone}",
			f"Note: {note}" if note else None,
		]
		description = "\n".join(line for line in description_lines if line)

		event_body = {
			'summary': f"Discovery Call - {lead_name}",
			'description': description,
			'start': {'dateTime': start_dt.isoformat(), 'timeZone': self.TIMEZONE},
			'end': {'dateTime': end_dt.isoformat(), 'timeZone': self.TIMEZONE},
			'attendees': [{'email': email}] if email else [],
			'reminders': {'useDefault': True},
		}

		return self.service.events().insert(
			calendarId=calendar_id,
			body=event_body,
			sendUpdates='none',  # email confirmation is sent by email_service.py, not Calendar's invite
		).execute()

	def update_event(self, event_id, start_dt, lead_name, email, phone, note='', booking_id='',
	                  time_changed=True):
		"""Update an EXISTING event in place — used for the "edit my
		booking" flow (change contact info and/or move the time) as an
		alternative to delete_event() + create_event(), so the booking
		keeps its identity (same Calendar event, same Booking ID upstream
		in booking_manager.py) instead of being cancelled and recreated.

		`time_changed` tells this method whether it needs to re-check slot
		availability at all: if the date/time isn't changing (e.g. the
		customer only updated their email), there's no need to run
		is_slot_available() — the slot is obviously still "available to
		this same event" since it already occupies it. If the date/time
		DID change, we must check the NEW slot is free, but must exclude
		THIS event's own current slot from the busy-list check first —
		otherwise an event would always appear to conflict with itself
		when moving within the same day, or fail to detect that it's
		freeing up its old slot.

		Raises RuntimeError if the new slot isn't actually available
		(same race-condition defense as create_event()).
		"""
		start_dt = self.to_local(start_dt)
		end_dt = start_dt + datetime.timedelta(minutes=self.SLOT_DURATION_MINUTES)
		calendar_id = self.get_or_create_booking_calendar()

		if time_changed:
			if not self.is_aligned_to_slot_grid(start_dt):
				raise RuntimeError("New time is not aligned to a 30-minute slot.")
			if not self.is_within_business_hours(start_dt):
				raise RuntimeError("New time is outside business hours.")

			# Check availability, excluding THIS event's own current slot —
			# otherwise moving a booking from 2pm to 2:30pm on the same day
			# could falsely see the old 2pm slot's busy entry and, depending
			# on overlap math, cause confusing false conflicts. Since we're
			# about to overwrite this exact event anyway, its own existing
			# time should never count as "taken" from its own perspective.
			busy = self.get_busy_slots(start_dt.date())
			busy = [(s, e) for (s, e) in busy if not self._event_matches(event_id, s, e, calendar_id)]
			for busy_start, busy_end in busy:
				if start_dt < busy_end and end_dt > busy_start:
					raise RuntimeError(
						f"Slot {start_dt.isoformat()} is no longer available "
						f"(already booked by another event)."
					)

		description_lines = [
			f"Booking ID: {booking_id}" if booking_id else None,
			f"Phone: {phone}",
			f"Note: {note}" if note else None,
		]
		description = "\n".join(line for line in description_lines if line)

		event_body = {
			'summary': f"Discovery Call - {lead_name}",
			'description': description,
			'start': {'dateTime': start_dt.isoformat(), 'timeZone': self.TIMEZONE},
			'end': {'dateTime': end_dt.isoformat(), 'timeZone': self.TIMEZONE},
			'attendees': [{'email': email}] if email else [],
			'reminders': {'useDefault': True},
		}

		return self.service.events().update(
			calendarId=calendar_id,
			eventId=event_id,
			body=event_body,
			sendUpdates='none',
		).execute()

	def _event_matches(self, event_id, slot_start, slot_end, calendar_id):
		"""Best-effort check: does this (start, end) busy-slot tuple, as
		returned by get_busy_slots(), belong to `event_id` itself? Used by
		update_event() to exclude an event's own current slot from its own
		availability check. get_busy_slots() doesn't return event IDs
		directly (only start/end times), so this re-fetches the specific
		event to compare — acceptable since this only runs during the
		(infrequent) reschedule path, not on every availability check.
		"""
		try:
			event = self.get_event(event_id)
			if event is None:
				return False
			event_start = event.get('start', {}).get('dateTime')
			if not event_start:
				return False
			existing_start = datetime.datetime.fromisoformat(event_start)
			return existing_start == slot_start
		except Exception:
			return False

	def delete_event(self, event_id):
		"""Cancel/remove a booking's calendar event.

		Idempotent-ish: if the event is already gone (404), this is
		treated as success rather than raising, since the end state the
		caller wants ("no event exists") is already true. Any other
		failure (auth, network, etc.) re-raises so booking_manager.py can
		decide how to surface it (e.g. "cancellation partially failed,
		contact support").
		"""
		calendar_id = self.get_or_create_booking_calendar()
		try:
			self.service.events().delete(
				calendarId=calendar_id,
				eventId=event_id,
				sendUpdates='none',
			).execute()
			return True
		except Exception as e:
			# googleapiclient raises HttpError; avoid importing it here just
			# to check status, so we match on message content defensively.
			if '404' in str(e) or 'Not Found' in str(e):
				return True
			raise

	def get_event(self, event_id):
		"""Fetch a single event by ID, or None if it doesn't exist /
		was already cancelled. Used by cancellation_manager.py to verify
		a booking before acting on it."""
		calendar_id = self.get_or_create_booking_calendar()
		try:
			event = self.service.events().get(
				calendarId=calendar_id, eventId=event_id
			).execute()
			if event.get('status') == 'cancelled':
				return None
			return event
		except Exception as e:
			if '404' in str(e) or 'Not Found' in str(e):
				return None
			raise


class GoogleDriverHelper:
	...



if __name__ == '__main__':
	g = GoogleSheetsHelper()
	print(g.Delimiter_Type)