# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Adopt the six child DocTypes this app used to create imperatively.

Until now ``sigzenlead_connector.install.create_child_doctypes`` inserted these DocTypes at
install/migrate time with ``custom = 1``. A custom DocType is not owned by any app: it is
invisible to ``bench migrate``'s model sync, it is never versioned, and it collides with the
file-backed definitions this app now ships.

This patch runs in ``pre_model_sync`` so that, by the time model sync walks
``sigzenlead_connector/sigzenlead_connector/doctype/``, the existing rows are already marked as standard and
have no stale ``migration_hash`` -- the JSON is then imported over them in place. Importing a
DocType uses ``for_reload``, so the underlying tables and every existing row survive.

Adoption is not free, though, and the price falls on the client. A DocType with ``custom = 1``
is one a System Manager may add fields to from the Desk without developer mode, so a client
may well have done exactly that. Model sync re-imports the shipped JSON over the row
(``import_file`` deletes the old doc and inserts the new one), and every DocField the client
added disappears from the meta: the column and its values stay in the table, invisible to
every form, list view, report and REST response, with no warning. So before handing a DocType
over, each field that is not part of the shipped definition is re-created as a Custom Field,
which lives in its own table and survives the re-import.

Nothing happens on a fresh install: the DocTypes do not exist yet, and model sync creates them
from the JSON directly.
"""

import json

import frappe
from frappe.model import no_value_fields

ADOPTED_DOCTYPES = (
	"ERPNext Lead Email",
	"ERPNext Lead Mobile Number",
	"ERPNext Lead Activity Log",
	"Lead Source Text Message",
	"Lead Source WhatsApp Message",
	"Lead Source Email Message",
)

# Everything that describes the field to the user and to the database. Anything not in this
# list is either bookkeeping (name, parent, idx, creation) or does not exist on Custom Field.
CARRIED_OVER_PROPERTIES = (
	"fieldname",
	"label",
	"fieldtype",
	"options",
	"default",
	"description",
	"precision",
	"length",
	"reqd",
	"unique",
	"read_only",
	"hidden",
	"in_list_view",
	"in_standard_filter",
	"in_global_search",
	"no_copy",
	"bold",
	"allow_on_submit",
	"ignore_user_permissions",
	"depends_on",
	"mandatory_depends_on",
	"read_only_depends_on",
	"fetch_from",
	"fetch_if_empty",
	"non_negative",
	"translatable",
	"columns",
	"width",
	"print_hide",
	"print_width",
	"permlevel",
)


def _shipped_field_order(doctype):
	"""The fieldnames of the definition this app ships, in order, or None if unreadable.

	If the JSON cannot be read we cannot tell a client's field from a shipped one, and
	guessing would either drop the client's field or duplicate a shipped one as a Custom
	Field. The caller leaves such a DocType alone instead.
	"""
	folder = frappe.scrub(doctype)
	path = frappe.get_app_path(
		"sigzenlead_connector", "sigzenlead_connector", "doctype", folder, folder + ".json"
	)

	try:
		# Safe to open: path is built from frappe.get_app_path and fixed names inside this app, never from input
		with open(path) as definition_file:  # nosemgrep
			definition = json.load(definition_file)
	except (OSError, ValueError):
		return None

	return [field.get("fieldname") for field in (definition.get("fields") or []) if field.get("fieldname")]


def preserve_client_added_fields(doctype):
	"""Re-create every non-shipped DocField of ``doctype`` as a Custom Field.

	Returns the list of fieldnames carried over, or None when the shipped definition could
	not be read (the caller then skips the DocType entirely rather than risk losing fields).

	The DocField row is deleted first, on purpose: ``CustomField.validate`` refuses a
	fieldname that the DocType's meta already has, and it reads that meta uncached. The row
	is going to be deleted by the re-import moments later in any case.
	"""
	shipped = _shipped_field_order(doctype)
	if shipped is None:
		return None

	existing = frappe.db.sql(
		"""select * from `tabDocField` where parent = %s and parenttype = 'DocType' order by idx asc""",
		(doctype,),
		as_dict=True,
	)

	carried_over = []
	last_shipped = shipped[-1] if shipped else None

	for field in existing:
		fieldname = field.get("fieldname")
		if not fieldname or fieldname in shipped:
			continue

		if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
			continue

		frappe.db.delete("DocField", {"parent": doctype, "parenttype": "DocType", "fieldname": fieldname})
		frappe.clear_cache(doctype=doctype)

		custom_field = frappe.new_doc("Custom Field")
		custom_field.dt = doctype

		for prop in CARRIED_OVER_PROPERTIES:
			value = field.get(prop)
			if value not in (None, ""):
				custom_field.set(prop, value)

		if not custom_field.label and field.get("fieldtype") not in no_value_fields:
			custom_field.label = fieldname

		# Keep it after the shipped fields, which is where the client had it: the shipped
		# definition is re-imported wholesale, so any other anchor would dangle.
		custom_field.insert_after = last_shipped
		custom_field.insert(ignore_permissions=True)

		carried_over.append(fieldname)
		frappe.clear_cache(doctype=doctype)

	return carried_over


def execute():
	for doctype in ADOPTED_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue

		if preserve_client_added_fields(doctype) is None:
			# Shipped definition unreadable -- model sync has nothing to import over this
			# DocType either, so leaving it custom is strictly safer than adopting it.
			continue

		frappe.db.set_value(
			"DocType",
			doctype,
			{
				# Hand ownership of the DocType to this app.
				"custom": 0,
				"module": "SigzenLead Connector",
				# Force import_file to re-import from JSON instead of trusting the row.
				"migration_hash": None,
			},
			update_modified=False,
		)

	frappe.clear_cache()
