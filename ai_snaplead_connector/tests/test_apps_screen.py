# Copyright (c) 2026, support@sigzen.com and contributors
"""The client can FIND this app, and the tile never leads to a page they cannot open.

Run: bench --site clienttest run-tests --module ai_snaplead_connector.tests.test_apps_screen

Frappe applies the /apps tile's `has_permission` and the destination workspace's own `roles`
independently, and never checks that the two agree. This app's workspace is System Manager
only, so before the permission hook existed every Desk user on a client site saw a
"VC Connector" tile, clicked it, and landed on a workspace they were not allowed to open.
The client reads that as "the app does not show" and "the workspace was never created" --
and both readings are wrong in a way no error message corrects.
"""

import frappe
from frappe.tests import IntegrationTestCase

from ai_snaplead_connector.install import WORKSPACE, ensure_workspace, has_app_permission
from ai_snaplead_connector.tests.cleanup import LeavesNoContactsBehind

APP = "ai_snaplead_connector"


def _entry():
	entries = frappe.get_hooks("add_to_apps_screen", app_name=APP)
	return entries[0] if entries else None


class TestTheAppIsReachable(LeavesNoContactsBehind, IntegrationTestCase):
	def test_the_hook_is_declared_with_a_route_and_a_permission(self):
		entry = _entry()
		self.assertIsNotNone(entry, "no add_to_apps_screen entry: the app is unfindable")
		self.assertEqual(entry.get("route"), f"/desk/{WORKSPACE.lower().replace(' ', '-')}")
		self.assertTrue(
			entry.get("has_permission"),
			"without has_permission the tile is shown to users who cannot open it",
		)

	def test_the_workspace_exists(self):
		self.assertTrue(frappe.db.exists("Workspace", WORKSPACE))

	def test_ensure_workspace_is_a_noop_when_it_is_already_there(self):
		"""It runs on every install AND every migrate, so a repeat must cost nothing and
		must never overwrite a workspace a client has customised.

		The FIRST call after a release that renames the workspace legitimately does work --
		it pushes the new label onto a site that already had the row -- so settle first and
		assert on the call after that. Asserting on the first call made this pass or fail
		depending on whether a rename had shipped since the last run.
		"""
		ensure_workspace()
		frappe.db.commit()
		self.assertFalse(ensure_workspace())

	def test_everyone_offered_the_tile_can_open_the_workspace(self):
		allowed = set(
			frappe.get_all(
				"Has Role", filters={"parent": WORKSPACE, "parenttype": "Workspace"}, pluck="role"
			)
		)
		self.assertTrue(allowed, "an unrestricted workspace would make this test vacuous")

		for role in sorted(allowed):
			with self.subTest(role=role):
				frappe.set_user(self._user_with_roles([role]))
				self.addCleanup(frappe.set_user, "Administrator")
				self.assertTrue(has_app_permission())
				frappe.set_user("Administrator")

	def test_a_plain_desk_user_is_not_offered_a_tile_they_cannot_use(self):
		frappe.set_user(self._user_with_roles([]))
		self.addCleanup(frappe.set_user, "Administrator")
		self.assertFalse(has_app_permission())

	def test_administrator_is_always_offered_the_tile(self):
		frappe.set_user("Administrator")
		self.assertTrue(has_app_permission())

	def _user_with_roles(self, roles):
		email = f"qa-apps-{frappe.generate_hash(length=8)}@vctest.example"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "QA Apps",
				"send_welcome_email": 0,
				"user_type": "System User",
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(
			lambda: frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		)
		frappe.clear_cache(user=user.name)
		return user.name


class TestTheDesktopIconExists(IntegrationTestCase):
	"""Without a Desktop Icon row the workspace is absent from the client's desk.

	And without a "logo" in add_to_apps_screen, create_desktop_icons_from_installed_apps()
	raises KeyError before the workspace pass ever runs -- so an app we ship would leave the
	CLIENT's entire site with no desktop icons, failing silently inside a print()-only
	except block.
	"""

	def test_the_entry_declares_a_logo_that_exists(self):
		import os

		logo = (_entry() or {}).get("logo") or ""
		self.assertTrue(logo.startswith("/assets/ai_snaplead_connector/"), logo)
		path = frappe.get_app_path(
			"ai_snaplead_connector", "public", *logo.split("/assets/ai_snaplead_connector/")[1].split("/")
		)
		self.assertTrue(os.path.exists(path), f"{logo} does not exist on disk at {path}")

	def test_the_workspace_has_a_desktop_icon(self):
		"""Either the workspace's own icon (older installs) or the app tile (a fresh one)."""
		from ai_snaplead_connector.install import workspace_is_on_the_desk

		self.assertTrue(
			workspace_is_on_the_desk(),
			f"{WORKSPACE} is public but no Desktop Icon opens it, so it is not on the desk",
		)
