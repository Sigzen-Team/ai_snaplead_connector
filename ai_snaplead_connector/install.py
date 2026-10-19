# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _

# The two patches that move an existing client off the legacy Custom Fields. They are
# registered in patches.txt, and `frappe.installer.install_app` writes a Patch Log row for
# every one of them BEFORE it calls after_install -- see `set_all_patches_as_completed`, which
# runs a few lines above the after_install hook. So on any install they are recorded as
# executed without having run, and they will never run again on that site.
LEGACY_PATCHES = (
	"ai_snaplead_connector.patches.v1_0.migrate_legacy_lead_fields",
	"ai_snaplead_connector.patches.v1_0.drop_legacy_lead_fields",
)

# The role a client can hand an integration user instead of System Manager. See
# `ensure_integration_role`.
INTEGRATION_ROLE = "Connector Integration"

# Only the link row: everything else this endpoint writes (Lead, Contact, Address, File, the
# log row) it writes with ignore_permissions, and the gate in api.v1.sync._check_permissions
# is what actually decides whether the integration user may write at all. Lead, Contact and
# Address come from the client's own sales role; Connector Lead Link is this app's own DocType and
# nothing but System Manager could reach it before this role existed.
INTEGRATION_ROLE_PERMISSIONS = {
	"Connector Lead Link": ("read", "write", "create"),
	"Connector Source Link": ("read", "write", "create"),
}


#: The workspace's name, label and title -- one string, see brand_rename.WORKSPACE. desktop.js
#: builds the tile's route from the workspace TITLE but looks the workspace up by slug(NAME),
#: so any difference produces a URL that resolves to nothing. The route is
#: /desk/ai-snaplead-connector.
#:
#: APP_LABEL ("AI SnapLead - Connector") is what people read: the tile, the sidebar header and
#: the heading inside the workspace.
from ai_snaplead_connector.brand_rename import (
	APP_LABEL,
	LEGACY_APP_LABELS,
	LEGACY_WORKSPACES,
	WORKSPACE,
)

APP = "ai_snaplead_connector"

#: The Workspace Sidebar the app tile opens. desktop.js finds it by the tile's LABEL
#: (frappe.boot.workspace_sidebar_item[label.toLowerCase()]), so it carries the app's label.
SIDEBAR = APP_LABEL


#: extend_doctype_class (hooks.py), which carries the whole Lead extension, exists only from
#: Frappe v16. On v15 the hook is silently ignored and the app "installs" without it.
MIN_FRAPPE_MAJOR = 16


def frappe_major_version(version=None):
	"""The leading number of frappe.__version__, or None when it is not a number (a develop build)."""
	version = str(version if version is not None else getattr(frappe, "__version__", "") or "")
	digits = ""
	for char in version.strip():
		if not char.isdigit():
			break
		digits += char
	return int(digits) if digits else None


def check_frappe_version(version=None):
	"""Refuse to install or migrate on Frappe < 16 instead of silently skipping the Lead extension."""
	major = frappe_major_version(version)
	if major is not None and major < MIN_FRAPPE_MAJOR:
		frappe.throw(
			_(
				"AI SnapLead - Connector needs Frappe/ERPNext v16. This site runs Frappe {0}; upgrade"
				" the bench to v16 before installing or migrating this app."
			).format(version if version is not None else frappe.__version__),
			title=_("Unsupported Frappe Version"),
		)


def before_install():
	"""Make sure frappe will actually sync this app's DocTypes, then hand over to the rename.

	`bench install-app` reads the bench's app -> modules map from Redis when it starts, and its
	model sync walks only the modules that map lists. A map cached before this app reached the
	bench -- `bench get-app` or a folder rename without a `bench clear-cache` -- has no entry
	for it, so the sync imports none of the app's DocTypes and still prints 100%. after_install
	then adds Lead fields whose child tables were never created and the install dies with
	"Lead: Options must be a valid DocType for field Email IDs". install_app runs this hook
	before its sync, so rebuilding the map here is in time.
	"""
	from ai_snaplead_connector.brand_rename import refresh_module_map
	from ai_snaplead_connector.upgrade.from_old_package import before_install as rename_old_install

	check_frappe_version()
	refresh_module_map()
	return rename_old_install()


def after_install():
	settle_install_time_migration_state()
	setup_connector_fields()
	ensure_workspace()
	report_duplicate_contacts_preflight()


def has_app_permission():
	"""Whether this user can open what the /apps tile points at.

	Frappe shows the tile from `add_to_apps_screen` and applies the workspace's own `roles`
	separately, and it never checks that the two agree. The workspace is System Manager
	only, so without this every Desk user on the client got a tile that led to a page they
	could not open -- which is what "the app does not show / the workspace was not created"
	actually looked like from the client's side.

	Answered from the workspace itself rather than a hardcoded role, so the two can never
	drift apart again.
	"""
	roles = set(frappe.get_roles())
	if "Administrator" in roles or frappe.session.user == "Administrator":
		return True

	allowed = set(
		frappe.get_all(
			"Has Role",
			filters={"parent": WORKSPACE, "parenttype": "Workspace"},
			pluck="role",
		)
	)
	# A workspace with no roles at all is open to everyone; mirror that rather than
	# locking the tile out of an installation that deliberately widened access.
	return not allowed or bool(roles & allowed)


