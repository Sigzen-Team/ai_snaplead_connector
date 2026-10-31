# Copyright (c) 2026, support@sigzen.com and contributors
"""Carry an installed client across the rename to "SigzenLead - Connector" (/desk/sigzenlead-connector).

Runs from the `before_migrate` hook, before any patch of any app and before the model sync.

The module has been renamed three times: "Vc Connector" -> "Sigzen - AI Scan CRM Connector" ->
"AI SnapLead Connector" -> "SigzenLead Connector". Each rename moved its code into a new folder inside the package (a
module's folder is frappe.scrub(name), now `sigzenlead_connector/`). Frappe finds a DocType's
controller through `tabDocType.module`, so until that column holds the new name every load of
Connector Settings, Connector Lead Link or Connector Log fails with a module-not-found error --
and this runs on a CLIENT's ERPNext, where a broken migrate is somebody else's outage.

Frappe refuses to rename a standard Module Def, and in developer_mode saving one creates
folders and edits modules.txt. So the row is moved with plain SQL and the links are
repointed with frappe's own rename helpers, without loading the Module Def controller.

This step renames the MODULE, the workspace and the desktop tile. A site that installed the
package under an old name (`vc_connector`, `sigzen_ai_scan_crm_connector`, `ai_snaplead_connector`) is first renamed by
upgrade/from_old_package.py, from the stand-in package's own `before_migrate` in the same
`bench migrate`; that step then calls this one.

Every step is idempotent and isolated in a savepoint: a site already on the new names is
left alone, and one failing step cannot take the others (or the migrate) with it.
"""

import frappe

APP = "sigzenlead_connector"

MODULE = "SigzenLead Connector"
LEGACY_MODULES = ("AI SnapLead Connector", "Sigzen - AI Scan CRM Connector", "Vc Connector")

#: The workspace. Its NAME is its route (/desk/sigzenlead-connector: frappe's slug only turns
#: spaces into hyphens, so a name with " - " in it became /desk/sigzenlead---connector). Its
#: label and title carry the same string: desktop.js builds the tile's URL from the workspace
#: TITLE, and Workspace.before_export forces name = title whenever the two differ.
WORKSPACE = "SigzenLead Connector"

#: What a person sees: hooks.app_title, the desk tile's label, the Workspace Sidebar the tile
#: opens (its header) and the heading inside the workspace. desktop.js finds a tile's sidebar
#: by the tile's LABEL, so the sidebar is named after the app, not after the workspace.
APP_LABEL = "SigzenLead - Connector"

#: The workspace's previous name. Unlike the older names below it was never the app's label, and
#: frappe keeps a second, empty sidebar under it. That one moves to WORKSPACE: moved to APP_LABEL
#: first, it would take the name the real sidebar needs and strand that under the old label.
PREVIOUS_WORKSPACE = "AI SnapLead Connector"

#: Every name the workspace had before, newest first. The second is the visible title it shared
#: until the route lost its "---"; the rest were app names too.
LEGACY_WORKSPACES = (
	PREVIOUS_WORKSPACE,
	"AI SnapLead - Connector",
	"Sigzen - AI Scan CRM Connector",
	"Sigzen Scan to CRM-ERPNext Connector",
	"VC Connector",
)
#: Earlier app titles: the tile, the sidebar and the heading carried these.
LEGACY_APP_LABELS = tuple(name for name in LEGACY_WORKSPACES if name != PREVIOUS_WORKSPACE)

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
	rename_desk()
	for legacy, new in LEGACY_DOCTYPES.items():
		# Not in a savepoint: renaming a table commits (see rename_doctype).
		_committed_step(rename_doctype, legacy, new)
	for legacy, new in LEGACY_ROLES.items():
		_step(rename_role, legacy, new)
	frappe.clear_cache()


def rename_desk():
	"""Workspace, sidebar, app tile and the workspace's Link icon onto the current names.

	The four stores are independent and `bench migrate` reconciles none of them for a row that
	already exists: the Workspace (name = route, label, title), the Workspace Sidebar the tile
	opens (name = title, UNIQUE) with its items' `link_to`, the app's Desktop Icon (label
	UNIQUE) and the workspace's own hidden Link icon. The heading inside `Workspace.content` is
	repaired after the sync by install.ensure_workspace.
	"""
	for legacy in LEGACY_WORKSPACES:
		_step(rename_workspace, legacy, WORKSPACE)
		_step(rename_sidebar, legacy, WORKSPACE if legacy == PREVIOUS_WORKSPACE else APP_LABEL)
		_step(repoint_icon_links, legacy)
	for legacy in LEGACY_APP_LABELS:
		_step(rename_app_icon, legacy, APP_LABEL)
	_step(retire_legacy_link_icons)


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
	"""Rename the workspace row; its roles, links and shortcuts are child rows and move with it.

	Only the Workspace. Its sidebar is named after the app, not after the workspace, and is
	handled by rename_sidebar. Returns False when there is nothing to do, so a second run is a
	no-op.
	"""
	if old == new or not frappe.db.exists("Workspace", old):
		return False
	if frappe.db.exists("Workspace", new):
		return False

	frappe.rename_doc("Workspace", old, new, force=True, show_alert=False)
	frappe.db.set_value("Workspace", new, {"label": new, "title": new}, update_modified=False)
	repoint_sidebar_items(old, new)
	return True


