# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""What a stranger's site looks like after `install-app` and after `bench migrate`.

The defect these pin is not subtle and it was not visible from the code: `install-app` writes
a `Patch Log` row for EVERY patch in `patches.txt` before it calls `after_install`
(`frappe.installer.install_app` -> `set_all_patches_as_completed`). So the two patches that
move this app off its own Custom Fields were recorded as executed on every fresh install
without either of them having run, and `bench migrate` would never call them again. Nothing
then ever set `legacy_fields_dropped`, so `create_custom_fields` took the "not migrated yet"
branch for ever and re-created the full historical set -- fourteen Custom Fields and four
empty sections on Lead, ten more on UTM Source -- on a site that had never held one legacy
value, on this install and on every later migrate.

`install.settle_install_time_migration_state` is what closes that, and these are its three
cases plus the end state it produces.
"""

import frappe
from frappe.tests import IntegrationTestCase

from ai_snaplead_connector import install
from ai_snaplead_connector.legacy import state
from ai_snaplead_connector.tenancy import SETTINGS


def _snapshot_settings():
	return frappe.db.sql(
		"""select field, value from `tabSingles` where doctype = %s order by field""",
		(SETTINGS,),
		as_dict=True,
	)


def _restore_settings(rows):
	frappe.db.delete("Singles", {"doctype": SETTINGS})
	for row in rows:
		frappe.db.sql(
			"""insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)""",
			(SETTINGS, row.field, row.value),
		)
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	frappe.clear_cache()


def _restore_patch_log(patches):
	for patch in patches:
		if not frappe.db.exists("Patch Log", {"patch": patch}):
			frappe.get_doc({"doctype": "Patch Log", "patch": patch}).insert(ignore_permissions=True)


class InstallStateTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(_restore_settings, _snapshot_settings())
		self.addCleanup(
			_restore_patch_log,
			[
				patch
				for patch in install.LEGACY_PATCHES
				if frappe.db.exists("Patch Log", {"patch": patch})
			],
		)

	def _pretend(self, dropped, legacy_present):
		real_dropped = state.fields_are_dropped
		real_present = install.legacy_fields_present
		state.fields_are_dropped = lambda: dropped
		install.legacy_fields_present = lambda: legacy_present
		self.addCleanup(setattr, state, "fields_are_dropped", real_dropped)
		self.addCleanup(setattr, install, "legacy_fields_present", real_present)


class TestFreshInstallSettlesAtTheEndState(InstallStateTestCase):
	def test_a_site_that_never_had_the_legacy_fields_is_recorded_as_done(self):
		self._pretend(dropped=False, legacy_present=False)
		frappe.db.set_single_value(SETTINGS, "legacy_fields_dropped", 0)
		frappe.db.set_single_value(SETTINGS, "legacy_migration_status", "")

		install.settle_install_time_migration_state()

		self.assertEqual(frappe.db.get_single_value(SETTINGS, "legacy_migration_status"), "Complete")
		self.assertTrue(frappe.db.get_single_value(SETTINGS, "legacy_fields_dropped"))
		self.assertIn(
			"Fresh install",
			frappe.db.get_single_value(SETTINGS, "legacy_migration_message") or "",
		)

	def test_the_patch_log_rows_are_left_alone_on_a_fresh_install(self):
		"""On a fresh site the two patches really do have nothing to do, so the rows install
		wrote are correct and deleting them would only make the next migrate walk an empty
		table."""
		self._pretend(dropped=False, legacy_present=False)
		_restore_patch_log(install.LEGACY_PATCHES)

		install.settle_install_time_migration_state()

		for patch in install.LEGACY_PATCHES:
			self.assertTrue(frappe.db.exists("Patch Log", {"patch": patch}), patch)


class TestAReinstallOverLegacyFieldsRewindsThePatches(InstallStateTestCase):
	def test_the_unrun_patch_log_rows_are_removed(self):
		"""The only way an INSTALL can meet these fields is a re-install over an older
		version of this app. Then the two patches have real work to do and the rows install
		just wrote are a lie: leave them and the migration can never run again."""
		self._pretend(dropped=False, legacy_present=True)
		_restore_patch_log(install.LEGACY_PATCHES)
		frappe.db.set_single_value(SETTINGS, "legacy_fields_dropped", 0)
		frappe.db.set_single_value(SETTINGS, "legacy_migration_message", "untouched marker")

		install.settle_install_time_migration_state()

		for patch in install.LEGACY_PATCHES:
			self.assertFalse(frappe.db.exists("Patch Log", {"patch": patch}), patch)

		# and it does NOT declare the site finished: there is real work to do here, and
		# writing the end state would put the site back in the state this whole function
		# exists to prevent -- the full historical field set, recreated for ever.
		self.assertFalse(frappe.db.get_single_value(SETTINGS, "legacy_fields_dropped"))
		self.assertEqual(
			frappe.db.get_single_value(SETTINGS, "legacy_migration_message"), "untouched marker"
		)


class TestAFinishedSiteIsNeverResettled(InstallStateTestCase):
	def test_a_dropped_site_is_left_exactly_as_it_is(self):
		"""`fields_are_dropped` IS the end state. Re-deriving it from what is on the DocType
		would undo a restore -- `legacy.restore` puts the Custom Fields back deliberately."""
		self._pretend(dropped=True, legacy_present=True)
		_restore_patch_log(install.LEGACY_PATCHES)
		frappe.db.set_single_value(SETTINGS, "legacy_migration_message", "untouched marker")

		install.settle_install_time_migration_state()

		self.assertEqual(
			frappe.db.get_single_value(SETTINGS, "legacy_migration_message"), "untouched marker"
		)
		for patch in install.LEGACY_PATCHES:
			self.assertTrue(frappe.db.exists("Patch Log", {"patch": patch}), patch)


class TestTheLegacyPatchNamesAreReal(IntegrationTestCase):
	def test_every_named_patch_is_registered(self):
		"""If a patch is renamed and this constant is not, the rewind silently stops working
		and a re-install over legacy fields goes back to being unfixable."""
		import os

		import ai_snaplead_connector

		path = os.path.join(os.path.dirname(ai_snaplead_connector.__file__), "patches.txt")
		with open(path) as handle:
			registered = {line.strip() for line in handle if line.strip() and not line.startswith("[")}

		for patch in install.LEGACY_PATCHES:
			self.assertIn(patch, registered, patch)

	def test_the_end_state_is_one_custom_field_on_lead(self):
		"""The README's promise, asserted against the site the suite is running on -- but
		only once that site has actually reached the end state."""
		if not state.fields_are_dropped():
			self.skipTest("this site has not finished the legacy migration yet")

		fieldnames = frappe.get_all("Custom Field", filters={"dt": "Lead"}, pluck="fieldname")

		self.assertEqual(sorted(fieldnames), ["custom_address"])


class TestTheIntegrationRoleExists(IntegrationTestCase):
	"""The README used to tell a client to give the integration user "Sales User plus write
	access". No such combination existed: `connector_lead_link.json` ships one DocPerm row, System
	Manager, and write is not create -- so the only user that could push was a full System
	Manager, which is a large thing to ask a client to hand a remote party.
	"""

	def test_the_role_this_app_installs_is_on_the_site(self):
		self.assertTrue(frappe.db.exists("Role", install.INTEGRATION_ROLE))

	def test_it_grants_exactly_what_the_endpoint_checks(self):
		for doctype, ptypes in install.INTEGRATION_ROLE_PERMISSIONS.items():
			for ptype in ptypes:
				with self.subTest(doctype=doctype, ptype=ptype):
					self.assertTrue(
						frappe.db.get_value(
							"Custom DocPerm",
							{"parent": doctype, "role": install.INTEGRATION_ROLE, "permlevel": 0},
							ptype,
						),
						f"{install.INTEGRATION_ROLE} has no {ptype} on {doctype}",
					)

	def test_it_covers_the_one_doctype_a_sales_role_cannot(self):
		"""Lead, Contact and Address come from the client's own sales role. Connector Lead Link is
		this app's, and nothing but System Manager could reach it before this role."""
		from ai_snaplead_connector.api.v1.sync import REQUIRED_PERMISSIONS
		from ai_snaplead_connector.brand_rename import MODULE

		# By module, not by name: the app's DocTypes lost their "VC " prefix.
		connector_owned = {
			doctype
			for doctype, _ptype in REQUIRED_PERMISSIONS
			if frappe.db.get_value("DocType", doctype, "module") == MODULE
		}
		self.assertTrue(connector_owned)
		for doctype in connector_owned:
			self.assertIn(doctype, install.INTEGRATION_ROLE_PERMISSIONS, doctype)


class TestTheAppDeclaresWhatItNeeds(IntegrationTestCase):
	"""ERPNext is a hard dependency and the app must say so.

	overrides/lead.py does `from erpnext.crm.doctype.lead.lead import Lead` at import time,
	and this app extends Lead, Contact and Address -- all ERPNext doctypes. Without
	`required_apps`, installing on a Frappe-only site succeeds and then every request dies
	on an ImportError, which is a terrible first experience for anyone installing this from
	the Marketplace.
	"""

	def test_erpnext_is_declared_as_required(self):
		self.assertIn("erpnext", frappe.get_hooks("required_apps", app_name="ai_snaplead_connector"))

	def test_the_import_that_makes_it_required_is_still_there(self):
		"""Guards the premise: if the hard import goes away, revisit the declaration."""
		import inspect

		from ai_snaplead_connector.overrides import lead as lead_override

		self.assertIn("erpnext", inspect.getsource(lead_override)[:800])