def ensure_workspace():
	"""Guarantee the workspace exists at install time, not only after a later `bench migrate`.

	Workspaces are synced by the model sync, which a plain `bench install-app` does run --
	but an install that was interrupted, or a site restored from a backup taken mid-install,
	leaves the app installed with no workspace and no error. The client then sees the app
	tile and an empty desk, and nothing says why.

	Idempotent and never fatal: a missing workspace is a navigation problem, and failing the
	install over it would be worse than the problem.
	"""
	try:
		changed = repair_desk()
		if changed:
			frappe.clear_cache()
			frappe.db.commit()
		return changed
	except Exception:
		frappe.logger("ai_snaplead_connector").warning(
			"could not ensure the connector workspace", exc_info=True
		)
		return False


def repair_desk():
	"""Bring the workspace, its sidebar, its Link icon and the app tile to the current names.

	Every step is idempotent and runs in its own savepoint; nothing here commits.
	"""
	path = workspace_json_path()
	changed = bool(_in_savepoint(_rename_legacy_workspace))
	changed = bool(_in_savepoint(repair_legacy_sidebar_labels)) or changed
	if not frappe.db.exists("Workspace", WORKSPACE):
		if not path:
			return changed
		from frappe.modules.import_file import import_file_by_path

		import_file_by_path(path, force=True)
		changed = True

	# The workspace exists -- but Frappe's model sync does NOT reconcile an existing
	# Workspace's child tables, so a `roles` change in the JSON never reaches a site
	# that already has the row. `bench migrate` reports success and changes nothing,
	# and the tile keeps pointing at a page the user still may not open.
	# Each repair runs on its own. They were chained, so the first one to raise -- a
	# UNIQUE collision on a label, say -- was caught by the outer except and the rest
	# never ran at all, on every migrate, without a word in the log beyond one warning.
	for step in (
		_sync_workspace_roles,
		_sync_workspace_label,
		_sync_workspace_header,
		_ensure_sidebar,
		_ensure_desktop_icon,
		_ensure_app_icon,
		_ensure_sidebar_placeholder,
		_add_icon_to_saved_layouts,
	):
		if step in (_sync_workspace_roles, _sync_workspace_label, _sync_workspace_header):
			if not path:
				continue
			args = (path,)
		else:
			args = ()
		changed = bool(_in_savepoint(step, *args)) or changed
	return changed


def _in_savepoint(step, *args):
	"""Run one repair step; on failure roll back THAT step only and carry on.

	A SAVEPOINT, not a plain rollback: this runs inside `bench migrate`, and discarding the
	whole open transaction over a cosmetic label would take the migration's own uncommitted
	work with it.
	"""
	sp = f"vc_{step.__name__}"
	frappe.db.savepoint(sp)
	try:
		return step(*args)
	except Exception:
		frappe.db.rollback(save_point=sp)
		frappe.logger("ai_snaplead_connector").warning(
			f"workspace repair step {step.__name__} failed", exc_info=True
		)
		return False


def workspace_json_path():
	"""The workspace JSON this app ships, found by the name it DECLARES.

	Not by frappe.scrub(WORKSPACE): a folder that was not renamed with the workspace would
	resolve to a path that does not exist, and every repair that reads the file would quietly
	do nothing.
	"""
	import json
	import os

	folder = frappe.get_app_path(APP, "ai_snaplead_connector", "workspace")
	expected = os.path.join(folder, frappe.scrub(WORKSPACE), f"{frappe.scrub(WORKSPACE)}.json")
	candidates = [expected]
	if os.path.isdir(folder):
		candidates += sorted(
			os.path.join(folder, entry, f"{entry}.json")
			for entry in os.listdir(folder)
			if entry != frappe.scrub(WORKSPACE)
		)
	for candidate in candidates:
		if not os.path.isfile(candidate):
			continue
		try:
			# Safe to open: a path inside this app's own workspace folder, never from input
			with open(candidate) as handle:  # nosemgrep
				if json.load(handle).get("name") == WORKSPACE:
					return candidate
		except ValueError:
			continue
	return None


