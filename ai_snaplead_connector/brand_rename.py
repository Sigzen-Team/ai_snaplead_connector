# Copyright (c) 2026, support@sigzen.com and contributors
"""Carry an installed client across the rename to "AI SnapLead - Connector".

Runs from the `before_migrate` hook, before any patch of any app and before the model sync.

The module has been renamed twice: "Vc Connector" -> "Sigzen - AI Scan CRM Connector" ->
"AI SnapLead Connector". Each rename moved its code into a new folder inside the package (a
module's folder is frappe.scrub(name), now `ai_snaplead_connector/`). Frappe finds a DocType's
controller through `tabDocType.module`, so until that column holds the new name every load of
Connector Settings, Connector Lead Link or Connector Log fails with a module-not-found error --
and this runs on a CLIENT's ERPNext, where a broken migrate is somebody else's outage.

Frappe refuses to rename a standard Module Def, and in developer_mode saving one creates
folders and edits modules.txt. So the row is moved with plain SQL and the links are
repointed with frappe's own rename helpers, without loading the Module Def controller.

This step renames the MODULE, the workspace and the desktop tile. A site that installed the
package under an old name (`vc_connector`, `sigzen_ai_scan_crm_connector`) is first renamed by
upgrade/from_old_package.py, from the stand-in package's own `before_migrate` in the same
`bench migrate`; that step then calls this one.

Every step is idempotent and isolated in a savepoint: a site already on the new names is
left alone, and one failing step cannot take the others (or the migrate) with it.
"""

import frappe

APP = "ai_snaplead_connector"

MODULE = "AI SnapLead Connector"
LEGACY_MODULES = ("Sigzen - AI Scan CRM Connector", "Vc Connector")

#: The workspace. Its NAME is its route and the desktop tile's label, so it moves with the
#: app title (see hooks.app_title).
WORKSPACE = "AI SnapLead - Connector"
LEGACY_WORKSPACES = ("Sigzen - AI Scan CRM Connector", "Sigzen Scan to CRM-ERPNext Connector", "VC Connector")

#: Doctypes that hold a module name in a column that is NOT a Link, so frappe's rename
#: helpers never see it.
PLAIN_MODULE_COLUMNS = ("Block Module", "Workspace Sidebar")

#: The app's own DocTypes and role lost their "VC" prefix. Where dropping it would have left a
#: name as generic as "Lead Link" -- DocType names are global across every app on a site -- they
#: say "Connector" instead. Field names (vc_uuid, ...) are the wire contract and stay.
LEGACY_DOCTYPES = {
	"VC Connector Settings": "Connector Settings",
	"VC Connector Log": "Connector Log",
	"VC Lead Link": "Connector Lead Link",
	"VC Source Link": "Connector Source Link",
	"VC Legacy Field Archive": "Connector Legacy Field Archive",
}
LEGACY_ROLES = {"VC Connector Integration": "Connector Integration"}


def execute():
	if APP not in frappe.get_installed_apps():
		return
	refresh_module_map()
	for legacy in LEGACY_MODULES:
		_step(rename_module, legacy, MODULE, APP)
		_step(repoint_plain_module_columns, legacy, MODULE)
	_step(ensure_module_def, MODULE, APP)
	for legacy in LEGACY_WORKSPACES:
		_step(rename_workspace, legacy, WORKSPACE)
		_step(rename_app_icon, legacy, WORKSPACE)
		_step(repoint_icon_links, legacy, WORKSPACE)
	_step(retire_legacy_link_icons)
	for legacy, new in LEGACY_DOCTYPES.items():
		# Not in a savepoint: renaming a table commits (see rename_doctype).
		_committed_step(rename_doctype, legacy, new)
	for legacy, new in LEGACY_ROLES.items():
		_step(rename_role, legacy, new)
	frappe.clear_cache()


def refresh_module_map():
	"""Drop the cached app -> modules map, which still lists the old folder.

	frappe caches that map in Redis and `bench migrate` does not rebuild it, so the model sync
	would look for the old module folder and die with ModuleNotFoundError on the first
	migrate after the rename.
	"""
	drop_module_map_cache()
	frappe.setup_module_map(include_all_apps=True)


def drop_module_map_cache():
	"""Forget both cached module maps on any frappe this app installs on.

	frappe 16 keeps `installed_app_modules` in `frappe.client_cache`; frappe 15 has no
	client_cache and keeps it in `frappe.cache` next to `app_modules`.
	"""
	frappe.cache.delete_value("app_modules")
	frappe.cache.delete_value("installed_app_modules")
	client_cache = getattr(frappe, "client_cache", None)
	if client_cache is not None:
		client_cache.delete_value("installed_app_modules")


