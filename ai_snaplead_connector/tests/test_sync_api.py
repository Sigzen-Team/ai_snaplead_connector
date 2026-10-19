# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Tests for the connector's API surface.

The single most important one is ``test_a_failed_address_leaves_no_lead_behind``: it is the
whole reason ``upsert_lead_graph`` exists. The old per-document REST push wrote the Lead in
one transaction and the Address in another, so an Address that failed validation left a Lead
that no one had asked for, indistinguishable from a lead that legitimately has no address --
and the retry then made a second one.

None of these tests commits. frappe's IntegrationTestCase rolls the whole class back at the
end, so a commit here would permanently write test leads into whatever site the suite was
pointed at.
"""

import hashlib
import itertools
import json

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from ai_snaplead_connector.api.v1 import sync
from ai_snaplead_connector.legacy.schema import lead_has_contact_tables
from ai_snaplead_connector.tenancy import SETTINGS
from ai_snaplead_connector.tests.cleanup import LeavesNoContactsBehind
from ai_snaplead_connector.utils.phone import normalize_mobile_for_auth

TOKEN = "vc-test-connector-token"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
BUSINESS = "VCTEST-BIZ"
OTHER_BUSINESS = "VCTEST-OTHER-BIZ"

COUNTED_DOCTYPES = ("Lead", "Contact", "Address", "Connector Lead Link")

# A real 1x1 PNG: frappe identifies and optimises uploaded images, so an attachment test has
# to send bytes that actually are the image its file name claims.
ONE_PIXEL_PNG = (
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _snapshot_settings():
	# "Singles" is a raw table, not a DocType, so it has to be read with SQL.
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
	_forget_settings()


def _configure(business_id=BUSINESS, enabled=1, token_hash=TOKEN_HASH):
	frappe.db.delete("Singles", {"doctype": SETTINGS})
	values = {
		"business_id": business_id,
		"enabled": enabled,
		"token_hash": token_hash,
		"reject_business_mismatch": 1,
		"name": SETTINGS,
	}
	for field, value in values.items():
		frappe.db.sql(
			"""insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)""",
			(SETTINGS, field, value),
		)
	_forget_settings()


def _forget_settings():
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	frappe.clear_cache()


def _set_request(token=TOKEN):
	headers = {sync.CONNECTOR_TOKEN_HEADER: token} if token is not None else {}
	frappe.local.request = Request(EnvironBuilder(method="POST", headers=headers).get_environ())


def _clear_request():
	frappe.local.request = None


def _ensure_address_template():
	"""frappe refuses to save ANY Address on a site with no default Address Template.

	clienttest was created without the setup wizard and has none, which is a real thing that
	happens to a client site -- `ping()` reports it for exactly that reason. It is a setup
	gap, not a connector bug, so the address tests supply the missing template rather than
	pretending addresses cannot be written.
	"""
	if frappe.db.exists("Address Template", {"is_default": 1}):
		return

	frappe.get_doc(
		{
			"doctype": "Address Template",
			"country": frappe.db.get_value("Country", {"name": "India"})
			or frappe.db.get_value("Country", {}),
			"is_default": 1,
			"template": "{{ address_line1 }}<br>{{ city }}<br>{{ state }}<br>{{ pincode }}",
		}
	).insert(ignore_permissions=True)


def _remove_site_files():
	"""A savepoint rolls back the database, not the disk.

	The endpoint cleans up after ITS OWN rollback; the test suite's class-level rollback is
	outside that, so a test that really wrote a file has to remove the bytes itself or the
	site accumulates an orphan on every run.
	"""
	for file_url in frappe.get_all("File", filters={"attached_to_doctype": "Lead"}, pluck="file_url"):
		_remove_one_site_file(file_url)


def _remove_one_site_file(file_url):
	if not file_url:
		return
	path = frappe.get_site_path(file_url.lstrip("/").replace("private/files", "private/files", 1))
	try:
		import os

		if os.path.exists(path):
			os.remove(path)
	except Exception:
		pass


def _counts():
	return {doctype: frappe.db.count(doctype) for doctype in COUNTED_DOCTYPES}


# frappe's IntegrationTestCase rolls back once per CLASS, not per test, so rows written by
# one test are still there for the next one -- and this app's duplicate guard refuses a second
# Lead that reuses an email or a phone number. Every generated payload is therefore unique.
_SEQUENCE = itertools.count(1)
# A per-RUN salt, and it is load-bearing. The counter alone restarts at 1 on every run, so
# "asha-17@vctest.example" recurs -- and this app's duplicate guard then refuses the push
# against a Lead an EARLIER run left on the site, failing a test that has nothing to do with
# the behaviour it is checking. That is exactly what happened on ankitpulp, where the suite
# had accumulated 41 Leads. The salt makes every run claim values no previous run can hold.
_RUN = frappe.generate_hash(length=4)


def uniq_email(tag="lead"):
	return f"{tag}-{_RUN}-{next(_SEQUENCE)}@vctest.example"


def uniq_phone():
	return f"+91 9{int(_RUN, 16) % 10000:04d}{next(_SEQUENCE):05d}"


def payload(**overrides):
	base = {
		"protocol": sync.PROTOCOL,
		"request_id": frappe.generate_hash(length=16),
		"business_id": BUSINESS,
		"vc_uuid": frappe.generate_hash(length=16),
		"lead": {
			"first_name": "Asha",
			"last_name": "Verma",
			"company_name": "Verma Textiles",
			"emails": [uniq_email("asha")],
			"phones": [uniq_phone()],
		},
		"address": {
			"address_line1": "12 MG Road",
			"city": "Ahmedabad",
			"pincode": "380015",
			"country": "India",
		},
	}
	for key, value in overrides.items():
		if isinstance(value, dict) and isinstance(base.get(key), dict):
			merged = dict(base[key])
			merged.update(value)
			base[key] = merged
		else:
			base[key] = value
	return base


class ConnectorAPITestCase(LeavesNoContactsBehind, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# Connector Settings is a Single: it survives an ordinary rollback, and it holds a
		# token hash no admin can recover. Snapshot first, restore always.
		self.saved_settings = _snapshot_settings()
		self.addCleanup(_restore_settings, self.saved_settings)
		self.addCleanup(_clear_request)
		self.addCleanup(frappe.set_user, "Administrator")
		_configure()
		_ensure_address_template()
		_set_request()
		frappe.set_user("Administrator")

	def assertFailed(self, response, code, status=None):
		self.assertFalse(response["ok"], response)
		self.assertEqual(response["error"]["code"], code, response)
		self.assertIn("retryable", response["error"])
		self.assertIn("request_id", response["error"])
		self.assertEqual(
			frappe.local.response.get("http_status_code"),
			status or sync.ERROR_STATUS[code],
		)
		return response["error"]

	def assertNothingWritten(self, before):
		self.assertEqual(_counts(), before)


class TestUpsertLeadGraphAtomicity(ConnectorAPITestCase):
	def test_a_failed_address_leaves_no_lead_behind(self):
		"""THE test. A real Address failure -- a country that is not a Country -- and the
		Lead, the Contact ERPNext auto-creates with it and the link row must all be gone."""
		before = _counts()

		response = sync.upsert_lead_graph(payload(address={"country": "Republic of Nowhere"}))

		self.assertFailed(response, "VALIDATION_FAILED")
		self.assertNothingWritten(before)

	def test_a_failed_address_insert_leaves_no_lead_behind(self):
		"""The same proof with the failure forced at the insert itself, so it holds even for
		an Address that fails for a reason validation cannot foresee (a database error, a
		hook in another app). The Lead and Contact inserts really run before it."""
		before = _counts()
		real_insert = sync._insert_doc
		inserted = []

		def failing_insert(doc):
			if doc.doctype == "Address":
				# Deliberately NOT a frappe exception: this proves the graph is rolled back
				# even for a failure the connector has no contract for at all.
				raise RuntimeError("forced failure inserting Address")
			real_insert(doc)
			inserted.append(doc.doctype)
			return doc

		sync._insert_doc = failing_insert
		self.addCleanup(setattr, sync, "_insert_doc", real_insert)

		response = sync.upsert_lead_graph(payload())

		sync._insert_doc = real_insert

		# The Lead really was written before the Address blew up ...
		self.assertIn("Lead", inserted)
		# ... and it is not there any more.
		self.assertFailed(response, "INTERNAL")
		self.assertTrue(response["error"]["retryable"])
		self.assertNothingWritten(before)

	def test_a_rolled_back_push_never_deletes_another_records_attachment(self):
		"""A savepoint rolls back the database, not the disk, so the endpoint removes the
		bytes it wrote. Frappe dedupes uploads by content hash: a second upload of identical
		bytes creates a new File ROW but writes nothing new and points at the EXISTING
		file_url. Removing that path destroyed the attachment of every other record sharing
		it -- their rows survive the rollback and point at bytes that are gone. No archive,
		no undo, and every retry of the same truncated payload did it again."""
		import base64
		import os

		self.addCleanup(_remove_site_files)
		victim = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Victim",
				"last_name": "Of File Discard",
				"email_id": uniq_email("victim"),
			}
		).insert(ignore_permissions=True)
		card = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "victim-card.png",
				"attached_to_doctype": "Lead",
				"attached_to_name": victim.name,
				"is_private": 1,
				"content": base64.b64decode(ONE_PIXEL_PNG),
			}
		).insert(ignore_permissions=True)
		path = card.get_full_path()
		self.assertTrue(os.path.exists(path), "no bytes on disk; the test would prove nothing")

		response = sync.upsert_lead_graph(
			payload(
				attachments=[
					# Byte-identical to the victim's card, so frappe hands back ITS file_url ...
					{"file_name": "card-front.png", "content_base64": ONE_PIXEL_PNG, "kind": "front_card"},
					# ... and then the push fails, which is an ordinary transport failure.
					{"file_name": "card-back.png", "content_base64": "!!! truncated payload !!!"},
				]
			)
		)

		self.assertFailed(response, "VALIDATION_FAILED")
		self.assertTrue(os.path.exists(path), "the rollback deleted another record's bytes")
		self.assertEqual(frappe.db.get_value("File", card.name, "file_url"), card.file_url)

	def test_a_rolled_back_push_still_removes_bytes_nothing_else_points_at(self):
		"""The other half: bytes this push really did write must not be left behind, or a
		retry loop fills the client's disk."""
		import base64
		import os

		self.addCleanup(_remove_site_files)
		unique_png = base64.b64encode(
			base64.b64decode(ONE_PIXEL_PNG) + b"\x00" + frappe.generate_hash(length=8).encode()
		).decode()

		written = {}
		real_attach = sync._attach_files

		def remember(lead, graph, created_files, touched):
			# finally, not after: the second attachment raises, and the first one's bytes --
			# the ones this test is about -- are on disk by then.
			try:
				return real_attach(lead, graph, created_files, touched)
			finally:
				for file_doc in created_files:
					written[file_doc.name] = file_doc.get_full_path()

		sync._attach_files = remember
		self.addCleanup(setattr, sync, "_attach_files", real_attach)

		# The failure has to come AFTER the write, so it is the second attachment that is
		# broken -- the first one really does put bytes on the client's disk first.
		response = sync.upsert_lead_graph(
			payload(
				attachments=[
					{"file_name": "orphan.png", "content_base64": unique_png},
					{"file_name": "orphan-back.png", "content_base64": "!!! truncated payload !!!"},
				]
			)
		)
		sync._attach_files = real_attach

		self.assertFailed(response, "VALIDATION_FAILED")
		self.assertTrue(written, "no file was written; the test would prove nothing")
		for name, path in written.items():
			self.assertFalse(os.path.exists(path), f"{name} left orphan bytes at {path}")

	def test_nothing_is_committed_per_document(self):
		"""A commit inside the endpoint would make the rollback above impossible. This
		catches a `frappe.db.commit()` being added to the write path later."""
		import inspect

		source = inspect.getsource(sync)
		self.assertNotIn("frappe.db.commit()", source)