def _sync_workspace_roles(path):
	"""Add roles the JSON declares and the live row lacks. ADDITIVE only.

	A role dropped from the JSON stays: a client may have widened access deliberately, and
	removing it mid-migrate would lock someone out of a page they used that morning.

	Written as child rows rather than doc.save(), because Workspace.on_update exports the
	workspace back to the app folder whenever developer_mode is on -- saving here would
	churn this app's own source JSON on every developer bench during every migrate.
	"""
	import json

	# Safe to open: path is built from frappe.get_app_path and fixed names inside this app, never from input
	with open(path) as handle:  # nosemgrep
		declared = [row["role"] for row in (json.load(handle).get("roles") or []) if row.get("role")]
	if not declared:
		return False

	existing = set(
		frappe.get_all("Has Role", filters={"parent": WORKSPACE, "parenttype": "Workspace"}, pluck="role")
	)
	missing = [role for role in declared if role not in existing and frappe.db.exists("Role", role)]
	if not missing:
		return False

	idx = (
		frappe.db.sql(
			"""select coalesce(max(idx), 0) from `tabHas Role`
			   where parent = %s and parenttype = 'Workspace'""",
			(WORKSPACE,),
		)[0][0]
		or 0
	)
	for role in missing:
		idx += 1
		frappe.get_doc(
			{
				"doctype": "Has Role",
				"parent": WORKSPACE,
				"parenttype": "Workspace",
				"parentfield": "roles",
				"role": role,
				"idx": idx,
			}
		).db_insert()

	frappe.clear_cache()
	return True


def report_duplicate_contacts_preflight():
	"""Say out loud, at install time, which existing Leads already share a contact detail.

	Installing this app puts a duplicate guard on `Lead.validate`, and that guard covers
	phone numbers -- stock ERPNext never deduplicated those. Nothing used to warn a client
	that the rule was about to change, so the first they heard of it was a refused save on a
	Lead they had not edited in months.

	Safe to run BEFORE `install-app`, because nothing here needs the app to be installed on
	the site::

	    bench --site <site> execute ai_snaplead_connector.install.report_duplicate_contacts_preflight

	Never raises: a pre-flight report that fails must not fail the install.
	"""
	try:
		from ai_snaplead_connector.utils.lead_contacts import duplicate_contacts_report

		report = duplicate_contacts_report()
	except Exception:
		frappe.logger("ai_snaplead_connector").warning(
			"duplicate contact pre-flight could not run", exc_info=True
		)
		return None

	emails = report.get("emails") or []
	mobiles = report.get("mobiles") or []
	if not emails and not mobiles:
		message = "Connector pre-flight: no two Leads on this site share an email address or a phone number."
	else:
		message = (
			"Connector pre-flight: {0} email address(es) and {1} phone number(s) are shared by more"
			" than one Lead on this site. Sharing that already exists is left alone -- only a save that"
			" INTRODUCES a shared value is refused -- but these are the rows to look at first: {2}".format(
				len(emails),
				len(mobiles),
				"; ".join(
					"{0} -> {1}".format(entry["value"], ", ".join(entry["leads"]))
					for entry in (emails + mobiles)[:10]
				),
			)
		)

	frappe.logger("ai_snaplead_connector").info(message)
	print(message)
	return report


def after_migrate():
	setup_connector_fields()
	# Also on migrate: a site that was installed before ensure_workspace existed, or whose
	# workspace was deleted by hand, repairs itself on the next migrate instead of needing
	# someone to notice and reinstall.
	ensure_workspace()


def setup_connector_fields():
	"""Idempotent, additive setup only.

	Everything with a schema now lives in ai_snaplead_connector/ai_snaplead_connector/doctype/ as versioned
	JSON and is applied by `bench migrate`'s own model sync. The only thing left to do
	imperatively is attach Custom Fields to DocTypes ERPNext owns.

	Two behaviours that used to live here were removed in this release and must not come
	back -- see README.md:

	  * hide_legacy_lead_contact_fields() set Property Setters that hid and read-only'd the
	    stock Lead.email_id / Lead.mobile_no fields.
	  * migrate_existing_lead_contacts() loaded and re-saved EVERY Lead on the client with
	    ignore_validate, nulling those two stock columns -- on every single migrate.

	A marketplace app does not mutate a stranger's existing records at install time.
	"""
	check_erpnext_installed()
	create_custom_fields()
	ensure_indexes()
	ensure_integration_role()
	frappe.clear_cache()


def settle_install_time_migration_state():
	"""Decide, at install time, which half of the migration this site is on.

	This has to happen here because of the order inside `frappe.installer.install_app`:
	`set_all_patches_as_completed(app)` writes a Patch Log row for every patch in
	patches.txt, and only THEN is the after_install hook called. Patch A (migrate) and
	Patch B (drop) are therefore marked executed on every install without either of them
	having done anything, and `bench migrate` will never call them again.

	Left alone, that has one very visible consequence on a brand new client: nothing ever
	sets `legacy_fields_dropped`, so `create_custom_fields` below takes the "client has not
	migrated yet" branch for ever and re-creates the full historical set -- fourteen Custom
	Fields and four empty sections on Lead, ten more on UTM Source -- on this install and on
	every later migrate, on a site that has not one legacy value to put in them. The end
	state the README promises (ONE Custom Field on Lead) was unreachable without a human
	running two bench commands nothing told them about.

	So:

	* nothing legacy on this site -- a fresh install -- record the end state the two patches
	  would have reached, which for a site with no legacy data is simply "done, nothing to
	  do". `create_custom_fields` then creates `custom_address` alone;
	* something legacy IS here -- a re-install over an older version of this app, the only
	  way an install can meet these fields -- then the two patches have real work to do, and
	  the Patch Log rows install just wrote are a lie. Delete them, so the next
	  `bench migrate` runs the migration properly.

	Deliberately does NOT touch a site that has already finished: `fields_are_dropped()` is
	the end state, and re-deriving it from what is on the DocType would undo a restore.
	"""
	from ai_snaplead_connector.legacy import state

	if not state.available():
		# pre_model_sync on a partially built site; after_migrate will settle it later.
		return

	if state.fields_are_dropped():
		return

	if legacy_fields_present():
		forget_unrun_legacy_patches()
		return

	state.mark_complete(scanned=0, migrated=0, failed=0, conflicts=0)
	state.mark_dropped()
	state.set_values(
		legacy_migration_message=(
			"Fresh install: this site has never had the legacy Custom Fields, so there was"
			" nothing to migrate and nothing to drop. The connector starts at its end state"
			" -- one Custom Field on Lead, custom_address."
		)
	)


