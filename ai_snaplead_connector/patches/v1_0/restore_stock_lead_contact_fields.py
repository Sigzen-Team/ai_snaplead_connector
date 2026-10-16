# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Undo the Property Setters that used to hide stock Lead.email_id / Lead.mobile_no.

``ai_snaplead_connector.install.hide_legacy_lead_contact_fields`` set ``hidden = 1`` and
``read_only = 1`` on two fields that ERPNext owns, on every install AND every migrate. That is
not a marketplace app's business: those fields are read by stock list views, reports, contact
lookups and email workflows, and a client who never asked for this app to touch them had no way
to get them back.

The connector no longer writes those Property Setters. This patch removes the ones it wrote so
an existing client is restored on the next migrate.

Scope is deliberately narrow: only DocField-level ``hidden``/``read_only`` Check setters whose
value is exactly ``"1"`` on those two fields -- i.e. exactly the shape this app wrote. A client
who hid the fields through Customize Form with any other value or property is left alone.
"""

import frappe

RESTORED_FIELDS = ("email_id", "mobile_no")
RESTORED_PROPERTIES = ("hidden", "read_only")


def execute():
	if not frappe.db.exists("DocType", "Lead"):
		return

	names = frappe.get_all(
		"Property Setter",
		filters={
			"doc_type": "Lead",
			"doctype_or_field": "DocField",
			"field_name": ("in", RESTORED_FIELDS),
			"property": ("in", RESTORED_PROPERTIES),
			"property_type": "Check",
			"value": "1",
		},
		pluck="name",
	)

	for name in names:
		frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)

	if names:
		frappe.clear_cache(doctype="Lead")
