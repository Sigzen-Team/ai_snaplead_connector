# Copyright (c) 2026, support@sigzen.com and contributors
"""Rename the workspace to the app's name, BEFORE the model sync sees the new JSON.

This has to run pre_model_sync. `bench migrate` imports workspace JSON before after_migrate
hooks, and `tabWorkspace.label` carries a UNIQUE index -- so on a client that already has
the old "VC Connector" row holding the new label, the sync tries to INSERT a second
workspace under the new name and dies with:

    UniqueValidationError: ('Workspace', 'Sigzen Scan to CRM-ERPNext Connector',
    IntegrityError('UNIQUE constraint failed: tabWorkspace.label'))

Renaming first also keeps the client's own links, charts and shortcuts on the row, which a
fresh import beside it would not.

WHY THE NAME CHANGES AT ALL, rather than just the label: desktop.js builds the app tile's
route from the workspace TITLE but looks the workspace up by slug(NAME). When they differ
the tile produces a URL that matches no workspace and drops the user back on the desktop --
and the only working alternative, link_type "External", is what made every tile open in a
new window.
"""

import frappe

from sigzenlead_connector.brand_rename import WORKSPACE

LEGACY = "VC Connector"
# The workspace's name today. It was "Sigzen Scan to CRM-ERPNext Connector" when this patch was
# written; brand_rename.py moves every later name onto the current one before patches run.
CURRENT = WORKSPACE


def _repair_sidebar_labels():
	"""rename_doc rewrites Link fields, not Data ones.

	`Workspace Sidebar Item.link_to` follows the workspace onto its new name; `label` does
	not, so the entry keeps reading "VC Connector" above a page now called something else.
	Core labels a workspace's own entry "Home". Runs even when the rename below is skipped,
	because a client renamed by an earlier release still carries the stale label.
	"""
	for name in frappe.get_all(
		"Workspace Sidebar Item", filters={"label": LEGACY, "link_to": CURRENT}, pluck="name"
	):
		frappe.db.set_value("Workspace Sidebar Item", name, "label", "Home", update_modified=False)


def execute():
	if not frappe.db.exists("Workspace", LEGACY):
		_repair_sidebar_labels()
		frappe.clear_cache()
		return
	if frappe.db.exists("Workspace", CURRENT):
		_repair_sidebar_labels()
		frappe.clear_cache()
		return

	try:
		frappe.rename_doc("Workspace", LEGACY, CURRENT, force=True, show_alert=False)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "sigzenlead_connector: workspace rename failed")
		return

	# The sidebar is keyed by name too, and desktop.js finds it by the tile's label.
	try:
		if frappe.db.exists("Workspace Sidebar", LEGACY) and not frappe.db.exists(
			"Workspace Sidebar", CURRENT
		):
			frappe.rename_doc("Workspace Sidebar", LEGACY, CURRENT, force=True, show_alert=False)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "sigzenlead_connector: sidebar rename failed")

	_repair_sidebar_labels()
	frappe.clear_cache()
