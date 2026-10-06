# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""PATCH A -- copy the legacy Custom Field values onto standard ERPNext fields.

This patch drops NOTHING. It only copies, and every copy it makes is idempotent, so running
it twice, or ten times, leaves exactly the state one run leaves. That is not a nicety: Patch B
re-enqueues this one whenever its verification finds a gap, so "run again" is a normal part of
the design and must never mean "append a second copy of everything".

Idempotency is achieved twice over, on purpose:

* per record, by the ``Connector Legacy Field Archive`` mark row. A record that already has one is
  skipped whole, which is what makes a full re-scan of a 200k-Lead client cheap;
* per value, by matching before writing -- an email address already on the Contact is not
  appended again, a CRM Note already carrying this app's tag is not written again, a File
  already re-parented is left alone. So even a record whose mark row was deleted by hand
  migrates cleanly a second time.

The mapping, and where each legacy field ends up:

  custom_email_ids[]        -> Contact.email_ids[]      (Contact found via Dynamic Link)
  custom_mobile_numbers[]   -> Contact.phone_nos[]      (deduped on phone_match_key)
  ...either of those, written in a form ERPNext refuses to store
                            -> Lead.notes[]             (a tagged CRM Note, as printed)
  custom_pincode            -> Address.pincode          (Address found via Dynamic Link)
  custom_scanned_text       -> Lead.notes[]             (a tagged CRM Note row)
  custom_front_card_image   -> a standard File attachment on the Lead
  custom_back_card_image    -> a standard File attachment on the Lead
  custom_created_from_scan  -> Lead.utm_source          (only when utm_source is empty)
  custom_business_id        -> Connector Lead Link.business_id (vc_uuid left NULL until the push)
  custom_lead_activity_log  -> nothing. It has no standard ERPNext counterpart; it survives
                               only in the Patch B archive, and that is stated plainly in
                               README.md rather than hidden behind a mapping that does not
                               exist.

