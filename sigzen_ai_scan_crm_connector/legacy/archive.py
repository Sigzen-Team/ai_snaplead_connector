# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Dump every legacy value into Connector Legacy Field Archive, before anything is dropped.

The archive is the undo button, and it lives on the client's own site -- in a DocType they can
open, filter and export -- rather than in a backup file somebody has to ask a support engineer
for. ``sigzen_ai_scan_crm_connector.legacy.restore`` reads nothing else.

Two rules make it trustworthy:

* it is written BEFORE the drop, and Patch B verifies the row count it expects to have
  written before it deletes a single Custom Field. An archive written afterwards would be an
  archive of nothing;
* it captures the raw column values and the raw child rows, not a summary. The restore has to
  be able to put back what was there, including ``custom_lead_activity_log``, which has no
  standard ERPNext counterpart and exists nowhere else once the field is gone.
"""

import json

import frappe
from frappe.utils import now_datetime

from sigzen_ai_scan_crm_connector.legacy.schema import (
	LEGACY_LEAD_DATA_FIELDS,
	LEGACY_LEAD_TABLE_FIELDS,
	LEGACY_SOURCE_DATA_FIELDS,
	LEGACY_SOURCE_TABLE_FIELDS,
	existing_lead_fields,
	get_source_doctype,
)
from sigzen_ai_scan_crm_connector.sigzen___ai_scan_crm_connector.doctype.connector_legacy_field_archive.connector_legacy_field_archive import (
	get_row,
	make_lookup_key,
)

ARCHIVE_VERSION = 1

# "the column holds something". NOT a `not in ("", None)` filter: SQL's `x NOT IN ('', NULL)`
# evaluates to NULL for every row, so that filter quietly matches NOTHING. It skipped a record
# whose only legacy value sat on a column that was NULL elsewhere -- and a record this query
# never finds is a record that is never archived and never verified, which is the one class of
# bug the whole design exists to prevent.
NON_EMPTY = ("is", "set")


def write_all():
	"""Archive every Lead and every Lead Source / UTM Source that holds a legacy value.

	Returns ``{"leads": n, "sources": n, "orphans": [...]}`` -- the number of records whose
	``legacy_values`` is now filled in, and the rows closed out because their record is gone.
	Patch B asserts on these counts before it drops anything.
	"""
	orphans = stamp_orphans()

	leads = 0
	for name in _lead_names():
		if write_lead(name):
			leads += 1

	sources = 0
	for name in _source_names():
		if write_source(name):
			sources += 1

	frappe.db.commit()  # nosemgrep - one-time legacy migration, run as a patch or by an operator; each stage must be durable before the next destructive one
	return {"leads": leads, "sources": sources, "orphans": orphans}


ORPHAN_STATUS = "Orphaned"


def stamp_orphans():
	"""Close out every archive row whose record no longer exists. Returns their names.

	Patch A writes a mark row per migrated Lead with ``archived_on`` empty -- it is a mark,
	not an archive, and Patch B fills the values in later. If that Lead is then DELETED, which
	is an ordinary thing for a client to do, nothing can ever fill it in: this module only
	archives records that still exist, and Patch B refuses to drop while any row has no
	``archived_on``. One deleted Lead therefore blocked the drop permanently, with no reset,
	no self-healing path and nothing in the report naming the row.

	The row is kept rather than deleted -- it records that this app migrated a Lead that is now
	gone, which is exactly the kind of question an archive exists to answer -- and marked
	Orphaned with ``archived_on`` set, because there is nothing left to capture. ``restore``
	only reads rows with ``legacy_values``, so an Orphaned row restores nothing.
	"""
	closed = []

	for row in frappe.get_all(
		"Connector Legacy Field Archive",
		filters={"archived_on": ("is", "not set")},
		fields=["name", "reference_doctype", "reference_name", "migration_notes"],
	):
		if _record_exists(row.reference_doctype, row.reference_name):
			continue

		try:
			notes = json.loads(row.migration_notes) or {}
		except (TypeError, ValueError):
			notes = {}
		if not isinstance(notes, dict):
			notes = {"migration_notes": notes}

		notes["orphaned"] = {
			"on": str(now_datetime()),
			"reason": f"{row.reference_doctype} {row.reference_name} no longer exists;"
			" there are no legacy values left to archive.",
		}

		frappe.db.set_value(
			"Connector Legacy Field Archive",
			row.name,
			{
				"migration_status": ORPHAN_STATUS,
				"archived_on": now_datetime(),
				"migration_notes": json.dumps(notes, default=str),
			},
		)
		closed.append(row.name)

	return closed


def _record_exists(doctype, name):
	doctype = (doctype or "").strip()
	name = (name or "").strip()
	if not doctype or not name:
		return False
	if not frappe.db.exists("DocType", doctype):
		return False

	return bool(frappe.db.exists(doctype, name))


def _lead_names():
	"""Every Lead that has a legacy value OR an archive mark row from Patch A.

	The mark rows are included so that a Lead whose values Patch A has already MOVED (it does
	not delete them, but a client could have) is still archived, and so the archive and the
	mark set never diverge.
	"""
	names = set(
		frappe.get_all(
			"Connector Legacy Field Archive", filters={"reference_doctype": "Lead"}, pluck="reference_name"
		)
	)

	meta = frappe.get_meta("Lead")
	for fieldname in existing_lead_fields(LEGACY_LEAD_DATA_FIELDS):
		field = meta.get_field(fieldname)
		if field.fieldtype == "Table":
			if frappe.db.table_exists(field.options):
				names.update(
					frappe.get_all(
						field.options, filters={"parenttype": "Lead"}, pluck="parent", distinct=True
					)
				)
			continue
		if field.fieldtype == "Check":
			names.update(frappe.get_all("Lead", filters={fieldname: 1}, pluck="name"))
			continue
		names.update(frappe.get_all("Lead", filters={fieldname: NON_EMPTY}, pluck="name"))

	return sorted(n for n in names if n and frappe.db.exists("Lead", n))


def _source_names():
	source_doctype = get_source_doctype()
	if not frappe.db.table_exists(source_doctype):
		return []

	meta = frappe.get_meta(source_doctype)
	names = set()

	for fieldname in LEGACY_SOURCE_DATA_FIELDS:
		if not meta.has_field(fieldname):
			continue
		field = meta.get_field(fieldname)
		if field.fieldtype == "Table":
			if frappe.db.table_exists(field.options):
				names.update(
					frappe.get_all(
						field.options, filters={"parenttype": source_doctype}, pluck="parent", distinct=True
					)
				)
			continue
		if field.fieldtype == "Check":
			names.update(frappe.get_all(source_doctype, filters={fieldname: 1}, pluck="name"))
			continue
		names.update(frappe.get_all(source_doctype, filters={fieldname: NON_EMPTY}, pluck="name"))

	return sorted(n for n in names if n and frappe.db.exists(source_doctype, n))


def write_lead(name):
	return _write("Lead", name, LEGACY_LEAD_DATA_FIELDS, LEGACY_LEAD_TABLE_FIELDS)


def write_source(name):
	return _write(get_source_doctype(), name, LEGACY_SOURCE_DATA_FIELDS, LEGACY_SOURCE_TABLE_FIELDS)


def _write(doctype, name, data_fields, table_fields):
	payload = capture(doctype, name, data_fields, table_fields)
	row = get_row(doctype, name)
	values = {"legacy_values": json.dumps(payload, default=str, indent=1), "archived_on": now_datetime()}

	if row:
		frappe.db.set_value("Connector Legacy Field Archive", row.name, values)
		return True

	doc = frappe.new_doc("Connector Legacy Field Archive")
	doc.reference_doctype = doctype
	doc.reference_name = name
	doc.lookup_key = make_lookup_key(doctype, name)
	doc.update(values)
	doc.insert(ignore_permissions=True)
	return True


def capture(doctype, name, data_fields, table_fields):
	"""The raw legacy state of one record, as plain JSON-able data."""
	meta = frappe.get_meta(doctype)
	scalar_fields = [
		fieldname
		for fieldname in data_fields
		if meta.has_field(fieldname) and meta.get_field(fieldname).fieldtype != "Table"
	]

	payload = {
		"archive_version": ARCHIVE_VERSION,
		"doctype": doctype,
		"name": name,
		"captured_on": str(now_datetime()),
		"fields": {},
		"tables": {},
	}

	if scalar_fields:
		values = frappe.db.get_value(doctype, name, scalar_fields, as_dict=True) or {}
		payload["fields"] = {key: values.get(key) for key in scalar_fields}

	for fieldname, (child_doctype, _columns) in table_fields.items():
		if not meta.has_field(fieldname) or not frappe.db.table_exists(child_doctype):
			continue
		payload["tables"][fieldname] = frappe.get_all(
			child_doctype,
			filters={"parent": name, "parenttype": doctype, "parentfield": fieldname},
			fields=["*"],
			order_by="idx asc",
		)

	return payload