def _step(fn, *args):
	save_point = f"br_{fn.__name__}"
	frappe.db.savepoint(save_point)
	try:
		return fn(*args)
	except Exception:
		frappe.db.rollback(save_point=save_point)
		print(f"{APP}: rename step {fn.__name__}{args} failed")
		frappe.log_error(frappe.get_traceback(), f"{APP}: rename step {fn.__name__} failed")
		return False


def rename_module(old, new, app):
	"""Move the Module Def row and everything that points at it. No controller is loaded."""
	if not frappe.db.exists("Module Def", old):
		return False

	from frappe.model.rename_doc import get_link_fields, update_link_field_values

	if frappe.db.exists("Module Def", new):
		# Both rows exist (a half-finished earlier attempt): keep the new one and drop the old
		# once nothing points at it.
		update_link_field_values(get_link_fields("Module Def"), old, new, "Module Def")
		frappe.db.delete("Module Def", {"name": old})
	else:
		frappe.db.sql(
			"update `tabModule Def` set name = %s, module_name = %s, app_name = %s where name = %s",
			(new, new, app, old),
		)
		update_link_field_values(get_link_fields("Module Def"), old, new, "Module Def")
	return True


def repoint_plain_module_columns(old, new):
	for doctype in PLAIN_MODULE_COLUMNS:
		if not frappe.db.table_exists(doctype):
			continue
		table = frappe.qb.DocType(doctype)
		frappe.qb.update(table).set(table.module, new).where(table.module == old).run()
	return True


def ensure_module_def(name, app):
	"""`bench migrate` never creates a Module Def; only install does. Make sure ours exists."""
	if frappe.db.exists("Module Def", name):
		return False
	doc = frappe.new_doc("Module Def")
	doc.name = name
	doc.module_name = name
	doc.app_name = app
	doc.custom = 0
	# db_insert, not insert(): on_update creates folders and edits modules.txt in developer_mode.
	doc.db_insert()
	return True


def rename_workspace(old, new):
	"""Rename the workspace and its sidebar, keeping the client's own links on the row."""
	if not frappe.db.exists("Workspace", old):
		return False
	if frappe.db.exists("Workspace", new):
		return False

	frappe.rename_doc("Workspace", old, new, force=True, show_alert=False)
	frappe.db.set_value("Workspace", new, {"label": new, "title": new}, update_modified=False)

	# The sidebar is keyed by name and desktop.js finds it by the tile's label.
	if frappe.db.exists("Workspace Sidebar", old) and not frappe.db.exists("Workspace Sidebar", new):
		frappe.rename_doc("Workspace Sidebar", old, new, force=True, show_alert=False)
	if frappe.db.exists("Workspace Sidebar", new):
		frappe.db.set_value("Workspace Sidebar", new, "title", new, update_modified=False)
	return True


def rename_app_icon(old, new):
	"""Move the app's standard Desktop Icon onto the name its JSON file now declares.

	The icon is a standard record with a file under desktop_icon/. The file was renamed with
	the app, and migrate deletes a standard icon whose file is gone -- taking the client's
	placement of the tile, and the parent of the workspace icon, with it. Renaming the row
	first keeps both. Plain SQL: in developer_mode the controller exports and deletes files.
	"""
	if not frappe.db.exists("Desktop Icon", {"name": old, "app": APP}):
		return False
	if frappe.db.exists("Desktop Icon", new):
		return False
	if frappe.db.exists("Desktop Icon", {"label": new}):
		return False

	frappe.db.sql("update `tabDesktop Icon` set name = %s, label = %s where name = %s", (new, new, old))
	frappe.db.sql("update `tabDesktop Icon` set parent_icon = %s where parent_icon = %s", (new, old))
	frappe.db.sql("update `tabHas Role` set parent = %s where parent = %s and parenttype = 'Desktop Icon'", (new, old))
	return True


def repoint_icon_links(old, new):
	"""Point desktop icons that still open the old workspace at the renamed one.

	`link_to` names the Workspace Sidebar and `link` holds the old /desk/<slug> route for an
	External tile; neither is a plain Link field that rename_doc is guaranteed to follow. A tile
	left on the old name opens nothing.
	"""
	from frappe.utils import slug

	if not frappe.db.exists("Workspace Sidebar", new):
		return False
	frappe.db.sql(
		"""update `tabDesktop Icon` set link_to = %s
		   where link_to = %s and link_type in ('Workspace Sidebar', 'Workspace')""",
		(new, old),
	)
	frappe.db.sql(
		"update `tabDesktop Icon` set link = %s where link = %s",
		(f"/desk/{slug(new)}", f"/desk/{slug(old)}"),
	)
	return True


