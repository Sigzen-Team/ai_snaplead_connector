# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""The undo button for Patch B.

	bench --site <site> execute sigzen_ai_scan_crm_connector.legacy.restore.restore
	bench --site <site> execute sigzen_ai_scan_crm_connector.legacy.restore.restore --kwargs "{'dry_run': True}"

Re-creates the Custom Fields exactly as ``legacy.definitions`` declares them and writes every
archived value back onto the record it came from. Nothing else on the site is touched: the
Contacts, Addresses, notes, attachments, sources and link rows that Patch A created are left
alone, because they are correct -- the restore exists to give a client back the OLD view of
their data, not to undo the migration of it.

Read this before running it:

* ``restore`` is additive. A field that a client has since re-created by hand keeps its
  definition; a value that is already set is not overwritten unless ``force=True``.
* it restores child rows only where none exist, for the same reason.
* ``install.py`` stops re-creating the legacy fields once ``legacy_fields_dropped`` is set, so
  the restore clears that flag. The next ``bench migrate`` will then keep the fields alive
  rather than quietly dropping them again -- but Patch B has already run and will not run a
  second time, so it is the flag, not the patch, that has to be cleared.
"""

import json

import frappe
from frappe.utils import cint

from sigzen_ai_scan_crm_connector.legacy import state
from sigzen_ai_scan_crm_connector.legacy.definitions import lead_legacy_fields, source_legacy_fields
from sigzen_ai_scan_crm_connector.legacy.schema import (
	LEGACY_LEAD_TABLE_FIELDS,
	LEGACY_SOURCE_TABLE_FIELDS,
	get_lead_source_fieldname,
	get_source_doctype,
)


def restore(dry_run=False, force=False):
	"""Put the Custom Fields and their values back. Returns a report dict."""
	dry_run = cint(dry_run)
	force = cint(force)

	report = {
		"dry_run": bool(dry_run),
		"archive_rows": frappe.db.count("Connector Legacy Field Archive"),
		"fields_recreated": {},
		"records_restored": 0,
		"records_already_intact": 0,
		"values_already_present": 0,
		"rows_restored": 0,
		"skipped": [],
		"note": None,
	}

	if not report["archive_rows"]:
		report["skipped"].append("Connector Legacy Field Archive is empty: there is nothing to restore.")
		return report

	if not dry_run:
		report["fields_recreated"] = recreate_custom_fields()
	else:
		report["fields_recreated"] = _missing_custom_fields()

	for row in frappe.get_all(
		"Connector Legacy Field Archive",
		filters={"legacy_values": ("is", "set")},
		fields=["name", "reference_doctype", "reference_name", "legacy_values"],
		order_by="reference_doctype asc, reference_name asc",
	):
		try:
			payload = json.loads(row.legacy_values)
		except (TypeError, ValueError):
			report["skipped"].append(f"{row.reference_doctype} {row.reference_name}: unreadable legacy_values")
			continue

		if not frappe.db.exists(row.reference_doctype, row.reference_name):
			report["skipped"].append(f"{row.reference_doctype} {row.reference_name}: record no longer exists")
			continue

		restored, rows, already_present = _restore_record(
			row.reference_doctype, row.reference_name, payload, dry_run, force
		)
		if restored:
			report["records_restored"] += 1
		elif already_present:
			report["records_already_intact"] += 1
		report["rows_restored"] += rows
		report["values_already_present"] += already_present

	if not dry_run:
		state.set_values(legacy_fields_dropped=0, legacy_fields_dropped_on=None)
		frappe.db.commit()  # nosemgrep - one-time legacy migration, run as a patch or by an operator; each stage must be durable before the next destructive one
		frappe.clear_cache()

	report["note"] = _explain(report)
	return report


def _explain(report):
	"""Say in words what the counters mean, because zero has two very different causes.

	``records_restored: 0`` reads like a failed undo, and on a site that has only had Patch B
	run it is the CORRECT and expected result: deleting a Custom Field in Frappe removes the
	field definition, not the column, so every value is still sitting in ``tabLead`` and the
	restore -- which never overwrites a value that is already there -- has nothing to write.
	An operator who cannot tell that apart from "the archive was empty" will assume their undo
	button is broken. So the report says which one happened.
	"""
	if not report["archive_rows"]:
		return "The archive is empty; there was nothing to restore."

	if report["records_restored"]:
		return (
			f"Restored {report['records_restored']} record(s) and {report['rows_restored']} child row(s)"
			" from the archive."
		)

	if report["records_already_intact"]:
		return (
			f"Nothing needed writing: all {report['values_already_present']} archived value(s) across"
			f" {report['records_already_intact']} record(s) were still present on the records. Deleting a"
			" Custom Field removes the field, not the column, so the values survive Patch B until"
			" `bench trim-tables` is run. The Custom Fields have been re-created, so the values are"
			" visible again. Pass force=True to overwrite from the archive anyway."
		)

	return "The archive held no restorable values for any surviving record."


def recreate_custom_fields():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	source_doctype = get_source_doctype()
	missing = _missing_custom_fields()

	fields = {
		"Lead": [f for f in lead_legacy_fields(get_lead_source_fieldname()) if f["fieldname"] in missing.get("Lead", [])],
		source_doctype: [
			f
			for f in source_legacy_fields(source_doctype == "UTM Source")
			if f["fieldname"] in missing.get(source_doctype, [])
		],
	}
	fields = {doctype: definitions for doctype, definitions in fields.items() if definitions}

	if fields:
		create_custom_fields(fields, update=True)
		frappe.clear_cache()

	return missing


def _missing_custom_fields():
	source_doctype = get_source_doctype()
	plan = {
		"Lead": [f["fieldname"] for f in lead_legacy_fields(get_lead_source_fieldname())],
		source_doctype: [f["fieldname"] for f in source_legacy_fields(source_doctype == "UTM Source")],
	}

	missing = {}
	for doctype, fieldnames in plan.items():
		meta = frappe.get_meta(doctype)
		absent = [fieldname for fieldname in fieldnames if not meta.has_field(fieldname)]
		if absent:
			missing[doctype] = absent

	return missing


def _restore_record(doctype, name, payload, dry_run, force):
	meta = frappe.get_meta(doctype)
	table_fields = LEGACY_LEAD_TABLE_FIELDS if doctype == "Lead" else LEGACY_SOURCE_TABLE_FIELDS

	updates = {}
	already_present = 0
	for fieldname, value in (payload.get("fields") or {}).items():
		if not meta.has_field(fieldname):
			continue

		field = meta.get_field(fieldname)
		if _is_empty(value, field):
			continue
		if not force and not _is_empty(frappe.db.get_value(doctype, name, fieldname), field):
			# The archived value is still on the record. Counted, not written, and not an error:
			# it is what a restore after a plain Patch B looks like.
			already_present += 1
			continue

		updates[fieldname] = value

	rows_written = 0
	for fieldname, rows in (payload.get("tables") or {}).items():
		if not rows or not meta.has_field(fieldname):
			continue
		child_doctype = (table_fields.get(fieldname) or (None,))[0] or meta.get_field(fieldname).options
		if not child_doctype or not frappe.db.table_exists(child_doctype):
			continue
		if frappe.db.exists(child_doctype, {"parent": name, "parenttype": doctype, "parentfield": fieldname}):
			already_present += len(rows)
			continue
		if dry_run:
			rows_written += len(rows)
			continue
		rows_written += _reinsert_rows(doctype, name, fieldname, child_doctype, rows)

	if updates and not dry_run:
		frappe.db.set_value(doctype, name, updates, update_modified=False)

	return bool(updates or rows_written), rows_written, already_present


def _is_empty(value, field):
	"""Is this value "nothing"? For a Check, 0 is nothing.

	Without the Check case, an unticked box reads as a value already present and the restore
	refuses to overwrite it -- so ``custom_created_from_scan`` came back as 0 on every Lead
	whose column had actually been cleared, silently losing the one bit the field carries.
	"""
	if field is not None and field.fieldtype == "Check":
		return not cint(value)

	return value in (None, "")


def _reinsert_rows(parent_doctype, parent_name, parentfield, child_doctype, rows):
	written = 0
	child_meta = frappe.get_meta(child_doctype)

	for index, row in enumerate(rows, start=1):
		doc = frappe.new_doc(child_doctype)
		for field in child_meta.fields:
			if field.fieldname in row and row[field.fieldname] not in (None, ""):
				doc.set(field.fieldname, row[field.fieldname])
		doc.parent = parent_name
		doc.parenttype = parent_doctype
		doc.parentfield = parentfield
		doc.idx = cint(row.get("idx")) or index
		doc.insert(ignore_permissions=True)
		written += 1

	return written