def legacy_fields_present():
	"""Does this site carry any of the Custom Fields the two legacy patches deal with?

	Asked of `tabCustom Field` rather than of the meta: this runs during install, where the
	meta for Lead has been rebuilt several times and the question is about what is stored,
	not about what some cache believes.
	"""
	from ai_snaplead_connector.legacy.schema import LEGACY_LEAD_DATA_FIELDS, LEGACY_SOURCE_DATA_FIELDS

	if not frappe.db.table_exists("Custom Field"):
		return False

	if frappe.db.exists("Custom Field", {"dt": "Lead", "fieldname": ("in", list(LEGACY_LEAD_DATA_FIELDS))}):
		return True

	source_doctype = get_source_doctype()
	if not frappe.db.exists("DocType", source_doctype):
		return False

	return bool(
		frappe.db.exists(
			"Custom Field",
			{"dt": source_doctype, "fieldname": ("in", list(LEGACY_SOURCE_DATA_FIELDS))},
		)
	)


def forget_unrun_legacy_patches():
	"""Remove the Patch Log rows install wrote for patches that never ran."""
	if not frappe.db.table_exists("Patch Log"):
		return

	for name in frappe.get_all("Patch Log", filters={"patch": ("in", list(LEGACY_PATCHES))}, pluck="name"):
		frappe.db.delete("Patch Log", {"name": name})


def ensure_integration_role():
	"""Ship the role the README tells a client to hand their integration user.

	Without it the only user that could satisfy `api.v1.sync._check_permissions` was a full
	System Manager, because `connector_lead_link.json` carries exactly one DocPerm row and that is
	it -- and the README's suggested alternative ("Sales User plus write access") did not
	exist and could not have worked, since write is not create.

	A client grants Sales User (or whatever sales role they use, for Lead / Contact /
	Address) plus this role, and nothing else.
	"""
	from frappe.permissions import add_permission, update_permission_property

	if not frappe.db.table_exists("Role"):
		return

	if not frappe.db.exists("Role", INTEGRATION_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": INTEGRATION_ROLE,
				"desk_access": 1,
				"is_custom": 0,
			}
		).insert(ignore_permissions=True)

	for doctype, ptypes in INTEGRATION_ROLE_PERMISSIONS.items():
		if not frappe.db.exists("DocType", doctype):
			continue

		if not frappe.db.get_value(
			"Custom DocPerm", {"parent": doctype, "role": INTEGRATION_ROLE, "permlevel": 0}
		):
			add_permission(doctype, INTEGRATION_ROLE, 0, "read")

		for ptype in ptypes:
			update_permission_property(doctype, INTEGRATION_ROLE, 0, ptype, 1, validate=False)

	frappe.clear_cache()


# Secondary indexes the shipped DocType JSON asks for via "search_index".
# SQLite only materialises those during an ALTER, never on the CREATE TABLE that a fresh
# install performs, so they would silently never exist on the client target. frappe.db.add_index
# is idempotent on every supported backend (and skips its Property Setter while in_install /
# in_migrate), so asserting them here costs nothing on a repeat run.
CONNECTOR_INDEXES = {
	"Connector Lead Link": ["lead", "business_id"],
	"Connector Source Link": ["source", "business_id"],
	"Connector Log": ["request_id", "vc_uuid"],
}


def _column_is_indexed(doctype, fieldname):
	"""Whether ANY index already leads with this column, whatever it is called.

	frappe.db.add_index only looks for an index named `<field>_index`. On MariaDB the model
	sync has already built one named `<field>` from the DocType's search_index flag, so
	add_index created a second, identical index on the same column -- every write to the
	table then maintained both. Checking the column, not the name, avoids that.
	"""
	lookup = getattr(frappe.db, "get_column_index", None)
	if not lookup:
		return False
	table = f"tab{doctype}"
	try:
		return bool(lookup(table, fieldname, unique=False) or lookup(table, fieldname, unique=True))
	except Exception:
		return False


