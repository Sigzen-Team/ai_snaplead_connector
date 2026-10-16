# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
#
# VENDORED COPY -- DO NOT IMPORT ai_snaplead FROM THIS APP.
#
# Source of truth:
#   normalize_mobile_for_auth  -- ai_snaplead/ai_snaplead/utils/otp.py
#   normalize_mobile_number    -- ai_snaplead/ai_snaplead/utils/business.py
#   normalize_email            -- the .strip().lower() applied by
#                                 ai_snaplead/ai_snaplead/utils/lead_contacts.normalize_email_list
#
# Both phone functions are copied verbatim, under the names they have in ai_snaplead, so a
# shared fixture can compare the two implementations function by function. See the note in
# india_state.py: ai_snaplead_connector/tests/fixtures/normaliser_cases.json is that fixture, and
# ai_snaplead still needs the twin test that reads it.
#
# The two phone functions answer different questions and the connector needs both:
#
#   normalize_mobile_for_auth  -- the STORED form. Whitespace removed, the leading "+" and
#                                 the country code kept. This is what lands in
#                                 Contact Phone.phone and in the Lead's mobile-number table,
#                                 so an international number survives the round trip.
#   normalize_mobile_number    -- the MATCH key: last 10 digits, India-shaped. Used only to
#                                 decide whether two numbers in one payload are the same
#                                 number. Storing this form would silently delete the country
#                                 code of every non-Indian lead, which is why it is not the
#                                 stored form.

import re


def normalize_mobile_for_auth(mobile):
	return re.sub(r"\s+", "", (mobile or "").strip())


def normalize_mobile_number(mobile):
	"""Normalize mobile numbers so invite/accept/signup can match reliably."""
	mobile = re.sub(r"\D", "", (mobile or "").strip())
	if not mobile:
		return ""

	if len(mobile) > 10:
		if mobile.startswith("91") and len(mobile) >= 12:
			mobile = mobile[-10:]
		else:
			mobile = mobile[-10:]

	if len(mobile) == 11 and mobile.startswith("0"):
		mobile = mobile[1:]

	return mobile


def normalize_email(email):
	"""The canonical stored form of an email address: trimmed and lower-cased.

	The client target is SQLite, whose default collation is case SENSITIVE, so a row stored
	as "A@B.com" is invisible to a later lookup for "a@b.com". Lower-casing on the way in is
	what makes the duplicate guard in ai_snaplead_connector.utils.lead_contacts work there at all.
	"""
	return (email or "").strip().lower()


def dedupe_phones(values):
	"""Stored forms of these numbers, in order, with repeats of the same number removed.

	"Same number" is decided by ``phone_match_key``, so "+91 98765 43210", "09876543210" and
	"9876543210" collapse to one row -- the first spelling wins, because that is the one the
	user typed. Lead.validate refuses a payload that repeats a contact, so collapsing here is
	what stops a harmless re-spelling from failing the whole push.

	NOT ``normalize_mobile_number``: that key is the last ten digits of anything longer, which
	makes "+1 212 555 0100" and "+44 20 2125 550100" one number and throws the second away.
	Collapsing two different numbers is data loss, and this is the function that writes the
	rows, so it uses the country-code-aware key.
	"""
	stored = []
	seen = set()

	for value in values or []:
		display = normalize_mobile_for_auth(value)
		if not display:
			continue

		if not is_storable_phone(display):
			# ERPNext validates Contact Phone against its own pattern and REFUSES the save,
			# so one mistyped number -- "+91-7405161796l3", an OCR 'l' for a '1' -- took the
			# entire lead down with "is not a valid Phone Number". Dropping the unusable
			# number keeps the lead, its name, its email and its other numbers; keeping it
			# loses all of them for the sake of a value nobody can dial anyway.
			continue

		key = phone_match_key(display) or display
		if key in seen:
			continue

		seen.add(key)
		stored.append(display)

	return stored


def is_storable_phone(value):
	"""Will ERPNext accept this as a phone number?

	Frappe's PHONE_NUMBER_PATTERN allows digits, spaces and the usual separators. A LETTER is
	the common OCR failure ('l' for '1', 'O' for '0') and is what makes the whole document
	unsaveable, so that is what this rejects. It deliberately does not try to REPAIR the
	number: guessing that 'l' meant '1' would invent a phone number and hand a salesperson a
	wrong one, which is worse than dropping it.
	"""
	value = (value or "").strip()
	if not value:
		return False
	if not any(ch.isdigit() for ch in value):
		return False
	return not any(ch.isalpha() for ch in value)