def retire_legacy_link_icons():
	"""Take the old names off the workspace's hidden Link icon.

	The workspace has its own Link icon besides the app tile. It is hidden, because the tile
	already opens the workspace, but it still shows in the Desktop Icon list under whatever the
	app was called when it was created ("VC Connector"), which reads as a second, older
	connector on the site. It is renamed rather than deleted: with no Link icon at all,
	install.py would make a fresh one on the next migrate.
	"""
	target = f"{WORKSPACE} workspace"
	if frappe.db.exists("Desktop Icon", target) or frappe.db.exists("Desktop Icon", {"label": target}):
		return False
	# The Link icon carries the workspace's name, or that name plus " workspace" once an earlier
	# release retired it.
	legacy_names = set(LEGACY_WORKSPACES) | {f"{name} workspace" for name in LEGACY_WORKSPACES}
	rows = frappe.db.sql(
		"""select name, label from `tabDesktop Icon`
		   where icon_type = 'Link' and link_to = %s order by creation""",
		(WORKSPACE,),
		as_dict=True,
	)
	for row in rows:
		if row.name in legacy_names or row.label in legacy_names:
			frappe.db.sql(
				"update `tabDesktop Icon` set name = %s, label = %s, hidden = 1 where name = %s",
				(target, target, row.name),
			)
			frappe.db.sql("update `tabDesktop Icon` set parent_icon = %s where parent_icon = %s", (target, row.name))
			frappe.db.sql(
				"update `tabHas Role` set parent = %s where parent = %s and parenttype = 'Desktop Icon'",
				(target, row.name),
			)
			return True
	return False


def rename_doctype(old, new):
	"""Move a DocType with frappe's own rename: the table (or the Single's values), every Link
	and Table field that points at it, dynamic links, customizations and attachments.

	Runs before the model sync, so the sync finds the DocType already under the name its JSON
	now carries and updates it instead of creating an empty second one beside the old data.
	"""
	if not frappe.db.exists("DocType", old):
		return
	if frappe.db.exists("DocType", new):
		# Never merge two tables of client data automatically.
		print(f"{APP}: both DocType {old!r} and {new!r} exist; left {old!r} in place")
		frappe.log_error(f"Both DocType {old!r} and {new!r} exist; {old!r} was not renamed", f"{APP}: rename")
		return
	in_patch, rename_table = frappe.flags.in_patch, frappe.db.rename_table
	# in_patch: allowed outside developer mode, and frappe does not try to move the code
	# folders, which the app already ships under the new name.
	frappe.flags.in_patch = True
	if frappe.db.db_type != "mariadb":
		frappe.db.rename_table = _rename_table_committing
	try:
		frappe.rename_doc("DocType", old, new, force=True, show_alert=False, rebuild_search=False)
	finally:
		frappe.flags.in_patch, frappe.db.rename_table = in_patch, rename_table
	# A Single keeps its Password fields in __Auth under the DocType's name; rename_doc does not.
	frappe.db.sql("update `__Auth` set doctype = %s where doctype = %s", (new, old))
	print(f"{APP}: renamed DocType {old!r} -> {new!r}")


def _rename_table_committing(old, new):
	"""What MariaDB's RENAME TABLE does by itself: commit, then rename.

	rename_doc writes before it renames the table. On MariaDB the RENAME commits those writes
	implicitly; SQLite and Postgres rename with ALTER TABLE, which frappe refuses while writes are
	pending (ImplicitCommitError), so frappe's own DocType rename fails there without this.
	"""
	from frappe.utils import get_table_name

	frappe.db.sql_ddl(f"ALTER TABLE `{get_table_name(old)}` RENAME TO `{get_table_name(new)}`")


def _committed_step(fn, *args):
	"""For a step that commits part-way: start from a clean commit, so that a failure rolls back
	this step only and never the earlier ones, and the migrate carries on."""
	frappe.db.commit()  # nosemgrep - the table rename inside commits anyway; this fixes where
	try:
		fn(*args)
		frappe.db.commit()  # nosemgrep - a renamed DocType must not be half-applied by a later rollback
	except Exception:
		frappe.db.rollback()
		print(f"{APP}: rename step {fn.__name__}{args} failed")
		frappe.log_error(frappe.get_traceback(), f"{APP}: rename step {fn.__name__} failed")


def rename_role(old, new):
	"""Every user holding the role, and every permission row naming it, moves with it."""
	if not frappe.db.exists("Role", old):
		return
	merge = bool(frappe.db.exists("Role", new))
	frappe.rename_doc("Role", old, new, force=True, merge=merge, show_alert=False, rebuild_search=False)
	print(f"{APP}: renamed Role {old!r} -> {new!r}")