def ensure_indexes():
	for doctype, fieldnames in CONNECTOR_INDEXES.items():
		if not frappe.db.table_exists(doctype):
			continue

		for fieldname in fieldnames:
			if _column_is_indexed(doctype, fieldname):
				continue
			frappe.db.add_index(doctype, [fieldname])


def check_erpnext_installed():
	if "erpnext" not in frappe.get_installed_apps():
		frappe.throw(_("ERPNext app is required before installing Connector app."))


def get_source_doctype():
	# ERPNext v16 dropped Lead Source in favour of UTM Source.
	return "Lead Source" if frappe.db.exists("DocType", "Lead Source") else "UTM Source"


def get_lead_source_fieldname():
	# v16 Lead has utm_source instead of source; anchor custom_business_id next to whichever exists.
	return "source" if frappe.get_meta("Lead").has_field("source") else "utm_source"


def create_custom_fields():
	"""Attach this app's Custom Fields to the DocTypes ERPNext owns.

	Two shapes, and which one a site gets is decided by one flag:

	* before the legacy field migration has finished, the full historical set -- the nine data
	  fields and four layout fields on Lead, the seven and three on Lead Source / UTM Source.
	  A client upgrading mid-flight must still see their data;
	* once ``legacy_fields_dropped`` is set, ``custom_address`` ALONE. That is the target end
	  state: one Custom Field on the client's Lead, the raw scanned address block, which has
	  no standard ERPNext home. Everything else now lives on a Contact, an Address, a note, an
	  attachment, ``utm_source`` or a Connector Lead Link row.

	Without that flag this function would re-create, on the very next ``bench migrate``,
	every field Patch B had just archived and dropped -- empty, and therefore worse than
	before, because the values would be in the archive and the columns back on the form.
	"""
	from frappe.custom.doctype.custom_field.custom_field import (
		create_custom_fields as frappe_create_custom_fields,
	)

	from ai_snaplead_connector.legacy import state
	from ai_snaplead_connector.legacy.definitions import (
		KEPT_LEAD_FIELD_DEFINITION,
		lead_legacy_fields,
		source_legacy_fields,
	)

	lead_fields = [KEPT_LEAD_FIELD_DEFINITION]

	if state.fields_are_dropped():
		frappe_create_custom_fields({"Lead": lead_fields}, update=True)
		return

	source_doctype = get_source_doctype()
	lead_fields = lead_fields + lead_legacy_fields(get_lead_source_fieldname())

	frappe_create_custom_fields(
		{
			"Lead": lead_fields,
			source_doctype: source_legacy_fields(source_doctype == "UTM Source"),
		},
		update=True,
	)


def workspace_is_on_the_desk():
	"""Whether some visible Desktop Icon opens the workspace's sidebar.

	That is the app tile: the workspace's own Link icon is hidden on purpose (the tile opens
	the same page, and a visible one would turn the tile into a folder).
	"""
	return bool(
		frappe.db.exists(
			"Desktop Icon",
			{"link_type": "Workspace Sidebar", "link_to": SIDEBAR, "hidden": 0},
		)
	)


def _ensure_sidebar():
	"""The Workspace Sidebar the tile opens, with the workspace as its FIRST link.

	desktop.js sends a "Workspace Sidebar" tile to the sidebar's first Link entry, so the
	entry for the workspace is put at the top when a client's sidebar lacks it; the rest of
	the client's entries are kept.
	"""
	if not frappe.db.exists("Workspace", WORKSPACE):
		return False

	if not frappe.db.exists("Workspace Sidebar", SIDEBAR):
		# A sidebar frappe built under the workspace's own name: rename it, keeping its entries.
		if frappe.db.exists("Workspace Sidebar", WORKSPACE):
			from ai_snaplead_connector.brand_rename import rename_sidebar

			rename_sidebar(WORKSPACE, SIDEBAR)
		else:
			sidebar = frappe.new_doc("Workspace Sidebar")
			sidebar.title = SIDEBAR
			sidebar.header_icon = frappe.db.get_value("Workspace", WORKSPACE, "icon")
			sidebar.module = frappe.db.get_value("Workspace", WORKSPACE, "module")
			sidebar.append(
				"items", {"label": "Home", "type": "Link", "link_type": "Workspace", "link_to": WORKSPACE}
			)
			sidebar.insert(ignore_permissions=True)
			return True

	items = frappe.get_all(
		"Workspace Sidebar Item",
		filters={"parent": SIDEBAR, "parenttype": "Workspace Sidebar"},
		fields=["name", "type", "link_type", "link_to"],
		order_by="idx asc",
	)
	first_link = next((item for item in items if item.type == "Link"), None)
	if first_link and first_link.link_type == "Workspace" and first_link.link_to == WORKSPACE:
		return False

	existing = next(
		(item for item in items if item.link_type == "Workspace" and item.link_to == WORKSPACE), None
	)
	table = frappe.qb.DocType("Workspace Sidebar Item")
	frappe.qb.update(table).set(table.idx, table.idx + 1).where(
		(table.parent == SIDEBAR) & (table.parenttype == "Workspace Sidebar")
	).run()
	if existing:
		frappe.db.set_value("Workspace Sidebar Item", existing.name, "idx", 0, update_modified=False)
	else:
		frappe.get_doc(
			{
				"doctype": "Workspace Sidebar Item",
				"parent": SIDEBAR,
				"parenttype": "Workspace Sidebar",
				"parentfield": "items",
				"idx": 0,
				"label": "Home",
				"type": "Link",
				"link_type": "Workspace",
				"link_to": WORKSPACE,
			}
		).db_insert()
	return True


