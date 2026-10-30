# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""PATCH B -- verify, archive, then drop.

The order is the whole design, and none of the three steps may be skipped or reordered:

1. **Refuse** unless Patch A reports Complete AND the verification queries return zero. A
   non-zero result means some legacy value has no copy on a standard field, so dropping would
   destroy it. Patch B then logs what it found, re-enqueues Patch A and returns having
   touched nothing.
2. **Archive** every legacy value into ``Connector Legacy Field Archive``, and check the row count
   it expected to write. This is the undo button, and it must exist before the thing it
   undoes. Rows whose record has since been DELETED are closed out as Orphaned first: they
   have nothing left to archive, and before that they blocked this step for ever.
3. **Drop** the nine Custom Fields and four layout fields on Lead, the seven and three on
   Lead Source / UTM Source, and the Property Setters that hid stock ``Lead.email_id`` and
   ``Lead.mobile_no``.

``custom_address`` is not dropped. It is the raw scanned address block and has no standard
ERPNext home, so it is the one Custom Field this app leaves on a client's Lead.

What "drop" means here, precisely: Frappe deletes the Custom Field DEFINITION, not the column
underneath it. After this runs, ``custom_pincode`` is gone from the Lead form, from
``frappe.get_meta("Lead")`` and from every query this app makes -- but the column and its
values are still in ``tabLead`` until somebody runs ``bench trim-tables``. That is a safety
margin, not an oversight, and it is why a restore run straight after Patch B reports zero
values written: there was nothing missing to write. Do not read that zero as a broken undo;
``restore`` spells the difference out in its ``note``.