``custom_address`` is not in this list. It is the one Custom Field that stays.
"""

import json
import re

import frappe
from frappe.model.naming import is_autoincremented
from frappe.query_builder.functions import Max
from frappe.utils import cint, cstr, now_datetime, validate_email_address

from sigzen_ai_scan_crm_connector.legacy import archive, state
from sigzen_ai_scan_crm_connector.legacy.schema import (
	ADDRESS_TITLE_LIMIT,
	LEGACY_LEAD_DATA_FIELDS,
	LEGACY_LEAD_TABLE_FIELDS,
	SCAN_SOURCE_NAME,
	SCANNED_ADDRESS_SUFFIX,
	SCANNED_TEXT_TAG,
	UNSTORABLE_CONTACT_TAG,
	existing_lead_fields,
	get_lead_source_fieldname,
	get_source_doctype,
	scanned_address_title,
)
from sigzen_ai_scan_crm_connector.sigzen___ai_scan_crm_connector.doctype.connector_legacy_field_archive.connector_legacy_field_archive import (
	get_row as get_archive_row,
)
from sigzen_ai_scan_crm_connector.sigzen___ai_scan_crm_connector.doctype.connector_legacy_field_archive.connector_legacy_field_archive import (
	make_lookup_key,
)
from sigzen_ai_scan_crm_connector.utils.phone import (
	normalize_email,
	normalize_mobile_for_auth,
	phone_match_key,
	storable_phones,
	unstorable_remainder,
)

BATCH_SIZE = 200
JOB_ID = "sigzen_ai_scan_crm_connector::legacy_field_migration"
DEFAULT_ADDRESS_CITY = "-"
DEFAULT_ADDRESS_COUNTRY = "India"
DEFAULT_ADDRESS_LINE1 = "Address from business card scan"


# ── entry points ─────────────────────────────────────────────────────────────────────────


def enqueue(reset=False):
	"""Queue the migration and return immediately. Called by the patch, and by Patch B.

	A patch that walks the client's whole Lead table inside ``bench migrate`` is a migrate
	that appears to hang, so this never does the work itself. If the site has no queue at all
	the failure is recorded rather than raised: a missing Redis must not abort the client's
	migrate, and ``run_now`` is there for exactly that case.
	"""
	state.mark_queued(reset=reset)

	try:
		frappe.enqueue(
			"sigzen_ai_scan_crm_connector.legacy.migrate.run",
			queue="long",
			timeout=21600,
			job_id=JOB_ID,
			deduplicate=True,
			enqueue_after_commit=True,
			reset=reset,
		)
		return True
	except Exception:
		state.mark_failed(
			"Could not enqueue the legacy field migration on this site. Run it by hand:\n"
			"  bench --site <site> execute sigzen_ai_scan_crm_connector.legacy.migrate.run_now\n\n"
			+ frappe.get_traceback()
		)
		return False


def run_now(reset=True):
	"""Run the whole migration in this process, committing per batch.

	The supported way to migrate a client that has no background workers, and the entry point
	the tests drive. ``reset`` defaults to True here because a human asking for it by hand
	means "go over everything again"; the per-record mark makes that cheap and safe.
	"""
	return run(reset=reset)


def run(reset=False):
	if not frappe.db.exists("DocType", "Connector Legacy Field Archive"):
		# Model sync has not created the archive yet, so there is nowhere to record a mark and
		# the migration would not be re-runnable. Refuse rather than migrate unmarked.
		state.mark_failed("Connector Legacy Field Archive does not exist yet; run bench migrate first.")
		return None

	if reset:
		state.set_values(
			legacy_migration_cursor=None,
			legacy_migration_scanned=0,
			legacy_migration_migrated=0,
			legacy_migration_failed=0,
			legacy_migration_conflicts=0,
			legacy_migration_started_on=None,
			legacy_migration_completed_on=None,
		)

	state.mark_running()

	totals = {
		"scanned": cint(state.get("legacy_migration_scanned")),
		"migrated": cint(state.get("legacy_migration_migrated")),
		"failed": cint(state.get("legacy_migration_failed")),
		"conflicts": cint(state.get("legacy_migration_conflicts")),
	}
	cursor = state.cursor()

	try:
		while True:
			names = _next_batch(cursor)
			if not names:
				break

			for name in names:
				_migrate_one_guarded(name, totals)

			cursor = names[-1]
			frappe.db.commit()
			state.advance(cursor, totals["scanned"], totals["migrated"], totals["failed"], totals["conflicts"])
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		state.mark_failed(frappe.get_traceback())
		frappe.db.commit()
		raise

	state.mark_complete(totals["scanned"], totals["migrated"], totals["failed"], totals["conflicts"])
	frappe.db.commit()  # nosemgrep - one-time legacy migration, run as a patch or by an operator; each stage must be durable before the next destructive one
	return totals


def _next_batch(cursor):
	"""The next page of Leads, ordered by name.

	Ordered by the primary key rather than by ``modified`` on purpose: the migration writes to
	Leads, so a ``modified``-ordered cursor would walk over rows it had just moved and could
	skip or repeat them. ``name`` is total, stable and indexed.
	"""
	filters = {}
	if cursor:
		filters["name"] = (">", cursor)

	return frappe.get_all("Lead", filters=filters, order_by="name asc", limit=BATCH_SIZE, pluck="name")


def _migrate_one_guarded(name, totals):
	"""One Lead, inside its own savepoint.

	A client's Lead book is not clean. One Lead whose Contact will not validate must not take
	the other 199,999 down with it, and it must not leave half a graph behind either -- so the
	failure rolls back to the start of THIS Lead and is recorded on its mark row.
	"""
	totals["scanned"] += 1
	savepoint = "vc_legacy_lead"
	frappe.db.savepoint(savepoint)

	try:
		outcome = migrate_lead(name)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		totals["failed"] += 1
		_record_mark(name, "Failed", {"error": frappe.get_traceback(with_context=False)})
		return

	frappe.db.release_savepoint(savepoint)

	if outcome is None:
		return

	totals["migrated"] += 1
	if outcome.get("custom_pincode", {}).get("outcome") == "conflict":
		totals["conflicts"] += 1


# ── one Lead ─────────────────────────────────────────────────────────────────────────────


def migrate_lead(name):
	"""Copy one Lead's legacy values onto standard fields. Returns the notes, or None.

	None means "there was nothing to do": either the Lead carries no legacy value at all, or
	it already has a mark row from an earlier run. Neither case gets a mark written, because
	a mark row per contactless Lead would add a row to this table for every Lead on the site
	and say nothing.
	"""
	mark = get_archive_row("Lead", name)
	if mark and mark.migration_status == "Migrated":
		return None
	# A mark row with status Failed is NOT a skip. The previous pass could not finish this
	# Lead -- and Patch B re-enqueues this migration precisely to give such a Lead another
	# go, after whatever blocked it has been fixed. Skipping it would make the re-enqueue a
	# no-op and leave the client stuck at "verification found 1 Lead" for ever.

	legacy = read_legacy_lead(name)
	if not _has_any_legacy_value(legacy):
		return None

	notes = {}
	notes["custom_email_ids"], notes["custom_mobile_numbers"] = _migrate_contacts(name, legacy)
	notes["custom_pincode"] = _migrate_pincode(name, legacy)
	notes["custom_scanned_text"] = _migrate_scanned_text(name, legacy)
	notes.update(_migrate_card_images(name, legacy))
	notes["custom_created_from_scan"] = _migrate_created_from_scan(name, legacy)
	notes["custom_business_id"] = _migrate_business_id(name, legacy)

	_record_mark(name, "Migrated", notes)
	return notes


def read_legacy_lead(name):
	"""Everything this Lead holds in the legacy fields, plus the few stock fields we need.

	Only columns the Lead actually has are selected: a client who deleted one of the Custom
	Fields by hand must not make every SELECT in this module raise.
	"""
	scalar_fields = [
		fieldname
		for fieldname in existing_lead_fields(LEGACY_LEAD_DATA_FIELDS)
		if fieldname not in LEGACY_LEAD_TABLE_FIELDS
	]
	stock_fields = ["name", "lead_name", "first_name", "last_name", "middle_name", "salutation"]
	stock_fields += ["company_name", "job_title", "email_id", "mobile_no", "city", "state", "country"]
	stock_fields += [get_lead_source_fieldname()]
	if frappe.get_meta("Lead").has_field("custom_address"):
		stock_fields.append("custom_address")

	values = frappe.db.get_value("Lead", name, stock_fields + scalar_fields, as_dict=True) or frappe._dict()

	for fieldname, (child_doctype, _columns) in LEGACY_LEAD_TABLE_FIELDS.items():
		values[fieldname] = _child_rows("Lead", name, fieldname, child_doctype)

	return values


def _child_rows(parent_doctype, parent_name, parentfield, child_doctype):
	if not frappe.get_meta(parent_doctype).has_field(parentfield):
		return []
	if not frappe.db.table_exists(child_doctype):
		return []

	return frappe.get_all(
		child_doctype,
		filters={"parent": parent_name, "parenttype": parent_doctype, "parentfield": parentfield},
		fields=["*"],
		order_by="idx asc",
	)


def _has_any_legacy_value(legacy):
	"""Does this Lead carry anything at all in the legacy fields?

	``custom_created_from_scan`` is a Check, and an unticked Check is the integer 0, whose
	string form "0" is perfectly truthy. Reading it as a value gave EVERY Lead on the site a
	mark row -- one row per Lead in a table that is supposed to hold only the migrated ones,
	which on a 200k-Lead client is 200k rows of nothing.
	"""
	meta = frappe.get_meta("Lead")

	for fieldname in LEGACY_LEAD_DATA_FIELDS:
		value = legacy.get(fieldname)

		if isinstance(value, list):
			if value:
				return True
			continue

		field = meta.get_field(fieldname)
		if field and field.fieldtype == "Check":
			if cint(value):
				return True
			continue

		if cstr(value).strip():
			return True

	return False


# ── custom_email_ids[] / custom_mobile_numbers[] -> Contact ──────────────────────────────


# One legacy email field is not always one address: a scanned card gives
# "sales@acme.com, ravi@acme.com" and a pasted mail client gives "Ravi Kumar <ravi@acme.com>".
_EMAIL_LIST_SEPARATOR = re.compile(r"[,;]+")
_EMAIL_IN_ANGLES = re.compile(r"<([^<>]+)>")
# Anything a Contact Email row can hold and a later lookup can find BY ITSELF: no separator,
# no display name, no second address hiding inside it.
_SINGLE_ADDRESS = re.compile(r"^[^\s<>,;]+@[^\s<>,;]+$")


def storable_emails(raw):
	"""Every address a legacy email value contains, each in a form the Contact can hold.

	``validate_email_address`` is NOT a usable answer on its own, and this is the bug that
	makes this function exist: it is truthy for "a@b.com, c@d.com" (it returns both, joined)
	and for "John Doe <j@d.com>" (it returns the address out of the angle brackets). Contact
	Email.email_id is a Data-backed Email field that stores either string without complaint,
	so the migration used to write ONE malformed row holding two addresses -- neither of them
	findable by a lookup for itself -- while the verification gate, asking the same question,
	agreed that the value had been migrated. That is the same "two values in one legacy field"
	bug that ``storable_phones`` exists to fix, and this is its other half.

	So each address is pulled out separately and every stored form is checked against a shape
	a lookup can match exactly. Whatever is left over -- the display name, an address that is
	not one -- is reported by ``unstorable_email_remainder`` and written to a tagged note.
	"""
	found = []

	for part in _EMAIL_LIST_SEPARATOR.split(cstr(raw)):
		part = part.strip()
		if not part:
			continue

		angled = _EMAIL_IN_ANGLES.search(part)
		if angled:
			part = angled.group(1).strip()

		email = normalize_email(part)
		if not email or not _SINGLE_ADDRESS.match(email):
			continue

		# Contact Email.email_id is an Email field: Frappe validates it on save and THROWS. A
		# scanned card that produced "john(at)example.com" would otherwise abort this Lead's
		# whole migration -- pincode, scanned text and card images with it.
		if not validate_email_address(email):
			continue

		if email not in found:
			found.append(email)

	return found


def unstorable_email_remainder(raw):
	"""The value as printed, when part of it cannot be stored on a Contact Email row.

	"" when the stored addresses account for the whole value -- separators and angle brackets
	are punctuation, not content. Otherwise the value as it was written, because a display
	name, or an address ERPNext refuses, is something a human has to be able to read after the
	Custom Field it came from is gone. The caller writes it to a tagged note.
	"""
	text = cstr(raw).strip()
	if not text:
		return ""

	remainder = text.lower()
	for email in storable_emails(text):
		remainder = remainder.replace(email, " ")

	return text if any(char.isalnum() for char in remainder) else ""


def _migrate_contacts(name, legacy):
	"""Legacy email/phone rows -> the Contact linked to this Lead.

	Two rules here exist because a client's card scans are not clean data, and because this is
	a migration: whatever does not land somewhere readable is gone for good once Patch B drops
	the field.

	* a value ERPNext refuses to store does NOT abort the Lead. "9876543210 (O)", "080-25551234
	  Ext 22" and two numbers in one field all fail Frappe's PHONE_NUMBER_PATTERN, and an
	  exception here used to roll this Lead's savepoint back -- losing its pincode, its scanned
	  text and its card images too, with no way to force-skip it and therefore no drop, ever.
	  The numbers are pulled out and stored, and the value as printed is written to a tagged
	  note so the annotation survives the drop;
	* two numbers are the same number only by ``phone_match_key``. The vendored
	  ``normalize_mobile_number`` keeps the last ten digits of anything longer, which makes
	  "+1 212 555 0100" and "+44 20 2125 550100" one number and writes only the first.
	"""
	emails = []
	unstorable = []

	for row in legacy.get("custom_email_ids") or []:
		raw = cstr(row.get("email_id")).strip()
		for email in storable_emails(raw):
			if email not in emails:
				emails.append(email)

		remainder = unstorable_email_remainder(raw)
		if remainder:
			unstorable.append(remainder)

	phones = []
	phone_keys = set()

	for row in legacy.get("custom_mobile_numbers") or []:
		raw = cstr(row.get("mobile_number")).strip()
		for stored in storable_phones(raw):
			key = phone_match_key(stored) or stored
			if key in phone_keys:
				continue
			phone_keys.add(key)
			phones.append(stored)

		remainder = unstorable_remainder(raw)
		if remainder:
			unstorable.append(remainder)

	notes_written = [write_unstorable_contact_note(name, value) for value in unstorable]
	if unstorable:
		_log_once(
			name,
			"CONTACT_VALUE_UNSTORABLE",
			"Lead {0}: ERPNext cannot store {1} as written on a Contact ({2}). The number(s) in"
			" it were stored and the value as printed was added to the Lead's notes, so it"
			" survives the drop of the legacy field.".format(
				name, "these contact values" if len(unstorable) > 1 else "this contact value",
				"; ".join(unstorable),
			),
		)

	if not emails and not phones:
		outcome = {"outcome": "unstorable", "unstorable": unstorable} if unstorable else {"outcome": "empty"}
		return (dict(outcome), dict(outcome))

	contact = _get_or_create_contact(name, legacy)

	held_emails = {normalize_email(row.email_id) for row in (contact.get("email_ids") or [])}
	added_emails = []
	for email in emails:
		if email in held_emails:
			continue
		held_emails.add(email)
		contact.append("email_ids", {"email_id": email})
		added_emails.append(email)

	held_phone_keys = set()
	for row in contact.get("phone_nos") or []:
		stored = normalize_mobile_for_auth(row.phone)
		held_phone_keys.add(phone_match_key(stored) or stored)

	added_phones = []
	for phone in phones:
		key = phone_match_key(phone) or phone
		if key in held_phone_keys:
			continue
		held_phone_keys.add(key)
		contact.append("phone_nos", {"phone": phone})
		added_phones.append(phone)

	# is_primary only when nothing is primary yet. Re-pointing a client's primary email or
	# phone at a scanned card is a change they never asked for, and it changes which address
	# every stock ERPNext email workflow uses.
	_set_primary_if_unset(contact.get("email_ids"), ("is_primary",))
	_set_primary_if_unset(contact.get("phone_nos"), ("is_primary_phone", "is_primary_mobile_no"))

	if contact.is_new():
		contact.insert(ignore_permissions=True)
	else:
		contact.save(ignore_permissions=True)

	email_note = {"outcome": "migrated" if added_emails else "already", "contact": contact.name}
	if added_emails:
		email_note["added"] = added_emails
	if not emails:
		email_note["outcome"] = "empty"

	phone_note = {"outcome": "migrated" if added_phones else "already", "contact": contact.name}
	if added_phones:
		phone_note["added"] = added_phones
	if not phones:
		phone_note["outcome"] = "empty"

	if unstorable:
		email_note["unstorable"] = phone_note["unstorable"] = unstorable
		email_note["unstorable_notes"] = phone_note["unstorable_notes"] = notes_written

	return email_note, phone_note


def write_unstorable_contact_note(name, value):
	"""Write a contact detail ERPNext will not store to a tagged CRM Note, once.

	Public for the same reason as ``write_scanned_note``: after the drop, a live payload
	carrying the same kind of value has to land in the same place, or the client would have
	the annotation in two shapes depending on when it arrived.

	Idempotent per value, not per Lead: a Lead with two unstorable values gets two notes, and
	a re-run finds each of them by its own text rather than appending a second copy.
	"""
	value = cstr(value).strip()
	if not value:
		return {"outcome": "empty"}

	if not frappe.get_meta("Lead").has_field("notes"):
		_log_once(name, "NOTES_FIELD_MISSING", f"Lead {name}: this ERPNext has no Lead.notes table.")
		return {"outcome": "unavailable", "value": value}

	body = frappe.utils.escape_html(value)
	existing = frappe.get_all(
		"CRM Note",
		filters={
			"parent": name,
			"parenttype": "Lead",
			"parentfield": "notes",
			"note": ("like", f"%{UNSTORABLE_CONTACT_TAG}%{body}%"),
		},
		limit=1,
		pluck="name",
	)
	if existing:
		return {"outcome": "already", "note": existing[0], "value": value}

	note = _append_child_row(
		"Lead",
		name,
		"notes",
		"CRM Note",
		{
			"note": "{0}<p>Contact detail as printed on the card, which ERPNext cannot store on a"
			" Contact: {1}</p>".format(UNSTORABLE_CONTACT_TAG, body),
			"added_by": frappe.session.user,
			"added_on": now_datetime(),
		},
	)
	return {"outcome": "migrated", "note": note, "value": value}


def _set_primary_if_unset(rows, fieldnames):
	rows = rows or []
	if not rows:
		return
	for fieldname in fieldnames:
		if any(cint(row.get(fieldname)) for row in rows):
			continue
		rows[0].set(fieldname, 1)


def _get_or_create_contact(name, legacy):
	"""The Contact already linked to this Lead, or a new one carrying that link.

	ERPNext creates a Contact itself inside ``Lead.before_insert``, so almost every Lead on a
	client already has one. Creating a second is how a client ends up with two Contacts per
	Lead and no way to tell which one their reports are reading.
	"""
	existing = _linked_document("Contact", name)
	if existing:
		return frappe.get_doc("Contact", existing)

	contact = frappe.new_doc("Contact")
	contact.first_name = (
		cstr(legacy.get("first_name")).strip()
		or cstr(legacy.get("lead_name")).strip()
		or cstr(legacy.get("company_name")).strip()
		or name
	)
	for fieldname in ("middle_name", "last_name", "salutation", "company_name"):
		value = cstr(legacy.get(fieldname)).strip()
		if value:
			contact.set(fieldname, value)
	if cstr(legacy.get("job_title")).strip():
		contact.designation = cstr(legacy.get("job_title")).strip()

	contact.append("links", {"link_doctype": "Lead", "link_name": name})
	return contact


def _linked_document(doctype, lead_name):
	return frappe.db.get_value(
		"Dynamic Link",
		{"parenttype": doctype, "link_doctype": "Lead", "link_name": lead_name},
		"parent",
	)


def _linked_documents(doctype, lead_name):
	return frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": doctype, "link_doctype": "Lead", "link_name": lead_name},
		pluck="parent",
		order_by="creation asc",
	)


# ── custom_pincode -> Address.pincode ────────────────────────────────────────────────────


def _migrate_pincode(name, legacy):
	"""Make sure SOME Address linked to this Lead carries the legacy pincode.

	Stated that way, the conflict rule and the idempotency rule are the same rule, and the
	verification query in Patch B is one lookup. The three shapes:

	* an Address already has this pincode -- nothing to do, on the first run or the fifth;
	* an Address has no pincode -- fill it in;
	* every linked Address already has a DIFFERENT, non-empty pincode -- keep BOTH. The
	  existing Address is not touched, a second one is created carrying the scanned pincode,
	  and a Connector Log row records the pair. Overwriting would silently replace an
	  address a human typed with one an OCR pass guessed, and the legacy column is about to
	  be dropped, so there would be no way back.
	"""
	pincode = cstr(legacy.get("custom_pincode")).strip()
	if not pincode:
		return {"outcome": "empty"}

	return ensure_pincode_on_address(name, pincode, legacy=legacy)


def ensure_pincode_on_address(name, pincode, legacy=None):
	"""Make sure some Address linked to this Lead carries ``pincode``. Idempotent.

	Public because the live push endpoint needs the identical rule once ``custom_pincode``
	has been dropped: a pincode that arrives in a payload after the drop must land the same
	way, and must refuse to overwrite a human-entered pincode the same way, as one the
	migration moved.
	"""
	pincode = cstr(pincode).strip()
	if not pincode:
		return {"outcome": "empty"}

	if legacy is None:
		legacy = frappe.db.get_value(
			"Lead", name, ["lead_name", "company_name", "city", "state", "country"], as_dict=True
		) or frappe._dict()
		if frappe.get_meta("Lead").has_field("custom_address"):
			legacy["custom_address"] = frappe.db.get_value("Lead", name, "custom_address")

	linked = _linked_documents("Address", name)
	held = {
		address: cstr(frappe.db.get_value("Address", address, "pincode")).strip() for address in linked
	}

	for address, existing in held.items():
		if existing == pincode:
			return {"outcome": "already", "address": address}

	for address, existing in held.items():
		if not existing:
			frappe.db.set_value("Address", address, "pincode", pincode, update_modified=False)
			return {"outcome": "filled", "address": address}

	conflicting = sorted(held.items())
	address = _create_address(
		name, legacy, pincode, address_type="Other", suffix=SCANNED_ADDRESS_SUFFIX
	)

	if conflicting:
		_log_once(
			name,
			"PINCODE_CONFLICT",
			"Lead {0} already had address(es) with a different pincode, so the scanned pincode"
			" {1} was kept on a SECOND address ({2}) instead of overwriting. Existing: {3}".format(
				name, pincode, address, ", ".join(f"{a}={p}" for a, p in conflicting)
			),
		)
		return {"outcome": "conflict", "address": address, "kept": dict(conflicting)}

	return {"outcome": "created", "address": address}


def _ensure_address_template():
	"""ERPNext refuses to save ANY Address on a site with no default Address Template.

	A client who never finished the ERPNext setup wizard has none, and on that site every
	pincode in this migration would fail -- which, because Patch B waits for a clean
	verification, would block the drop for ever with a message about print formats.

	So one is created, but only when the site has none at all. If the client has templates
	and simply has not marked one default, that is their configuration and this leaves it
	alone: the Address save then fails, the Lead is marked Failed and the reason is in the
	traceback on its mark row.
	"""
	if frappe.db.exists("Address Template", {"is_default": 1}):
		return
	if frappe.db.count("Address Template"):
		return

	template = frappe.new_doc("Address Template")
	template.country = DEFAULT_ADDRESS_COUNTRY
	template.is_default = 1
	template.template = (
		"{{ address_line1 }}<br>{% if address_line2 %}{{ address_line2 }}<br>{% endif -%}"
		"{{ city }}<br>{% if state %}{{ state }}<br>{% endif -%}"
		"{% if pincode %}{{ pincode }}<br>{% endif -%}{{ country }}<br>"
	)
	template.insert(ignore_permissions=True)


def _create_address(name, legacy, pincode, address_type="Billing", suffix=""):
	_ensure_address_template()
	title = cstr(legacy.get("lead_name")).strip() or cstr(legacy.get("company_name")).strip() or name
	line1 = cstr(legacy.get("custom_address")).strip().splitlines()
	line1 = (line1[0].strip() if line1 else "") or DEFAULT_ADDRESS_LINE1

	address = frappe.new_doc("Address")
	address.update(
		{
			"address_title": scanned_address_title(title)
			if suffix == SCANNED_ADDRESS_SUFFIX
			else title[:ADDRESS_TITLE_LIMIT],
			"address_type": address_type,
			"address_line1": line1[:240],
			"city": cstr(legacy.get("city")).strip() or DEFAULT_ADDRESS_CITY,
			"state": cstr(legacy.get("state")).strip(),
			"country": cstr(legacy.get("country")).strip() or DEFAULT_ADDRESS_COUNTRY,
			"pincode": pincode,
		}
	)
	address.append("links", {"link_doctype": "Lead", "link_name": name})
	address.insert(ignore_permissions=True)
	return address.name


# ── custom_scanned_text -> Lead.notes ────────────────────────────────────────────────────


def _migrate_scanned_text(name, legacy):
	"""Append the scanned text as a CRM Note, tagged so a re-run finds its own work.

	The row is written straight into the child table rather than through ``Lead.save()``. A
	save runs the whole Lead validation chain -- duplicate-contact guards, mandatory fields,
	ERPNext's own rules -- against data this migration did not create and cannot fix, so one
	client Lead that cannot re-validate would otherwise lose its scanned text for good. The
	note is additive and touches nothing else on the row, so there is no validation to run.
	"""
	text = cstr(legacy.get("custom_scanned_text")).strip()
	if not text:
		return {"outcome": "empty"}

	return write_scanned_note(name, text)


def _scanned_note_html(text):
	return "{0}<p>{1}</p>".format(SCANNED_TEXT_TAG, frappe.utils.escape_html(text).replace("\n", "<br>"))


def write_scanned_note(name, text, update_existing=False):
	"""Put ``text`` on Lead.notes as the one tagged CRM Note this connector owns.

	Public because the live push endpoint needs it too: once ``custom_scanned_text`` has been
	dropped, a protocol-1 payload carrying scanned text has to land in the same place the
	migration put the old values, or the client would have scanned text in two different
	shapes depending on when it arrived.

	``update_existing`` is what tells the two callers apart, and it is the whole of a fix.

	For the MIGRATION the note is write-once: it is copying a field that is about to be
	dropped, re-running must not duplicate it, and there is no newer text to prefer.

	For a LIVE PUSH it is not. `custom_scanned_text` no longer exists, so every push after
	the first found this function's own tag, returned {"outcome": "already"} and wrote
	nothing -- and the caller discarded the return value, so a corrected or re-OCR'd scan
	was answered ok:true, outcome:"updated", warnings:[] and stored nowhere on the client.
	The text simply ceased to exist. With ``update_existing`` the tagged note is rewritten
	when the text has actually changed, which is what the dropped field would have done.
	"""
	text = cstr(text).strip()
	if not text:
		return {"outcome": "empty"}

	if not frappe.get_meta("Lead").has_field("notes"):
		_log_once(name, "NOTES_FIELD_MISSING", f"Lead {name}: this ERPNext has no Lead.notes table.")
		return {"outcome": "unavailable"}

	existing = frappe.get_all(
		"CRM Note",
		filters={
			"parent": name,
			"parenttype": "Lead",
			"parentfield": "notes",
			"note": ("like", f"%{SCANNED_TEXT_TAG}%"),
		},
		fields=["name", "note"],
	)
	note_html = _scanned_note_html(text)

	if existing:
		row = existing[0]
		if not update_existing or cstr(row.note) == note_html:
			return {"outcome": "already", "note": row.name}

		frappe.db.set_value(
			"CRM Note",
			row.name,
			{"note": note_html, "added_by": frappe.session.user, "added_on": now_datetime()},
			update_modified=False,
		)
		return {"outcome": "updated", "note": row.name}

	note = _append_child_row(
		"Lead",
		name,
		"notes",
		"CRM Note",
		{
			"note": note_html,
			"added_by": frappe.session.user,
			"added_on": now_datetime(),
		},
	)
	return {"outcome": "migrated", "note": note}


def _append_child_row(parent_doctype, parent_name, parentfield, child_doctype, values):
	table = frappe.qb.DocType(child_doctype)
	highest = (
		frappe.qb.from_(table)
		.select(Max(table.idx))
		.where(table.parent == parent_name)
		.where(table.parenttype == parent_doctype)
		.where(table.parentfield == parentfield)
	).run()
	idx = cint(highest[0][0] if highest and highest[0] else 0) + 1

	row = frappe.new_doc(child_doctype)
	row.update(values)
	row.parent = parent_name
	row.parenttype = parent_doctype
	row.parentfield = parentfield
	row.idx = idx

	if is_autoincremented(child_doctype):
		row.name = _next_autoincrement_name(child_doctype)
		row.db_insert()
	else:
		row.insert(ignore_permissions=True)

	return row.name


def _next_autoincrement_name(doctype):
	"""The next primary key for an autoincrement DocType, on every supported database.

	``CRM Note`` -- the child table Lead.notes points at -- is autoincrement, and frappe names
	such a row by asking the database for ``nextval(<doctype>_id_seq)``. SQLite has no
	sequences at all, so that call raises ``no such column: crm_note_id_seq`` and every single
	scanned-text note on a SQLite client would be lost. The table itself is fine -- SQLite
	declares the column ``INTEGER PRIMARY KEY AUTOINCREMENT`` -- it is only frappe's naming
	layer that assumes a sequence exists.

	So: ask for the sequence, and fall back to MAX(name) + 1 when there is none. The fallback
	is only ever reached on SQLite, where a failed statement does not poison the transaction,
	and where this migration is the only writer.
	"""
	try:
		return frappe.db.get_next_sequence_val(doctype)
	except Exception:
		table = frappe.qb.DocType(doctype)
		highest = (frappe.qb.from_(table).select(Max(table.name))).run()
		return cint(highest[0][0] if highest and highest[0] else 0) + 1


# ── card images -> standard File attachments ─────────────────────────────────────────────


def _migrate_card_images(name, legacy):
	"""Re-parent THIS Lead's File row; never re-upload, never steal another Lead's.

	The bytes are already on the client's disk. Reading them back and writing them again
	would double the storage a scanned-card client pays for and would change the file_url,
	breaking every link anyone had saved. So this moves the row's pointer only.

	``attached_to_field`` is cleared, and that is the whole point of the exercise: a File
	whose ``attached_to_field`` names ``custom_front_card_image`` is orphaned the moment that
	Custom Field is dropped -- it disappears from the Lead's attachment sidebar. Clearing it
	makes the File a plain attachment on the Lead, which outlives the field.

	The hard part is WHICH File row. Frappe dedupes uploads by content hash, so two Leads
	scanned from the same card -- or two failed scans that produced the same placeholder image
	-- have two File rows sharing one ``file_url``. Resolving the row by URL alone returns
	whichever one the database hands back first, and re-parenting it moved a single row back
	and forth between the two Leads: each pass stripped the attachment off the Lead that had
	it, verification failed for whichever Lead was not holding it, Patch B reset the marks and
	the next pass flipped the pair again. An infinite repair loop in which the drop can never
	happen and one of the two Leads has no card image at any moment.

	So: prefer a row already attached to THIS Lead, then an unattached one, and if every row
	with this URL belongs to some other record, write a sibling row for this Lead pointing at
	the same bytes. That is exactly the shape frappe's own duplicate-upload handling produces,
	it costs no disk, and it takes nothing away from the other Lead.
	"""
	notes = {}

	for fieldname in ("custom_front_card_image", "custom_back_card_image"):
		url = cstr(legacy.get(fieldname)).strip()
		if not url:
			notes[fieldname] = {"outcome": "empty"}
			continue

		rows = _file_rows(url)
		if not rows:
			# The field holds a URL whose File row is gone. There is nothing to re-parent and
			# nothing to fabricate, so this is logged and the migration continues. Patch B's
			# verification accepts a "missing" note, otherwise one broken link on one Lead
			# would block the drop for the whole site for ever.
			_log_once(
				name,
				"CARD_FILE_MISSING",
				f"Lead {name}: {fieldname} points at {url}, but no File row has that file_url."
				" The attachment could not be re-parented and the URL survives only in the"
				" Connector Legacy Field Archive.",
			)
			notes[fieldname] = {"outcome": "missing", "file_url": url}
			continue

		owned = _own_file_row(rows, name)
		if owned:
			if not owned.attached_to_field:
				notes[fieldname] = {"outcome": "already", "file": owned.name, "file_url": url}
				continue

			frappe.db.set_value(
				"File",
				owned.name,
				{"attached_to_doctype": "Lead", "attached_to_name": name, "attached_to_field": None},
				update_modified=False,
			)
			notes[fieldname] = {"outcome": "migrated", "file": owned.name, "file_url": url}
			continue

		free = next((row for row in rows if not row.attached_to_name), None)
		if free:
			frappe.db.set_value(
				"File",
				free.name,
				{"attached_to_doctype": "Lead", "attached_to_name": name, "attached_to_field": None},
				update_modified=False,
			)
			notes[fieldname] = {"outcome": "migrated", "file": free.name, "file_url": url}
			continue

		sibling = _copy_file_row(rows[0], name)
		_log_once(
			name,
			"CARD_FILE_SHARED",
			f"Lead {name}: {fieldname} ({url}) is the same image as another record's"
			f" attachment ({rows[0].name}, on {rows[0].attached_to_doctype} {rows[0].attached_to_name})."
			f" A second File row ({sibling}) was created for this Lead pointing at the same"
			" bytes, rather than taking the attachment away from that record.",
		)
		notes[fieldname] = {"outcome": "copied", "file": sibling, "file_url": url, "shared_with": rows[0].name}

	return notes


def _file_rows(url):
	"""Every File row carrying this URL, oldest first.

	``frappe.db.get_value("File", {"file_url": url}, "name")`` returns ONE of them, chosen by
	the database. When the URL is shared -- which frappe's content-hash dedupe makes routine --
	that one row is as likely to belong to another Lead as to this one.
	"""
	return frappe.get_all(
		"File",
		filters={"file_url": url},
		fields=[
			"name",
			"file_name",
			"file_url",
			"is_private",
			"file_size",
			"content_hash",
			"folder",
			"attached_to_doctype",
			"attached_to_name",
			"attached_to_field",
		],
		order_by="creation asc, name asc",
	)


def _own_file_row(rows, name):
	"""The row already attached to THIS Lead, preferring one that needs no change."""
	mine = [
		row for row in rows if row.attached_to_doctype == "Lead" and row.attached_to_name == name
	]
	if not mine:
		return None

	return next((row for row in mine if not row.attached_to_field), mine[0])


def _copy_file_row(row, name):
	"""A second File row on this Lead for bytes another record's File row already owns.

	No bytes are written: the new row carries the same ``file_url``, which is precisely what
	frappe does itself when the same image is uploaded twice. ``_discard_files`` in the live
	endpoint knows about this shape too -- it never deletes bytes another row still points at.
	"""
	doc = frappe.new_doc("File")
	doc.file_name = row.file_name
	doc.file_url = row.file_url
	doc.is_private = cint(row.is_private)
	doc.file_size = row.file_size
	doc.content_hash = row.content_hash
	doc.folder = row.folder
	doc.attached_to_doctype = "Lead"
	doc.attached_to_name = name
	doc.attached_to_field = None
	doc.flags.ignore_duplicate_entry_error = True
	doc.insert(ignore_permissions=True)
	return doc.name


# ── custom_created_from_scan -> Lead.utm_source ──────────────────────────────────────────


def ensure_scan_source():
	"""The UTM Source / Lead Source record that a scanned card is attributed to."""
	source_doctype = get_source_doctype()
	if frappe.db.exists(source_doctype, SCAN_SOURCE_NAME):
		return SCAN_SOURCE_NAME

	doc = frappe.new_doc(source_doctype)
	doc.name = SCAN_SOURCE_NAME
	if doc.meta.has_field("source_name"):
		doc.source_name = SCAN_SOURCE_NAME
	if doc.meta.has_field("description"):
		doc.description = "Lead created by scanning a business card."
	doc.insert(ignore_permissions=True)
	return SCAN_SOURCE_NAME


def _migrate_created_from_scan(name, legacy):
	"""Set the source ONLY when the Lead has none.

	``custom_created_from_scan`` records how the row was captured; ``utm_source`` records who
	the lead came from. They are not the same fact, and on a client who has been running
	campaigns the second one is worth real money. Overwriting a real attribution with
	"Business Card Scan" would quietly rewrite their marketing reports, so an already-attributed
	Lead keeps its source and the scan flag is preserved in the archive instead.
	"""
	if not cint(legacy.get("custom_created_from_scan")):
		return {"outcome": "empty"}

	return set_scan_source_if_empty(name, current=legacy.get(get_lead_source_fieldname()))


def set_scan_source_if_empty(name, current=None):
	"""Attribute a scanned Lead to "Business Card Scan", but only if nothing else claims it.

	Public for the same reason as ``write_scanned_note``: after the drop, a protocol-1 payload
	with ``created_from_scan`` has to reach ``utm_source`` by this exact rule, or the live
	endpoint would start overwriting attributions the migration was careful not to touch.
	"""
	fieldname = get_lead_source_fieldname()
	if current is None:
		current = frappe.db.get_value("Lead", name, fieldname)
	if cstr(current).strip():
		return {"outcome": "kept", "existing": cstr(current).strip()}

	ensure_scan_source()
	frappe.db.set_value("Lead", name, fieldname, SCAN_SOURCE_NAME, update_modified=False)
	return {"outcome": "migrated", "source": SCAN_SOURCE_NAME}


# ── custom_business_id -> Connector Lead Link.business_id ───────────────────────────────────────


def _migrate_business_id(name, legacy):
	"""Create the identity row now, with vc_uuid left NULL.

	The Lead has a business but no central UUID yet: it was pushed by the old transport, which
	had no idea what a vc_uuid was. Writing the row now means the business survives the drop
	of ``custom_business_id``, and the first protocol-1 push adopts this row and fills the
	UUID in (see ``api.v1.sync._claim_link_row``) instead of tripping over it.
	"""
	business_id = cstr(legacy.get("custom_business_id")).strip()
	if not business_id:
		return {"outcome": "empty"}

	existing = frappe.db.get_value("Connector Lead Link", {"lead": name}, ["name", "business_id"], as_dict=True)
	if existing:
		if not cstr(existing.business_id).strip():
			frappe.db.set_value("Connector Lead Link", existing.name, "business_id", business_id)
		return {"outcome": "already", "link": existing.name, "business_id": business_id}

	link = frappe.new_doc("Connector Lead Link")
	link.lead = name
	link.business_id = business_id
	link.vc_uuid = None
	link.flags.ignore_business_mismatch = True
	link.insert(ignore_permissions=True)
	return {"outcome": "migrated", "link": link.name, "business_id": business_id}


# ── bookkeeping ──────────────────────────────────────────────────────────────────────────


def _record_mark(name, status_value, notes):
	"""The per-record mark that makes a re-run skip this Lead.

	It is a row of the archive DocType, not a Custom Field on Lead: this migration exists to
	REMOVE Custom Fields from a DocType ERPNext owns, and adding a tenth one to keep track of
	removing nine would be a joke at the client's expense. Patch B fills ``legacy_values`` on
	this same row before dropping anything.
	"""
	row = get_archive_row("Lead", name)
	if row:
		values = {"migration_status": status_value, "migration_notes": json.dumps(notes, default=str)}

		if row.migration_status == archive.ORPHAN_STATUS:
			# The row belongs to a Lead that was DELETED, and this name is in use again:
			# frappe's `revert_series_if_last` hands the next Lead the name of the one just
			# deleted. This is a different record, so its archive must not start life holding
			# the deleted record's closed-out stamp -- Patch B would count it as archived and
			# the new Lead's values would never be captured.
			values["archived_on"] = None
			values["legacy_values"] = None
			values["migrated_on"] = now_datetime()

		frappe.db.set_value("Connector Legacy Field Archive", row.name, values)
		return row.name

	doc = frappe.new_doc("Connector Legacy Field Archive")
	doc.reference_doctype = "Lead"
	doc.reference_name = name
	doc.lookup_key = make_lookup_key("Lead", name)
	doc.migration_status = status_value
	doc.migrated_on = now_datetime()
	doc.migration_notes = json.dumps(notes, default=str)
	doc.insert(ignore_permissions=True)
	return doc.name


def _log_once(lead, error_code, message):
	"""A Connector Log row, written at most once per (Lead, code).

	Without the "once", a re-run would add one more row for every conflict and every missing
	file on every pass, and the log a client is supposed to read to find out what happened
	would be the least readable thing on the site.
	"""
	request_id = f"legacy-migration:{lead}"
	if frappe.db.exists("Connector Log", {"request_id": request_id, "error_code": error_code}):
		return

	doc = frappe.new_doc("Connector Log")
	doc.request_id = request_id
	doc.outcome = "Skipped"
	doc.error_code = error_code
	doc.message = message
	doc.insert(ignore_permissions=True)