def _ensure_desktop_icon():
	"""The workspace's own Link icon: present, HIDDEN, unparented, labelled with its name.

	Present, because frappe's create_desktop_icons_from_workspace (run after every app
	install) otherwise adds one for the workspace -- visible and parented to the app tile.
	desktop.js groups icons by label, so a visible Link icon whose parent_icon equals the
	tile's label turns the tile into a folder ("1 Workspaces") with no link. Hidden, because the
	tile already opens the workspace. The app ships it (desktop_icon/ai_snaplead_connector.json);
	this repairs a site where it went missing or was changed.
	"""
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR):
		return False

	row = frappe.db.get_value(
		"Desktop Icon",
		{"label": WORKSPACE},
		["name", "icon_type", "hidden", "parent_icon", "link_type", "link_to"],
		as_dict=True,
	)
	if row:
		if row.icon_type != "Link":
			return False
		if (
			row.hidden
			and not row.parent_icon
			and row.link_type == "Workspace Sidebar"
			and row.link_to == SIDEBAR
		):
			return False
		frappe.db.set_value(
			"Desktop Icon",
			row.name,
			{"hidden": 1, "parent_icon": None, "link_type": "Workspace Sidebar", "link_to": SIDEBAR},
			update_modified=False,
		)
		_clear_desk_caches()
		return True

	icon = frappe.new_doc("Desktop Icon")
	icon.name = icon.label = WORKSPACE
	icon.icon_type = "Link"
	icon.link_type = "Workspace Sidebar"
	icon.link_to = SIDEBAR
	icon.icon = frappe.db.get_value("Workspace", WORKSPACE, "icon")
	icon.hidden = 1
	icon.standard = 0
	# db_insert: the controller exports a standard icon to this app's folder in developer_mode.
	icon.db_insert()
	_clear_desk_caches()
	return True


def _ensure_app_icon():
	"""The app tile: labelled with the app title, opening the sidebar IN THE SAME TAB.

	link_type "Workspace Sidebar", never "External". desktop.js get_route() turns an External
	tile's relative link into `window.location.origin + link`, and setup_click() sets
	target="_blank" on anything starting with http -- so an External tile opens a new window.
	"""
	entries = frappe.get_hooks("add_to_apps_screen", app_name=APP)
	if not entries:
		return False
	entry = entries[0]
	sidebar_ready = bool(frappe.db.exists("Workspace Sidebar", SIDEBAR))
	link = (
		{"link_type": "Workspace Sidebar", "link_to": SIDEBAR, "link": ""}
		if sidebar_ready
		else {
			"link_type": "External",
			"link_to": None,
			"link": entry.get("route"),
		}
	)

	existing = frappe.db.get_value(
		"Desktop Icon",
		{"icon_type": "App", "app": APP},
		["name", "label", "link_type", "link_to", "link"],
		as_dict=True,
	)
	if existing:
		changes = {}
		# Desktop Icon.label is UNIQUE: never hand it a label another row holds.
		if existing.label != APP_LABEL and not frappe.db.exists("Desktop Icon", {"label": APP_LABEL}):
			changes["label"] = APP_LABEL
		if (existing.link_type, existing.link_to or None, existing.link or "") != (
			link["link_type"],
			link["link_to"],
			link["link"] or "",
		):
			changes.update(link)
		if not changes:
			return False
		frappe.db.set_value("Desktop Icon", existing.name, changes, update_modified=False)
		_clear_desk_caches()
		return True

	if frappe.db.exists("Desktop Icon", {"label": APP_LABEL}):
		return False
	icon = frappe.new_doc("Desktop Icon")
	icon.name = icon.label = APP_LABEL
	icon.icon_type = "App"
	icon.app = APP
	icon.logo_url = entry.get("logo")
	icon.bg_color = "gray"
	icon.standard = 1
	icon.update(link)
	# db_insert: the controller would export this standard icon over the shipped JSON.
	icon.db_insert()
	_clear_desk_caches()
	return True