class TestUpsertLeadGraphDoor(ConnectorAPITestCase):
	def test_unauthenticated_is_401_and_writes_nothing(self):
		before = _counts()
		logs_before = frappe.db.count("Connector Log")
		frappe.set_user("Guest")

		response = sync.upsert_lead_graph(payload())

		frappe.set_user("Administrator")
		self.assertFailed(response, "UNAUTHENTICATED", status=401)
		self.assertNothingWritten(before)
		# Not even a log row: an unauthenticated caller must not be able to make this site
		# write anything at all.
		self.assertEqual(frappe.db.count("Connector Log"), logs_before)

	def test_missing_connector_token_is_401(self):
		before = _counts()
		_set_request(token=None)

		response = sync.upsert_lead_graph(payload())

		self.assertFailed(response, "TOKEN_INVALID", status=401)
		self.assertNothingWritten(before)

	def test_wrong_connector_token_is_401(self):
		before = _counts()
		_set_request(token="not-the-token")

		response = sync.upsert_lead_graph(payload())

		self.assertFailed(response, "TOKEN_INVALID", status=401)
		self.assertNothingWritten(before)

	def test_wrong_business_id_is_rejected_at_the_door(self):
		before = _counts()

		response = sync.upsert_lead_graph(payload(business_id=OTHER_BUSINESS))

		error = self.assertFailed(response, "BUSINESS_MISMATCH", status=403)
		self.assertEqual(error["field"], "business_id")
		self.assertFalse(error["retryable"])
		self.assertNothingWritten(before)

	def test_disabled_connector_is_409(self):
		before = _counts()
		_configure(enabled=0)

		response = sync.upsert_lead_graph(payload())

		self.assertFailed(response, "CONNECTOR_DISABLED", status=409)
		self.assertNothingWritten(before)

	def test_unsupported_protocol_is_426(self):
		before = _counts()

		response = sync.upsert_lead_graph(payload(protocol="vc-connector/99"))

		self.assertFailed(response, "PROTOCOL_UNSUPPORTED", status=426)
		self.assertNothingWritten(before)

	def test_a_user_without_the_roles_fails_loudly(self):
		"""ignore_permissions on the writes is only defensible because this check runs."""
		before = _counts()
		email = "vcsync-noroles@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "No Roles",
					"send_welcome_email": 0,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
		before = _counts()
		frappe.set_user(email)

		response = sync.upsert_lead_graph(payload())

		frappe.set_user("Administrator")
		error = self.assertFailed(response, "REMOTE_PERMISSION", status=403)
		self.assertIn(email, error["message"])
		self.assertIn("role", error["message"].lower())
		self.assertNothingWritten(before)


class TestUpsertLeadGraphSchema(ConnectorAPITestCase):
	def test_unknown_payload_key_is_rejected_not_dropped(self):
		before = _counts()

		response = sync.upsert_lead_graph(payload(lead={"nickname": "Ash"}))

		error = self.assertFailed(response, "VALIDATION_FAILED", status=422)
		self.assertEqual(error["field"], "lead.nickname")
		self.assertNothingWritten(before)

	def test_unknown_top_level_key_is_rejected(self):
		before = _counts()

		response = sync.upsert_lead_graph(payload(deal_value=100))

		error = self.assertFailed(response, "VALIDATION_FAILED", status=422)
		self.assertEqual(error["field"], "deal_value")
		self.assertNothingWritten(before)

	def test_wrong_type_is_rejected(self):
		before = _counts()

		response = sync.upsert_lead_graph(payload(lead={"first_name": ["Asha"]}))

		error = self.assertFailed(response, "VALIDATION_FAILED", status=422)
		self.assertEqual(error["field"], "lead.first_name")
		self.assertNothingWritten(before)

	def test_address_without_a_street_is_mandatory_missing(self):
		before = _counts()

		response = sync.upsert_lead_graph(
			payload(address={"address_line1": "", "city": "Ahmedabad", "pincode": "380015"})
		)

		error = self.assertFailed(response, "MANDATORY_MISSING", status=422)
		self.assertEqual(error["field"], "address.address_line1")
		self.assertNothingWritten(before)

	def test_payload_may_arrive_as_a_json_string(self):
		response = sync.upsert_lead_graph(json.dumps(payload()))
		self.assertTrue(response["ok"], response)


class TestUpsertLeadGraphWrites(ConnectorAPITestCase):
	def test_a_lead_with_four_phones_and_two_emails_keeps_all_of_them(self):
		phones = [uniq_phone(), uniq_phone(), uniq_phone(), uniq_phone()]
		emails = [uniq_email("asha"), uniq_email("accounts")]
		expected_phones = [phone.replace(" ", "") for phone in phones]

		response = sync.upsert_lead_graph(payload(lead={"phones": phones, "emails": emails}))

		self.assertTrue(response["ok"], response)
		contact = response["contact"]
		self.assertIsNotNone(contact)

		contact_phones = frappe.get_all(
			"Contact Phone", filters={"parent": contact}, fields=["phone"], order_by="idx asc"
		)
		contact_emails = frappe.get_all(
			"Contact Email", filters={"parent": contact}, fields=["email_id"], order_by="idx asc"
		)
		self.assertEqual(len(contact_phones), 4, contact_phones)
		self.assertEqual(len(contact_emails), 2, contact_emails)
		self.assertEqual([row.phone for row in contact_phones], expected_phones)
		self.assertEqual([row.email_id for row in contact_emails], emails)

		if lead_has_contact_tables():
			# And on the Lead's own child tables, which are what this app's duplicate guard
			# reads until the legacy field migration moves that job to the Contact.
			lead = frappe.get_doc("Lead", response["lead"])
			self.assertEqual(len(lead.custom_mobile_numbers), 4)
			self.assertEqual(len(lead.custom_email_ids), 2)

		# Exactly one Contact for the lead: ERPNext creates one itself in Lead.before_insert
		# and this endpoint must enrich THAT one, not add a second.
		linked = frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": "Contact", "link_doctype": "Lead", "link_name": response["lead"]},
		)
		self.assertEqual(len(linked), 1, linked)

	def test_the_whole_graph_lands_once(self):
		before = _counts()

		response = sync.upsert_lead_graph(payload())

		self.assertTrue(response["ok"], response)
		self.assertEqual(response["outcome"], "created")
		after = _counts()
		self.assertEqual(after["Lead"], before["Lead"] + 1)
		self.assertEqual(after["Address"], before["Address"] + 1)
		self.assertEqual(after["Connector Lead Link"], before["Connector Lead Link"] + 1)

		address = frappe.get_doc("Address", response["address"])
		# The state was derived from the city/pincode, not sent.
		self.assertEqual(address.state, "Gujarat")
		self.assertEqual(response["defaults_applied"].get("address.state"), "Gujarat")
		self.assertEqual(_lead_business(response["lead"]), BUSINESS)

	def test_declared_defaults_are_applied_and_reported(self):
		default_phone = uniq_phone()
		response = sync.upsert_lead_graph(
			payload(
				lead={
					"first_name": None,
					"last_name": None,
					"company_name": None,
					"emails": [],
					"phones": [default_phone],
				},
				address={
					"address_line1": "Plot 9, Industrial Estate",
					"city": "",
					"state": "Gujarat",
					"pincode": "",
				},
			)
		)

		self.assertTrue(response["ok"], response)
		defaults = response["defaults_applied"]
		self.assertEqual(defaults.get("lead.lead_name"), default_phone.replace(" ", ""))
		self.assertEqual(defaults.get("address.city"), sync.DEFAULT_ADDRESS_CITY)
		self.assertEqual(
			frappe.db.get_value("Address", response["address"], "city"), sync.DEFAULT_ADDRESS_CITY
		)

	def test_a_lead_without_an_address_block_gets_no_address(self):
		before = _counts()

		response = sync.upsert_lead_graph(
			payload(address=None, lead={"emails": [uniq_email("noaddr")], "phones": []})
		)

		self.assertTrue(response["ok"], response)
		self.assertIsNone(response["address"])
		self.assertEqual(_counts()["Address"], before["Address"])

	def test_attachments_land_on_the_lead(self):
		self.addCleanup(_remove_site_files)
		response = sync.upsert_lead_graph(
			payload(
				attachments=[
					{
						"file_name": "front.png",
						"content_base64": ONE_PIXEL_PNG,
						"kind": "front_card",
						"is_private": 1,
					}
				]
			)
		)

		self.assertTrue(response["ok"], response)
		self.assertEqual(len(response["attachments"]), 1)

		if frappe.get_meta("Lead").has_field("custom_front_card_image"):
			self.assertEqual(
				frappe.db.get_value("Lead", response["lead"], "custom_front_card_image"),
				response["attachments"][0],
			)
		else:
			# After the migration there is no field to point at, and none is needed: the File
			# is a plain attachment on the Lead, which is what the migration re-parented the
			# old card images into.
			self.assertTrue(
				frappe.db.exists(
					"File",
					{
						"file_url": response["attachments"][0],
						"attached_to_doctype": "Lead",
						"attached_to_name": response["lead"],
					},
				)
			)

	def test_a_corrupt_attachment_is_a_payload_error_not_a_retry(self):
		"""Bytes that are not the image the file name claims fail inside frappe's image
		handling. That is this payload's problem forever, so it must not come back as a
		retryable 500 -- and it must take the whole graph down with it."""
		import base64

		before = _counts()
		response = sync.upsert_lead_graph(
			payload(
				attachments=[
					{
						"file_name": "front.jpg",
						"content_base64": base64.b64encode(b"not a jpeg at all").decode(),
						"kind": "front_card",
					}
				]
			)
		)

		error = self.assertFailed(response, "VALIDATION_FAILED", status=422)
		self.assertEqual(error["field"], "attachments[0]")
		self.assertFalse(error["retryable"])
		self.assertNothingWritten(before)


class TestUpsertLeadGraphIdempotency(ConnectorAPITestCase):
	def test_the_same_request_id_twice_writes_one_lead_and_replays(self):
		data = payload()
		before = _counts()

		first = sync.upsert_lead_graph(data)
		self.assertTrue(first["ok"], first)

		# Same request id, DIFFERENT content: a replay answers with what was actually stored,
		# it does not quietly apply the new content under an old id.
		changed = dict(data)
		changed["lead"] = dict(data["lead"], first_name="Somebody Else")
		second = sync.upsert_lead_graph(changed)

		self.assertTrue(second["ok"], second)
		self.assertTrue(second["replayed"])
		self.assertEqual(second["lead"], first["lead"])
		self.assertEqual(_counts()["Lead"], before["Lead"] + 1)
		self.assertEqual(frappe.db.get_value("Lead", first["lead"], "first_name"), "Asha")

	def test_the_same_payload_with_a_new_request_id_is_unchanged(self):
		data = payload()
		first = sync.upsert_lead_graph(data)
		self.assertTrue(first["ok"], first)
		modified_before = frappe.db.get_value("Lead", first["lead"], "modified")

		again = dict(data)
		again["request_id"] = frappe.generate_hash(length=16)
		second = sync.upsert_lead_graph(again)

		self.assertTrue(second["ok"], second)
		self.assertTrue(second["unchanged"])
		self.assertFalse(second["replayed"])
		self.assertEqual(second["lead"], first["lead"])
		self.assertEqual(frappe.db.get_value("Lead", first["lead"], "modified"), modified_before)

	def test_the_same_vc_uuid_with_changed_content_updates_in_place(self):
		data = payload()
		before = _counts()

		first = sync.upsert_lead_graph(data)
		self.assertTrue(first["ok"], first)

		changed = dict(data)
		changed["request_id"] = frappe.generate_hash(length=16)
		changed["lead"] = dict(data["lead"], company_name="Verma Textiles Pvt Ltd")
		changed["address"] = dict(data["address"], address_line1="14 MG Road")
		second = sync.upsert_lead_graph(changed)

		self.assertTrue(second["ok"], second)
		self.assertEqual(second["outcome"], "updated")
		self.assertEqual(second["lead"], first["lead"])
		self.assertEqual(second["contact"], first["contact"])
		self.assertEqual(second["address"], first["address"])

		after = _counts()
		self.assertEqual(after["Lead"], before["Lead"] + 1)
		self.assertEqual(after["Address"], before["Address"] + 1)
		self.assertEqual(after["Connector Lead Link"], before["Connector Lead Link"] + 1)
		self.assertEqual(frappe.db.get_value("Lead", first["lead"], "company_name"), "Verma Textiles Pvt Ltd")
		self.assertEqual(frappe.db.get_value("Address", first["address"], "address_line1"), "14 MG Road")

	def test_a_lead_pushed_before_this_protocol_is_adopted_not_duplicated(self):
		legacy_email = uniq_email("legacy")
		legacy = frappe.get_doc(
			{
				"doctype": "Lead",
				"lead_name": "Legacy Traders",
				"company_name": "Legacy Traders",
				"email_id": legacy_email,
			}
		).insert(ignore_permissions=True)

		before = _counts()
		response = sync.upsert_lead_graph(
			payload(
				legacy_remote_name=legacy.name,
				lead={
					"first_name": None,
					"last_name": None,
					"company_name": "Legacy Traders",
					"emails": [legacy_email],
					"phones": [uniq_phone()],
				},
			)
		)

		self.assertTrue(response["ok"], response)
		self.assertEqual(response["outcome"], "adopted")
		self.assertEqual(response["lead"], legacy.name)
		after = _counts()
		self.assertEqual(after["Lead"], before["Lead"], "adoption must not create a second Lead")
		self.assertEqual(after["Connector Lead Link"], before["Connector Lead Link"] + 1)

	def test_a_second_central_record_cannot_claim_an_adopted_lead(self):
		contested_email = uniq_email("contested")
		legacy = frappe.get_doc(
			{
				"doctype": "Lead",
				"lead_name": "Contested Traders",
				"company_name": "Contested Traders",
				"email_id": contested_email,
			}
		).insert(ignore_permissions=True)

		first = sync.upsert_lead_graph(
			payload(
				legacy_remote_name=legacy.name,
				lead={"company_name": "Contested Traders", "emails": [contested_email], "phones": []},
			)
		)
		self.assertTrue(first["ok"], first)

		before = _counts()
		second = sync.upsert_lead_graph(
			payload(
				legacy_remote_name=legacy.name,
				lead={"company_name": "Contested Traders", "emails": [contested_email], "phones": []},
			)
		)

		self.assertFailed(second, "CONFLICT", status=409)
		self.assertNothingWritten(before)