def dedupe_emails(values):
	"""Canonical forms of these addresses, in order, with repeats removed."""
	stored = []
	seen = set()

	for value in values or []:
		email = normalize_email(value)
		if not email or email in seen:
			continue

		seen.add(email)
		stored.append(email)

	return stored


# ── ai_snaplead_connector's own helpers (NOT vendored; ai_snaplead has no twin of these) ───────────
#
# `normalize_mobile_number` above is the vendored match key and must stay byte-identical to
# ai_snaplead's. It has two properties that are wrong for a MIGRATION, where being wrong means
# losing a number for good:
#
#   * it keeps only the last ten digits, so "+1 212 555 0100" and "+44 20 2125 550100" are the
#     same number to it. Deduping on that key writes one of them to the Contact and drops the
#     other -- and the verification gate, asking the same question, cannot see the gap;
#   * it says nothing about whether a value can actually be STORED. Frappe validates every
#     Contact Phone row against PHONE_NUMBER_PATTERN, so an ordinary business-card spelling
#     ("9876543210 (O)", "080-25551234 Ext 22", two numbers in one field) raises
#     InvalidPhoneNumberError and takes the whole Lead's migration down with it.
#
# So the migration uses `phone_match_key` and `storable_phones` below, and `unstorable_remainder`
# to make sure the part that cannot be stored is still written somewhere a human can read.

# Frappe's own rule for Contact Phone.phone / Lead.mobile_no, anchored:
# frappe/utils/__init__.py PHONE_NUMBER_PATTERN = r"([0-9 \+_\-,.\*#()]){1,20}$"
PHONE_STORAGE_PATTERN = re.compile(r"^[0-9 +_\-,.*#()]{1,20}$")

# A phone-shaped run: an optional bracketed trunk code, an optional "+", then a digit, then
# whatever else belongs to the number. Letters end the run, which is what separates
# "080-25551234" from the "Ext 22" written next to it on the card.
_PHONE_RUN = re.compile(r"\(?\s*\+?\d[\d\s\-.()]*")

# Below this, a run is an annotation fragment ("Ext 22", "(O)" with the O eaten), not a number.
MIN_SIGNIFICANT_DIGITS = 6

# Prefixes that mean "no country code was given": nothing at all, the Indian trunk 0, and the
# Indian country code. Everything else is significant, so two numbers that differ only in
# country code stay two numbers.
_LOCAL_PREFIXES = ("", "0", "91", "091", "0091", "00091", "00")


def _digits(value):
	return re.sub(r"\D", "", value or "")


def phone_match_key(mobile):
	"""When are two spellings the same phone number?

	The same answer as ``normalize_mobile_number`` for every Indian spelling -- "+91 98765
	43210", "09876543210" and "9876543210" all collapse -- but a country code that is NOT
	India's is kept in the key. Dropping it is how "+1 212 555 0100" and "+44 20 2125 550100"
	became one number, one of them never reaching the Contact and the drop going ahead anyway
	because verification asked the same question and got the same wrong answer.
	"""
	digits = _digits(mobile)
	if not digits:
		return ""

	if len(digits) <= 10:
		prefix, core = "", digits
	else:
		prefix, core = digits[:-10], digits[-10:]

	if prefix in _LOCAL_PREFIXES:
		prefix = ""

	return f"{prefix}:{core}"


def _candidate(text):
	"""The stored form of one phone-shaped run: whitespace out, stray bracketry closed."""
	candidate = normalize_mobile_for_auth(text).rstrip("-.(")
	if candidate.count("(") != candidate.count(")"):
		candidate = candidate.replace("(", "").replace(")", "")
	if candidate.startswith("(") and candidate.endswith(")") and candidate.count("(") == 1:
		# The whole run was written inside brackets -- "(7011477005)" -- so they are quoting,
		# not a trunk code, and the number is what is inside them.
		candidate = candidate[1:-1]
	return candidate


