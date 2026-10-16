# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Move a site that installed this app under an older package name onto `ai_snaplead_connector`.

The package has carried three names:

    vc_connector                    development builds (before the first release)
    sigzen_ai_scan_crm_connector    release 1.0.0, when the product was "Sigzen - AI Scan CRM"
    ai_snaplead_connector           now, the product is "AI SnapLead"

A site that installs the app fresh from the Marketplace never runs any of this.

NOTHING HERE IS RUN BY HAND. The upgrade happens inside the standard bench commands:

    bench get-app <repository-url> --branch master
    bench --site all migrate
    bench remove-app sigzen_ai_scan_crm_connector      (only if bench still lists the old folder)

HOW `bench migrate` REACHES IT

A site keeps its installed apps in the database, and `bench migrate` imports `<app>.hooks` for
every name on that list before it does anything else. So this repository ships a tiny stand-in
package for each old name next to the real one (vc_connector/ and sigzen_ai_scan_crm_connector/).
They have no features; the only hook of each is `before_migrate` -> upgrade_during_migrate()
below, which renames the site and then lets the migrate carry on under the new name. They also
make the old names importable again, which is what stops `bench build` failing with "expected
str ... not NoneType" on a bench whose old app folder still holds leftovers of the old package.

`bench --site <site> install-app ai_snaplead_connector` on such a site does the same rename
(before_install below) instead of installing a second copy.

WHAT IS RENAMED

    installed apps list       tabDefaultValue, defkey = installed_apps
    Installed Application     the row frappe shows under "Installed Applications"
    Module Def.app_name       which package a module's code is loaded from
    Patch Log.patch           so patches that already ran are not run a second time
    Scheduled Job Type        a `method` under the old package
    app columns               Desktop Icon / Workspace / Workspace Sidebar / Sidebar Item Group /
                              Website Theme Ignore App `app`, User / System Settings `default_app`,
                              User Invitation / Changelog Feed `app_name`
    asset URLs                /assets/<old>/... in Desktop Icon logos, saved Desktop Layouts,
                              File, Print Format, Letter Head, Web Page, Workspace content and
                              Single values (Website Settings, Navbar Settings, ...)
    site_config.json          the `installed_apps` key, where a site keeps one

The MODULE, the workspace and the desktop tile are renamed by brand_rename.py, which this calls
once the package rename is done.

Every step is idempotent: a site already on the new name has nothing to rename.
Nothing is deleted except a duplicate row that the rename would collide with.
"""

import json
import os

import frappe

from ai_snaplead_connector.brand_rename import drop_module_map_cache

#: Every package name this app was installed under before, newest first.
OLD_APPS = ("sigzen_ai_scan_crm_connector", "vc_connector")
#: The default `old` of the single-name helpers below: the release a live client most likely runs.
OLD_APP = OLD_APPS[0]
NEW_APP = "ai_snaplead_connector"

#: (doctype, column) pairs that hold an app's package name as their whole value.
APP_COLUMNS = (
	("Desktop Icon", "app"),
	("Workspace", "app"),
	("Workspace Sidebar", "app"),
	("Sidebar Item Group", "app"),
	("Website Theme Ignore App", "app"),
	("User", "default_app"),
	("User Invitation", "app_name"),
	("Changelog Feed", "app_name"),
)
#: Single DocTypes whose field holds a package name as its whole value.
APP_SINGLE_FIELDS = (("System Settings", "default_app"),)

#: (doctype, column) pairs that can carry an /assets/<package>/ URL somewhere inside their text.
ASSET_URL_COLUMNS = (
	("Desktop Icon", "logo_url"),
	("File", "file_url"),
	("File", "thumbnail_url"),
	("Print Format", "html"),
	("Print Format", "css"),
	("Letter Head", "content"),
	("Letter Head", "footer"),
	("Letter Head", "image"),
	("Web Page", "main_section"),
	("Web Page", "main_section_html"),
	("Workspace", "content"),
	("Custom HTML Block", "html"),
	("Desktop Layout", "layout"),
)


def renamed_app_list(apps, old=OLD_APP, new=NEW_APP):
	"""`apps` with `old` replaced by `new`, in place, never listing `new` twice."""
	result = []
	for app in apps:
		name = new if app == old else app
		if name not in result:
			result.append(name)
	return result


def renamed_patch(patch, old=OLD_APP, new=NEW_APP):
	"""A patches.txt entry moved to the new package, or None when it is not the old app's."""
	prefix = f"{old}."
	if not (patch or "").startswith(prefix):
		return None
	return f"{new}.{patch[len(prefix):]}"