class TestReadEndpoints(ConnectorAPITestCase):
	def test_ping_detects_rather_than_assumes(self):
		result = sync.ping()

		self.assertEqual(result["protocol"], sync.PROTOCOL)
		self.assertEqual(result["business_id"], BUSINESS)
		self.assertEqual(result["india_compliance"], "india_compliance" in frappe.get_installed_apps())
		self.assertIn(result["lead_source_field"], ("source", "utm_source"))
		self.assertEqual(
			result["lead_source_field"],
			"source" if frappe.get_meta("Lead").has_field("source") else "utm_source",
		)
		self.assertTrue(result["enabled"])
		self.assertTrue(result["connector_token_configured"])
		self.assertEqual(
			result["default_address_template"],
			bool(frappe.db.exists("Address Template", {})),
		)

	def test_ping_reports_a_site_that_cannot_store_addresses(self):
		"""A site with no Address Template refuses every Address. The central site has to be
		able to learn that before it pushes, not from ten thousand 422s."""
		frappe.db.delete("Address Template")
		frappe.clear_cache()
		self.assertFalse(sync.ping()["default_address_template"])

	def test_pull_changes_returns_what_was_pushed(self):
		data = payload()
		pushed = sync.upsert_lead_graph(data)
		self.assertTrue(pushed["ok"], pushed)

		page = sync.pull_changes(since="1900-01-01 00:00:00")

		self.assertTrue(page["ok"], page)
		found = [row for row in page["records"] if row["vc_uuid"] == pushed["vc_uuid"]]
		self.assertEqual(len(found), 1, page)
		self.assertEqual(found[0]["lead"], pushed["lead"])
		self.assertEqual(found[0]["emails"], data["lead"]["emails"])
		self.assertEqual(found[0]["phones"], [data["lead"]["phones"][0].replace(" ", "")])

	def test_pull_changes_needs_the_connector_token(self):
		_set_request(token=None)
		result = sync.pull_changes()
		self.assertFailed(result, "TOKEN_INVALID", status=401)

	def test_pull_changes_pages_with_a_cursor(self):
		for _ in range(3):
			self.assertTrue(sync.upsert_lead_graph(payload())["ok"])

		first = sync.pull_changes(since="1900-01-01 00:00:00", limit=1)
		self.assertEqual(first["count"], 1)
		self.assertTrue(first["has_more"])
		self.assertIsNotNone(first["cursor"])

		second = sync.pull_changes(cursor=first["cursor"], limit=1)
		self.assertEqual(second["count"], 1)
		self.assertNotEqual(second["records"][0]["lead"], first["records"][0]["lead"])


def _child_values(doctype, lead_name, fieldname):
	return sorted(
		frappe.get_all(
			doctype,
			filters={"parenttype": "Lead", "parent": lead_name},
			pluck=fieldname,
		)
	)


def _lead_emails(lead_name):
	"""The email addresses this Lead holds, in whichever shape the site under test is in.

	Before the legacy field migration that is ``ERPNext Lead Email``; after it that table is
	orphaned (its rows survive the drop, unreachable) and the Contact is the record. A test
	that only ever read the child table would pass against stale rows on a migrated site,
	which is worse than failing.
	"""
	if lead_has_contact_tables():
		return _child_values("ERPNext Lead Email", lead_name, "email_id")

	contact = frappe.db.get_value(
		"Dynamic Link",
		{"parenttype": "Contact", "link_doctype": "Lead", "link_name": lead_name},
		"parent",
	)
	if not contact:
		return sorted(value for value in [frappe.db.get_value("Lead", lead_name, "email_id")] if value)

	return sorted(frappe.get_all("Contact Email", filters={"parent": contact}, pluck="email_id"))


def _lead_business(lead_name):
	"""The business stamp, from the Custom Field or -- after the migration -- the link row."""
	if frappe.get_meta("Lead").has_field("custom_business_id"):
		return frappe.db.get_value("Lead", lead_name, "custom_business_id")

	return frappe.db.get_value("Connector Lead Link", {"lead": lead_name}, "business_id")


class TestAdoptionIsNotAWriteByPrimaryKey(ConnectorAPITestCase):
	"""`legacy_remote_name` is a primary key the CALLER supplies, and ERPNext Lead names are
	sequential and guessable. Without these guards, a valid token for business A was enough to
	overwrite, and re-stamp, any unlinked Lead on the site -- including business B's."""

	def _preexisting_lead(self, email, company="Preexisting Co"):
		return frappe.get_doc(
			{
				"doctype": "Lead",
				"lead_name": company,
				"company_name": company,
				"email_id": email,
			}
		).insert(ignore_permissions=True)

	def test_a_lead_stamped_for_another_business_cannot_be_adopted(self):
		email = uniq_email("otherbiz")
		victim = self._preexisting_lead(email)
		# Stamped directly: Lead.validate refuses a foreign stamp, which is exactly the guard
		# this endpoint was bypassing, so the row has to be put into that state by hand. After
		# the legacy field migration the stamp is the Connector Lead Link row, so that is where a
		# foreign claim has to be planted instead -- and the guard has to find it there.
		if frappe.get_meta("Lead").has_field("custom_business_id"):
			frappe.db.set_value("Lead", victim.name, "custom_business_id", OTHER_BUSINESS)
		else:
			link = frappe.new_doc("Connector Lead Link")
			link.lead = victim.name
			link.business_id = OTHER_BUSINESS
			link.vc_uuid = None
			link.flags.ignore_business_mismatch = True
			link.insert(ignore_permissions=True)
		frappe.clear_document_cache("Lead", victim.name)

		before = _counts()
		response = sync.upsert_lead_graph(
			payload(
				legacy_remote_name=victim.name,
				lead={
					"first_name": None,
					"last_name": None,
					"company_name": "Attacker Inc",
					"emails": [email],
					"phones": [],
				},
			)
		)

		error = self.assertFailed(response, "BUSINESS_MISMATCH", status=403)
		self.assertEqual(error["field"], "legacy_remote_name")
		self.assertNothingWritten(before)

		self.assertEqual(frappe.db.get_value("Lead", victim.name, "company_name"), "Preexisting Co")
		self.assertEqual(_lead_business(victim.name), OTHER_BUSINESS)
		self.assertEqual(_lead_emails(victim.name), [email])

	def test_a_lead_the_payload_cannot_identify_cannot_be_adopted(self):
		"""Knowing the name is not knowing the record. Same business, guessed name, a payload
		that shares no contact detail with the Lead it names -- refused, nothing overwritten."""
		email = uniq_email("bimal")
		victim = self._preexisting_lead(email, company="Preexisting Co 2")

		before = _counts()
		response = sync.upsert_lead_graph(
			payload(
				legacy_remote_name=victim.name,
				lead={
					"first_name": None,
					"last_name": None,
					"company_name": "Attacker Inc",
					"emails": [uniq_email("attacker")],
					"phones": [uniq_phone()],
				},
			)
		)

		error = self.assertFailed(response, "CONFLICT", status=409)
		self.assertEqual(error["field"], "legacy_remote_name")
		self.assertNothingWritten(before)
		self.assertEqual(frappe.db.get_value("Lead", victim.name, "company_name"), "Preexisting Co 2")
		self.assertEqual(_lead_emails(victim.name), [email])

	def test_adopting_a_lead_keeps_the_contact_details_the_client_already_had(self):
		"""The client's row predates the protocol, so the central record may not know all of
		it. Adoption merges; it must not wipe an email address no one asked it to remove."""
		known = uniq_email("known")
		only_on_client = uniq_email("clientside")
		values = {"doctype": "Lead", "lead_name": "Merge Traders", "company_name": "Merge Traders"}
		if lead_has_contact_tables():
			values["custom_email_ids"] = [{"email_id": known}, {"email_id": only_on_client}]
		else:
			values["email_id"] = known
		victim = frappe.get_doc(values).insert(ignore_permissions=True)

		if not lead_has_contact_tables():
			# The second address lives on the Contact ERPNext auto-created for this Lead --
			# that is where a migrated client keeps it.
			contact = frappe.get_doc(
				"Contact",
				frappe.db.get_value(
					"Dynamic Link",
					{"parenttype": "Contact", "link_doctype": "Lead", "link_name": victim.name},
					"parent",
				),
			)
			contact.append("email_ids", {"email_id": only_on_client})
			contact.save(ignore_permissions=True)

		new_email = uniq_email("central")
		response = sync.upsert_lead_graph(
			payload(
				legacy_remote_name=victim.name,
				lead={
					"first_name": None,
					"last_name": None,
					"company_name": "Merge Traders",
					"emails": [known, new_email],
					"phones": [],
				},
			)
		)

		self.assertTrue(response["ok"], response)
		self.assertEqual(response["outcome"], "adopted")
		self.assertEqual(response["lead"], victim.name)
		self.assertEqual(_lead_emails(victim.name), sorted([known, only_on_client, new_email]))

		contact_emails = sorted(
			frappe.get_all("Contact Email", filters={"parent": response["contact"]}, pluck="email_id")
		)
		self.assertEqual(contact_emails, sorted([known, only_on_client, new_email]))


class TestRequestIdIsScopedToTheRecord(ConnectorAPITestCase):
	def test_a_request_id_reused_for_another_record_is_refused_not_replayed(self):
		"""The reply used to be the FIRST record's stored response with ok/replayed true, so
		the central site marked a record synced that had never been written."""
		shared_request_id = frappe.generate_hash(length=16)

		first = sync.upsert_lead_graph(payload(request_id=shared_request_id))
		self.assertTrue(first["ok"], first)

		before = _counts()
		other_uuid = frappe.generate_hash(length=16)
		second = sync.upsert_lead_graph(
			payload(
				request_id=shared_request_id,
				vc_uuid=other_uuid,
				lead={"company_name": "Beta Ltd", "emails": [uniq_email("beta")], "phones": []},
			)
		)

		error = self.assertFailed(second, "CONFLICT", status=409)
		self.assertEqual(error["field"], "request_id")
		self.assertIn(first["vc_uuid"], error["message"])
		self.assertNothingWritten(before)
		self.assertFalse(frappe.db.exists("Connector Lead Link", {"vc_uuid": other_uuid}))
		self.assertEqual(frappe.get_all("Lead", filters={"company_name": "Beta Ltd"}), [])


