# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""The gate Patch B has to get through before it drops anything.

One rule: for every legacy value still on a Lead, the standard field it was supposed to land
on must actually hold it. The check reads the STANDARD side -- the Contact, the Address, the
note, the File, the source, the link row -- not the migration's own bookkeeping, because a
migration that recorded success and wrote nothing is precisely the failure this exists to
catch.

A non-empty result is not an error to shrug at. It means the drop would destroy data that has
no copy anywhere else, so Patch B logs it, re-enqueues Patch A and returns without touching a
single field.

The one concession: a card image whose File row had already been deleted before the migration
ran can never be re-parented. That is recorded as ``missing`` on the mark row and accepted
here, otherwise one broken URL on one Lead would block the drop for the whole site for ever.
The URL itself still survives in the archive.
"""

import json

import frappe
from frappe.utils import cint, cstr

from ai_snaplead_connector.ai_snaplead_connector.doctype.connector_legacy_field_archive.connector_legacy_field_archive import (
	get_row as get_archive_row,
)
from ai_snaplead_connector.legacy.migrate import (
	_linked_document,
	_linked_documents,
	read_legacy_lead,
	storable_emails,
	unstorable_email_remainder,
)
from ai_snaplead_connector.legacy.schema import (
	LEGACY_LEAD_DATA_FIELDS,
	SCANNED_TEXT_TAG,
	UNSTORABLE_CONTACT_TAG,
	existing_lead_fields,
	get_lead_source_fieldname,
	lead_has_legacy_fields,
)
from ai_snaplead_connector.utils.phone import (
	normalize_email,
	normalize_mobile_for_auth,
	phone_match_key,
	storable_phones,
	unstorable_remainder,
)

MAX_REPORTED = 50

# "the column holds something". NOT a `not in ("", None)` filter: SQL's `x NOT IN ('', NULL)`
# evaluates to NULL for every row, so that filter quietly matches NOTHING. It skipped a record
# whose only legacy value sat on a column that was NULL elsewhere -- and a record this query
# never finds is a record that is never archived and never verified, which is the one class of
# bug the whole design exists to prevent.
NON_EMPTY = ("is", "set")


def run(limit=MAX_REPORTED):
	"""Every Lead whose legacy values have not fully landed on standard fields.

	Returns a list of ``{"lead": ..., "reasons": [...]}``. Empty means Patch B may proceed.
	"""
	if not lead_has_legacy_fields():
		return []

	failures = []

	for name in _leads_with_legacy_values():
		reasons = verify_lead(name)
		if not reasons:
			continue

		failures.append({"lead": name, "reasons": reasons})
		if len(failures) >= limit:
			break

	return failures


def count():
	"""How many Leads are unverified, without capping at the reporting limit."""
	total = 0
	if not lead_has_legacy_fields():
		return 0

	for name in _leads_with_legacy_values():
		if verify_lead(name):
			total += 1

	return total


def reset_failed_marks():
	"""Downgrade the mark row of every Lead that does not verify, and return how many.

	Without this, Patch B's "log it and re-enqueue Patch A" is a promise it cannot keep.
	Patch A skips any Lead whose mark row says Migrated -- that skip is what makes a re-scan
	of a 200k-Lead client affordable -- so a Lead that WAS migrated and whose Contact, note
	or attachment was deleted afterwards would be skipped on every re-run, and the client
	would sit at "verification found 1 Lead" for ever with the re-enqueue doing nothing at
	all. Marking it Failed puts it back in scope for the next pass.

	Only the Leads that actually fail are touched. The other 199,999 keep their marks.
	"""
	reset = 0

	for name in _leads_with_legacy_values():
		if not verify_lead(name):
			continue

		row = get_archive_row("Lead", name)
		if row and row.migration_status != "Failed":
			frappe.db.set_value("Connector Legacy Field Archive", row.name, "migration_status", "Failed")
			reset += 1

	return reset


def _leads_with_legacy_values():
	"""Names of every Lead carrying at least one legacy value, scalar or child row.

	Built as a union of cheap per-field queries rather than one OR-ed WHERE, so it works the
	same on SQLite, MariaDB and PostgreSQL and uses whatever index each column has.
	"""
	names = set()

	scalar_fields = [
		fieldname
		for fieldname in existing_lead_fields(LEGACY_LEAD_DATA_FIELDS)
		if frappe.get_meta("Lead").get_field(fieldname).fieldtype != "Table"
	]
	for fieldname in scalar_fields:
		filters = {fieldname: NON_EMPTY}
		if frappe.get_meta("Lead").get_field(fieldname).fieldtype == "Check":
			filters = {fieldname: 1}
		names.update(frappe.get_all("Lead", filters=filters, pluck="name"))

	for child_doctype in ("ERPNext Lead Email", "ERPNext Lead Mobile Number", "ERPNext Lead Activity Log"):
		if not frappe.db.table_exists(child_doctype):
			continue
		names.update(
			frappe.get_all(child_doctype, filters={"parenttype": "Lead"}, pluck="parent", distinct=True)
		)

	return sorted(n for n in names if n and frappe.db.exists("Lead", n))


def verify_lead(name):
	"""Why this Lead is not safe to drop yet. Empty list means it is."""
	legacy = read_legacy_lead(name)
	reasons = []
	mark = get_archive_row("Lead", name)
	notes = {}
	if mark and mark.migration_notes:
		try:
			notes = json.loads(mark.migration_notes) or {}
		except ValueError:
			notes = {}

	if not mark:
		reasons.append("no Connector Legacy Field Archive mark row: Patch A has not processed this Lead")
	elif mark.migration_status != "Migrated":
		reasons.append(f"mark row status is {mark.migration_status or 'empty'}, not Migrated")

	reasons += _verify_contacts(name, legacy)
	reasons += _verify_pincode(name, legacy)
	reasons += _verify_scanned_text(name, legacy)
	reasons += _verify_card_images(name, legacy, notes)
	reasons += _verify_created_from_scan(name, legacy)
	reasons += _verify_business_id(name, legacy)

	return reasons


def _contact_rows(name):
	contact = _linked_document("Contact", name)
	if not contact:
		return None, set(), set()

	emails = {
		normalize_email(value)
		for value in frappe.get_all(
			"Contact Email", filters={"parent": contact, "parenttype": "Contact"}, pluck="email_id"
		)
		if normalize_email(value)
	}
	phones = set()
	for value in frappe.get_all(
		"Contact Phone", filters={"parent": contact, "parenttype": "Contact"}, pluck="phone"
	):
		stored = normalize_mobile_for_auth(value)
		if stored:
			phones.add(phone_match_key(stored) or stored)

	return contact, emails, phones


def _verify_contacts(name, legacy):
	"""Every legacy email and phone has to be readable on this Lead after the drop.

	"Readable" has two shapes, and the gate checks both, because Patch A has two. A value
	ERPNext can store must be on the Contact. A value it CANNOT store -- "080-25551234 Ext 22",
	an address that is not a valid email -- must be on a tagged CRM Note, written as printed.
	Asking only the first question is how a gate passes while an annotation the client scanned
	exists nowhere but the archive.

	The storable/unstorable split is decided by the same two functions Patch A writes with. If
	the two sides ever answered differently, one of them would be wrong for ever: a gate that
	demands what the migration cannot write refuses the drop on every run; a gate that ignores
	what the migration skipped lets the drop destroy it.
	"""
	wanted_emails = set()
	wanted_notes = []

	for row in legacy.get("custom_email_ids") or []:
		raw = cstr(row.get("email_id")).strip()
		for email in storable_emails(raw):
			wanted_emails.add(email)

		remainder = unstorable_email_remainder(raw)
		if remainder:
			wanted_notes.append(remainder)

	wanted_phones = set()
	for row in legacy.get("custom_mobile_numbers") or []:
		raw = cstr(row.get("mobile_number")).strip()
		for stored in storable_phones(raw):
			wanted_phones.add(phone_match_key(stored) or stored)

		remainder = unstorable_remainder(raw)
		if remainder:
			wanted_notes.append(remainder)

	reasons = _verify_unstorable_notes(name, wanted_notes)

	if not wanted_emails and not wanted_phones:
		return reasons

	contact, held_emails, held_phones = _contact_rows(name)
	if not contact:
		return [*reasons, "custom_email_ids/custom_mobile_numbers are set but no Contact is linked to this Lead"]

	missing_emails = sorted(wanted_emails - held_emails)
	if missing_emails:
		reasons.append(f"Contact {contact} is missing email_ids: {', '.join(missing_emails)}")

	missing_phones = sorted(wanted_phones - held_phones)
	if missing_phones:
		reasons.append(f"Contact {contact} is missing phone_nos: {', '.join(missing_phones)}")

	return reasons


def _verify_unstorable_notes(name, values):
	"""Each value ERPNext refuses to store must be on a tagged note, exactly as printed."""
	if not values:
		return []

	if not frappe.get_meta("Lead").has_field("notes"):
		# Nowhere to put it on this ERPNext, and Patch A logged that. Refusing the drop for
		# ever would not create the notes table.
		return []

	notes = frappe.get_all(
		"CRM Note",
		filters={
			"parent": name,
			"parenttype": "Lead",
			"parentfield": "notes",
			"note": ("like", f"%{UNSTORABLE_CONTACT_TAG}%"),
		},
		pluck="note",
	)
	blob = "\n".join(notes)

	return [
		f"{value!r} cannot be stored on a Contact and is on no tagged CRM Note on this Lead"
		for value in dict.fromkeys(values)
		if frappe.utils.escape_html(value) not in blob
	]


def _verify_pincode(name, legacy):
	pincode = cstr(legacy.get("custom_pincode")).strip()
	if not pincode:
		return []

	for address in _linked_documents("Address", name):
		if cstr(frappe.db.get_value("Address", address, "pincode")).strip() == pincode:
			return []

	return [f"custom_pincode {pincode} is on no Address linked to this Lead"]


def _verify_scanned_text(name, legacy):
	if not cstr(legacy.get("custom_scanned_text")).strip():
		return []

	if not frappe.get_meta("Lead").has_field("notes"):
		return ["custom_scanned_text is set but this ERPNext has no Lead.notes table"]

	tagged = frappe.get_all(
		"CRM Note",
		filters={
			"parent": name,
			"parenttype": "Lead",
			"parentfield": "notes",
			"note": ("like", f"%{SCANNED_TEXT_TAG}%"),
		},
		limit=1,
		pluck="name",
	)
	if tagged:
		return []

	return ["custom_scanned_text is set but no tagged CRM Note exists on this Lead"]


def _verify_card_images(name, legacy, notes):
	reasons = []

	for fieldname in ("custom_front_card_image", "custom_back_card_image"):
		url = cstr(legacy.get(fieldname)).strip()
		if not url:
			continue

		if (notes.get(fieldname) or {}).get("outcome") == "missing":
			# Recorded as unrecoverable by Patch A and logged to Connector Log. The URL
			# still reaches the archive, so the drop loses nothing that was not already lost.
			continue

		attached = frappe.db.exists(
			"File",
			{
				"file_url": url,
				"attached_to_doctype": "Lead",
				"attached_to_name": name,
				"attached_to_field": ("in", ("", None)),
			},
		)
		if not attached:
			reasons.append(f"{fieldname} ({url}) is not a re-parented File attachment on this Lead")

	return reasons


def _verify_created_from_scan(name, legacy):
	if not cint(legacy.get("custom_created_from_scan")):
		return []

	fieldname = get_lead_source_fieldname()
	if cstr(legacy.get(fieldname)).strip():
		return []

	return [f"custom_created_from_scan is set but Lead.{fieldname} is still empty"]


def _verify_business_id(name, legacy):
	business_id = cstr(legacy.get("custom_business_id")).strip()
	if not business_id:
		return []

	held = cstr(frappe.db.get_value("Connector Lead Link", {"lead": name}, "business_id")).strip()
	if held == business_id:
		return []

	return [f"custom_business_id {business_id} is on no Connector Lead Link row for this Lead (found {held or 'none'})"]