def _has_column(doctype, column):
	return frappe.db.table_exists(doctype) and frappe.db.has_column(doctype, column)


def rename_installed_apps_list(old=OLD_APP, new=NEW_APP, dry_run=False):
	rows = frappe.db.sql(
		"select name, defvalue from `tabDefaultValue` where defkey = 'installed_apps'", as_dict=True
	)
	changed = 0
	for row in rows:
		try:
			apps = json.loads(row.defvalue or "[]")
		except ValueError:
			continue
		if old not in apps:
			continue
		if not dry_run:
			frappe.db.sql(
				"update `tabDefaultValue` set defvalue = %s where name = %s",
				(json.dumps(renamed_app_list(apps, old, new)), row.name),
			)
		changed += 1
	return changed


def rename_installed_application(old=OLD_APP, new=NEW_APP, dry_run=False):
	if not frappe.db.table_exists("Installed Application"):
		return 0
	table = frappe.qb.DocType("Installed Application")
	stale = frappe.qb.from_(table).select(table.name).where(table.app_name == old).run(pluck=True)
	if not stale:
		return 0
	if dry_run:
		return len(stale)
	if frappe.qb.from_(table).select(table.name).where(table.app_name == new).run():
		# Both rows exist: keep the new one.
		frappe.qb.from_(table).delete().where(table.app_name == old).run()
	else:
		frappe.qb.update(table).set(table.app_name, new).where(table.app_name == old).run()
	return len(stale)


def rename_module_app(old=OLD_APP, new=NEW_APP, dry_run=False):
	table = frappe.qb.DocType("Module Def")
	stale = frappe.qb.from_(table).select(table.name).where(table.app_name == old).run(pluck=True)
	if stale and not dry_run:
		frappe.qb.update(table).set(table.app_name, new).where(table.app_name == old).run()
	return len(stale)


def rename_patch_log(old=OLD_APP, new=NEW_APP, dry_run=False):
	"""Without this every patch of the app would run again under its new name."""
	table = frappe.qb.DocType("Patch Log")
	rows = (
		frappe.qb.from_(table).select(table.name, table.patch).where(table.patch.like(f"{old}.%")).run(as_dict=True)
	)
	changed = 0
	for row in rows:
		target = renamed_patch(row.patch, old, new)
		if not target:
			continue
		changed += 1
		if dry_run:
			continue
		if frappe.qb.from_(table).select(table.name).where(table.patch == target).run():
			frappe.qb.from_(table).delete().where(table.name == row.name).run()
		else:
			frappe.qb.update(table).set(table.patch, target).where(table.name == row.name).run()
	return changed


def rename_app_columns(old=OLD_APP, new=NEW_APP, dry_run=False):
	"""Columns whose whole value is a package name: the desk's app fields and default apps."""
	changed = 0
	for doctype, column in APP_COLUMNS:
		if not _has_column(doctype, column):
			continue
		table = frappe.qb.DocType(doctype)
		stale = frappe.qb.from_(table).select(table.name).where(table[column] == old).run(pluck=True)
		changed += len(stale)
		if stale and not dry_run:
			frappe.qb.update(table).set(table[column], new).where(table[column] == old).run()

	for doctype, field in APP_SINGLE_FIELDS:
		count = frappe.db.sql(
			"select count(*) from `tabSingles` where doctype = %s and field = %s and value = %s",
			(doctype, field, old),
		)[0][0]
		changed += count
		if count and not dry_run:
			frappe.db.sql(
				"update `tabSingles` set value = %s where doctype = %s and field = %s and value = %s",
				(new, doctype, field, old),
			)
	return changed


