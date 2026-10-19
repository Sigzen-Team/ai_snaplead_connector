# Copyright (c) 2026, support@sigzen.com and contributors
"""The app's rename reaches a client that already had the old workspace.

Run: bench --site clienttest run-tests --module ai_snaplead_connector.tests.test_workspace_repair

`frappe.rename_doc` rewrites Link fields, not Data ones. So when "VC Connector" was renamed
after the app, `Workspace Sidebar Item.link_to` followed the
workspace while `label` stayed behind -- the client landed on the right page under a sidebar
entry still naming the old app. The in-page heading lives in a FOURTH place again,
`Workspace.content`, which the model sync never reconciles for a row that already exists.

And a UNIQUE index on `tabDesktop Icon.label` turned a cosmetic relabel into an
IntegrityError that the caller's `except` swallowed, abandoning every repair after it. That
is why the tile on an existing client kept its old `External` routing -- the one thing that
makes an app tile open in a NEW WINDOW -- however many times migrate ran.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_snaplead_connector import install


class TestWorkspaceRepair(IntegrationTestCase):
	def test_stale_sidebar_label_is_repaired(self):
		if not frappe.db.exists("Workspace Sidebar", install.SIDEBAR):
			self.skipTest("connector sidebar not present on this site")

		row = frappe.db.get_value(
			"Workspace Sidebar Item",
			{"parent": install.SIDEBAR, "link_to": install.WORKSPACE},
			"name",
		)
		if not row:
			self.skipTest("no sidebar entry points at the connector workspace")

		original = frappe.db.get_value("Workspace Sidebar Item", row, "label")
		self.addCleanup(
			frappe.db.set_value,
			"Workspace Sidebar Item",
			row,
			"label",
			original,
			update_modified=False,
		)

		frappe.db.set_value(
			"Workspace Sidebar Item",
			row,
			"label",
			install.LEGACY_WORKSPACES[-1],
			update_modified=False,
		)
		self.assertEqual(install.repair_legacy_sidebar_labels(), 1)
		self.assertNotEqual(
			frappe.db.get_value("Workspace Sidebar Item", row, "label"),
			install.LEGACY_WORKSPACES[-1],
		)

	def test_header_sync_rewrites_the_legacy_name(self):
		if not frappe.db.exists("Workspace", install.WORKSPACE):
			self.skipTest("connector workspace not present on this site")

		path = install.workspace_json_path()
		self.assertTrue(path, "the workspace JSON declaring the workspace's name was not found")
		original = frappe.db.get_value("Workspace", install.WORKSPACE, "content")
		self.addCleanup(
			frappe.db.set_value,
			"Workspace",
			install.WORKSPACE,
			"content",
			original,
			update_modified=False,
		)

		blocks = json.loads(original)
		mine = {"id": "zz_test", "type": "paragraph", "data": {"text": "client note", "col": 12}}
		for block in blocks:
			if block.get("type") == "header":
				block["data"]["text"] = f'<span class="h4"><b>{install.LEGACY_WORKSPACES[-1]}</b></span>'
				break
		blocks.append(mine)
		frappe.db.set_value(
			"Workspace", install.WORKSPACE, "content", json.dumps(blocks), update_modified=False
		)

		self.assertTrue(install._sync_workspace_header(path))

		after = json.loads(frappe.db.get_value("Workspace", install.WORKSPACE, "content"))
		headers = [b for b in after if b.get("type") == "header"]
		self.assertTrue(headers)
		self.assertEqual(
			headers[0]["data"]["text"],
			f'<span class="h4"><b>{install.APP_LABEL}</b></span>',
		)
		# A block the client added survives.
		self.assertIn(mine, after)

	def test_ensure_workspace_survives_a_failing_step(self):
		"""One repair raising must not abandon the ones after it."""
		original = install._sync_workspace_label

		def explode(*args, **kwargs):
			raise frappe.ValidationError("boom")

		install._sync_workspace_label = explode
		self.addCleanup(setattr, install, "_sync_workspace_label", original)
		try:
			install.ensure_workspace()
		except Exception as exc:  # pragma: no cover - ensure_workspace must never raise
			self.fail(f"ensure_workspace propagated a step failure: {exc}")

		# The step AFTER the failing one still ran: the tile is not left on External, which
		# is what makes it open in a new window.
		icon = frappe.db.get_value(
			"Desktop Icon", {"link_to": install.SIDEBAR, "icon_type": "App"}, "link_type"
		)
		if icon:
			self.assertEqual(icon, "Workspace Sidebar")

	def test_app_tile_never_routes_externally_when_a_sidebar_exists(self):
		"""`External` is the one link_type that forces target="_blank"."""
		if not frappe.db.exists("Workspace Sidebar", install.SIDEBAR):
			self.skipTest("connector sidebar not present on this site")

		icon = frappe.db.get_value(
			"Desktop Icon",
			{"label": install.APP_LABEL, "icon_type": "App"},
			["link_type", "link_to"],
			as_dict=True,
		)
		if not icon:
			self.skipTest("no app tile on this site")
		self.assertNotEqual(icon.link_type, "External")
		self.assertTrue(frappe.db.exists("Workspace Sidebar", icon.link_to))


class TestBrandRename(IntegrationTestCase):
	"""The module and the workspace carry the app's current name on this site."""

	def test_module_def_and_doctypes_are_on_the_new_module(self):
		import os

		from ai_snaplead_connector import brand_rename

		self.assertTrue(frappe.db.exists("Module Def", brand_rename.MODULE))
		for legacy in brand_rename.LEGACY_MODULES:
			self.assertFalse(frappe.db.exists("Module Def", legacy), f"{legacy} survived the rename")
			self.assertEqual(frappe.db.count("DocType", {"module": legacy}), 0)
		self.assertEqual(frappe.db.get_value("DocType", "Connector Settings", "module"), brand_rename.MODULE)
		# The folder a module's code lives in is frappe.scrub(name); both must agree.
		self.assertTrue(os.path.isdir(frappe.get_module_path(brand_rename.MODULE)))

	def test_rename_is_a_no_op_on_a_site_already_renamed(self):
		from ai_snaplead_connector import brand_rename

		self.assertFalse(
			brand_rename.rename_module("Vc Connector", brand_rename.MODULE, "ai_snaplead_connector")
		)
		for legacy in brand_rename.LEGACY_WORKSPACES:
			self.assertFalse(brand_rename.rename_workspace(legacy, brand_rename.WORKSPACE))

	def test_tile_label_and_workspace_name_agree(self):
		"""desktop.js resolves the tile through the sidebar named like the tile's LABEL and builds
		the URL from the workspace's TITLE, which must slug to the route of its NAME."""
		title = frappe.get_hooks("app_title", app_name="ai_snaplead_connector")[0]
		self.assertEqual(title, install.APP_LABEL)
		self.assertEqual(
			frappe.get_hooks("app_home", app_name="ai_snaplead_connector")[-1], "/desk/ai-snaplead-connector"
		)
		if frappe.db.exists("Workspace", install.WORKSPACE):
			row = frappe.db.get_value("Workspace", install.WORKSPACE, ["label", "title"], as_dict=True)
			self.assertEqual((row.label, row.title), (install.WORKSPACE, install.WORKSPACE))
