# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Move a site that installed this app as `vc_connector` onto `sigzen_ai_scan_crm_connector`.

Only development builds were ever called `vc_connector`; a site that installs the app from the
Marketplace never runs any of this.

NOTHING HERE IS RUN BY HAND. The upgrade happens inside the standard bench commands:

    bench get-app <repository-url> --branch master
    bench --site all migrate
    bench remove-app vc_connector

HOW `bench migrate` REACHES IT

A site keeps its installed apps in the database, and `bench migrate` imports `<app>.hooks` for
every name on that list before it does anything else. So this repository ships a tiny package
named `vc_connector` next to the real one (see vc_connector/hooks.py). It has no features; its
only hook is `before_migrate` -> upgrade_during_migrate() below, which renames the site and then
lets the migrate carry on under the new name. It also makes the old name importable again, which
is what stops `bench build` failing with "expected str ... not NoneType" on a bench whose
`apps/vc_connector` folder still holds leftovers of the old package.

`bench --site <site> install-app sigzen_ai_scan_crm_connector` on such a site does the same
rename (before_install below) instead of installing a second copy.

WHAT IS RENAMED

    installed apps list       tabDefaultValue, defkey = installed_apps
    Installed Application     the row frappe shows under "Installed Applications"
    Module Def.app_name       which package a module's code is loaded from
    Patch Log.patch           so patches that already ran are not run a second time
    Desktop Icon / Workspace / Workspace Sidebar `app`, and the logo path under /assets/
    Desktop Layout            saved layouts carry a frozen copy of the tile
    site_config.json          the `installed_apps` key, where a site keeps one

Every step is idempotent: a site already on the new name has nothing to rename.
Nothing is deleted except a duplicate row that the rename would collide with.
"""

import json
import os

import frappe

OLD_APP = "vc_connector"
NEW_APP = "sigzen_ai_scan_crm_connector"

#: Doctypes that carry the app's package name in an `app` column.
APP_COLUMN_DOCTYPES = ("Desktop Icon", "Workspace", "Workspace Sidebar")


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


def rename_desk_records(old=OLD_APP, new=NEW_APP, dry_run=False):
	changed = 0
	for doctype in APP_COLUMN_DOCTYPES:
		if not _has_column(doctype, "app"):
			continue
		table = frappe.qb.DocType(doctype)
		stale = frappe.qb.from_(table).select(table.name).where(table.app == old).run(pluck=True)
		changed += len(stale)
		if stale and not dry_run:
			frappe.qb.update(table).set(table.app, new).where(table.app == old).run()

	if _has_column("Desktop Icon", "logo_url"):
		table = frappe.qb.DocType("Desktop Icon")
		for row in (
			frappe.qb.from_(table)
			.select(table.name, table.logo_url)
			.where(table.logo_url.like(f"/assets/{old}/%"))
			.run(as_dict=True)
		):
			changed += 1
			if not dry_run:
				frappe.qb.update(table).set(
					table.logo_url, row.logo_url.replace(f"/assets/{old}/", f"/assets/{new}/", 1)
				).where(table.name == row.name).run()

	if _has_column("Desktop Layout", "layout"):
		table = frappe.qb.DocType("Desktop Layout")
		for row in frappe.qb.from_(table).select(table.name, table.layout).run(as_dict=True):
			layout = row.layout or ""
			updated = layout.replace(f'"{old}"', f'"{new}"').replace(f"/assets/{old}/", f"/assets/{new}/")
			if updated == layout:
				continue
			changed += 1
			if not dry_run:
				frappe.qb.update(table).set(table.layout, updated).where(table.name == row.name).run()
	return changed


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
	"""Every rename step on the connected site. Returns what each step changed."""
	return {
		"installed_apps": rename_installed_apps_list(old, new, dry_run),
		"installed_application": rename_installed_application(old, new, dry_run),
		"module_def": rename_module_app(old, new, dry_run),
		"patch_log": rename_patch_log(old, new, dry_run),
		"desk_records": rename_desk_records(old, new, dry_run),
		"site_config": rename_site_config(frappe.get_site_path(), old, new, dry_run),
	}


def site_lists_old_app(old=OLD_APP):
	"""Read the table, not frappe.get_installed_apps(): that list is cached per request and the
	defaults behind it in Redis, and both go stale the moment the rename writes the row."""
	for value in frappe.db.sql(
		"select defvalue from `tabDefaultValue` where defkey = 'installed_apps'", pluck=True
	):
		try:
			if old in json.loads(value or "[]"):
				return True
		except ValueError:
			continue
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
	frappe.cache.delete_value("app_modules")
	frappe.client_cache.delete_value("installed_app_modules")
	frappe.setup_module_map(include_all_apps=True)


def _describe(report):
	return ", ".join(f"{key}={count}" for key, count in report.items() if count) or "nothing"


def upgrade_during_migrate():
	"""`before_migrate` of the `vc_connector` stand-in package: rename the site, then run what
	the real app's own `before_migrate` would have run, because the migrate's loop over installed
	apps was read before the rename and will not reach the new name in this pass."""
	if not site_lists_old_app():
		return

	if NEW_APP not in frappe.get_all_apps():
		# The repository is on the bench only under its old folder name. Renaming the site now
		# would point it at an app the bench does not list, and the connector would silently
		# stop working, so stop here with the one standard command that fixes it.
		raise frappe.ValidationError(
			f"{frappe.local.site} has the connector installed under its old name {OLD_APP}, "
			f"but {NEW_APP} is not on this bench yet. Run:\n"
			f"    bench get-app <connector repository URL> --branch master\n"
			f"then run the migrate again. Nothing has been changed."
		)

	report = rename_site()
	forget_cached_app_list()
	print(f"{frappe.local.site}: renamed the installed app {OLD_APP} -> {NEW_APP} ({_describe(report)})")

	for fn in frappe.get_hooks("before_migrate", app_name=NEW_APP):
		frappe.get_attr(fn)()


def before_install():
	"""`bench install-app sigzen_ai_scan_crm_connector` on a site that has the old package.

	Installing would sync the app as new and mark every patch as already run, including ones
	the old install never ran. So rename the existing install instead and stop; the migrate that
	follows runs exactly the patches that are still pending.
	"""
	if not site_lists_old_app():
		return None

	report = rename_site()
	frappe.db.commit()  # nosemgrep - install_app returns early on False and commits nothing itself
	forget_cached_app_list()
	print(
		f"{frappe.local.site} already had this app under its old name {OLD_APP}; "
		f"renamed it to {NEW_APP} ({_describe(report)}) instead of installing it again.\n"
		f"Now run: bench --site {frappe.local.site} migrate"
	)
	return False