def rewrite_asset_urls(old=OLD_APP, new=NEW_APP, dry_run=False):
	"""/assets/<old>/... no longer resolves once the bench builds the app under its new name.

	REPLACE() is plain SQL on MariaDB, SQLite and Postgres alike, so the text is rewritten in
	the database without loading a single document (and without their controllers).
	"""
	old_prefix, new_prefix = f"/assets/{old}/", f"/assets/{new}/"
	pattern = f"%{old_prefix}%"
	changed = 0
	for doctype, column in ASSET_URL_COLUMNS:
		if not _has_column(doctype, column):
			continue
		count = frappe.db.sql(  # nosemgrep - table/column names come from this module's fixed lists, never from input; values are bound parameters
			f"select count(*) from `tab{doctype}` where `{column}` like %s", (pattern,)
		)[0][0]
		changed += count
		if count and not dry_run:
			frappe.db.sql(  # nosemgrep - table/column names come from this module's fixed lists, never from input; values are bound parameters
				f"update `tab{doctype}` set `{column}` = replace(`{column}`, %s, %s) where `{column}` like %s",
				(old_prefix, new_prefix, pattern),
			)

	# Single DocTypes (Website Settings logo and favicon, Navbar Settings, ...) keep their values
	# as rows of tabSingles.
	count = frappe.db.sql("select count(*) from `tabSingles` where value like %s", (pattern,))[0][0]
	changed += count
	if count and not dry_run:
		frappe.db.sql(
			"update `tabSingles` set value = replace(value, %s, %s) where value like %s",
			(old_prefix, new_prefix, pattern),
		)

	# Saved desktop layouts also carry the app's package name as a quoted JSON value.
	if _has_column("Desktop Layout", "layout"):
		quoted_old, quoted_new = f'"{old}"', f'"{new}"'
		table = frappe.qb.DocType("Desktop Layout")
		for row in (
			frappe.qb.from_(table)
			.select(table.name, table.layout)
			.where(table.layout.like(f"%{quoted_old}%"))
			.run(as_dict=True)
		):
			changed += 1
			if not dry_run:
				frappe.qb.update(table).set(table.layout, row.layout.replace(quoted_old, quoted_new)).where(
					table.name == row.name
				).run()
	return changed


def rename_scheduled_jobs(old=OLD_APP, new=NEW_APP, dry_run=False):
	"""A scheduled job whose method still names the old package would fail on every tick."""
	if not _has_column("Scheduled Job Type", "method"):
		return 0
	table = frappe.qb.DocType("Scheduled Job Type")
	rows = (
		frappe.qb.from_(table)
		.select(table.name, table.method)
		.where(table.method.like(f"{old}.%"))
		.run(as_dict=True)
	)
	for row in rows:
		if not dry_run:
			frappe.qb.update(table).set(table.method, renamed_patch(row.method, old, new)).where(
				table.name == row.name
			).run()
	return len(rows)


def rename_site_config(site_path, old=OLD_APP, new=NEW_APP, dry_run=False):
	"""Some sites keep their own copy of the list in site_config.json."""
	path = os.path.join(site_path, "site_config.json")
	if not os.path.exists(path):
		return 0
	with open(path) as handle:  # nosemgrep - the site's own site_config.json, located by frappe
		config = json.load(handle)
	apps = config.get("installed_apps")
	if not isinstance(apps, list) or old not in apps:
		return 0
	if not dry_run:
		config["installed_apps"] = renamed_app_list(apps, old, new)
		with open(path, "w") as handle:  # nosemgrep - same file, written back
			json.dump(config, handle, indent=1)
			handle.write("\n")
	return 1


