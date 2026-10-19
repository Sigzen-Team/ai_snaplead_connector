# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""One row per record whose legacy Custom Fields were migrated, and later archived.

The row is created by Patch A, as the per-record mark that makes the migration re-runnable:
a second pass sees the mark and does nothing rather than appending a second copy of the same
email address, phone number or scanned-text note.

Patch B then fills ``legacy_values`` on the same row with everything the record held in those
fields, BEFORE a single one of them is dropped. That JSON is the only copy left afterwards,
and it lives on the client's own site -- not in a support engineer's backup -- which is what
makes ``ai_snaplead_connector.legacy.restore`` a real undo button rather than a promise.

Deliberately NOT deletable by a System Manager: the permission block grants read/report/export
only. An archive somebody can clear from the list view is not an archive.
"""

import frappe
from frappe.model.document import Document


class ConnectorLegacyFieldArchive(Document):
	def validate(self):
		self.reference_doctype = (self.reference_doctype or "").strip()
		self.reference_name = (self.reference_name or "").strip()
		self.lookup_key = make_lookup_key(self.reference_doctype, self.reference_name)


def make_lookup_key(reference_doctype, reference_name):
	return f"{(reference_doctype or '').strip()}|{(reference_name or '').strip()}"


def get_row(reference_doctype, reference_name):
	"""The archive row for a record, or None."""
	return frappe.db.get_value(
		"Connector Legacy Field Archive",
		{"lookup_key": make_lookup_key(reference_doctype, reference_name)},
		["name", "migration_status", "migration_notes", "legacy_values"],
		as_dict=True,
	)
