# Copyright (c) 2026, support@sigzen.com and contributors
"""Remove the stored value of a setting that no longer exists.

`Connector Settings.allow_insecure_http` was a checkbox no code path ever read. It sat
under "Security", so a client could tick it and reasonably believe transport rules had
changed. The field is gone from the DocType; a Single keeps one row per field in
`tabSingles`, and the model sync does not remove the row of a deleted field, so it is
removed here. Nothing else is touched.
"""

import frappe

SETTINGS = "Connector Settings"
REMOVED_FIELDS = ("allow_insecure_http",)


def execute():
	for fieldname in REMOVED_FIELDS:
		frappe.db.delete("Singles", {"doctype": SETTINGS, "field": fieldname})
