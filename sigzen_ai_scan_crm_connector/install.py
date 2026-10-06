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
	"sigzen_ai_scan_crm_connector.patches.v1_0.migrate_legacy_lead_fields",
	"sigzen_ai_scan_crm_connector.patches.v1_0.drop_legacy_lead_fields",
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


#: The workspace name, which MUST equal its title: desktop.js builds the tile's route
#: from the workspace TITLE but looks the workspace up by slug(NAME), so any difference
#: produces a URL that resolves to nothing and drops the user back on the desktop.
WORKSPACE = "Sigzen - AI Scan CRM Connector"


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
		path = frappe.get_app_path(
			"sigzen_ai_scan_crm_connector", "sigzen___ai_scan_crm_connector", "workspace",
			"sigzen___ai_scan_crm_connector", "sigzen___ai_scan_crm_connector.json"
		)
		_rename_legacy_workspace()
		repair_legacy_sidebar_labels()
		if not frappe.db.exists("Workspace", WORKSPACE):
			from frappe.modules.import_file import import_file_by_path

			import_file_by_path(path, force=True)
			_ensure_desktop_icon()
			frappe.db.commit()
			return True

		# The workspace exists -- but Frappe's model sync does NOT reconcile an existing
		# Workspace's child tables, so a `roles` change in the JSON never reaches a site
		# that already has the row. `bench migrate` reports success and changes nothing,
		# and the tile keeps pointing at a page the user still may not open.
		# Each repair runs on its own. They were chained, so the first one to raise -- a
		# UNIQUE collision on a label, say -- was caught by the outer except and the rest
		# never ran at all, on every migrate, without a word in the log beyond one warning.
		changed = False
		for step in (
			_sync_workspace_roles,
			_sync_workspace_label,
			_sync_workspace_header,
			_ensure_desktop_icon,
			_parent_icon_to_app,
			_add_icon_to_saved_layouts,
		):
			# A SAVEPOINT, not a plain rollback: this runs inside `bench migrate`, and
			# discarding the whole open transaction over a cosmetic label would take the
			# migration's own uncommitted work with it.
			sp = f"vc_{step.__name__}"
			frappe.db.savepoint(sp)
			try:
				changed = bool(step(path) if step in (
					_sync_workspace_roles, _sync_workspace_label, _sync_workspace_header
				) else step()) or changed
			except Exception:
				frappe.db.rollback(save_point=sp)
				frappe.logger("sigzen_ai_scan_crm_connector").warning(
					f"workspace repair step {step.__name__} failed", exc_info=True
				)
		if changed:
			frappe.db.commit()
		return changed
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not ensure the connector workspace", exc_info=True)
		return False