def repoint_sidebar_items(old, new):
	"""Sidebar entries that open the workspace by its old name.

	`link_to` is a Dynamic Link, which rename_doc is not guaranteed to follow; an entry left on
	the old name drops out of the sidebar (is_item_allowed finds no such workspace) and a
	sidebar with no item left disappears from the desk with its tile.
	"""
	table = frappe.qb.DocType("Workspace Sidebar Item")
	frappe.qb.update(table).set(table.link_to, new).where(
		(table.link_type == "Workspace") & (table.link_to == old)
	).run()
	return True


def rename_sidebar(old, new):
	"""Move the Workspace Sidebar the tile opens onto the app's label.

	`Workspace Sidebar.title` is UNIQUE and is also the row's name, and desktop.js resolves a
	tile through the sidebar named like the tile's label. Never merges two sidebars: if both
	exist the client's own one is left where it is.
	"""
	if old == new or not frappe.db.exists("Workspace Sidebar", old):
		return False
	if frappe.db.exists("Workspace Sidebar", new):
		return False
	frappe.rename_doc("Workspace Sidebar", old, new, force=True, show_alert=False)
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
	frappe.db.sql(
		"update `tabHas Role` set parent = %s where parent = %s and parenttype = 'Desktop Icon'", (new, old)
	)
	return True


def repoint_icon_links(old):
	"""Point desktop icons that still open an old name at the current one.

	`link_to` names the Workspace Sidebar (now APP_LABEL) and `link` holds the /desk/<slug>
	route of an External tile (now /desk/sigzenlead-connector); neither is a plain Link field
	that rename_doc is guaranteed to follow. A tile left on an old name opens nothing.
	"""
	from frappe.utils import slug

	if old != APP_LABEL and frappe.db.exists("Workspace Sidebar", APP_LABEL):
		frappe.db.sql(
			"""update `tabDesktop Icon` set link_to = %s
			   where link_to = %s and link_type in ('Workspace Sidebar', 'Workspace')""",
			(APP_LABEL, old),
		)
	frappe.db.sql(
		"update `tabDesktop Icon` set link = %s where link = %s",
		(f"/desk/{slug(WORKSPACE)}", f"/desk/{slug(old)}"),
	)
	return True


def retire_legacy_link_icons():
	"""Give the workspace's own hidden Link icon the workspace's name.

	The workspace has its own Link icon besides the app tile. It is hidden, because the tile
	already opens the workspace. It is LABELLED with the workspace's name on purpose: frappe's
	create_desktop_icons_from_workspace (run after every app install) skips a workspace that
	already has a Link icon under its name, and otherwise adds a visible one parented to the
	app tile -- which turns the tile into a folder. Renamed rather than deleted, and the
	client's roles on it move with it.
	"""
	if frappe.db.exists("Desktop Icon", WORKSPACE) or frappe.db.exists("Desktop Icon", {"label": WORKSPACE}):
		return False
	# The Link icon carries an old workspace name, or that name plus " workspace" once an
	# earlier release retired it.
	legacy_names = set(LEGACY_WORKSPACES) | {f"{name} workspace" for name in LEGACY_WORKSPACES}
	rows = frappe.get_all(
		"Desktop Icon",
		filters={"icon_type": "Link", "link_to": ("in", [APP_LABEL, WORKSPACE, *LEGACY_WORKSPACES])},
		fields=["name", "label"],
		order_by="creation asc",
	)
	for row in rows:
		if row.name in legacy_names or row.label in legacy_names:
			# Hidden and unparented: a visible Link icon whose parent_icon equals the app tile's
			# label makes desktop.js render the tile as a folder.
			frappe.db.sql(
				"""update `tabDesktop Icon` set name = %s, label = %s, hidden = 1, parent_icon = null
				   where name = %s""",
				(WORKSPACE, WORKSPACE, row.name),
			)
			frappe.db.sql(
				"update `tabDesktop Icon` set parent_icon = %s where parent_icon = %s", (WORKSPACE, row.name)
			)
			frappe.db.sql(
				"update `tabHas Role` set parent = %s where parent = %s and parenttype = 'Desktop Icon'",
				(WORKSPACE, row.name),
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