class TestABrokenGraphCanBeRepaired(ConnectorAPITestCase):
	"""`unchanged: true` and `replayed: true` both repeat names this endpoint wrote earlier.
	An ordinary user deleting the Contact or the Address afterwards is allowed, and used to be
	permanent: the payload hash never changes for unchanged content, so every later push
	short-circuited and answered with the names of documents that no longer existed."""

	def _deleted_contact_and_address(self, response):
		frappe.delete_doc("Address", response["address"], ignore_permissions=True, force=True)
		frappe.delete_doc("Contact", response["contact"], ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.exists("Address", response["address"]))
		self.assertFalse(frappe.db.exists("Contact", response["contact"]))

	def test_an_identical_push_rebuilds_a_deleted_contact_and_address(self):
		data = payload()
		first = sync.upsert_lead_graph(data)
		self.assertTrue(first["ok"], first)
		self._deleted_contact_and_address(first)

		again = dict(data)
		again["request_id"] = frappe.generate_hash(length=16)
		second = sync.upsert_lead_graph(again)

		self.assertTrue(second["ok"], second)
		self.assertFalse(second["unchanged"], "a graph missing its Contact is not unchanged")
		self.assertEqual(second["lead"], first["lead"])
		self.assertTrue(frappe.db.exists("Contact", second["contact"]), second)
		self.assertTrue(frappe.db.exists("Address", second["address"]), second)

	def test_a_replay_of_the_same_request_id_rebuilds_it_too(self):
		data = payload()
		first = sync.upsert_lead_graph(data)
		self.assertTrue(first["ok"], first)
		self._deleted_contact_and_address(first)

		second = sync.upsert_lead_graph(dict(data))

		self.assertTrue(second["ok"], second)
		self.assertFalse(second["replayed"], "a replay must not name documents that are gone")
		self.assertEqual(second["lead"], first["lead"])
		self.assertTrue(frappe.db.exists("Contact", second["contact"]), second)
		self.assertTrue(frappe.db.exists("Address", second["address"]), second)

	def test_a_graph_that_is_whole_still_answers_unchanged(self):
		"""The repair path must not turn every no-op push into a rewrite."""
		data = payload()
		first = sync.upsert_lead_graph(data)
		self.assertTrue(first["ok"], first)
		modified_before = frappe.db.get_value("Lead", first["lead"], "modified")

		again = dict(data)
		again["request_id"] = frappe.generate_hash(length=16)
		second = sync.upsert_lead_graph(again)

		self.assertTrue(second["unchanged"], second)
		self.assertEqual(frappe.db.get_value("Lead", first["lead"], "modified"), modified_before)


def _addresses_of(lead_name):
	return frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "link_doctype": "Lead", "link_name": lead_name},
		pluck="parent",
		order_by="creation asc",
	)


def _address_row(name):
	return frappe.db.get_value(
		"Address", name, ["address_title", "address_line1", "city", "pincode", "address_type"], as_dict=True
	)


def _contact_emails(contact):
	return sorted(frappe.get_all("Contact Email", filters={"parent": contact}, pluck="email_id"))


def _contact_phones(contact):
	return sorted(frappe.get_all("Contact Phone", filters={"parent": contact}, pluck="phone"))


class TestAnAddressKeepsWhatThePayloadDoesNotSend(ConnectorAPITestCase):
	"""A field the payload does not carry is not a field the payload deleted.

	The pincode is the one that matters, because after Patch B it has no other home: Patch A
	moved it off ``custom_pincode``, Patch B dropped that Custom Field, and the archive JSON
	is the only remaining copy. Blanking it on an ordinary push destroys the value the whole
	migration was built to preserve -- and it answered ok:true, warnings [].
	"""

	def _pushed_lead(self):
		first = sync.upsert_lead_graph(payload())
		self.assertTrue(first["ok"], first)
		return first

	def test_a_push_without_a_pincode_does_not_blank_the_one_on_the_address(self):
		first = self._pushed_lead()
		self.assertEqual(_address_row(first["address"]).pincode, "380015")

		second = sync.upsert_lead_graph(
			payload(
				vc_uuid=frappe.db.get_value("Connector Lead Link", first["link"], "vc_uuid"),
				request_id=frappe.generate_hash(length=16),
				lead={"status": "Open"},
				address={
					"address_line1": "Plot 9, MIDC",
					"city": "Pune",
					"country": "India",
					"state": "Maharashtra",
					"pincode": None,
				},
			)
		)

		self.assertTrue(second["ok"], second)
		self.assertEqual(second["outcome"], "updated")
		row = _address_row(second["address"])
		self.assertEqual(row.pincode, "380015", "the pincode was blanked by a push that never mentioned it")
		# The rest of the address IS the central record's to change.
		self.assertEqual(row.address_line1, "Plot 9, MIDC")
		self.assertTrue(
			any("kept its existing pincode" in warning for warning in second["warnings"]),
			second["warnings"],
		)

	def test_a_pincode_the_payload_does_send_still_wins(self):
		first = self._pushed_lead()
		second = sync.upsert_lead_graph(
			payload(
				vc_uuid=frappe.db.get_value("Connector Lead Link", first["link"], "vc_uuid"),
				address={"pincode": "411045"},
			)
		)

		self.assertTrue(second["ok"], second)
		self.assertEqual(_address_row(second["address"]).pincode, "411045")

	def test_a_long_company_name_does_not_truncate_the_scanned_marker(self):
		"""The marker is how the endpoint knows to leave that row alone. address_title is 100
		characters, so appending the suffix and THEN truncating would cut the marker off a long
		company name -- and an unrecognisable scanned address is one the next push overwrites."""
		from ai_snaplead_connector.legacy.schema import is_scanned_address_title, scanned_address_title

		title = scanned_address_title("Ziegler Manufacturing and Precision Components " * 3)

		self.assertLessEqual(len(title), 100)
		self.assertTrue(is_scanned_address_title(title), title)

	def test_a_push_never_rewrites_the_address_the_migration_made_to_hold_a_pincode(self):
		"""Patch A's conflict rule: a Lead that already had a human-entered address with a
		DIFFERENT pincode keeps that one and the scanned pincode goes on a second "(Scanned)"
		Address. Resolving "the Address of this Lead" by `modified desc` handed the next push
		exactly that second row, so one ordinary push rewrote its line, city, type and pincode
		and undid the rule -- while the human address it was protecting sat untouched."""
		from ai_snaplead_connector.legacy.migrate import ensure_pincode_on_address

		first = self._pushed_lead()
		lead = first["lead"]
		human = _addresses_of(lead)[0]
		frappe.db.set_value("Address", human, {"address_line1": "Human typed line", "pincode": "400001"})

		conflict = ensure_pincode_on_address(lead, "411099")
		self.assertEqual(conflict["outcome"], "conflict", conflict)
		scanned = conflict["address"]
		self.assertNotEqual(scanned, human)

		second = sync.upsert_lead_graph(
			payload(
				vc_uuid=frappe.db.get_value("Connector Lead Link", first["link"], "vc_uuid"),
				lead={"status": "Open"},
				address={
					"address_line1": "Central HQ, 5th Floor",
					"city": "Delhi",
					"state": "Delhi",
					"country": "India",
					"pincode": None,
				},
			)
		)

		self.assertTrue(second["ok"], second)
		self.assertEqual(second["address"], human, "the push wrote to the scanned address")

		kept = _address_row(scanned)
		self.assertEqual(kept.pincode, "411099", "the scanned pincode was overwritten")
		self.assertEqual(kept.address_type, "Other")
		self.assertTrue(kept.address_title.endswith("(Scanned)"), kept.address_title)

		written = _address_row(human)
		self.assertEqual(written.address_line1, "Central HQ, 5th Floor")
		self.assertEqual(written.pincode, "400001", "the human pincode was blanked")


class TestTheAdoptionMergeSurvivesTheNextPush(ConnectorAPITestCase):
	"""Adoption merged the client's own contact details into the payload -- and the very next
	ordinary push deleted them again.

	``_upsert_contact`` replaces email_ids/phone_nos wholesale whenever the payload sent any,
	and the link row stores the hash of the payload as SENT, so the second push -- differing
	in one unrelated field -- ran as "updated" with the payload's own short lists. Those are
	the values Patch A moved off custom_email_ids / custom_mobile_numbers, and Patch B has
	dropped those tables, so the Contact was the only live copy.
	"""

	def _adopted(self):
		central_email = uniq_email("central")
		client_email = uniq_email("clientonly")
		central_phone = uniq_phone()
		client_phone = uniq_phone()

		values = {"doctype": "Lead", "lead_name": "ADV Merge Victim", "company_name": "AdvCo"}
		if lead_has_contact_tables():
			values["custom_email_ids"] = [{"email_id": central_email}, {"email_id": client_email}]
			values["custom_mobile_numbers"] = [
				{"mobile_number": central_phone},
				{"mobile_number": client_phone},
			]
		else:
			values["email_id"] = central_email
			values["mobile_no"] = central_phone
		victim = frappe.get_doc(values).insert(ignore_permissions=True)

		if not lead_has_contact_tables():
			contact = frappe.get_doc("Contact", sync._linked_document("Contact", victim.name))
			contact.append("email_ids", {"email_id": client_email})
			contact.append("phone_nos", {"phone": client_phone})
			contact.save(ignore_permissions=True)

		uuid = frappe.generate_hash(length=16)
		first = sync.upsert_lead_graph(
			payload(
				vc_uuid=uuid,
				legacy_remote_name=victim.name,
				address={},
				lead={
					"first_name": None,
					"last_name": None,
					"lead_name": "ADV Merge Victim",
					"company_name": "AdvCo",
					"emails": [central_email],
					"phones": [central_phone],
					"status": "Lead",
				},
			)
		)
		self.assertTrue(first["ok"], first)
		self.assertEqual(first["outcome"], "adopted")
		return first, uuid, central_email, client_email, central_phone, client_phone

	def test_a_second_ordinary_push_does_not_delete_what_adoption_preserved(self):
		first, uuid, central_email, client_email, central_phone, client_phone = self._adopted()
		self.assertIn(client_email, _contact_emails(first["contact"]))

		second = sync.upsert_lead_graph(
			payload(
				vc_uuid=uuid,
				address={},
				lead={
					"first_name": None,
					"last_name": None,
					"lead_name": "ADV Merge Victim",
					"company_name": "AdvCo",
					"emails": [central_email],
					"phones": [central_phone],
					"status": "Open",
				},
			)
		)

		self.assertTrue(second["ok"], second)
		self.assertEqual(second["outcome"], "updated")
		self.assertFalse(second["unchanged"], second)

		emails = _contact_emails(second["contact"])
		phones = _contact_phones(second["contact"])
		self.assertIn(client_email, emails, emails)
		self.assertIn(normalize_mobile_for_auth(client_phone), phones, phones)
		self.assertIn(central_email, emails, emails)

	def test_a_value_the_client_deletes_is_not_put_back(self):
		"""The merge preserves; it must not resurrect. A client who removes the address by
		hand has removed it, and a push that re-added it would make it unremovable."""
		first, uuid, central_email, client_email, central_phone, _client_phone = self._adopted()

		# "The client deletes it" means from wherever THIS site keeps the list. Before the
		# legacy field migration that is the Lead's own table; after it, the Contact.
		contact = frappe.get_doc("Contact", first["contact"])
		contact.set("email_ids", [row for row in contact.email_ids if row.email_id != client_email])
		contact.save(ignore_permissions=True)
		if lead_has_contact_tables():
			frappe.db.delete(
				"ERPNext Lead Email",
				{"parent": first["lead"], "parenttype": "Lead", "email_id": client_email},
			)
			frappe.clear_document_cache("Lead", first["lead"])

		second = sync.upsert_lead_graph(
			payload(
				vc_uuid=uuid,
				address={},
				lead={
					"first_name": None,
					"last_name": None,
					"lead_name": "ADV Merge Victim",
					"company_name": "AdvCo",
					"emails": [central_email],
					"phones": [central_phone],
					"status": "Replied",
				},
			)
		)

		self.assertTrue(second["ok"], second)
		self.assertNotIn(client_email, _contact_emails(second["contact"]))

	def test_a_value_the_central_record_starts_sending_becomes_the_central_records(self):
		"""Once the central record sends it, it is the central record's to remove -- which is
		the protocol's rule for everything the central record owns."""
		_first, uuid, central_email, client_email, central_phone, _client_phone = self._adopted()

		sent_both = sync.upsert_lead_graph(
			payload(
				vc_uuid=uuid,
				address={},
				lead={
					"first_name": None,
					"last_name": None,
					"lead_name": "ADV Merge Victim",
					"company_name": "AdvCo",
					"emails": [central_email, client_email],
					"phones": [central_phone],
					"status": "Open",
				},
			)
		)
		self.assertTrue(sent_both["ok"], sent_both)
		self.assertIn(client_email, _contact_emails(sent_both["contact"]))

		removed = sync.upsert_lead_graph(
			payload(
				vc_uuid=uuid,
				address={},
				lead={
					"first_name": None,
					"last_name": None,
					"lead_name": "ADV Merge Victim",
					"company_name": "AdvCo",
					"emails": [central_email],
					"phones": [central_phone],
					"status": "Open",
				},
			)
		)
		self.assertTrue(removed["ok"], removed)
		self.assertNotIn(client_email, _contact_emails(removed["contact"]))


