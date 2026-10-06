# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""The package was `vc_connector` until its first release. Nothing may break because of that.

Run: bench --site clienttest run-tests --module sigzen_ai_scan_crm_connector.tests.test_package_rename

Two promises are pinned here.

1. A central site that still calls the API under the old package name is answered. The
   central and a client's ERPNext are upgraded by different people, so for a while the old
   paths are what arrives. If one of them stops resolving, that client silently stops
   receiving leads.

2. A site that installed the old package is moved across by the ordinary `bench migrate`
   (through the stand-in package `vc_connector`) without re-running patches. One of those
   patches rewrites the client's existing Leads; running it twice because its name in the
   Patch Log changed is exactly the accident the rename exists to prevent.

The full round trip (install, turn the site back into a vc_connector install, `bench migrate`)
renames a real site, so it is not run here; it is recorded in CHANGELOG.md.
"""

import importlib
import inspect
import os
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from sigzen_ai_scan_crm_connector.api.v1 import legacy_sync, sync
from sigzen_ai_scan_crm_connector.upgrade import from_vc_connector as upgrade

OLD = "vc_connector"
NEW = "sigzen_ai_scan_crm_connector"


def _whitelisted(module):
	"""Dotted paths (minus the package) of every whitelisted function a module defines."""
	found = []
	for name, fn in inspect.getmembers(module, inspect.isfunction):
		if fn.__module__ == module.__name__ and fn in frappe.whitelisted:
			found.append(f"{module.__name__[len(NEW) + 1 :]}.{name}")
	return found


class TestTheOldApiPathsStillAnswer(IntegrationTestCase):
	def test_every_api_method_has_an_alias_under_the_old_package(self):
		aliases = frappe.get_hooks("override_whitelisted_methods")
		methods = _whitelisted(sync) + _whitelisted(legacy_sync)
		self.assertTrue(methods, "found no whitelisted API methods; this test is looking in the wrong place")
		for suffix in methods:
			with self.subTest(method=suffix):
				self.assertIn(
					f"{NEW}.{suffix}",
					aliases.get(f"{OLD}.{suffix}") or [],
					f"{OLD}.{suffix} no longer resolves: a central site built before the rename cannot call it",
				)

	def test_each_alias_points_at_a_real_whitelisted_function(self):
		for old_path, targets in frappe.get_hooks("override_whitelisted_methods").items():
			if not old_path.startswith(f"{OLD}."):
				continue
			with self.subTest(path=old_path):
				fn = frappe.get_attr(targets[-1])
				self.assertIn(fn, frappe.whitelisted)

	def test_the_app_is_installed_under_its_new_name(self):
		apps = frappe.get_installed_apps()
		self.assertIn(NEW, apps)
		self.assertNotIn(OLD, apps)
		self.assertEqual(frappe.get_hooks("app_name", app_name=NEW), [NEW])


class TestUpgradeFromTheOldPackage(IntegrationTestCase):
	def test_the_installed_apps_list_is_rewritten_in_place(self):
		self.assertEqual(
			upgrade.renamed_app_list(["frappe", "erpnext", OLD, "other"]), ["frappe", "erpnext", NEW, "other"]
		)
		# Already carrying both: the new name must not end up listed twice.
		self.assertEqual(upgrade.renamed_app_list(["frappe", NEW, OLD]), ["frappe", NEW])
		self.assertEqual(upgrade.renamed_app_list(["frappe", "erpnext"]), ["frappe", "erpnext"])

	def test_only_this_apps_patches_are_renamed(self):
		self.assertEqual(
			upgrade.renamed_patch(f"{OLD}.patches.v1_0.migrate_legacy_lead_fields"),
			f"{NEW}.patches.v1_0.migrate_legacy_lead_fields",
		)
		self.assertIsNone(upgrade.renamed_patch("erpnext.patches.v16_0.something"))
		self.assertIsNone(upgrade.renamed_patch(f"{OLD}_other_app.patches.x"))
		self.assertIsNone(upgrade.renamed_patch(None))

	def test_a_patch_that_already_ran_is_not_left_to_run_again(self):
		"""Exercised with throwaway app names so the site's real Patch Log is not touched."""
		old_app, new_app = "zz_old_pkg", "zz_new_pkg"
		for patch in (f"{old_app}.patches.one", f"{old_app}.patches.two", f"{new_app}.patches.two"):
			frappe.get_doc({"doctype": "Patch Log", "patch": patch}).insert(ignore_permissions=True)

		self.assertEqual(upgrade.rename_patch_log(old_app, new_app, dry_run=True), 2)
		self.assertEqual(frappe.db.count("Patch Log", {"patch": ("like", f"{old_app}.%")}), 2)

		self.assertEqual(upgrade.rename_patch_log(old_app, new_app), 2)
		self.assertEqual(frappe.db.count("Patch Log", {"patch": ("like", f"{old_app}.%")}), 0)
		self.assertEqual(frappe.db.count("Patch Log", {"patch": f"{new_app}.patches.one"}), 1)
		# The one that existed under both names is kept once, not twice.
		self.assertEqual(frappe.db.count("Patch Log", {"patch": f"{new_app}.patches.two"}), 1)

		self.assertEqual(upgrade.rename_patch_log(old_app, new_app), 0, "a second run must find nothing")

	def test_this_site_carries_no_trace_of_the_old_package(self):
		self.assertEqual(frappe.db.count("Module Def", {"app_name": OLD}), 0)
		self.assertEqual(frappe.db.count("Patch Log", {"patch": ("like", f"{OLD}.%")}), 0)
		self.assertEqual(frappe.db.count("Desktop Icon", {"app": OLD}), 0)


