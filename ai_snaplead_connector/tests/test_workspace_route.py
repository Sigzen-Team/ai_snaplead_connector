# Copyright (c) 2026, support@sigzen.com and contributors
"""The workspace's route is /desk/ai-snaplead-connector, and every older one still gets there.

Run: bench --site clienttest run-tests --app ai_snaplead_connector --module ai_snaplead_connector.tests.test_workspace_route

The workspace was named "AI SnapLead - Connector", and a workspace's name is its route: frappe's
slug only turns spaces into hyphens, so the URL read /desk/ai-snaplead---connector. It is now
named "AI SnapLead Connector" while the tile, the sidebar and the heading keep reading
"AI SnapLead - Connector". These tests put a site back in the old shape -- with the names it
had on release 1.0.0 as well -- and check that the migrate repair brings all four stores across
(Workspace, Workspace Sidebar, the app's Desktop Icon and the workspace's Link icon), that a
second run changes nothing, and that the old URLs redirect.

Nothing here commits: the steps are called directly (brand_rename.execute and
install.ensure_workspace commit), and IntegrationTestCase rolls the class back.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import slug
from werkzeug.exceptions import HTTPException
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from ai_snaplead_connector import brand_rename, install
from ai_snaplead_connector.utils import renamed_routes

OLD_ROUTE = "/desk/ai-snaplead---connector"
NEW_ROUTE = "/desk/ai-snaplead-connector"


def _app_icon():
	return frappe.db.get_value(
		"Desktop Icon",
		{"icon_type": "App", "app": install.APP},
		["name", "label", "link_type", "link_to", "link", "hidden", "parent_icon"],
		as_dict=True,
	)


class TestWorkspaceRoute(IntegrationTestCase):
	def setUp(self):
		if not frappe.db.exists("Workspace", install.WORKSPACE):
			self.skipTest("the connector workspace is not on this site")
		if not frappe.db.exists("Workspace Sidebar", install.SIDEBAR):
			self.skipTest("the connector sidebar is not on this site")
		if not _app_icon():
			self.skipTest("the connector tile is not on this site")

	# -- the shape the site is in now ---------------------------------------------------------

	def test_names_and_route(self):
		self.assertEqual(install.WORKSPACE, "AI SnapLead Connector")
		self.assertEqual(install.APP_LABEL, "AI SnapLead - Connector")
		self.assertEqual(f"/desk/{slug(install.WORKSPACE)}", NEW_ROUTE)

		hooks = frappe.get_hooks(app_name=install.APP)
		self.assertEqual(hooks.app_title[-1], install.APP_LABEL)
		self.assertEqual(hooks.app_home[-1], NEW_ROUTE)
		self.assertEqual(hooks.add_to_apps_screen[0]["route"], NEW_ROUTE)
		self.assertEqual(hooks.add_to_apps_screen[0]["title"], install.APP_LABEL)

	def test_workspace_json_declares_the_new_name(self):
		path = install.workspace_json_path()
		self.assertTrue(path)
		self.assertTrue(path.endswith("workspace/ai_snaplead_connector/ai_snaplead_connector.json"), path)
		with open(path) as handle:
			declared = json.load(handle)
		self.assertEqual(
			(declared["name"], declared["label"], declared["title"]), (install.WORKSPACE,) * 3
		)
		# The heading inside the workspace keeps the visible title.
		self.assertIn(f"<b>{install.APP_LABEL}</b>", declared["content"])

	def test_tile_opens_the_workspace_in_the_same_tab_and_is_no_folder(self):
		icon = _app_icon()
		self.assertEqual(icon.label, install.APP_LABEL)
		# "External" is what opens a new window.
		self.assertEqual((icon.link_type, icon.link_to), ("Workspace Sidebar", install.SIDEBAR))
		self.assertFalse(icon.hidden)

		# desktop.js sends the tile to the sidebar's FIRST Link entry, and builds that URL from the
		# workspace's TITLE.
		first = frappe.get_all(
			"Workspace Sidebar Item",
			filters={"parent": install.SIDEBAR, "parenttype": "Workspace Sidebar", "type": "Link"},
			fields=["link_type", "link_to"],
			order_by="idx asc",
			limit=1,
		)[0]
		self.assertEqual((first.link_type, first.link_to), ("Workspace", install.WORKSPACE))
		title = frappe.db.get_value("Workspace", install.WORKSPACE, "title")
		self.assertEqual(f"/desk/{slug(title)}", NEW_ROUTE)

		# A visible icon parented to the tile's label turns the tile into a folder.
		self.assertFalse(
			frappe.db.exists("Desktop Icon", {"parent_icon": icon.label, "hidden": 0}),
			"a visible icon hangs under the tile, which makes it a folder",
		)
		link_icon = frappe.db.get_value(
			"Desktop Icon", {"label": install.WORKSPACE}, ["icon_type", "hidden", "parent_icon"], as_dict=True
		)
		self.assertTrue(link_icon, "the workspace's own Link icon is missing")
		self.assertEqual((link_icon.icon_type, link_icon.hidden, link_icon.parent_icon or None), ("Link", 1, None))

	# -- the migrate repair -----------------------------------------------------------------

	def _put_site_back(self, workspace, app_label, link_icon):
		"""Move the live rows to the names an older release left behind."""
		self.assertTrue(brand_rename.rename_workspace(install.WORKSPACE, workspace))
		if workspace != install.SIDEBAR:
			self.assertTrue(brand_rename.rename_sidebar(install.SIDEBAR, workspace))
		if app_label != install.APP_LABEL:
			self.assertTrue(brand_rename.rename_app_icon(install.APP_LABEL, app_label))
		frappe.db.set_value(
			"Desktop Icon",
			_app_icon().name,
			{"link_type": "External", "link_to": None, "link": f"/desk/{slug(workspace)}"},
			update_modified=False,
		)
		frappe.db.sql(
			"update `tabDesktop Icon` set name = %s, label = %s, link_to = %s where name = %s",
			(link_icon, link_icon, workspace, install.WORKSPACE),
		)
		frappe.db.sql(
			"update `tabHas Role` set parent = %s where parent = %s and parenttype = 'Desktop Icon'",
			(link_icon, install.WORKSPACE),
		)
		content = json.loads(frappe.db.get_value("Workspace", workspace, "content"))
		for block in content:
			if block.get("type") == "header":
				block["data"]["text"] = f'<span class="h4"><b>{app_label}</b></span>'
		frappe.db.set_value("Workspace", workspace, "content", json.dumps(content), update_modified=False)

		self.assertFalse(frappe.db.exists("Workspace", install.WORKSPACE))
		self.assertEqual(frappe.db.get_value("Workspace", workspace, "title"), workspace)

	def _repair(self):
		"""What `bench migrate` runs: before_migrate's rename, then after_migrate's repairs."""
		brand_rename.rename_desk()
		return install.repair_desk()

	def _assert_current_shape(self):
		for legacy in brand_rename.LEGACY_WORKSPACES:
			self.assertFalse(frappe.db.exists("Workspace", legacy), legacy)
		row = frappe.db.get_value("Workspace", install.WORKSPACE, ["label", "title"], as_dict=True)
		self.assertEqual((row.label, row.title), (install.WORKSPACE, install.WORKSPACE))
		self.assertTrue(
			frappe.db.exists("Has Role", {"parent": install.WORKSPACE, "parenttype": "Workspace"}),
			"the workspace's roles did not move with it",
		)
		self.assertIn(
			f"<b>{install.APP_LABEL}</b>", frappe.db.get_value("Workspace", install.WORKSPACE, "content")
		)

		self.assertEqual(
			frappe.db.get_value("Workspace Sidebar", install.SIDEBAR, "title"), install.SIDEBAR
		)
		self.assertTrue(
			frappe.db.exists(
				"Workspace Sidebar Item",
				{"parent": install.SIDEBAR, "link_type": "Workspace", "link_to": install.WORKSPACE},
			)
		)
		self.assertFalse(
			frappe.db.exists(
				"Workspace Sidebar Item",
				{"link_type": "Workspace", "link_to": ("in", brand_rename.LEGACY_WORKSPACES)},
			)
		)

		icon = _app_icon()
		self.assertEqual(icon.label, install.APP_LABEL)
		self.assertEqual(
			(icon.link_type, icon.link_to, icon.link or ""), ("Workspace Sidebar", install.SIDEBAR, "")
		)
		link_icon = frappe.db.get_value(
			"Desktop Icon", install.WORKSPACE, ["label", "hidden", "parent_icon", "link_to"], as_dict=True
		)
		self.assertEqual(
			(link_icon.label, link_icon.hidden, link_icon.parent_icon or None, link_icon.link_to),
			(install.WORKSPACE, 1, None, install.SIDEBAR),
		)
		self.assertFalse(frappe.db.exists("Desktop Icon", {"link": OLD_ROUTE}))

	def test_site_on_the_dashed_name_is_renamed(self):
		"""The shape every site had before this release: name, route and tile all "- Connector"."""
		self._put_site_back(install.APP_LABEL, install.APP_LABEL, f"{install.APP_LABEL} workspace")
		self.assertTrue(self._repair())
		self._assert_current_shape()

		# Idempotent: a second migrate finds nothing to do.
		self.assertFalse(self._repair())
		self._assert_current_shape()

	def test_site_on_the_release_1_names_is_renamed(self):
		"""A site still on "Sigzen - AI Scan CRM Connector" (release 1.0.0) lands on the same shape."""
		old = "Sigzen - AI Scan CRM Connector"
		self._put_site_back(old, old, f"{old} workspace")
		self.assertTrue(self._repair())
		self._assert_current_shape()
		self.assertFalse(frappe.db.exists("Desktop Icon", {"label": old}))
		self.assertFalse(self._repair())

	def test_client_entries_in_the_sidebar_survive(self):
		sidebar = install.SIDEBAR
		frappe.get_doc(
			{
				"doctype": "Workspace Sidebar Item",
				"parent": sidebar,
				"parenttype": "Workspace Sidebar",
				"parentfield": "items",
				"idx": 99,
				"label": "zz client entry",
				"type": "Link",
				"link_type": "DocType",
				"link_to": "Lead",
			}
		).db_insert()
		self._put_site_back(install.APP_LABEL, install.APP_LABEL, f"{install.APP_LABEL} workspace")
		self._repair()
		self.assertTrue(
			frappe.db.exists("Workspace Sidebar Item", {"parent": sidebar, "label": "zz client entry"})
		)

	def test_the_workspace_name_holds_an_empty_sidebar(self):
		"""Otherwise frappe makes one up (the module shares the name) and a visit by URL shows it."""
		from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import auto_generate_sidebar_from_module

		self.assertTrue(frappe.db.exists("Workspace Sidebar", install.WORKSPACE))
		self.assertFalse(frappe.db.exists("Workspace Sidebar Item", {"parent": install.WORKSPACE}))
		self.assertNotIn(install.WORKSPACE, [sidebar.title for sidebar in auto_generate_sidebar_from_module()])
		self.assertFalse(install._ensure_sidebar_placeholder())

	def test_a_generated_sidebar_under_the_workspace_name_is_emptied(self):
		shortcut = frappe.get_all(
			"Workspace Shortcut", filters={"parent": install.WORKSPACE}, pluck="link_to", limit=1
		)
		items = [{"label": "Home", "type": "Link", "link_type": "Workspace", "link_to": install.WORKSPACE}]
		items += [{"label": s, "type": "Link", "link_type": "DocType", "link_to": s} for s in shortcut]
		self._fill_placeholder(items)
		self.assertTrue(install._ensure_sidebar_placeholder())
		self.assertTrue(frappe.db.exists("Workspace Sidebar", install.WORKSPACE))
		self.assertFalse(frappe.db.exists("Workspace Sidebar Item", {"parent": install.WORKSPACE}))
		self.assertFalse(install._ensure_sidebar_placeholder())

	def test_a_sidebar_a_client_edited_is_kept(self):
		items = [
			{"label": "Home", "type": "Link", "link_type": "Workspace", "link_to": install.WORKSPACE},
			{"label": "Customers", "type": "Link", "link_type": "DocType", "link_to": "Customer"},
		]
		self._fill_placeholder(items)
		self.assertFalse(install._ensure_sidebar_placeholder())
		self.assertEqual(frappe.db.count("Workspace Sidebar Item", {"parent": install.WORKSPACE}), 2)

	def test_a_missing_placeholder_is_created(self):
		frappe.db.delete("Workspace Sidebar", {"name": install.WORKSPACE})
		self.assertTrue(install._ensure_sidebar_placeholder())
		self.assertTrue(frappe.db.exists("Workspace Sidebar", install.WORKSPACE))

	def _fill_placeholder(self, items):
		name = install.WORKSPACE
		self.assertTrue(frappe.db.exists("Workspace Sidebar", name))
		# The class rolls back only at its end; the next test must find the placeholder empty.
		self.addCleanup(frappe.db.delete, "Workspace Sidebar Item", {"parent": name, "parenttype": "Workspace Sidebar"})
		for idx, item in enumerate(items, start=1):
			frappe.get_doc(
				{
					"doctype": "Workspace Sidebar Item",
					"parent": name,
					"parenttype": "Workspace Sidebar",
					"parentfield": "items",
					"idx": idx,
					**item,
				}
			).db_insert()


class TestRenamedDeskRoutes(IntegrationTestCase):
	"""Old desk URLs 301 to the new route. A before_request hook: frappe serves /desk/* before it
	reads website_redirects."""

	def test_hook_is_registered(self):
		self.assertIn(
			"ai_snaplead_connector.utils.renamed_routes.redirect_renamed_desk_routes",
			frappe.get_hooks("before_request", app_name=install.APP),
		)

	def test_old_routes_map_to_the_new_one(self):
		self.assertEqual(renamed_routes.renamed_desk_route(OLD_ROUTE), NEW_ROUTE)
		self.assertEqual(renamed_routes.renamed_desk_route(OLD_ROUTE + "/"), NEW_ROUTE)
		self.assertEqual(renamed_routes.renamed_desk_route("/desk/sigzen---ai-scan-crm-connector"), NEW_ROUTE)
		self.assertEqual(renamed_routes.renamed_desk_route(f"{OLD_ROUTE}/x"), f"{NEW_ROUTE}/x")
		self.assertIsNone(renamed_routes.renamed_desk_route(NEW_ROUTE))
		self.assertIsNone(renamed_routes.renamed_desk_route("/desk/ai-snaplead---connectorx"))
		self.assertIsNone(renamed_routes.renamed_desk_route("/desk/lead"))

	def _run(self, path, method="GET", query=""):
		previous = getattr(frappe.local, "request", None)
		frappe.local.request = Request(EnvironBuilder(path=path, method=method, query_string=query).get_environ())
		try:
			renamed_routes.redirect_renamed_desk_routes()
		finally:
			frappe.local.request = previous

	def test_old_route_redirects_with_301_and_keeps_the_query(self):
		with self.assertRaises(HTTPException) as caught:
			self._run(OLD_ROUTE, query="sidebar=1")
		response = caught.exception.response
		self.assertEqual(response.status_code, 301)
		self.assertEqual(response.headers["Location"], f"{NEW_ROUTE}?sidebar=1")

	def test_other_requests_pass_through(self):
		self._run(NEW_ROUTE)
		self._run("/desk/lead")
		self._run(OLD_ROUTE, method="POST")