class TestOneCardIsOneFileRow(ConnectorAPITestCase):
	"""The card image piled up. Every write re-inserted a File row for the same attachment,
	and because frappe dedupes the BYTES by content hash, every row pointed at the same
	file_url -- so the Lead's sidebar grew one more copy of the same card per push, without
	bound, and the row count _discard_files reads grew with it.

	The assertions are "does not grow", not "is exactly one": on a client that still has the
	legacy Custom Fields, frappe itself adds a second row for the Attach field the card is
	pointed at. That row is created once for a value, not once per push, and it is not what
	this is about.
	"""

	def _rows(self, lead_name):
		return sorted(
			frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Lead", "attached_to_name": lead_name},
				pluck="file_url",
			)
		)

	def test_the_same_card_pushed_three_times_adds_no_file_rows(self):
		self.addCleanup(_remove_site_files)
		card = {"file_name": "card-front.png", "content_base64": ONE_PIXEL_PNG, "kind": "front_card"}

		first = sync.upsert_lead_graph(payload(lead={"status": "Lead"}, attachments=[card]))
		self.assertTrue(first["ok"], first)
		lead = first["lead"]
		uuid = frappe.db.get_value("Connector Lead Link", first["link"], "vc_uuid")
		after_one = self._rows(lead)

		for status in ("Open", "Replied"):
			response = sync.upsert_lead_graph(
				payload(vc_uuid=uuid, lead={"status": status}, attachments=[dict(card)])
			)
			self.assertTrue(response["ok"], response)
			# Not a retry: the payload-hash short-circuit is not what is being tested here.
			self.assertFalse(response["unchanged"], response)
			self.assertEqual(response["attachments"], first["attachments"])
			self.assertEqual(self._rows(lead), after_one, self._rows(lead))

		self.assertEqual(len(set(after_one)), 1, after_one)

	def test_a_genuinely_different_attachment_still_lands(self):
		"""The guard is "these exact bytes are already on this Lead", not "this Lead has an
		attachment". Plain text on purpose: frappe rewrites an uploaded image, and two tests
		sharing one image's bytes share its file on disk."""
		import base64

		self.addCleanup(_remove_site_files)
		one = {"file_name": "scan-one.txt", "content_base64": base64.b64encode(b"vc scan one").decode()}
		two = {"file_name": "scan-two.txt", "content_base64": base64.b64encode(b"vc scan two").decode()}

		first = sync.upsert_lead_graph(payload(attachments=[dict(one)]))
		self.assertTrue(first["ok"], first)
		uuid = frappe.db.get_value("Connector Lead Link", first["link"], "vc_uuid")
		self.assertEqual(len(self._rows(first["lead"])), 1, self._rows(first["lead"]))

		second = sync.upsert_lead_graph(payload(vc_uuid=uuid, attachments=[dict(one), dict(two)]))

		self.assertTrue(second["ok"], second)
		self.assertEqual(len(second["attachments"]), 2, second["attachments"])
		self.assertEqual(len(self._rows(second["lead"])), 2, self._rows(second["lead"]))


class TestAnEarlierRequestIsNeverReapplied(ConnectorAPITestCase):
	"""request_id has to be idempotent for EVERY request, not only the most recent one.

	The link row keeps one `last_request_id`, so a re-delivery of any earlier request used to
	miss the replay lookup, fail the payload-hash comparison as well (the hash on the row is
	the LATER payload's), and fall through to the write -- silently re-applying the stale
	payload over the newer one and answering ok:true outcome:"updated" replayed:false. An
	at-least-once transport is the entire reason request_id exists, and those reorder and
	redeliver as a matter of course.
	"""

	def _push(self, request_id, uuid, email, phone, status, job_title):
		return sync.upsert_lead_graph(
			payload(
				request_id=request_id,
				vc_uuid=uuid,
				lead={
					"first_name": "Stale",
					"last_name": "Case",
					"company_name": "Stale Traders",
					"emails": [email],
					"phones": [phone],
					"status": status,
					"job_title": job_title,
				},
			)
		)

	def test_redelivering_an_earlier_request_does_not_revert_the_record(self):
		uuid = frappe.generate_hash(length=16)
		email = uniq_email("stale")
		phone = uniq_phone()
		first_id = frappe.generate_hash(length=16)

		first = self._push(first_id, uuid, email, phone, "Lead", "Manager")
		self.assertTrue(first["ok"], first)
		lead = first["lead"]

		second = self._push(frappe.generate_hash(length=16), uuid, email, phone, "Open", "Director")
		self.assertTrue(second["ok"], second)
		self.assertEqual(second["outcome"], "updated")
		self.assertEqual(second["lead"], lead)
		self.assertEqual(
			frappe.db.get_value("Lead", lead, ["status", "job_title"], as_dict=True),
			frappe._dict(status="Open", job_title="Director"),
		)

		before = _counts()
		# The first call again, byte for byte -- a duplicate delivery, not a new intent.
		replay = self._push(first_id, uuid, email, phone, "Lead", "Manager")

		self.assertTrue(replay["ok"], replay)
		self.assertTrue(replay["replayed"], replay)
		self.assertTrue(replay.get("superseded"), replay)
		self.assertTrue(replay["unchanged"], replay)
		self.assertEqual(replay["lead"], lead)
		self.assertTrue(
			any("superseded" in warning for warning in (replay.get("warnings") or [])),
			replay.get("warnings"),
		)
		self.assertNothingWritten(before)
		# The newer central data is still what the client holds.
		self.assertEqual(
			frappe.db.get_value("Lead", lead, ["status", "job_title"], as_dict=True),
			frappe._dict(status="Open", job_title="Director"),
		)

	def test_the_history_is_bounded(self):
		"""The row remembers a window of request ids, not an unbounded list -- a link row is
		not an audit log and must not grow without limit."""
		history = None
		for _index in range(sync.REQUEST_HISTORY_LIMIT + 5):
			history = sync._extended_request_history(history, f"REQ-{_index}")

		ids = sync._seen_request_ids(history)
		self.assertEqual(len(ids), sync.REQUEST_HISTORY_LIMIT)
		self.assertEqual(ids[-1], f"REQ-{sync.REQUEST_HISTORY_LIMIT + 4}")

	def test_a_repeated_request_id_is_not_stored_twice(self):
		history = sync._extended_request_history(None, "REQ-A")
		history = sync._extended_request_history(history, "REQ-B")
		history = sync._extended_request_history(history, "REQ-A")

		self.assertEqual(sync._seen_request_ids(history), ["REQ-B", "REQ-A"])


class TestAMissingAddressTemplateIsASetupGap(ConnectorAPITestCase):
	"""A site created without the setup wizard has no Address Template, and frappe refuses to
	save ANY Address without one. That used to surface as a bare ERPNext ValidationError,
	which the contract types as VALIDATION_FAILED -- a 422 marked NOT retryable -- so a
	conforming central site marked every card-scan carrying an address permanently failed for
	a client-side gap a human fixes in a minute.
	"""

	def _remove_templates(self):
		self.removed = frappe.get_all("Address Template", pluck="name")
		for name in self.removed:
			frappe.db.delete("Address Template", {"name": name})
		frappe.clear_cache()

	def test_an_address_without_a_template_is_setup_required_and_retryable(self):
		self._remove_templates()
		self.addCleanup(_ensure_address_template)
		before = _counts()

		response = sync.upsert_lead_graph(payload())

		error = self.assertFailed(response, "SETUP_REQUIRED", status=409)
		self.assertTrue(error["retryable"], error)
		self.assertIn("Address Template", error["message"])
		self.assertEqual(error["field"], "address")
		# The whole graph is rolled back, as ever -- but the caller is told to keep the record.
		self.assertNothingWritten(before)

	def test_ping_reports_the_same_gap_before_anything_is_pushed(self):
		self._remove_templates()
		self.addCleanup(_ensure_address_template)

		self.assertFalse(sync.ping()["default_address_template"])

	def test_a_payload_with_no_address_is_unaffected(self):
		"""The gap is about Addresses. A card with no address block must still land."""
		self._remove_templates()
		self.addCleanup(_ensure_address_template)
		data = payload()
		data.pop("address")

		response = sync.upsert_lead_graph(data)

		self.assertTrue(response["ok"], response)
		self.assertIsNone(response.get("address"))


class TestAConcurrentDeskSaveIsRetryable(ConnectorAPITestCase):
	"""Somebody pressing Ctrl+S on the Lead while a push is in flight is a race, not a
	verdict. It used to be folded into CONFLICT, whose contract says re-sending the same
	bytes will not help -- so the record was dropped permanently by any central site that
	honours `error.retryable`.
	"""

	def test_a_timestamp_mismatch_is_not_a_conflict(self):
		exc = frappe.exceptions.TimestampMismatchError("Document has been modified after you have opened it")

		code, message, field = sync._classify(exc)

		self.assertEqual(code, "CONCURRENT_MODIFICATION")
		self.assertIn("Re-send this payload unchanged", message)
		self.assertIsNone(field)

	def test_the_contract_marks_it_retryable_at_409(self):
		self.assertIn("CONCURRENT_MODIFICATION", sync.RETRYABLE_CODES)
		self.assertEqual(sync.ERROR_STATUS["CONCURRENT_MODIFICATION"], 409)
		# A real duplicate is still a permanent refusal.
		self.assertNotIn("CONFLICT", sync.RETRYABLE_CODES)
		self.assertIn("SETUP_REQUIRED", sync.RETRYABLE_CODES)

	def test_a_desk_save_mid_push_answers_retryable_and_writes_nothing(self):
		"""End to end: the Lead really is written, somebody else really saves it, and the
		push really fails -- with a code the central site is allowed to retry."""
		before = _counts()
		real_save = sync._save_doc
		fired = {"done": False}

		def racing_save(doc):
			if doc.doctype == "Lead" and not fired["done"]:
				fired["done"] = True
				raise frappe.exceptions.TimestampMismatchError(
					"Document has been modified after you have opened it"
				)
			return real_save(doc)

		sync._save_doc = racing_save
		self.addCleanup(setattr, sync, "_save_doc", real_save)

		first = sync.upsert_lead_graph(payload())
		self.assertTrue(first["ok"], first)
		uuid = frappe.db.get_value("Connector Lead Link", first["link"], "vc_uuid")
		before = _counts()

		response = sync.upsert_lead_graph(payload(vc_uuid=uuid, lead={"company_name": "Renamed Mid Flight"}))

		sync._save_doc = real_save

		self.assertTrue(fired["done"], "the racing save never ran; the test proved nothing")
		error = self.assertFailed(response, "CONCURRENT_MODIFICATION", status=409)
		self.assertTrue(error["retryable"], error)
		self.assertNothingWritten(before)
		self.assertNotEqual(frappe.db.get_value("Lead", first["lead"], "company_name"), "Renamed Mid Flight")

		# ... and the very same bytes, re-sent, now succeed. That is what retryable means.
		retry = sync.upsert_lead_graph(payload(vc_uuid=uuid, lead={"company_name": "Renamed Mid Flight"}))
		self.assertTrue(retry["ok"], retry)
		self.assertEqual(frappe.db.get_value("Lead", first["lead"], "company_name"), "Renamed Mid Flight")