def _ensure_sidebar_placeholder():
	"""An EMPTY Workspace Sidebar under the workspace's name, so that nothing else takes it.

	The workspace and the module share the name "AI SnapLead Connector", and frappe fills a
	missing sidebar of that name twice over: boot's auto_generate_sidebar_from_module builds one
	for every module without a sidebar, and create_workspace_sidebar_for_workspaces (after every
	app install) builds one for every public workspace without one. The desk's
	set_workspace_sidebar prefers the sidebar NAMED like the workspace, so a visit to
	/desk/ai-snaplead-connector by URL -- a bookmark, the old-route redirect, a breadcrumb --
	showed that generated list under the header "AI SnapLead Connector" instead of the app's
	own sidebar.

	Empty is the point: get_sidebar_items drops a sidebar with no item from the boot payload,
	so the desk falls through to the sidebar that LINKS to the workspace, the app's own. A
	sidebar under this name holding only what frappe generates (the workspace and its
	shortcuts) is emptied; one a client edited is left alone. Plain writes: the controller
	exports and deletes files in developer_mode.
	"""
	if WORKSPACE == SIDEBAR or not frappe.db.exists("Workspace Sidebar", SIDEBAR):
		return False

	row = frappe.db.get_value("Workspace Sidebar", WORKSPACE, ["name", "for_user", "standard"], as_dict=True)
	if not row:
		doc = frappe.new_doc("Workspace Sidebar")
		doc.name = doc.title = WORKSPACE
		doc.standard = 0
		doc.db_insert()
		_clear_desk_caches()
		return True
	if row.for_user or row.standard:
		return False

	items = frappe.get_all(
		"Workspace Sidebar Item",
		filters={"parent": WORKSPACE, "parenttype": "Workspace Sidebar"},
		fields=["type", "link_to"],
	)
	if not items:
		return False
	generated = {WORKSPACE} | set(
		frappe.get_all(
			"Workspace Shortcut", filters={"parent": WORKSPACE, "parenttype": "Workspace"}, pluck="link_to"
		)
	)
	if any(item.type != "Section Break" and item.link_to not in generated for item in items):
		return False

	frappe.db.delete("Workspace Sidebar Item", {"parent": WORKSPACE, "parenttype": "Workspace Sidebar"})
	table = frappe.qb.DocType("Desktop Icon")
	frappe.qb.update(table).set(table.link_to, SIDEBAR).where(
		(table.link_type == "Workspace Sidebar") & (table.link_to == WORKSPACE)
	).run()
	_clear_desk_caches()
	return True


def _clear_desk_caches():
	"""Desktop icons and the sidebar map are cached per user inside bootinfo."""
	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")


def _sync_workspace_header(path):
	"""Rewrite the in-page heading when it still carries an old app name.

	A FOURTH copy of the name: the heading lives inside `Workspace.content`, the editor.js
	block list. The model sync creates a Workspace once and never reconciles an existing
	row's content, so on every already-installed client the page kept announcing itself as
	"VC Connector" under a tile and sidebar that had both been renamed.

	The heading carries the app's label (APP_LABEL), not the workspace's name. Only the header
	block is touched, and only while it still holds a legacy name -- a wholesale overwrite
	would discard any block a client added to their own workspace.
	"""
	import json

	# Safe to open: path is built from frappe.get_app_path and fixed names inside this app, never from input
	with open(path) as handle:  # nosemgrep
		declared = json.load(handle)

	raw = frappe.db.get_value("Workspace", WORKSPACE, "content")
	if not raw:
		return False
	try:
		blocks = json.loads(raw)
	except ValueError:
		return False

	declared_paragraphs = {
		(b.get("data") or {}).get("text")
		for b in json.loads(declared.get("content") or "[]")
		if b.get("type") == "paragraph"
	}

	changed = False
	for block in blocks:
		data = block.get("data") or {}
		text = data.get("text") or ""
		if block.get("type") == "header":
			legacy = next((name for name in LEGACY_APP_LABELS if name in text), None)
			if legacy and APP_LABEL not in text:
				data["text"] = text.replace(legacy, APP_LABEL)
				changed = True
			continue

		# Body copy names the product too ("your VC Scan account"). It is replaced only by a
		# paragraph the JSON actually declares, and only while the live text differs, so a
		# client who rewrote the blurb keeps their wording.
		if block.get("type") == "paragraph" and any(name in text for name in LEGACY_PRODUCT_NAMES):
			replacement = next(
				(d for d in declared_paragraphs if d and d[:24] == text[:24] and d != text), None
			)
			if replacement:
				data["text"] = replacement
				changed = True

	if changed:
		frappe.db.set_value("Workspace", WORKSPACE, "content", json.dumps(blocks), update_modified=False)
		frappe.clear_cache()
	return changed