What is deliberately NOT deleted: the rows in ``ERPNext Lead Email``, ``ERPNext Lead Mobile
Number`` and ``ERPNext Lead Activity Log``. They are unreachable once the parent field is
gone, and they are in the archive, but deleting them would make the restore script depend on
the archive alone. They cost a few MB and they are the second copy. Everything that READS
them is switched off by ``schema.lead_has_contact_tables()`` on the same migrate, so a stale
child row can no longer be matched against a new Lead.
"""

import json

import frappe
from frappe.utils import now_datetime

from sigzenlead_connector.legacy import archive, state, verify
from sigzenlead_connector.legacy.schema import (
	HIDDEN_STOCK_FIELDS,
	HIDDEN_STOCK_PROPERTIES,
	KEPT_LEAD_FIELD,
	LEGACY_LEAD_DATA_FIELDS,
	LEGACY_LEAD_LAYOUT_FIELDS,
	LEGACY_SOURCE_DATA_FIELDS,
	LEGACY_SOURCE_LAYOUT_FIELDS,
	get_source_doctype,
)


class DropRefused(Exception):
	"""Raised only by ``run(strict=True)``; the patch logs and returns instead."""


def run(strict=False):
	"""Verify, archive and drop. Returns a report dict; never raises unless ``strict``.

	A patch that raises aborts ``bench migrate`` for the whole site. This one has exactly one
	legitimate reason to stop -- the client's data is not ready -- and that is not a reason to
	break their migrate, so it returns a refusal the client can read in Connector Log.
	"""
	report = {"dropped": False, "reason": None, "failures": [], "archived": {}, "fields_dropped": {}}

	if state.fields_are_dropped():
		report["reason"] = "already dropped"
		return report

	refusal = _refusal_reason()
	if refusal:
		report["reason"] = refusal["reason"]
		report["failures"] = refusal["failures"]
		_log_refusal(refusal)
		# Put the failing Leads back in scope BEFORE re-enqueueing. Patch A skips a Lead whose
		# mark row says Migrated, so without this the re-enqueue would re-scan the whole table
		# and change nothing -- and the client would never get past this refusal.
		report["marks_reset"] = verify.reset_failed_marks()
		frappe.db.commit()  # nosemgrep - one-time legacy migration, run as a patch or by an operator; each stage must be durable before the next destructive one

		# Re-enqueue Patch A from the top. A gap means some Lead was never migrated or was
		# migrated before its Contact existed, and only a full re-scan finds it -- the cursor
		# is at the end of the table by now.
		from sigzenlead_connector.legacy.migrate import enqueue

		enqueue(reset=True)
		if strict:
			raise DropRefused(refusal["reason"])
		return report

	# ── archive BEFORE any drop ──────────────────────────────────────────────────────────
	report["archived"] = archive.write_all()

	unarchived = frappe.get_all(
		"Connector Legacy Field Archive",
		filters={"archived_on": ("is", "not set")},
		fields=["name", "reference_doctype", "reference_name"],
		limit=20,
	)
	if unarchived:
		# The archive did not finish. Dropping now would leave those records with no copy of
		# their legacy values anywhere, so this stops short of the irreversible step.
		#
		# This branch used to be reachable by ordinary client behaviour and impossible to get
		# out of: deleting a Lead that Patch A had marked left a mark row nothing could ever
		# fill in, and this count looked at the WHOLE table. ``archive.stamp_orphans`` (run as
		# the first thing in ``write_all`` above) closes those out, so what is left here is a
		# record that still exists and whose archive write genuinely did not happen. The rows
		# are named, because "1 archive rows have no archived_on" told the operator nothing.
		report["reason"] = "{0} archive row(s) have no archived_on; refusing to drop: {1}".format(
			frappe.db.count("Connector Legacy Field Archive", {"archived_on": ("is", "not set")}),
			", ".join(f"{row.name} ({row.reference_doctype} {row.reference_name})" for row in unarchived),
		)
		report["unarchived"] = unarchived
		_log_refusal({"reason": report["reason"], "failures": unarchived})
		if strict:
			raise DropRefused(report["reason"])
		return report

	# ── only now, the irreversible step ──────────────────────────────────────────────────
	report["fields_dropped"] = drop_custom_fields()
	report["property_setters_removed"] = remove_legacy_property_setters()
	state.mark_dropped()
	frappe.db.commit()  # nosemgrep - one-time legacy migration, run as a patch or by an operator; each stage must be durable before the next destructive one
	frappe.clear_cache()

	report["dropped"] = True
	_log_success(report)
	return report


def _refusal_reason():
	if not state.migration_is_complete():
		return {
			"reason": "Patch A (legacy field migration) has not reported Complete; status is"
			f" {state.status() or 'not started'}.",
			"failures": [],
		}

	failures = verify.run()
	if failures:
		return {
			"reason": f"verification found {len(failures)} Lead(s) whose legacy values have not landed"
			" on standard fields.",
			"failures": failures,
		}

	return None


def drop_custom_fields():
	"""Delete the thirteen Lead fields and the ten source fields. Never ``custom_address``."""
	source_doctype = get_source_doctype()
	plan = {
		"Lead": [name for name in LEGACY_LEAD_DATA_FIELDS + LEGACY_LEAD_LAYOUT_FIELDS],
		source_doctype: [name for name in LEGACY_SOURCE_DATA_FIELDS + LEGACY_SOURCE_LAYOUT_FIELDS],
	}

	if KEPT_LEAD_FIELD in plan["Lead"]:
		# Belt and braces. If a future edit ever adds custom_address to the legacy list, this
		# app must fail loudly rather than quietly delete the client's scanned addresses.
		frappe.throw(f"{KEPT_LEAD_FIELD} must never be dropped.")

	dropped = {}
	for doctype, fieldnames in plan.items():
		removed = []
		for fieldname in fieldnames:
			name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname}, "name")
			if not name:
				continue
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
			removed.append(fieldname)
		dropped[doctype] = removed
		if removed:
			frappe.clear_cache(doctype=doctype)

	return dropped


def remove_legacy_property_setters():
	"""Remove the Property Setters that hid and read-only'd stock Lead.email_id / mobile_no.

	``patches.v1_0.restore_stock_lead_contact_fields`` already does this once. It is repeated
	here because a client can install an older build of this app between the two patches, and
	because leaving the stock contact fields hidden on a Lead that now has nothing BUT stock
	contact fields would hand them a form with no email address on it.
	"""
	names = frappe.get_all(
		"Property Setter",
		filters={
			"doc_type": "Lead",
			"doctype_or_field": "DocField",
			"field_name": ("in", HIDDEN_STOCK_FIELDS),
			"property": ("in", HIDDEN_STOCK_PROPERTIES),
			"property_type": "Check",
			"value": "1",
		},
		pluck="name",
	)

	for name in names:
		frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)

	if names:
		frappe.clear_cache(doctype="Lead")

	return names


def _log_refusal(refusal):
	doc = frappe.new_doc("Connector Log")
	doc.request_id = "legacy-drop"
	doc.outcome = "Rejected"
	doc.error_code = "DROP_REFUSED"
	doc.message = "{0}\n\nNothing was dropped. Patch A has been re-enqueued.\n\n{1}".format(
		refusal["reason"], json.dumps(refusal.get("failures") or [], indent=1, default=str)
	)[:100000]
	doc.insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep - one-time legacy migration, run as a patch or by an operator; each stage must be durable before the next destructive one


def _log_success(report):
	doc = frappe.new_doc("Connector Log")
	doc.request_id = "legacy-drop"
	doc.outcome = "Success"
	doc.error_code = "DROPPED"
	doc.message = (
		"Legacy Custom Fields dropped on {0}.\n\n{1}\n\n"
		"Undo with: bench --site <site> execute sigzenlead_connector.legacy.restore.restore".format(
			now_datetime(), json.dumps(report, indent=1, default=str)
		)
	)[:100000]
	doc.insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep - one-time legacy migration, run as a patch or by an operator; each stage must be durable before the next destructive one