class TestASecondaryContactIsNotAWayInForADuplicate(ConnectorAPITestCase):
	"""A Lead's second and third email address live on its Contact, and after the legacy
	Custom Fields are dropped that is the ONLY place they live. A guard that reads the child
	tables and the two stock Lead columns therefore compared primary against primary: the
	same business card, re-pushed under a new vc_uuid with the addresses in a different
	order, created a second Lead for the same person, with one address sitting on two Leads
	and two Contacts.
	"""

	def test_a_new_uuid_reusing_a_secondary_email_is_refused(self):
		primary = uniq_email("gamma-primary")
		secondary = uniq_email("gamma-secondary")

		first = sync.upsert_lead_graph(payload(lead={"first_name": "Gamma", "emails": [primary, secondary]}))
		self.assertTrue(first["ok"], first)
		contact = frappe.get_doc("Contact", first["contact"])
		self.assertEqual(sorted(row.email_id for row in contact.email_ids), sorted([primary, secondary]))

		before = _counts()
		# Same person, same card, new central record, addresses the other way round.
		response = sync.upsert_lead_graph(payload(lead={"first_name": "Impostor", "emails": [secondary]}))

		self.assertFailed(response, "VALIDATION_FAILED")
		self.assertNothingWritten(before)

	def test_a_new_uuid_reusing_a_secondary_phone_is_refused(self):
		primary = uniq_phone()
		secondary = uniq_phone()

		first = sync.upsert_lead_graph(payload(lead={"first_name": "Gamma", "phones": [primary, secondary]}))
		self.assertTrue(first["ok"], first)

		before = _counts()
		response = sync.upsert_lead_graph(payload(lead={"first_name": "Impostor", "phones": [secondary]}))

		self.assertFailed(response, "VALIDATION_FAILED")
		self.assertNothingWritten(before)

	def test_an_unrelated_contact_still_lands(self):
		"""The guard must refuse a duplicate, not every second Lead."""
		first = sync.upsert_lead_graph(payload(lead={"emails": [uniq_email("one"), uniq_email("one-alt")]}))
		self.assertTrue(first["ok"], first)

		second = sync.upsert_lead_graph(payload(lead={"emails": [uniq_email("two"), uniq_email("two-alt")]}))

		self.assertTrue(second["ok"], second)
		self.assertNotEqual(second["lead"], first["lead"])

	def test_the_guard_reaches_a_contact_this_app_never_wrote(self):
		"""The Contact is reached through Dynamic Link, which is how stock ERPNext attaches
		one -- including the Contact `Lead.before_insert` creates by itself. So a Lead that
		predates this app is covered too."""
		from ai_snaplead_connector.utils.lead_contacts import find_duplicate_lead_by_contacts

		stock_email = uniq_email("stock-primary")
		extra_email = uniq_email("stock-secondary")
		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"lead_name": "Stock Traders",
				"company_name": "Stock Traders",
				"email_id": stock_email,
			}
		).insert(ignore_permissions=True)

		contact_name = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Lead", "link_name": lead.name, "parenttype": "Contact"},
			"parent",
		)
		self.assertIsNotNone(contact_name, "ERPNext did not auto-create the Contact")
		contact = frappe.get_doc("Contact", contact_name)
		contact.append("email_ids", {"email_id": extra_email, "is_primary": 0})
		contact.save(ignore_permissions=True)

		found = find_duplicate_lead_by_contacts(business_ids=[], email_ids=[extra_email])

		self.assertIsNotNone(found, "the secondary address on a stock Contact was invisible")
		self.assertEqual(found["lead"], lead.name)
		self.assertEqual(found["type"], "email")

	def test_a_dropped_business_field_is_never_read_as_a_stamp(self):
		"""The cause behind the whole class, pinned on its own.

		``frappe.db.get_value`` does not fail on a fieldname the DocType no longer has -- it
		hands the FIELDNAME BACK as a literal. ``custom_business_id`` is one of the Custom
		Fields Patch B drops, so on a migrated site every candidate Lead was read as being
		stamped for a business called "custom_business_id", matched no scope, and was thrown
		away: a configured client came out of the migration with the duplicate guard switched
		off entirely, for primary contacts as much as secondary ones.
		"""
		from ai_snaplead_connector.utils.lead_contacts import _candidate_business_stamp

		created = sync.upsert_lead_graph(payload())
		self.assertTrue(created["ok"], created)

		stamp = _candidate_business_stamp(created["lead"])

		self.assertNotEqual(stamp, "custom_business_id")
		self.assertEqual(stamp, BUSINESS)

	def test_the_guard_still_runs_when_a_business_is_configured(self):
		"""The broken read only ever mattered on a CONFIGURED site: an empty scope excludes
		nothing, so the bug was invisible on a site whose Settings was still blank."""
		from ai_snaplead_connector.utils.lead_contacts import find_duplicate_lead_by_contacts

		email = uniq_email("scoped")
		created = sync.upsert_lead_graph(payload(lead={"emails": [email, uniq_email("scoped-alt")]}))
		self.assertTrue(created["ok"], created)

		self.assertIsNotNone(
			find_duplicate_lead_by_contacts(business_ids=[BUSINESS], email_ids=[email]),
			"a configured site found no candidate at all",
		)


class TestTheRequestWindowSaysWhenItCannotAnswer(ConnectorAPITestCase):
	"""The idempotency history is a WINDOW, and the window used to be silent about its edge.

	At fifty remembered ids, the fifty-first push pushed the first request id out of the
	list. A re-delivery of that request then missed the replay lookup, failed the
	payload-hash comparison (the stored hash is the newer payload's), fell through to the
	write and re-applied the stale payload over the newer one -- answering ok:true,
	outcome:"updated", replayed:false, warnings:[]. Fifty pushes of work reverted and
	reported as a clean success.

	Two things are proved here: a re-delivery anywhere INSIDE the window is still refused a
	write, and a re-delivery that could be one of the ids the row has forgotten is written
	but never again reported as an ordinary update -- the answer names what could not be
	checked.
	"""

	def setUp(self):
		super().setUp()
		# Five, so the boundary can be crossed in a handful of pushes instead of five
		# hundred. The production value is REQUEST_HISTORY_LIMIT itself.
		self.real_limit = sync.REQUEST_HISTORY_LIMIT
		sync.REQUEST_HISTORY_LIMIT = 5
		self.addCleanup(setattr, sync, "REQUEST_HISTORY_LIMIT", self.real_limit)

		self.uuid = frappe.generate_hash(length=16)
		self.email = uniq_email("window")
		self.phone = uniq_phone()

	def _push(self, request_id, job_title):
		return sync.upsert_lead_graph(
			payload(
				request_id=request_id,
				vc_uuid=self.uuid,
				lead={
					"first_name": "Window",
					"last_name": "Case",
					"company_name": "Window Traders",
					"emails": [self.email],
					"phones": [self.phone],
					"job_title": job_title,
				},
			)
		)

	def test_the_production_window_is_large_enough_to_be_a_window(self):
		"""Fifty was the number that made the failure reachable in an afternoon."""
		self.assertGreaterEqual(self.real_limit, 500)

	def test_a_redelivery_at_the_far_edge_of_the_window_is_still_refused_a_write(self):
		first_id = frappe.generate_hash(length=16)
		first = self._push(first_id, "TITLE-000")
		self.assertTrue(first["ok"], first)
		lead = first["lead"]

		# Fill the window exactly: the first id is the oldest one still remembered.
		for index in range(1, sync.REQUEST_HISTORY_LIMIT):
			self.assertTrue(self._push(frappe.generate_hash(length=16), f"TITLE-{index:03d}")["ok"])

		self.assertEqual(frappe.db.get_value("Lead", lead, "job_title"), "TITLE-004")

		before = _counts()
		replay = self._push(first_id, "TITLE-000")

		self.assertTrue(replay["replayed"], replay)
		self.assertTrue(replay.get("superseded"), replay)
		self.assertNothingWritten(before)
		# The stale payload was NOT applied.
		self.assertEqual(frappe.db.get_value("Lead", lead, "job_title"), "TITLE-004")

	def test_a_redelivery_past_the_window_is_written_but_never_reported_as_clean(self):
		first_id = frappe.generate_hash(length=16)
		first = self._push(first_id, "TITLE-000")
		lead = first["lead"]

		# One more push than the window holds, so the first id is genuinely forgotten.
		for index in range(1, sync.REQUEST_HISTORY_LIMIT + 1):
			self.assertTrue(self._push(frappe.generate_hash(length=16), f"TITLE-{index:03d}")["ok"])

		link = frappe.db.get_value("Connector Lead Link", {"vc_uuid": self.uuid}, "seen_request_ids")
		self.assertNotIn(first_id, sync._seen_request_ids(link))
		self.assertGreaterEqual(sync._forgotten_request_count(link), 1)

		replay = self._push(first_id, "TITLE-000")

		# It cannot be refused -- a genuinely new request id looks exactly like this one --
		# but the answer says so instead of claiming a clean update.
		self.assertTrue(replay["ok"], replay)
		self.assertTrue(
			any("could not be checked" in warning for warning in (replay.get("warnings") or [])),
			replay.get("warnings"),
		)
		self.assertEqual(frappe.db.get_value("Lead", lead, "job_title"), "TITLE-000")

	def test_an_ordinary_push_inside_the_window_carries_no_such_warning(self):
		self._push(frappe.generate_hash(length=16), "TITLE-000")
		second = self._push(frappe.generate_hash(length=16), "TITLE-001")

		self.assertEqual(
			[warning for warning in (second.get("warnings") or []) if "could not be checked" in warning],
			[],
			second.get("warnings"),
		)


class TestTheRequestHistoryFormat(IntegrationTestCase):
	"""The stored shape, on its own. It has to count what it drops, and it has to keep
	reading rows written before it started counting."""

	def test_the_window_counts_what_it_forgets(self):
		history = None
		for index in range(sync.REQUEST_HISTORY_LIMIT + 7):
			history = sync._extended_request_history(history, f"REQ-{index}")

		ids = sync._seen_request_ids(history)
		self.assertEqual(len(ids), sync.REQUEST_HISTORY_LIMIT)
		self.assertEqual(sync._forgotten_request_count(history), 7)
		self.assertEqual(ids[-1], f"REQ-{sync.REQUEST_HISTORY_LIMIT + 6}")

	def test_nothing_forgotten_while_the_window_is_not_full(self):
		history = sync._extended_request_history(None, "REQ-A")
		history = sync._extended_request_history(history, "REQ-B")

		self.assertEqual(sync._seen_request_ids(history), ["REQ-A", "REQ-B"])
		self.assertEqual(sync._forgotten_request_count(history), 0)

	def test_a_row_written_before_this_release_is_still_readable(self):
		"""Rows in the field hold a bare JSON array and know nothing about what they
		dropped. They are read as "nothing forgotten", which is what they believed."""
		legacy = json.dumps(["REQ-A", "REQ-B"])

		self.assertEqual(sync._seen_request_ids(legacy), ["REQ-A", "REQ-B"])
		self.assertEqual(sync._forgotten_request_count(legacy), 0)

		grown = sync._extended_request_history(legacy, "REQ-C")
		self.assertEqual(sync._seen_request_ids(grown), ["REQ-A", "REQ-B", "REQ-C"])

	def test_rubbish_is_not_an_exception(self):
		self.assertEqual(sync._seen_request_ids("not json"), [])
		self.assertEqual(sync._forgotten_request_count("not json"), 0)