def _sync_workspace_label(path):
	"""Push the JSON's label/title onto the workspace, and the app's label onto its sidebar.

	The model sync creates a Workspace once and then leaves it alone, so renaming the app
	changed nothing on any site that was already installed -- which is every existing
	client. The `name` itself is moved by brand_rename.rename_workspace.
	"""
	import json

	# Safe to open: path is built from frappe.get_app_path and fixed names inside this app, never from input
	with open(path) as handle:  # nosemgrep
		declared = json.load(handle)
	label = declared.get("label") or declared.get("title")
	if not label:
		return False

	current = frappe.db.get_value("Workspace", WORKSPACE, ["label", "title"], as_dict=True)
	if not current:
		return False

	# Each place is checked on its own. Gating them behind the Workspace row meant that once
	# IT matched the function returned early and the rest kept the old name for ever.
	changed = False
	if current.label != label or current.title != label:
		frappe.db.set_value("Workspace", WORKSPACE, {"label": label, "title": label}, update_modified=False)
		changed = True

	# The SIDEBAR's header reads its own copy: `Workspace Sidebar` is its own doctype, created
	# once and never revisited.
	if (
		frappe.db.exists("Workspace Sidebar", SIDEBAR)
		and frappe.db.get_value("Workspace Sidebar", SIDEBAR, "title") != SIDEBAR
	):
		frappe.db.set_value("Workspace Sidebar", SIDEBAR, "title", SIDEBAR, update_modified=False)
		changed = True

	if changed:
		frappe.clear_cache()
	return changed


def _add_icon_to_saved_layouts():
	"""Put this app's icon into every saved Desktop Layout.

	The desktop does NOT render frappe.boot.desktop_icons when the user has a saved layout:
	desktop.js does `frappe.desktop_icons = this.data` and only falls back to boot when that
	is empty. `Desktop Layout` stores that per user as frozen JSON, captured the last time
	the desk was arranged -- so an icon created by a later release is in the boot payload and
	still never appears.

	Appending keeps the arrangement the user chose. A stored entry for the tile is REPLACED,
	never skipped: it keeps whatever link_type and link it was stored with (an External
	/desk/ai-snaplead---connector, say), which is how a tile kept opening a new tab long after
	the icon itself was fixed.
	"""
	import json

	icon = frappe.db.get_value(
		"Desktop Icon",
		{"icon_type": "App", "app": APP},
		[
			"name",
			"label",
			"app",
			"icon_type",
			"link_type",
			"link",
			"link_to",
			"logo_url",
			"icon",
			"icon_image",
			"bg_color",
			"idx",
			"hidden",
			"standard",
		],
		as_dict=True,
	)
	if not icon:
		return 0

	stale_labels = {icon.label, *LEGACY_APP_LABELS}
	updated = 0
	for row in frappe.get_all("Desktop Layout", fields=["name", "layout"]):
		try:
			layout = json.loads(row.layout or "[]")
		except ValueError:
			continue
		if not isinstance(layout, list):
			continue

		changed = False
		found = False
		for i, entry in enumerate(layout):
			if not isinstance(entry, dict) or entry.get("label") not in stale_labels:
				continue
			if found:
				continue
			found = True
			merged = dict(icon)
			merged["child_icons"] = entry.get("child_icons") or []
			if merged != entry:
				layout[i] = merged
				changed = True

		if not found:
			fresh = dict(icon)
			fresh["child_icons"] = []
			layout.append(fresh)
			changed = True

		if changed:
			frappe.db.set_value(
				"Desktop Layout", row.name, "layout", json.dumps(layout), update_modified=False
			)
			updated += 1

	if updated:
		_clear_desk_caches()
	return updated


#: What the mobile product was called in body copy before it became "AI SnapLead".
LEGACY_PRODUCT_NAMES = ("AI Scan CRM", "VC Scan", "Sigzen Scan", "SigzenScan")


def _rename_legacy_workspace():
	"""Move a client's existing workspace, sidebar and Link icon onto the current names, once.

	brand_rename.py does the same before the model sync; this is the install-time and
	belt-and-braces copy.
	"""
	from ai_snaplead_connector import brand_rename

	renamed = False
	for legacy in LEGACY_WORKSPACES:
		renamed = bool(brand_rename.rename_workspace(legacy, WORKSPACE)) or renamed
		renamed = bool(brand_rename.rename_sidebar(legacy, SIDEBAR)) or renamed
		brand_rename.repoint_icon_links(legacy)
	renamed = bool(brand_rename.retire_legacy_link_icons()) or renamed
	if renamed:
		frappe.clear_cache()
	return renamed


def repair_legacy_sidebar_labels():
	"""Sidebar entries that still SAY an old workspace name are called "Home".

	`frappe.rename_doc` rewrites Link fields, so `Workspace Sidebar Item.link_to` followed
	the workspace onto its new name -- but `label` is plain Data and nobody rewrites it. A
	client that had the workspace before the rename therefore lands on the right page under
	a sidebar entry still reading "VC Connector".

	Core names a workspace's own entry "Home", which is what a freshly built sidebar gets,
	so that is the name used here too. Idempotent, and deliberately NOT gated on the rename
	having just happened: clients renamed by an earlier release still carry the stale label.
	"""
	stale = frappe.get_all(
		"Workspace Sidebar Item",
		filters={"label": ("in", LEGACY_WORKSPACES), "link_to": WORKSPACE},
		pluck="name",
	)
	for name in stale:
		frappe.db.set_value("Workspace Sidebar Item", name, "label", "Home", update_modified=False)
	if stale:
		frappe.clear_cache()
	return len(stale)