def _sync_workspace_roles(path):
	"""Add roles the JSON declares and the live row lacks. ADDITIVE only.

	A role dropped from the JSON stays: a client may have widened access deliberately, and
	removing it mid-migrate would lock someone out of a page they used that morning.

	Written as child rows rather than doc.save(), because Workspace.on_update exports the
	workspace back to the app folder whenever developer_mode is on -- saving here would
	churn this app's own source JSON on every developer bench during every migrate.
	"""
	import json

	with open(path) as handle:  # nosemgrep - path is built from frappe.get_app_path and fixed names inside this app, never from input
		declared = [row["role"] for row in (json.load(handle).get("roles") or []) if row.get("role")]
	if not declared:
		return False

	existing = set(
		frappe.get_all(
			"Has Role", filters={"parent": WORKSPACE, "parenttype": "Workspace"}, pluck="role"
		)
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

	    bench --site <site> execute sigzen_ai_scan_crm_connector.install.report_duplicate_contacts_preflight

	Never raises: a pre-flight report that fails must not fail the install.
	"""
	try:
		from sigzen_ai_scan_crm_connector.utils.lead_contacts import duplicate_contacts_report

		report = duplicate_contacts_report()
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("duplicate contact pre-flight could not run", exc_info=True)
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

	frappe.logger("sigzen_ai_scan_crm_connector").info(message)
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

	Everything with a schema now lives in sigzen_ai_scan_crm_connector/sigzen___ai_scan_crm_connector/doctype/ as versioned
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
	from sigzen_ai_scan_crm_connector.legacy import state

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
	from sigzen_ai_scan_crm_connector.legacy.schema import LEGACY_LEAD_DATA_FIELDS, LEGACY_SOURCE_DATA_FIELDS

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

	from sigzen_ai_scan_crm_connector.legacy import state
	from sigzen_ai_scan_crm_connector.legacy.definitions import (
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
	"""Whether some Desktop Icon already opens the workspace.

	Two shapes count. An older install has the workspace's own Link icon. A FRESH install
	has only the app tile: the app title and the workspace share one name, Desktop Icon.label
	is UNIQUE, so frappe cannot create a second icon under that label and the tile is the one
	that opens the workspace. Looking only for the Link icon made every migrate on a fresh
	site try to create it again, fail on the label, and report that it had changed something.
	"""
	if frappe.db.exists("Desktop Icon", {"link_to": WORKSPACE, "icon_type": "Link"}):
		return True
	return bool(
		frappe.db.exists(
			"Desktop Icon",
			{
				"app": "sigzen_ai_scan_crm_connector",
				"icon_type": "App",
				"link_type": "Workspace Sidebar",
				"link_to": WORKSPACE,
				"hidden": 0,
			},
		)
	)


def _ensure_desktop_icon():
	"""A public workspace with no Desktop Icon row is simply not on the client's desk.

	Desktop Icon records are created ONCE, by after_app_install, so a client who installed
	this app before the workspace existed -- or whose icon pass died -- has the workspace in
	the sidebar and nothing on the desk, with no error to explain the difference.

	The icon pass dies easily: create_desktop_icons() reads app_details[0]["logo"] with no
	default and runs that pass before the workspace one, so any installed app lacking a
	"logo" in add_to_apps_screen takes out desktop icons for the ENTIRE site, inside a
	caller whose except block only print()s. This app now declares a logo; this repairs the
	clients who installed it while it did not.

	Idempotent, mirrors frappe's own add_workspace_to_desktop().
	"""
	if workspace_is_on_the_desk():
		return False

	row = frappe.db.get_value("Workspace", WORKSPACE, ["icon", "public"], as_dict=True)
	if not row or not row.public:
		return False

	# Delegate to frappe's own helper rather than inserting the row here. Desktop Icon's
	# link_to is validated against WORKSPACE SIDEBAR, not Workspace, so a hand-rolled insert
	# dies with "Could not find Link To" on any site that has no sidebar record for the
	# workspace yet -- which is every client that installed before the sidebar existed.
	# add_workspace_to_desktop() creates the sidebar first, then the icon.
	from frappe.desk.doctype.desktop_icon.desktop_icon import add_workspace_to_desktop

	add_workspace_to_desktop(WORKSPACE)
	if row.icon:
		frappe.db.set_value(
			"Desktop Icon",
			{"link_to": WORKSPACE, "icon_type": "Link"},
			"icon",
			row.icon,
			update_modified=False,
		)
	return True


def _sync_workspace_header(path):
	"""Rewrite the in-page heading when it still carries the old app name.

	A FOURTH copy of the name: the heading lives inside `Workspace.content`, the editor.js
	block list. The model sync creates a Workspace once and never reconciles an existing
	row's content, so on every already-installed client the page kept announcing itself as
	"VC Connector" under a tile and sidebar that had both been renamed.

	Only the header block is touched, and only while it still holds the legacy text -- a
	wholesale overwrite would discard any block a client added to their own workspace.
	"""
	import json

	with open(path) as handle:  # nosemgrep - path is built from frappe.get_app_path and fixed names inside this app, never from input
		declared = json.load(handle)
	label = declared.get("label") or declared.get("title")
	if not label:
		return False

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
			legacy = next((name for name in LEGACY_WORKSPACES if name in text), None)
			if legacy and label not in text:
				data["text"] = text.replace(legacy, label)
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
		frappe.db.set_value(
			"Workspace", WORKSPACE, "content", json.dumps(blocks), update_modified=False
		)
		frappe.clear_cache()
	return changed


def _sync_workspace_label(path):
	"""Push the JSON's label/title onto a workspace a site already has.

	The model sync creates a Workspace once and then leaves it alone, so renaming the app
	changed nothing on any site that was already installed -- which is every existing
	client. The sidebar entry and the Desk tile kept the old name while the /apps listing
	showed the new one.

	Only label and title move. `name` is the primary key AND the route: the Desk builds
	frappe.workspaces keyed by slug(page.name), so renaming it would break every link to
	this workspace and the tile that points at it.
	"""
	import json

	with open(path) as handle:  # nosemgrep - path is built from frappe.get_app_path and fixed names inside this app, never from input
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
		frappe.db.set_value(
			"Workspace", WORKSPACE, {"label": label, "title": label}, update_modified=False
		)
		changed = True

	# The Desk tile carries its own copy of the name. `tabDesktop Icon.label` is UNIQUE, so
	# this must never hand a second row a label another row already holds -- the IntegrityError
	# was escaping into ensure_workspace's except and silently abandoning every repair after
	# this one, which is why the tile kept its old routing however often migrate ran.
	holder = frappe.db.get_value("Desktop Icon", {"label": label}, "name")
	icon = frappe.db.exists(
		"Desktop Icon", {"link_to": WORKSPACE, "icon_type": "Link", "hidden": 0}
	)
	if (
		icon
		and not holder
		and frappe.db.get_value("Desktop Icon", icon, "label") not in (label, WORKSPACE)
	):
		frappe.db.set_value("Desktop Icon", icon, "label", label, update_modified=False)
		changed = True

	# And the SIDEBAR reads a third copy: `Workspace Sidebar` is its own doctype, created
	# once and never revisited, so a rename left the sidebar -- where a client actually
	# looks -- still showing the old name.
	if (
		frappe.db.exists("Workspace Sidebar", WORKSPACE)
		and frappe.db.get_value("Workspace Sidebar", WORKSPACE, "title") != label
	):
		frappe.db.set_value("Workspace Sidebar", WORKSPACE, "title", label, update_modified=False)
		changed = True

	if changed:
		frappe.clear_cache()
	return changed


def _ensure_app_icon():
	"""The App-type Desktop Icon this app's workspace icon hangs under.

	The desktop groups Link icons beneath their app's App icon. Without one the workspace
	icon exists with hidden=0 and never renders -- ERPNext's "Assets" carries
	parent_icon="ERPNext", ours carried nothing, so a client saw every other app on their
	desk and not this one.

	frappe builds these in create_desktop_icons_from_installed_apps(), which reads
	app_details[0]["logo"] with no default; while this app declared no logo that pass died
	on KeyError before creating anything. The logo is declared now.
	"""
	entries = frappe.get_hooks("add_to_apps_screen", app_name="sigzen_ai_scan_crm_connector")
	if not entries:
		return None
	entry = entries[0]
	label = frappe.get_hooks("app_title", app_name="sigzen_ai_scan_crm_connector")[0]

	existing = frappe.db.exists("Desktop Icon", {"icon_type": "App", "app": "sigzen_ai_scan_crm_connector"})
	if existing:
		if frappe.db.get_value("Desktop Icon", existing, "label") != label:
			frappe.db.set_value("Desktop Icon", existing, "label", label, update_modified=False)
		if frappe.db.exists("Workspace Sidebar", label):
			row = frappe.db.get_value("Desktop Icon", existing, ["link_type", "link_to"], as_dict=True)
			if row.link_type != "Workspace Sidebar" or row.link_to != label:
				frappe.db.set_value(
					"Desktop Icon",
					existing,
					{"link_type": "Workspace Sidebar", "link_to": label, "link": ""},
					update_modified=False,
				)
				frappe.clear_cache()
		return existing

	# Desktop Icon.label carries a UNIQUE index, and this app's title is the same string as
	# its workspace's label -- so the App icon cannot be created while the workspace's own
	# Link icon holds that label. frappe hits the same case in
	# create_desktop_icons_from_workspace and resolves it by HIDING the child when a desk app
	# has one workspace named after itself: the App tile links straight to the workspace
	# route, so showing both would be the same name twice on one desk.
	clash = frappe.db.exists("Desktop Icon", {"label": label, "icon_type": "Link"})
	if clash:
		# Hiding alone is not enough: the UNIQUE index is on `label` whatever `hidden` says,
		# so the row has to stop HOLDING the string before the App icon can take it. The
		# child keeps the workspace's own name and is hidden, because the App tile already
		# links to /desk/vc-connector and two tiles with one name on a desk helps nobody.
		frappe.db.set_value(
			"Desktop Icon", clash, {"label": WORKSPACE, "hidden": 1}, update_modified=False
		)

	icon = frappe.new_doc("Desktop Icon")
	icon.label = label
	icon.icon_type = "App"
	icon.app = "sigzen_ai_scan_crm_connector"
	icon.logo_url = entry.get("logo")
	icon.standard = 1
	_point_app_icon(icon, entry, label)
	icon.insert(ignore_permissions=True, ignore_if_duplicate=True)
	return icon.name


def _parent_icon_to_app():
	"""Hang the workspace icon under the app icon, so the client's desktop shows it."""
	try:
		app_icon = _ensure_app_icon()
		if not app_icon:
			return False
		row = frappe.db.get_value(
			"Desktop Icon",
			{"link_to": WORKSPACE, "icon_type": "Link"},
			["name", "parent_icon", "hidden"],
			as_dict=True,
		)
		if not row or row.hidden or row.parent_icon == app_icon:
			return False
		frappe.db.set_value("Desktop Icon", row.name, "parent_icon", app_icon, update_modified=False)
		frappe.clear_cache()
		return True
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not parent the connector desktop icon", exc_info=True)
		return False


def _add_icon_to_saved_layouts():
	"""Put this app's icon into every saved Desktop Layout.

	The desktop does NOT render frappe.boot.desktop_icons when the user has a saved layout:
	desktop.js does `frappe.desktop_icons = this.data` and only falls back to boot when that
	is empty. `Desktop Layout` stores that per user as frozen JSON, captured the last time
	the desk was arranged -- so an icon created by a later release is in the boot payload and
	still never appears. On this bench the layout held 36 entries and the connector was not
	one of them.

	Appending keeps the arrangement the user chose. Wiping the row would work too and is
	what "just reset it" would mean, but it would throw away every deliberate placement on
	the site to deliver one new icon.
	"""
	try:
		import json

		icon = frappe.db.get_value(
			"Desktop Icon",
			{"icon_type": "App", "app": "sigzen_ai_scan_crm_connector"},
			["name", "label", "app", "icon_type", "link_type", "link", "link_to", "logo_url",
			 "icon", "icon_image", "bg_color", "idx", "hidden", "standard"],
			as_dict=True,
		)
		if not icon:
			return 0

		updated = 0
		for row in frappe.get_all("Desktop Layout", fields=["name", "layout"]):
			try:
				layout = json.loads(row.layout or "[]")
			except Exception:
				continue
			if not isinstance(layout, list):
				continue

			fresh = dict(icon)
			changed = False
			found = False
			for i, entry in enumerate(layout):
				if (entry or {}).get("label") != icon.label:
					continue
				found = True
				# REPLACE, never skip. The saved layout is what the desktop renders, so a
				# stale entry keeps whatever link_type it was stored with -- which is how
				# the tile kept opening a new tab long after the icon itself was fixed.
				merged = dict(fresh)
				merged["child_icons"] = entry.get("child_icons") or []
				if merged != entry:
					layout[i] = merged
					changed = True
				break

			if not found:
				fresh["child_icons"] = []
				layout.append(fresh)
				changed = True

			if changed:
				frappe.db.set_value(
					"Desktop Layout", row.name, "layout", json.dumps(layout), update_modified=False
				)
				updated += 1

		if updated:
			frappe.clear_cache()
		return updated
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not add the icon to saved layouts", exc_info=True)
		return 0


def _point_app_icon(icon, entry, label):
	"""In-tab navigation, which only works when the sidebar key matches the tile label.

	desktop.js resolves a "Workspace Sidebar" icon through
	frappe.boot.workspace_sidebar_item[icon.label.toLowerCase()] -- keyed by the sidebar's
	NAME -- and then builds the URL from the workspace's TITLE while looking the workspace up
	by slug(NAME). All three have to agree, which is why the workspace is named after the
	app rather than carrying a separate display label.

	The alternative, "External", is what opened a new window: get_route() turns a relative
	link into `window.location.origin + link` and setup_click() sets target="_blank" on
	anything starting with http.
	"""
	if frappe.db.exists("Workspace Sidebar", label):
		icon.link_type = "Workspace Sidebar"
		icon.link_to = label
		icon.link = ""
	else:
		icon.link_type = "External"
		icon.link = entry.get("route")


def _parent_icon_to_app():
	"""Hang the workspace icon under the app icon, so the client's desktop shows it."""
	try:
		app_icon = _ensure_app_icon()
		if not app_icon:
			return False
		row = frappe.db.get_value(
			"Desktop Icon",
			{"link_to": WORKSPACE, "icon_type": "Link"},
			["name", "parent_icon", "hidden"],
			as_dict=True,
		)
		if not row or row.hidden or row.parent_icon == app_icon:
			return False
		frappe.db.set_value("Desktop Icon", row.name, "parent_icon", app_icon, update_modified=False)
		frappe.clear_cache()
		return True
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not parent the connector desktop icon", exc_info=True)
		return False


def _add_icon_to_saved_layouts():
	"""Put this app's icon into every saved Desktop Layout.

	The desktop does NOT render frappe.boot.desktop_icons when the user has a saved layout:
	desktop.js does `frappe.desktop_icons = this.data` and only falls back to boot when that
	is empty. `Desktop Layout` stores that per user as frozen JSON, captured the last time
	the desk was arranged -- so an icon created by a later release is in the boot payload and
	still never appears. On this bench the layout held 36 entries and the connector was not
	one of them.

	Appending keeps the arrangement the user chose. Wiping the row would work too and is
	what "just reset it" would mean, but it would throw away every deliberate placement on
	the site to deliver one new icon.
	"""
	try:
		import json

		icon = frappe.db.get_value(
			"Desktop Icon",
			{"icon_type": "App", "app": "sigzen_ai_scan_crm_connector"},
			["name", "label", "app", "icon_type", "link_type", "link", "link_to", "logo_url",
			 "icon", "icon_image", "bg_color", "idx", "hidden", "standard"],
			as_dict=True,
		)
		if not icon:
			return 0

		updated = 0
		for row in frappe.get_all("Desktop Layout", fields=["name", "layout"]):
			try:
				layout = json.loads(row.layout or "[]")
			except Exception:
				continue
			if not isinstance(layout, list):
				continue

			fresh = dict(icon)
			changed = False
			found = False
			for i, entry in enumerate(layout):
				if (entry or {}).get("label") != icon.label:
					continue
				found = True
				# REPLACE, never skip. The saved layout is what the desktop renders, so a
				# stale entry keeps whatever link_type it was stored with -- which is how
				# the tile kept opening a new tab long after the icon itself was fixed.
				merged = dict(fresh)
				merged["child_icons"] = entry.get("child_icons") or []
				if merged != entry:
					layout[i] = merged
					changed = True
				break

			if not found:
				fresh["child_icons"] = []
				layout.append(fresh)
				changed = True

			if changed:
				frappe.db.set_value(
					"Desktop Layout", row.name, "layout", json.dumps(layout), update_modified=False
				)
				updated += 1

		if updated:
			frappe.clear_cache()
		return updated
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not add the icon to saved layouts", exc_info=True)
		return 0


def _point_app_icon(icon, entry, label):
	"""Aim the tile at the app's workspace.

	link_type stays "External". The alternative was tried end to end and is worse:
	desktop.js get_route() resolves a "Workspace Sidebar" icon through
	frappe.boot.workspace_sidebar_item[icon.label.toLowerCase()] and then builds the URL
	from the SIDEBAR's name -- so with the tile labelled by app title, the click went to
	/desk/sigzen-scan-to-crm-erpnext-connector, which matches no workspace (frappe.workspaces
	is keyed by slug(workspace.name) = "vc-connector") and rendered the desktop again.

	Same tab, wrong page is worse than new tab, right page. The new tab itself is stock:
	get_route() turns a relative link into `window.location.origin + link` and setup_click()
	sets target="_blank" on anything starting with http, which is why EVERY app tile behaves
	this way, ERPNext's included. Making ours differ needs the workspace itself renamed so
	its slug matches the tile, which would change the route every existing client uses.
	"""
	icon.link_type = "External"
	icon.link = entry.get("route")


def _parent_icon_to_app():
	"""Hang the workspace icon under the app icon, so the client's desktop shows it."""
	try:
		app_icon = _ensure_app_icon()
		if not app_icon:
			return False
		row = frappe.db.get_value(
			"Desktop Icon",
			{"link_to": WORKSPACE, "icon_type": "Link"},
			["name", "parent_icon", "hidden"],
			as_dict=True,
		)
		if not row or row.hidden or row.parent_icon == app_icon:
			return False
		frappe.db.set_value("Desktop Icon", row.name, "parent_icon", app_icon, update_modified=False)
		frappe.clear_cache()
		return True
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not parent the connector desktop icon", exc_info=True)
		return False


def _add_icon_to_saved_layouts():
	"""Put this app's icon into every saved Desktop Layout.

	The desktop does NOT render frappe.boot.desktop_icons when the user has a saved layout:
	desktop.js does `frappe.desktop_icons = this.data` and only falls back to boot when that
	is empty. `Desktop Layout` stores that per user as frozen JSON, captured the last time
	the desk was arranged -- so an icon created by a later release is in the boot payload and
	still never appears. On this bench the layout held 36 entries and the connector was not
	one of them.

	Appending keeps the arrangement the user chose. Wiping the row would work too and is
	what "just reset it" would mean, but it would throw away every deliberate placement on
	the site to deliver one new icon.
	"""
	try:
		import json

		icon = frappe.db.get_value(
			"Desktop Icon",
			{"icon_type": "App", "app": "sigzen_ai_scan_crm_connector"},
			["name", "label", "app", "icon_type", "link_type", "link", "link_to", "logo_url",
			 "icon", "icon_image", "bg_color", "idx", "hidden", "standard"],
			as_dict=True,
		)
		if not icon:
			return 0

		updated = 0
		for row in frappe.get_all("Desktop Layout", fields=["name", "layout"]):
			try:
				layout = json.loads(row.layout or "[]")
			except Exception:
				continue
			if not isinstance(layout, list):
				continue

			fresh = dict(icon)
			changed = False
			found = False
			for i, entry in enumerate(layout):
				if (entry or {}).get("label") != icon.label:
					continue
				found = True
				# REPLACE, never skip. The saved layout is what the desktop renders, so a
				# stale entry keeps whatever link_type it was stored with -- which is how
				# the tile kept opening a new tab long after the icon itself was fixed.
				merged = dict(fresh)
				merged["child_icons"] = entry.get("child_icons") or []
				if merged != entry:
					layout[i] = merged
					changed = True
				break

			if not found:
				fresh["child_icons"] = []
				layout.append(fresh)
				changed = True

			if changed:
				frappe.db.set_value(
					"Desktop Layout", row.name, "layout", json.dumps(layout), update_modified=False
				)
				updated += 1

		if updated:
			frappe.clear_cache()
		return updated
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not add the icon to saved layouts", exc_info=True)
		return 0


def _sidebar_alias_for(label):
	"""A Workspace Sidebar record NAMED after the app tile, so the tile can navigate in-tab.

	desktop.js get_route() resolves a "Workspace Sidebar" icon through
	frappe.boot.workspace_sidebar_item[icon.label.toLowerCase()] -- keyed by the SIDEBAR's
	name. Our tile is labelled with the app title while the sidebar is named after the
	workspace, so the lookup missed and the tile refused to navigate at all.

	The alternative, "External", is what makes it open a NEW TAB: get_route() turns a
	relative link into `window.location.origin + link`, and setup_click() sets
	target="_blank" on anything starting with http. That is why every app tile spawns a
	window, ERPNext's included.

	So this adds a second sidebar record under the tile's own name, pointing at the same
	workspace. Additive: the workspace's own sidebar is untouched.
	"""
	if label == WORKSPACE:
		return WORKSPACE
	if frappe.db.exists("Workspace Sidebar", label):
		return label

	# The boot map is keyed by the sidebar's NAME, but this app's sidebar is named after the
	# workspace ("VC Connector") while the tile is labelled with the app title. A second
	# record cannot carry that title either -- Workspace Sidebar.title is UNIQUE and the
	# existing row already holds it. So the record is RENAMED: frappe.rename_doc repoints
	# the Link fields that reference it, and the sidebar itself is unchanged apart from its
	# key.
	existing = frappe.db.get_value("Workspace Sidebar", {"title": label}, "name")
	if existing and existing != label:
		try:
			frappe.rename_doc("Workspace Sidebar", existing, label, force=True, show_alert=False)
			frappe.clear_cache()
			return label
		except Exception:
			frappe.logger("sigzen_ai_scan_crm_connector").warning("could not rename the sidebar", exc_info=True)
			return None
	return existing or None


def _point_app_icon(icon, entry, label):
	"""Navigate in-tab when we can, and fall back to the stock URL when we cannot."""
	alias = _sidebar_alias_for(label)
	if alias:
		icon.link_type = "Workspace Sidebar"
		icon.link_to = alias
		icon.link = ""
	else:
		icon.link_type = "External"
		icon.link = entry.get("route")


#: Every name the workspace has carried before this one, newest first. Existing clients
#: carry one of them, and importing the new JSON beside it would leave them with two
#: workspaces and two tiles, so the old row is RENAMED -- which also carries its links,
#: charts and shortcuts. (brand_rename.py does the same before the model sync; this is the
#: install-time and belt-and-braces copy.)
LEGACY_WORKSPACES = ("Sigzen Scan to CRM-ERPNext Connector", "VC Connector")
#: What the mobile product was called in body copy before it became "AI Scan CRM".
LEGACY_PRODUCT_NAMES = ("VC Scan", "Sigzen Scan", "SigzenScan")


def _rename_legacy_workspace():
	"""Move a client's existing workspace onto the new name, once."""
	try:
		from sigzen_ai_scan_crm_connector import brand_rename

		renamed = False
		for legacy in LEGACY_WORKSPACES:
			renamed = bool(brand_rename.rename_workspace(legacy, WORKSPACE)) or renamed
		if renamed:
			frappe.clear_cache()
		return renamed
	except Exception:
		frappe.logger("sigzen_ai_scan_crm_connector").warning("could not rename the legacy workspace", exc_info=True)
		return False


def repair_legacy_sidebar_labels():
	"""Repoint sidebar entries that still SAY the old workspace name.

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