class TestCorrectedScannedTextReachesTheClient(ConnectorAPITestCase):
	"""`lead.scanned_text` after the Custom Field has been dropped.

	`_route_dropped_lead_fields` defers the value and `_settle_deferred_fields` hands it to
	`write_scanned_note`, which is tag-idempotent: it found its own
	`<!--vc-connector:scanned_text-->` CRM Note and returned {"outcome": "already"} WITHOUT
	updating it, and the return value was discarded. So every corrected or re-OCR'd scan
	after the first was accepted with a 200, warned about nothing, and stored nowhere.
	"""

	def _push(self, uuid, email, phone, text):
		return sync.upsert_lead_graph(
			payload(
				request_id=frappe.generate_hash(length=16),
				vc_uuid=uuid,
				lead={
					"first_name": "Scan",
					"last_name": "Case",
					"company_name": "Scan Traders",
					"emails": [email],
					"phones": [phone],
					"scanned_text": text,
				},
			)
		)

	def _stored_text(self, lead):
		from ai_snaplead_connector.legacy.migrate import SCANNED_TEXT_TAG

		if frappe.get_meta("Lead").has_field("custom_scanned_text"):
			return frappe.db.get_value("Lead", lead, "custom_scanned_text")

		notes = frappe.get_all(
			"CRM Note",
			filters={"parent": lead, "parenttype": "Lead", "note": ("like", f"%{SCANNED_TEXT_TAG}%")},
			pluck="note",
		)
		return "\n".join(notes)

	def test_a_corrected_scan_replaces_the_first_one(self):
		uuid = frappe.generate_hash(length=16)
		email = uniq_email("scan")
		phone = uniq_phone()

		first = self._push(uuid, email, phone, "FIRST SCAN TEXT")
		self.assertTrue(first["ok"], first)
		lead = first["lead"]
		self.assertIn("FIRST SCAN TEXT", self._stored_text(lead))

		second = self._push(uuid, email, phone, "SECOND SCAN TEXT CORRECTED")
		self.assertTrue(second["ok"], second)
		self.assertEqual(second["lead"], lead)

		stored = self._stored_text(lead)
		self.assertIn("SECOND SCAN TEXT CORRECTED", stored)
		self.assertNotIn("FIRST SCAN TEXT", stored)

	def test_the_corrected_text_does_not_pile_up_a_second_note(self):
		if frappe.get_meta("Lead").has_field("custom_scanned_text"):
			self.skipTest("this site still has custom_scanned_text; there is no note to count")

		from ai_snaplead_connector.legacy.migrate import SCANNED_TEXT_TAG

		uuid = frappe.generate_hash(length=16)
		email = uniq_email("scan")
		phone = uniq_phone()

		lead = self._push(uuid, email, phone, "ONE")["lead"]
		self._push(uuid, email, phone, "TWO")
		self._push(uuid, email, phone, "THREE")

		notes = frappe.get_all(
			"CRM Note",
			filters={"parent": lead, "parenttype": "Lead", "note": ("like", f"%{SCANNED_TEXT_TAG}%")},
			pluck="name",
		)
		self.assertEqual(len(notes), 1)

	def test_the_migration_itself_is_still_write_once(self):
		"""The migration copies a field that is about to be dropped: re-running it must not
		rewrite or duplicate the note. Only the live push updates."""
		from ai_snaplead_connector.legacy.migrate import write_scanned_note

		uuid = frappe.generate_hash(length=16)
		lead = self._push(uuid, uniq_email("scan"), uniq_phone(), "ORIGINAL")["lead"]

		if frappe.get_meta("Lead").has_field("custom_scanned_text"):
			self.skipTest("this site still has custom_scanned_text; the note path is not in use")

		result = write_scanned_note(lead, "SOMETHING ELSE")
		self.assertEqual(result["outcome"], "already")
		self.assertIn("ORIGINAL", self._stored_text(lead))

		updated = write_scanned_note(lead, "SOMETHING ELSE", update_existing=True)
		self.assertEqual(updated["outcome"], "updated")
		self.assertIn("SOMETHING ELSE", self._stored_text(lead))

	def test_the_same_text_twice_is_not_a_rewrite(self):
		from ai_snaplead_connector.legacy.migrate import write_scanned_note

		uuid = frappe.generate_hash(length=16)
		lead = self._push(uuid, uniq_email("scan"), uniq_phone(), "STEADY")["lead"]

		if frappe.get_meta("Lead").has_field("custom_scanned_text"):
			self.skipTest("this site still has custom_scanned_text; the note path is not in use")

		self.assertEqual(write_scanned_note(lead, "STEADY", update_existing=True)["outcome"], "already")


class TestClearingAContactClearsBothHalvesOfTheRecord(ConnectorAPITestCase):
	"""`lead.emails: []` wiped every Contact Email row and left Lead.email_id standing.

	The Lead then advertised an address the central record had deleted -- for ever, and to
	every stock ERPNext list view, report and email workflow, which read the Lead -- while
	the duplicate guard went on reserving that address for it. No warning was emitted. The
	same asymmetry applied to `phones` / `mobile_no`.
	"""

	def _push(self, uuid, emails, phones):
		return sync.upsert_lead_graph(
			payload(
				request_id=frappe.generate_hash(length=16),
				vc_uuid=uuid,
				lead={
					"first_name": "Empty",
					"last_name": "Case",
					"company_name": "Empty Traders",
					"emails": emails,
					"phones": phones,
				},
			)
		)

	def _contact_emails(self, lead):
		contacts = frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": "Contact", "link_doctype": "Lead", "link_name": lead},
			pluck="parent",
		)
		if not contacts:
			return []
		return frappe.get_all("Contact Email", filters={"parent": ("in", contacts)}, pluck="email_id")

	def test_an_empty_email_list_clears_the_lead_as_well_as_the_contact(self):
		uuid = frappe.generate_hash(length=16)
		first_email = uniq_email("empty")
		second_email = uniq_email("empty")
		phone = uniq_phone()

		created = self._push(uuid, [first_email, second_email], [phone])
		lead = created["lead"]
		self.assertEqual(frappe.db.get_value("Lead", lead, "email_id"), first_email)
		self.assertEqual(sorted(self._contact_emails(lead)), sorted([first_email, second_email]))

		cleared = self._push(uuid, [], [phone])
		self.assertTrue(cleared["ok"], cleared)
		self.assertEqual(cleared["lead"], lead)

		self.assertEqual(self._contact_emails(lead), [])
		self.assertFalse(frappe.db.get_value("Lead", lead, "email_id"))

		# The warning comes from _project_cleared_contact, which only runs once the legacy
		# child tables are gone -- before that, _replace_rows keeps the two halves in step and
		# there is no separate projection to announce. The clearing itself is asserted above
		# either way; this last assertion is about the migrated shape only.
		if lead_has_contact_tables():
			return

		self.assertTrue(
			any("email_id was cleared" in warning for warning in (cleared.get("warnings") or [])),
			cleared.get("warnings"),
		)

	def test_an_omitted_email_key_is_not_a_cleared_one(self):
		"""The rule is "a field the payload omits is not a field the payload cleared" -- so
		leaving `emails` out entirely must leave both halves exactly as they were."""
		uuid = frappe.generate_hash(length=16)
		email = uniq_email("kept")
		phone = uniq_phone()

		lead = self._push(uuid, [email], [phone])["lead"]

		sent = payload(request_id=frappe.generate_hash(length=16), vc_uuid=uuid)
		# Replaced, not merged: `emails` has to be genuinely absent from this payload.
		sent["lead"] = {
			"first_name": "Empty",
			"last_name": "Case",
			"company_name": "Empty Traders Ltd",
			"phones": [phone],
		}
		response = sync.upsert_lead_graph(sent)

		self.assertTrue(response["ok"], response)
		self.assertEqual(frappe.db.get_value("Lead", lead, "email_id"), email)
		self.assertEqual(self._contact_emails(lead), [email])

	def test_an_empty_phone_list_clears_the_mobile_projection(self):
		uuid = frappe.generate_hash(length=16)
		email = uniq_email("empty")
		phone = uniq_phone()

		lead = self._push(uuid, [email], [phone])["lead"]
		self.assertTrue(frappe.db.get_value("Lead", lead, "mobile_no"))

		cleared = self._push(uuid, [email], [])
		self.assertTrue(cleared["ok"], cleared)
		self.assertFalse(frappe.db.get_value("Lead", lead, "mobile_no"))


class TestAnAddressKeepsEveryFieldThePayloadDoesNotSend(ConnectorAPITestCase):
	"""The rule is "a field the payload omits is not a field the payload cleared", and it
	used to be implemented for `pincode` ALONE.

	`address_line2` was written from the payload unconditionally, so an omitted line2 blanked
	the stored one; `city` fell back to the declared default "-" and overwrote a real stored
	city, reported as a "default applied" rather than as the overwrite it was; `state` was
	blanked to "" -- and on a non-India country nothing even warned. A push that narrowed a
	Lead's street address therefore destroyed the rest of its address, and the response
	warned about the pincode it had KEPT while saying nothing about the three fields it had
	just erased.
	"""

	def _push(self, uuid, email, phone, address):
		sent = payload(
			request_id=frappe.generate_hash(length=16),
			vc_uuid=uuid,
			lead={
				"first_name": "Addr",
				"last_name": "Tester",
				"company_name": "Addr Traders",
				"emails": [email],
				"phones": [phone],
			},
		)
		# `payload` MERGES a dict override into its base. This class is about what happens
		# when a field is absent, so the block has to be sent exactly as written.
		sent["address"] = address
		return sync.upsert_lead_graph(sent)

	def _stored(self, name):
		return frappe.db.get_value(
			"Address",
			name,
			["address_line1", "address_line2", "city", "state", "country", "pincode"],
			as_dict=True,
		)

	def test_a_narrower_push_does_not_erase_the_rest_of_the_address(self):
		uuid = frappe.generate_hash(length=16)
		email = uniq_email("addr")
		phone = uniq_phone()

		first = self._push(
			uuid,
			email,
			phone,
			{
				"address_line1": "12 Main Road",
				"address_line2": "Suite 4B",
				"city": "Mumbai",
				"state": "Maharashtra",
				"country": "India",
				"pincode": "400001",
			},
		)
		self.assertTrue(first["ok"], first)
		address = first["address"]
		self.assertEqual(
			self._stored(address),
			frappe._dict(
				address_line1="12 Main Road",
				address_line2="Suite 4B",
				city="Mumbai",
				state="Maharashtra",
				country="India",
				pincode="400001",
			),
		)

		second = self._push(uuid, email, phone, {"address_line1": "12 Main Road, Block C"})

		self.assertTrue(second["ok"], second)
		self.assertEqual(second["address"], address)
		stored = self._stored(address)
		self.assertEqual(stored.address_line1, "12 Main Road, Block C")
		self.assertEqual(stored.address_line2, "Suite 4B")
		self.assertEqual(stored.city, "Mumbai")
		self.assertEqual(stored.state, "Maharashtra")
		self.assertEqual(stored.pincode, "400001")
		# And it SAID so, rather than reporting a "-" as a default politely applied.
		self.assertNotIn("address.city", second.get("defaults_applied") or {})
		kept = [warning for warning in (second.get("warnings") or []) if "kept its stored" in warning]
		self.assertTrue(kept, second.get("warnings"))
		for fieldname in ("address_line2", "city", "state"):
			self.assertIn(fieldname, kept[0])

	def test_a_state_is_not_lost_on_a_country_that_derives_nothing(self):
		"""Outside India there is no derivation to fall back on, so the stored state was
		blanked with no warning at all."""
		uuid = frappe.generate_hash(length=16)
		email = uniq_email("addr")
		phone = uniq_phone()

		first = self._push(
			uuid,
			email,
			phone,
			{
				"address_line1": "500 5th Avenue",
				"city": "New York",
				"state": "New York",
				"country": "United States",
				"pincode": "10110",
			},
		)
		address = first["address"]

		self._push(
			uuid, email, phone, {"address_line1": "500 5th Avenue, Floor 3", "country": "United States"}
		)

		stored = self._stored(address)
		self.assertEqual(stored.state, "New York")
		self.assertEqual(stored.city, "New York")

	def test_a_value_the_payload_does_send_still_wins(self):
		uuid = frappe.generate_hash(length=16)
		email = uniq_email("addr")
		phone = uniq_phone()

		first = self._push(
			uuid,
			email,
			phone,
			{
				"address_line1": "12 Main Road",
				"address_line2": "Suite 4B",
				"city": "Mumbai",
				"state": "Maharashtra",
				"country": "India",
				"pincode": "400001",
			},
		)
		address = first["address"]

		self._push(
			uuid,
			email,
			phone,
			{
				"address_line1": "12 Main Road",
				"address_line2": "Suite 9A",
				"city": "Pune",
				"state": "Maharashtra",
				"country": "India",
				"pincode": "411001",
			},
		)

		stored = self._stored(address)
		self.assertEqual(stored.address_line2, "Suite 9A")
		self.assertEqual(stored.city, "Pune")
		self.assertEqual(stored.pincode, "411001")

	def test_a_brand_new_address_still_gets_the_declared_defaults(self):
		"""Keeping what is stored must not stop a FIRST address being completed -- the
		default city is still applied, and still reported."""
		response = self._push(
			frappe.generate_hash(length=16),
			uniq_email("addr"),
			uniq_phone(),
			{"address_line1": "9 Nowhere Lane", "country": "India", "state": "Gujarat"},
		)

		self.assertTrue(response["ok"], response)
		self.assertEqual((response.get("defaults_applied") or {}).get("address.city"), "-")
		self.assertEqual(self._stored(response["address"]).city, "-")


