# Copyright (c) 2026, support@sigzen.com and contributors
"""1.1.1: issue_connector_token fills in a blank Business ID, and refuses to move a set one.

Nothing here commits: issue_connector_token runs with frappe.db.commit patched out, and
Connector Settings is snapshotted and restored around every test.
"""

import hashlib
from unittest.mock import patch

import frappe

from ai_snaplead_connector.api.v1 import legacy_sync
from ai_snaplead_connector.tenancy import SETTINGS
from ai_snaplead_connector.tests.test_release_hardening import SettingsTestCase, _make_user
from ai_snaplead_connector.tests.test_sync_api import _configure

STARTING_HASH = hashlib.sha256(b"starting-token").hexdigest()


def _stored(field):
	return frappe.db.get_single_value(SETTINGS, field) or ""


class IssueTokenTestCase(SettingsTestCase):
	def setUp(self):
		super().setUp()
		saved = frappe.local.response.pop("http_status_code", None)
		self.addCleanup(self._restore_status, saved)

	@staticmethod
	def _restore_status(saved):
		frappe.local.response.pop("http_status_code", None)
		if saved is not None:
			frappe.local.response["http_status_code"] = saved

	def issue(self, *args, **kwargs):
		with patch.object(frappe.db, "commit"):
			return frappe.call(legacy_sync.issue_connector_token, *args, **kwargs)

	def assertMinted(self, result, business_id):
		self.assertTrue(result["token"])
		self.assertEqual(result["business_id"], business_id)
		self.assertEqual(_stored("business_id"), business_id)
		self.assertEqual(_stored("token_hash"), hashlib.sha256(result["token"].encode()).hexdigest())
		self.assertIsNone(frappe.local.response.get("http_status_code"))

	def assertRefused(self, result, code, status, business_id):
		self.assertFalse(result["ok"])
		self.assertNotIn("token", result)
		self.assertEqual(result["error"]["code"], code)
		self.assertEqual(result["error"]["field"], "business_id")
		self.assertEqual(frappe.local.response.get("http_status_code"), status)
		# Nothing on the site moved: no new token, no business change.
		self.assertEqual(_stored("token_hash"), STARTING_HASH)
		self.assertEqual(_stored("business_id"), business_id)


class TestIssueTokenFillsBusinessId(IssueTokenTestCase):
	def test_blank_business_id_is_set(self):
		_configure(business_id="", token_hash=STARTING_HASH)
		self.assertMinted(self.issue(business_id="ORG-00103"), "ORG-00103")

	def test_value_is_stripped(self):
		_configure(business_id="", token_hash=STARTING_HASH)
		self.assertMinted(self.issue(business_id="  ORG-00103 \n"), "ORG-00103")

	def test_same_business_id_is_a_no_op(self):
		_configure(business_id="ORG-00103", token_hash=STARTING_HASH)
		result = self.issue(business_id="ORG-00103")
		self.assertMinted(result, "ORG-00103")
		self.assertNotEqual(_stored("token_hash"), STARTING_HASH)

	def test_different_business_id_is_refused(self):
		_configure(business_id="ORG-00001", token_hash=STARTING_HASH)
		result = self.issue(business_id="ORG-00103")
		self.assertRefused(result, "BUSINESS_MISMATCH", 403, "ORG-00001")
		message = result["error"]["message"]
		self.assertIn("ORG-00001", message)
		self.assertIn("System Manager", message)
		self.assertIn("Connector Settings", message)

	def test_mismatch_does_not_restamp_rows(self):
		_configure(business_id="ORG-00001", token_hash=STARTING_HASH)
		with patch(
			"ai_snaplead_connector.ai_snaplead_connector.doctype.connector_settings."
			"connector_settings.restamp_business_rows"
		) as restamp:
			self.issue(business_id="ORG-00103")
		restamp.assert_not_called()

	def test_junk_business_id_is_refused(self):
		for junk in ("", "   ", "ORG 00103", "<script>", "ORG-1;drop", "-ORG", "x" * 141, "ORG-00103\nORG-2"):
			with self.subTest(junk=junk):
				_configure(business_id="", token_hash=STARTING_HASH)
				frappe.local.response.pop("http_status_code", None)
				self.assertRefused(self.issue(business_id=junk), "VALIDATION_FAILED", 422, "")

	def test_omitted_business_id_keeps_legacy_behaviour(self):
		for current in ("", "ORG-00001"):
			with self.subTest(current=current):
				_configure(business_id=current, token_hash=STARTING_HASH)
				self.assertMinted(self.issue(), current)

	def test_only_a_system_manager_may_call_it(self):
		_configure(business_id="", token_hash=STARTING_HASH)
		for roles in ([], ["Sales User"]):
			frappe.set_user("Administrator")
			user = _make_user(roles)
			frappe.set_user(user)
			with self.subTest(roles=roles):
				with self.assertRaises(frappe.PermissionError):
					self.issue(business_id="ORG-00103")
		frappe.set_user("Administrator")
		self.assertEqual(_stored("business_id"), "")
		self.assertEqual(_stored("token_hash"), STARTING_HASH)

	def test_a_system_manager_user_sets_it(self):
		_configure(business_id="", token_hash=STARTING_HASH)
		frappe.set_user(_make_user(["System Manager"]))
		result = self.issue(business_id="ORG-00103")
		frappe.set_user("Administrator")
		self.assertMinted(result, "ORG-00103")