def rename_site(old=OLD_APP, new=NEW_APP, dry_run=False):
	"""Every rename step on the connected site for one old name. Returns what each step changed."""
	return {
		"installed_apps": rename_installed_apps_list(old, new, dry_run),
		"installed_application": rename_installed_application(old, new, dry_run),
		"module_def": rename_module_app(old, new, dry_run),
		"patch_log": rename_patch_log(old, new, dry_run),
		"scheduled_jobs": rename_scheduled_jobs(old, new, dry_run),
		"app_columns": rename_app_columns(old, new, dry_run),
		"asset_urls": rewrite_asset_urls(old, new, dry_run),
		"site_config": rename_site_config(frappe.get_site_path(), old, new, dry_run),
	}


def rename_site_from_every_old_name(new=NEW_APP, dry_run=False):
	"""rename_site() for every old name. A site renamed once before (vc_connector ->
	sigzen_ai_scan_crm_connector) may still hold a stray row under the oldest name."""
	report = {}
	for old in OLD_APPS:
		for key, count in rename_site(old, new, dry_run).items():
			report[key] = report.get(key, 0) + count
	return report


def site_lists_old_app(old=None):
	"""Whether the site still lists an old package name (`old`, or any of OLD_APPS).

	Read the table, not frappe.get_installed_apps(): that list is cached per request and the
	defaults behind it in Redis, and both go stale the moment the rename writes the row."""
	names = (old,) if old else OLD_APPS
	for value in frappe.db.sql(
		"select defvalue from `tabDefaultValue` where defkey = 'installed_apps'", pluck=True
	):
		try:
			apps = json.loads(value or "[]")
		except ValueError:
			continue
		if any(name in apps for name in names):
			return True
	return False


def forget_cached_app_list():
	"""frappe caches the installed apps per request, the hooks per site and the module map in
	Redis. All of them still name the old package after the rename."""
	from frappe.cache_manager import clear_defaults_cache

	clear_defaults_cache("__global")
	clear_defaults_cache("__default")
	frappe.clear_cache()
	if getattr(frappe.local, "request_cache", None) is not None:
		frappe.local.request_cache.clear()
	drop_module_map_cache()
	frappe.setup_module_map(include_all_apps=True)


def _describe(report):
	return ", ".join(f"{key}={count}" for key, count in report.items() if count) or "nothing"


def upgrade_during_migrate(old=OLD_APP):
	"""`before_migrate` of a stand-in package (`old` is its name): rename the site, then run what
	the real app's own `before_migrate` would have run, because the migrate's loop over installed
	apps was read before the rename and will not reach the new name in this pass."""
	if not site_lists_old_app(old):
		return

	if NEW_APP not in frappe.get_all_apps():
		# The repository is on the bench only under its old folder name. Renaming the site now
		# would point it at an app the bench does not list, and the connector would silently
		# stop working, so stop here with the one standard command that fixes it.
		raise frappe.ValidationError(
			f"{frappe.local.site} has the connector installed under its old name {old}, "
			f"but {NEW_APP} is not on this bench yet. Run:\n"
			f"    bench get-app <connector repository URL> --branch master\n"
			f"then run the migrate again. Nothing has been changed."
		)

	report = rename_site_from_every_old_name()
	forget_cached_app_list()
	print(f"{frappe.local.site}: renamed the installed app {old} -> {NEW_APP} ({_describe(report)})")

	for fn in frappe.get_hooks("before_migrate", app_name=NEW_APP):
		frappe.get_attr(fn)()


def before_install():
	"""`bench install-app ai_snaplead_connector` on a site that has an old package.

	Installing would sync the app as new and mark every patch as already run, including ones
	the old install never ran. So rename the existing install instead and stop; the migrate that
	follows runs exactly the patches that are still pending.
	"""
	if not site_lists_old_app():
		return None

	report = rename_site_from_every_old_name()
	frappe.db.commit()  # nosemgrep - install_app returns early on False and commits nothing itself
	forget_cached_app_list()
	print(
		f"{frappe.local.site} already had this app under an old name ({', '.join(OLD_APPS)}); "
		f"renamed it to {NEW_APP} ({_describe(report)}) instead of installing it again.\n"
		f"Now run: bench --site {frappe.local.site} migrate"
	)
	return False