class TestSharedContactsDoNotFreezeTheExistingLeadBook(ConnectorAPITestCase):
	"""The duplicate guard runs on EVERY Desk save, not only on connector pushes, and it
	covers phone numbers -- which stock ERPNext never deduplicated.

	Two Leads that legitimately share a number (a shared office landline, a switchboard, a
	receptionist's mobile) each named the other, so after installing this app neither could
	be saved again: every save was refused over a value that save had not touched, and the
	only way out was to edit data that was correct. Refusing the save does not un-share the
	number. It only stops the row being edited.
	"""

	SHARED_NUMBER = "02212345678"

	def _seed_pre_existing(self, lead_name, mobile_no, email_id=None):
		"""A Lead as it was BEFORE this app was installed: written without this app's
		validate, because that is exactly when these rows were written."""
		doc = frappe.new_doc("Lead")
		doc.update(
			{
				"lead_name": lead_name,
				"first_name": lead_name,
				"company_name": f"{lead_name} Pvt Ltd",
				"mobile_no": mobile_no,
				"email_id": email_id,
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		return doc.name

	def setUp(self):
		super().setUp()
		self.shared = f"0221{next(_SEQUENCE):07d}"
		self.first = self._seed_pre_existing("Ravi Shared", self.shared)
		self.second = self._seed_pre_existing("Meena Shared", self.shared)

	def test_both_leads_are_still_saveable(self):
		for name in (self.first, self.second):
			doc = frappe.get_doc("Lead", name)
			doc.company_name = f"{doc.company_name} (edited)"
			doc.save()  # must not raise
			self.assertEqual(frappe.db.get_value("Lead", name, "mobile_no"), self.shared)

	def test_a_save_that_introduces_somebody_elses_number_is_still_refused(self):
		fresh = self._seed_pre_existing("Third Party", f"0999{next(_SEQUENCE):07d}")

		doc = frappe.get_doc("Lead", fresh)
		doc.mobile_no = self.shared

		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save()

		self.assertIn(self.shared, str(caught.exception))

	def test_a_brand_new_lead_reusing_the_number_is_still_refused(self):
		doc = frappe.new_doc("Lead")
		doc.update(
			{
				"lead_name": "Fourth Party",
				"first_name": "Fourth",
				"company_name": "Fourth Pvt Ltd",
				"mobile_no": self.shared,
			}
		)

		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_the_refusal_names_a_table_that_exists_on_this_site(self):
		"""The old text always said "its Mobile Numbers table". That is a legacy Custom
		Field child table, and it is exactly what the migration drops -- so on a migrated or
		freshly installed client the refusal told the user to edit something their Desk does
		not have."""
		doc = frappe.new_doc("Lead")
		doc.update(
			{
				"lead_name": "Fifth Party",
				"first_name": "Fifth",
				"company_name": "Fifth Pvt Ltd",
				"mobile_no": self.shared,
			}
		)

		with self.assertRaises(frappe.ValidationError) as caught:
			doc.insert(ignore_permissions=True)

		message = str(caught.exception)
		if lead_has_contact_tables():
			self.assertIn("Mobile Numbers", message)
		else:
			self.assertNotIn("Mobile Numbers", message)
			self.assertIn("Contact", message)

	def test_the_pre_flight_report_names_the_pair_before_anything_is_refused(self):
		from ai_snaplead_connector.utils.lead_contacts import duplicate_contacts_report

		report = duplicate_contacts_report()
		clash = [entry for entry in report["mobiles"] if self.first in entry["leads"]]

		self.assertTrue(clash, report["mobiles"])
		self.assertIn(self.second, clash[0]["leads"])

	def test_only_the_values_this_save_introduces_are_checked(self):
		from ai_snaplead_connector.utils.lead_contacts import newly_introduced_contacts

		new_emails, new_mobiles = newly_introduced_contacts(self.first, [], [self.shared, "09999000011"])

		self.assertEqual(new_emails, [])
		self.assertEqual(new_mobiles, ["09999000011"])

	def test_a_respelling_of_a_held_number_is_not_a_new_value(self):
		from ai_snaplead_connector.utils.lead_contacts import newly_introduced_contacts

		held = frappe.db.get_value("Lead", self.first, "mobile_no")
		_emails, new_mobiles = newly_introduced_contacts(self.first, [], [f" {held} "])

		self.assertEqual(new_mobiles, [])

	def test_a_lead_that_never_had_a_contact_detail_is_still_editable(self):
		"""Stock ERPNext accepts a Lead with a name and nothing else. This app requires a
		contact detail, and enforcing that on every save made every such pre-existing row
		permanently unsaveable -- on the review site, two of its four Leads."""
		name = self._seed_pre_existing("No Contact Co", None)

		doc = frappe.get_doc("Lead", name)
		doc.company_name = "No Contact Co (edited)"
		doc.save()  # must not raise

		self.assertEqual(frappe.db.get_value("Lead", name, "company_name"), "No Contact Co (edited)")

	def test_a_new_lead_with_no_contact_detail_at_all_is_still_refused(self):
		doc = frappe.new_doc("Lead")
		doc.update({"lead_name": "Nameless Co", "company_name": "Nameless Co"})

		with self.assertRaises(frappe.ValidationError) as caught:
			doc.insert(ignore_permissions=True)

		self.assertIn("At least one contact detail", str(caught.exception))

	def test_deleting_the_last_contact_detail_a_lead_had_is_still_refused(self):
		"""Grandfathering is about rows that never had one -- not a licence to empty a row
		that does."""
		name = self._seed_pre_existing("Has Contact Co", f"0221{next(_SEQUENCE):07d}")

		doc = frappe.get_doc("Lead", name)
		doc.mobile_no = None
		if lead_has_contact_tables():
			doc.set("custom_mobile_numbers", [])

		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save()

		self.assertIn("At least one contact detail", str(caught.exception))

	def test_a_connector_push_reusing_the_number_is_still_refused(self):
		"""The guard must still do its job on the path it was written for."""
		before = _counts()

		response = sync.upsert_lead_graph(
			payload(
				lead={
					"first_name": "Impostor",
					"last_name": "Case",
					"company_name": "Impostor Traders",
					"emails": [uniq_email("impostor")],
					"phones": [self.shared],
				}
			)
		)

		self.assertFailed(response, "VALIDATION_FAILED")
		self.assertNothingWritten(before)


class TestAPushIsNotLostToClientSideSetup(ConnectorAPITestCase):
	"""Two real QA failures, both of which lost a lead the app had reported as SENT.

	Reported 2026-09-25 from qa-sigzen16: a sync produced "cannot create UTM Source -
	permission" and "email mandatory", and the leads never arrived on the client even though
	the mobile app showed the push as successful. Both are client-side SETUP problems -- a
	role the client never granted, a field the client marked mandatory -- and neither is
	something the central record can fix or the salesperson can act on.
	"""

	def _source_doctype(self):
		return "UTM Source" if sync._lead_source_fieldname() == "utm_source" else "Lead Source"

	def _drop_custom_field(self, fieldname):
		"""Remove the Custom Field this test added, and COMMIT -- it is DDL, so the class
		rollback will not undo it and the next suite inherits a mandatory field it knows
		nothing about."""
		frappe.set_user("Administrator")
		name = frappe.db.get_value("Custom Field", {"dt": "Lead", "fieldname": fieldname}, "name")
		if name:
			try:
				frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
				frappe.db.commit()
			except Exception:
				frappe.clear_last_message()
		frappe.clear_cache(doctype="Lead")

	def test_a_source_the_client_has_never_seen_is_created_not_refused(self):
		"""The payload carries the source as a STRING; Lead.utm_source is a LINK."""
		doctype = self._source_doctype()
		source = f"VC Test Source {frappe.generate_hash(length=6)}"
		self.assertFalse(frappe.db.exists(doctype, source))

		response = sync.upsert_lead_graph(payload(lead={"source": source}))

		self.assertTrue(response["ok"], response)
		self.assertTrue(
			frappe.db.exists(doctype, source),
			"the source master record was not created on the client",
		)
		stored = frappe.db.get_value("Lead", response["lead"], sync._lead_source_fieldname())
		self.assertEqual(stored, source)

	def test_creating_the_same_source_twice_is_not_an_error(self):
		"""Two pushes can race for one new source; the loser must not fail the lead."""
		doctype = self._source_doctype()
		source = f"VC Test Source {frappe.generate_hash(length=6)}"

		self.assertEqual(sync.ensure_lead_source(source), source)
		self.assertEqual(sync.ensure_lead_source(source), source)
		self.assertTrue(frappe.db.exists(doctype, source))

	def test_a_blank_source_creates_nothing(self):
		"""An empty source must not create a master record named ''."""
		self.assertIsNone(sync.ensure_lead_source(None))
		self.assertIsNone(sync.ensure_lead_source(""))

	def test_a_mandatory_custom_field_on_the_client_does_not_lose_the_lead(self):
		"""THE second failure. A client can mark ANY field mandatory, including a custom one
		this protocol has never heard of. Refusing the push there loses the lead entirely."""
		fieldname = "custom_vc_forced_mandatory"
		# This field MUST be removed again, not merely uncached. A Custom Field is DDL: it
		# survives the class rollback, and a mandatory one left on Lead makes every later
		# suite on this site fail with "Value missing for Lead: Forced Mandatory". That is
		# exactly what happened on ankitpulp the first time this test ran.
		self.addCleanup(self._drop_custom_field, fieldname)
		# Cleanups run last-registered-first, so this rollback runs BEFORE the drop above.
		# The drop has to commit (it is DDL), and a commit makes permanent whatever this test
		# still has pending: every run used to leave one Lead, its Address, its link row and
		# a log row on the site. Discard them first.
		self.addCleanup(frappe.db.rollback)
		if not frappe.db.exists("Custom Field", {"dt": "Lead", "fieldname": fieldname}):
			frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": "Lead",
					"fieldname": fieldname,
					"label": "Forced Mandatory",
					"fieldtype": "Data",
					"reqd": 1,
					"insert_after": "company_name",
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
			frappe.clear_cache(doctype="Lead")

		before = frappe.db.count("Lead")
		response = sync.upsert_lead_graph(payload())

		self.assertTrue(response["ok"], response)
		self.assertEqual(frappe.db.count("Lead"), before + 1)
		self.assertFalse(
			frappe.db.get_value("Lead", response["lead"], fieldname),
			"the field was left unfilled, which is the point -- the client fills it later",
		)


class TestANewLeadReachesARealPerson(ConnectorAPITestCase):
	"""A Lead that nobody owns is, in practice, a Lead that never arrived.

	The connector writes as the integration user, so without routing every synced Lead is
	owned by an account no salesperson logs into and assigned to nobody -- readable, but in
	no one's queue and matching none of the "my leads" filters a client's team works from.
	Central reports the push as a success and the client reports the lead as missing, and
	both are telling the truth.
	"""

	def _route_to(self, user, assign=0):
		for field, value in (("default_lead_owner", user), ("assign_new_leads", assign)):
			frappe.db.sql(
				"""insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)""",
				(SETTINGS, field, value),
			)
		_forget_settings()

	def _salesperson(self):
		email = f"vcroute-{frappe.generate_hash(length=6)}@vctest.example"
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Route", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.db.delete("User", {"name": email}))
		return user.name

	def test_a_created_lead_is_handed_to_the_configured_owner(self):
		user = self._salesperson()
		self._route_to(user, assign=1)

		response = sync.upsert_lead_graph(**payload())
		self.assertTrue(response["ok"], response)

		lead = frappe.get_doc("Lead", response["lead"])
		self.assertEqual(lead.lead_owner, user)
		self.assertIn(user, lead.get("_assign") or "")

	def test_the_payload_cannot_name_an_owner_today(self):
		"""Routing is a CLIENT-side decision, and this test is the boundary marker.

		`lead_owner` is not in the accepted field set, so a central site cannot assign a
		lead to an arbitrary user of someone else's ERPNext. _apply_lead_routing still
		defers to a payload-supplied owner -- so if this field is ever added, precedence is
		already right and this test is what tells you to check it.
		"""
		user = self._salesperson()
		named = self._salesperson()
		self._route_to(user, assign=0)

		response = sync.upsert_lead_graph(**payload(lead={"lead_owner": named}))
		error = self.assertFailed(response, "VALIDATION_FAILED")
		self.assertEqual(error["field"], "lead.lead_owner")

	def test_an_unset_owner_changes_nothing(self):
		"""The default for the setting is blank, and blank must mean stock ERPNext
		behaviour -- not a Lead routed to whatever get_value happened to return."""
		response = sync.upsert_lead_graph(**payload())
		self.assertTrue(response["ok"], response)
		self.assertEqual(frappe.db.get_value("Lead", response["lead"], "lead_owner"), frappe.session.user)

	def test_an_owner_that_no_longer_exists_does_not_lose_the_lead(self):
		"""A User can be deleted after the setting names it. Losing a lead over a stale
		routing setting would be absurd, so the Lead lands and simply is not routed."""
		self._route_to("deleted-user@vctest.example", assign=1)

		response = sync.upsert_lead_graph(**payload())
		self.assertTrue(response["ok"], response)
		self.assertEqual(frappe.db.get_value("Lead", response["lead"], "lead_owner"), frappe.session.user)