class TestTheOldNameStillLoads(IntegrationTestCase):
	"""What `bench build` and `bench migrate` need from `vc_connector` on an old bench."""

	def test_the_stand_in_is_a_real_package(self):
		# A folder of leftovers imports as a namespace package with __file__ = None, which is
		# the `bench build` TypeError this package exists to prevent.
		module = importlib.import_module(OLD)
		self.assertIsNotNone(module.__file__)

	def test_frappe_can_read_it_as_an_app(self):
		for name in ("modules.txt", "patches.txt"):
			with self.subTest(file=name):
				self.assertTrue(os.path.exists(frappe.get_app_path(OLD, name)))
		self.assertEqual(frappe.get_module_list(OLD), [])

	def test_its_only_hook_is_the_upgrade(self):
		hooks = importlib.import_module(f"{OLD}.hooks")
		self.assertEqual(hooks.app_name, OLD)
		# Inside the stand-in itself: frappe.get_attr() refuses a hook from an app the site does
		# not list, and at that moment the site lists only `vc_connector`.
		self.assertEqual(hooks.before_migrate, [f"{OLD}.upgrade.before_migrate"])
		self.assertTrue(callable(importlib.import_module(f"{OLD}.upgrade").before_migrate))
		for name in ("doc_events", "override_whitelisted_methods", "after_migrate", "scheduler_events"):
			self.assertFalse(hasattr(hooks, name), f"the stand-in must not carry {name}")

	def test_the_real_app_renames_instead_of_installing_twice(self):
		self.assertEqual(
			frappe.get_hooks("before_install", app_name=NEW),
			[f"{NEW}.install.before_install"],
		)


class TestUpgradeEntryPoints(IntegrationTestCase):
	def test_a_site_on_the_new_name_is_left_alone(self):
		self.assertFalse(upgrade.site_lists_old_app())
		with mock.patch.object(upgrade, "rename_site") as rename:
			upgrade.upgrade_during_migrate()
			self.assertIsNone(upgrade.before_install())
		rename.assert_not_called()

	def test_it_refuses_when_the_new_app_is_not_on_the_bench(self):
		"""Renaming then would point the site at an app the bench does not list."""
		with (
			mock.patch.object(upgrade, "site_lists_old_app", return_value=True),
			mock.patch.object(frappe, "get_all_apps", return_value=["frappe", "erpnext", OLD]),
			mock.patch.object(upgrade, "rename_site") as rename,
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				upgrade.upgrade_during_migrate()
		rename.assert_not_called()
		self.assertIn("bench get-app", str(caught.exception))

	def test_the_old_name_is_found_in_the_table(self):
		row = frappe.db.sql(
			"select name, defvalue from `tabDefaultValue` where defkey = 'installed_apps'", as_dict=True
		)[0]
		frappe.db.sql(
			"update `tabDefaultValue` set defvalue = %s where name = %s",
			(row.defvalue.replace(NEW, OLD), row.name),
		)
		self.assertTrue(upgrade.site_lists_old_app())
		# rolled back by IntegrationTestCase


class TestTheVcPrefixIsGone(IntegrationTestCase):
	"""The app's DocTypes and role were "VC ..."; migrate renames an existing site's copies."""

	def test_every_renamed_doctype_exists_under_its_new_name_only(self):
		from sigzen_ai_scan_crm_connector import brand_rename

		for old, new in brand_rename.LEGACY_DOCTYPES.items():
			with self.subTest(doctype=new):
				self.assertTrue(frappe.db.exists("DocType", new))
				self.assertFalse(frappe.db.exists("DocType", old))
		for old, new in brand_rename.LEGACY_ROLES.items():
			self.assertTrue(frappe.db.exists("Role", new))
			self.assertFalse(frappe.db.exists("Role", old))

	def test_renaming_a_doctype_that_is_already_gone_does_nothing(self):
		from sigzen_ai_scan_crm_connector import brand_rename

		brand_rename.rename_doctype("VC Connector Settings", "Connector Settings")
		brand_rename.rename_role("VC Connector Integration", "Connector Integration")
		self.assertTrue(frappe.db.exists("DocType", "Connector Settings"))


class TestInstallSeesItsOwnDocTypes(IntegrationTestCase):
	"""install-app syncs only the modules in the cached app -> modules map. A map cached before
	the app reached the bench made the install skip every DocType of this app and then fail on
	"Lead: Options must be a valid DocType for field Email IDs"."""

	def test_before_install_rebuilds_a_stale_module_map(self):
		from sigzen_ai_scan_crm_connector import install

		stale = {app: mods for app, mods in frappe.local.app_modules.items() if app != NEW}
		frappe.local.app_modules = stale
		frappe.cache.set_value("app_modules", stale)

		self.assertIsNone(install.before_install())

		self.assertIn(NEW, frappe.local.app_modules)
		self.assertTrue(frappe.local.app_modules[NEW])
		self.assertIn(NEW, frappe.cache.get_value("app_modules"))