def _is_storable(candidate):
	return bool(_digits(candidate)) and bool(PHONE_STORAGE_PATTERN.match(candidate))


def _split_long_run(start, text):
	"""Two whole numbers written as one run -- "7011477004(7011477005)" -- or nothing.

	A run longer than frappe's 20 characters is refused as written, and the migration used to
	answer that by keeping whatever shorter run was next to it. That is how a "9" from "ext 9"
	became a phone number. The honest repair is narrow: split ONLY at a bracket that opens a
	piece holding a full number's worth of digits, and only when EVERY piece is then a number
	this site can store. "+91 (080) 2555-1234-5678" does not split -- "(080)" is a trunk code,
	not a number -- so it stays one unstorable run and goes to the note as printed.
	"""
	bounds = []
	cursor = 0
	for match in re.finditer(r"\(", text):
		if match.start() == 0:
			continue
		bounds.append((cursor, match.start()))
		cursor = match.start()
	bounds.append((cursor, len(text)))

	if len(bounds) < 2:
		return []

	runs = []
	for begin, end in bounds:
		candidate = _candidate(text[begin:end])
		if not _is_storable(candidate) or len(_digits(candidate)) < MIN_SIGNIFICANT_DIGITS:
			return []
		runs.append((start + begin, start + end, candidate, True))

	return runs


def _phone_runs(raw):
	"""Every phone-shaped run in a legacy value: ``(start, end, stored_form, storable)``."""
	runs = []

	for match in _PHONE_RUN.finditer(raw or ""):
		candidate = _candidate(match.group(0))
		if _is_storable(candidate):
			runs.append((match.start(), match.end(), candidate, True))
			continue

		split = _split_long_run(match.start(), match.group(0))
		runs.extend(split or [(match.start(), match.end(), candidate, False)])

	return runs


def storable_phones(raw):
	"""Every number a legacy phone value actually contains, in the form that can be stored.

	One legacy row is not always one number: cards are scanned with two numbers in one field,
	with an extension written after the number, or with "(O)" for "office". Frappe refuses all
	three, so the migration used to abort the whole Lead. This pulls the numbers out instead
	and leaves the annotation to ``unstorable_remainder``.
	"""
	runs = _phone_runs(raw)
	if not runs:
		return []

	significant = [
		run for run in runs if run[3] and len(_digits(run[2])) >= MIN_SIGNIFICANT_DIGITS
	]
	if not significant:
		if any(len(_digits(run[2])) >= MIN_SIGNIFICANT_DIGITS for run in runs):
			# There IS a full number in this value; it is simply one this site cannot store
			# (too long for frappe's 20 characters, or otherwise refused). Falling back to the
			# longest STORABLE run here is what turned "+91 (080) 2555-1234-5678 ext 9" into
			# the phone number "9": a value that was never on the card, written to the client's
			# Contact by a migration, with the gate asking this same function and agreeing.
			# Nothing storable means nothing stored -- ``unstorable_remainder`` then carries the
			# value to a tagged note as printed, which is where the real number survives.
			return []

		storable = [run for run in runs if run[3]]
		if not storable:
			return []

		# Every run is short, so the value is one short number (an extension-only desk line,
		# say) rather than a number plus an annotation. Keep the longest.
		significant = [max(storable, key=lambda run: len(_digits(run[2])))]

	stored = []
	seen = set()
	for _start, _end, candidate, _storable in significant:
		key = phone_match_key(candidate) or candidate
		if key in seen:
			continue
		seen.add(key)
		stored.append(candidate)

	return stored


def unstorable_remainder(raw):
	"""The original value, when part of it cannot be stored on a Contact Phone row.

	Returns "" when the stored numbers account for everything. Otherwise it returns the value
	as it was written, because that -- not a fragment -- is what a human needs to see when the
	Custom Field it came from no longer exists. The caller writes it to a tagged note.
	"""
	text = (raw or "").strip()
	if not text:
		return ""

	kept = [run for run in _phone_runs(text) if run[3] and run[2] in storable_phones(text)]

	remainder = list(text)
	for start, end, _candidate, _storable in kept:
		for index in range(start, end):
			remainder[index] = " "

	leftover = "".join(remainder)
	return text if any(char.isalnum() for char in leftover) else ""
