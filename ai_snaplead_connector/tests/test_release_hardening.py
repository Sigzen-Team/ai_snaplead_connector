# Copyright (c) 2026, support@sigzen.com and contributors
"""The 1.1.0 review fixes: each test failed on 1.0.x and passes now.

Nothing here commits: the endpoints that commit (upsert_document, issue_connector_token) run
with frappe.db.commit patched out, and Connector Settings is snapshotted and restored.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from ai_snaplead_connector import hooks, install
from ai_snaplead_connector.ai_snaplead_connector.doctype.connector_settings import connector_settings
from ai_snaplead_connector.api.v1 import legacy_sync, sync
from ai_snaplead_connector.tenancy import SETTINGS
from ai_snaplead_connector.tests.test_sync_api import (
	ConnectorAPITestCase,
	_configure,
	_restore_settings,
	_snapshot_settings,
	payload,
)


def _is_post_only(fn):
	return frappe.allowed_http_methods_for_whitelisted_func.get(fn) == ["POST"]


def _refused_over_get(fn):
	"""What frappe.handler does before calling a whitelisted method, on a GET request."""
	from frappe.handler import is_valid_http_method

	saved = getattr(frappe.local, "request", None)
	frappe.local.request = Request(EnvironBuilder(method="GET").get_environ())
	try:
		is_valid_http_method(fn)
	except frappe.PermissionError:
		return True
	finally:
		frappe.local.request = saved
	return False


def _make_user(roles):
	email = f"hardening-{frappe.generate_hash(length=8)}@vctest.example"
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": "Hardening", "send_welcome_email": 0}
	)
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)
	if roles:
		user.add_roles(*roles)
	return email


class SettingsTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(_restore_settings, _snapshot_settings())
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Administrator")


class TestUpsertDocumentIsGated(SettingsTestCase):
	def setUp(self):
		super().setUp()
		_configure()

	def test_post_only(self):
		self.assertTrue(_is_post_only(legacy_sync.upsert_document))
		self.assertTrue(_refused_over_get(legacy_sync.upsert_document))

	def test_a_user_without_the_role_cannot_write(self):
		source = f"Hardening {frappe.generate_hash(length=6)}"
		for roles in ([], ["Employee"]):
			frappe.set_user("Administrator")
			user = _make_user(roles)
			frappe.set_user(user)
			with patch.object(frappe.db, "commit") as commit:
				with self.assertRaises(frappe.PermissionError):
					legacy_sync.upsert_document("UTM Source", {"source_name": source}, name=source)
			commit.assert_not_called()
			frappe.set_user("Administrator")
			self.assertFalse(frappe.db.exists("UTM Source", source))

	def test_the_integration_role_can_write(self):
		user = _make_user([install.INTEGRATION_ROLE])
		frappe.set_user(user)
		source = f"Hardening {frappe.generate_hash(length=6)}"
		with patch.object(frappe.db, "commit"):
			result = legacy_sync.upsert_document("UTM Source", {"source_name": source}, name=source)
		self.assertEqual(result["operation"], "created")
		self.assertTrue(frappe.db.exists("UTM Source", source))


class TestStateChangingEndpointsArePostOnly(IntegrationTestCase):
	def test_post_only(self):
		for fn in (
			legacy_sync.issue_connector_token,
			connector_settings.generate_connector_token,
			connector_settings.repair_business_id,
		):
			self.assertTrue(_is_post_only(fn), fn.__name__)
			self.assertTrue(_refused_over_get(fn), fn.__name__)


class TestIssueTokenBeforeBusinessId(SettingsTestCase):
	def test_token_is_issued_while_business_id_is_empty(self):
		_configure(business_id="", token_hash="")
		with patch.object(frappe.db, "commit"):
			result = legacy_sync.issue_connector_token()

		self.assertTrue(result["token"])
		import hashlib

		stored = frappe.db.get_single_value(SETTINGS, "token_hash")
		self.assertEqual(stored, hashlib.sha256(result["token"].encode()).hexdigest())


class TestUnloadableSettingsAreRetryable(ConnectorAPITestCase):
	def test_settings_that_cannot_load_answer_setup_required(self):
		real = frappe.get_cached_doc

		def broken(doctype, *args, **kwargs):
			if doctype == SETTINGS:
				raise ImportError("No module named 'ai_snaplead_connector.vc_connector'")
			return real(doctype, *args, **kwargs)

		with patch.object(frappe, "get_cached_doc", broken):
			response = sync.upsert_lead_graph(payload())

		error = self.assertFailed(response, "SETUP_REQUIRED", status=409)
		self.assertTrue(error["retryable"])

	def test_settings_that_load_disabled_stay_connector_disabled(self):
		_configure(enabled=0)
		response = sync.upsert_lead_graph(payload())
		error = self.assertFailed(response, "CONNECTOR_DISABLED", status=409)
		self.assertFalse(error["retryable"])


class TestBusinessIdRestampIsBulk(SettingsTestCase):
	def setUp(self):
		super().setUp()
		self.old = f"RESTAMP-OLD-{frappe.generate_hash(length=6)}"
		self.new = f"RESTAMP-NEW-{frappe.generate_hash(length=6)}"
		self.other = f"RESTAMP-OTHER-{frappe.generate_hash(length=6)}"
		self.has_lead_stamp = frappe.get_meta("Lead").has_field("custom_business_id")

		for business in (self.old, self.old, self.old, self.other):
			lead = frappe.get_doc(
				{
					"doctype": "Lead",
					"first_name": "Restamp",
					"company_name": "Restamp Co",
					"email_id": f"restamp-{frappe.generate_hash(length=8)}@vctest.example",
				}
			).insert(ignore_permissions=True)
			if self.has_lead_stamp:
				frappe.db.set_value("Lead", lead.name, "custom_business_id", business, update_modified=False)
			link = frappe.get_doc(
				{"doctype": "Connector Lead Link", "vc_uuid": frappe.generate_hash(length=12), "lead": lead.name}
			).insert(ignore_permissions=True)
			frappe.db.set_value("Connector Lead Link", link.name, "business_id", business, update_modified=False)

		source_doctype = frappe.get_meta("Connector Source Link").get_field("source").options
		for business in (self.old, self.old):
			source = f"Restamp {frappe.generate_hash(length=8)}"
			frappe.get_doc({"doctype": source_doctype, "name": source, "source_name": source}).insert(
				ignore_permissions=True
			)
			link = frappe.get_doc(
				{"doctype": "Connector Source Link", "vc_uuid": frappe.generate_hash(length=12), "source": source}
			).insert(ignore_permissions=True)
			frappe.db.set_value(
				"Connector Source Link", link.name, "business_id", business, update_modified=False
			)

	def test_counts_match_the_preview_and_one_update_per_table(self):
		preview = connector_settings.preview_business_id_change(self.old)
		modified_before = frappe.get_all(
			"Connector Lead Link", {"business_id": self.old}, ["name", "modified"], order_by="name"
		)

		real = frappe.db.set_value
		calls = []

		def spy(dt, dn, *args, **kwargs):
			calls.append((dt, dn))
			return real(dt, dn, *args, **kwargs)

		with patch.object(frappe.db, "set_value", spy):
			counts = connector_settings.restamp_business_rows(self.new, previous_business_id=self.old)

		expected = {"lead_links": 3, "source_links": 2, "leads": 3 if self.has_lead_stamp else 0}
		self.assertEqual(counts, expected)
		self.assertEqual(counts, preview)
		# Bulk: one UPDATE per table, addressed by filters, never one per row.
		self.assertLessEqual(len(calls), 3, calls)
		self.assertTrue(all(isinstance(dn, dict) for _dt, dn in calls), calls)

		self.assertEqual(frappe.db.count("Connector Lead Link", {"business_id": self.old}), 0)
		self.assertEqual(frappe.db.count("Connector Source Link", {"business_id": self.old}), 0)
		self.assertEqual(frappe.db.count("Connector Lead Link", {"business_id": self.new}), 3)
		self.assertEqual(frappe.db.count("Connector Lead Link", {"business_id": self.other}), 1)
		if self.has_lead_stamp:
			self.assertEqual(frappe.db.count("Lead", {"custom_business_id": self.old}), 0)
			self.assertEqual(frappe.db.count("Lead", {"custom_business_id": self.other}), 1)
		# update_modified=False is kept.
		modified_after = {
			r.name: r.modified
			for r in frappe.get_all("Connector Lead Link", {"business_id": self.new}, ["name", "modified"])
		}
		for row in modified_before:
			self.assertEqual(modified_after[row.name], row.modified)

	def test_nothing_to_move_is_zero(self):
		counts = connector_settings.restamp_business_rows(
			self.new, previous_business_id=f"NOPE-{frappe.generate_hash(length=6)}"
		)
		self.assertEqual(counts, {"lead_links": 0, "source_links": 0, "leads": 0})


class TestFrappeVersionGuard(IntegrationTestCase):
	def test_v15_is_refused_with_a_clear_message(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			install.check_frappe_version("15.62.1")
		self.assertIn("needs Frappe/ERPNext v16", str(ctx.exception))

	def test_v16_and_develop_pass(self):
		for version in ("16.0.0", "16.26.3", "17.0.0-dev", "develop", ""):
			install.check_frappe_version(version)
		install.check_frappe_version()  # the running bench is v16

	def test_before_install_refuses_before_doing_anything(self):
		with (
			patch.object(frappe, "__version__", "15.40.0"),
			patch("ai_snaplead_connector.brand_rename.refresh_module_map") as refresh,
		):
			with self.assertRaises(frappe.ValidationError):
				install.before_install()
		refresh.assert_not_called()

	def test_before_migrate_runs_the_guard_first(self):
		self.assertEqual(hooks.before_migrate[0], "ai_snaplead_connector.install.check_frappe_version")


class TestConnectorLogIsCleared(IntegrationTestCase):
	def test_registered_with_log_settings(self):
		from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing

		self.assertEqual(hooks.default_log_clearing_doctypes.get("Connector Log"), 90)
		self.assertTrue(_supports_log_clearing("Connector Log"))
