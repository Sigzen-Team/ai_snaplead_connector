# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Tests for the two-patch legacy Custom Field migration.

Nothing here commits, and nothing here calls ``migrate.run`` or a successful ``drop.run``:
both of those commit by design, and a test suite that is pointed at a real site must not be
able to drop that site's Custom Fields. The per-record entry points (``migrate_lead``,
``verify_lead``, ``archive.write_lead``) do all the work and roll back with the class, and the
one thing worth asserting about ``drop.run`` is that it REFUSES -- which writes nothing.

The bugs each of these caught, in the order they were found:

* an unticked Check reads as the string "0", so every Lead on the site looked like it carried
  a legacy value and got a mark row;
* ``CRM Note`` is autoincrement and SQLite has no sequences, so every scanned-text note was
  silently lost on the client target;
* ``("not in", ("", None))`` is ``x NOT IN ('', NULL)``, which matches nothing at all, so a
  record whose only legacy value sat on an otherwise-NULL column was never archived;
* a re-enqueue of Patch A could not fix anything, because Patch A skips a Lead already marked
  Migrated;
* an ordinary business-card phone spelling ("9876543210 (O)", two numbers in one field) fails
  Frappe's PHONE_NUMBER_PATTERN, and the exception rolled back the Lead's WHOLE migration --
  its pincode, its scanned text and its card images with it -- for ever, with no way to skip it;
* two numbers differing only in country code collapsed to one, and the gate asked the same
  question and so could not see the one that was dropped;
* two Leads scanned from the same card share one File row (frappe dedupes on content hash), so
  re-parenting by file_url moved a single attachment back and forth between them for ever;
* a Lead deleted after Patch A marked it left an archive row nothing could ever fill in, and
  Patch B refused to drop while any row was unarchived -- permanently, on any client.
"""

import json
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from sigzenlead_connector.legacy import archive, drop, migrate, restore, state, verify
from sigzenlead_connector.legacy.schema import (
	KEPT_LEAD_FIELD,
	LEGACY_LEAD_DATA_FIELDS,
	LEGACY_LEAD_LAYOUT_FIELDS,
	LEGACY_SOURCE_DATA_FIELDS,
	LEGACY_SOURCE_LAYOUT_FIELDS,
	SCAN_SOURCE_NAME,
	SCANNED_TEXT_TAG,
	UNSTORABLE_CONTACT_TAG,
	get_lead_source_fieldname,
	lead_has_legacy_fields,
)

LEGACY_PRESENT = lead_has_legacy_fields()
_SEQUENCE = frappe.generate_hash(length=6)


def _uniq(tag):
	return f"{tag}-{frappe.generate_hash(length=8)}"


SETTINGS = "Connector Settings"


def _snapshot_settings():
	"""Connector Settings is a Single: it survives an ordinary rollback."""
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


class LegacyMigrationTestCase(IntegrationTestCase):
	"""Own the tenancy setting instead of inheriting whoever ran last.

	These tests deliberately build Leads stamped with several different businesses --
	"legacy-biz", "undo-biz", "intact-biz" -- because that is what a real client's table
	looks like before the migration. The tenancy guard in sigzenlead_connector/tenancy.py refuses a
	Lead whose business does not match the site's, and it is switched on by
	`reject_business_mismatch` on the settings Single.

	Nothing in this module used to set that. So the result depended entirely on suite ORDER:
	run alone the settings were empty and everything passed, run after test_sync_api -- which
	commits business_id VCTEST-BIZ with rejection ON -- and six tests died with
	"This Lead belongs to business legacy-biz, but this site is configured for business
	VCTEST-BIZ". Order-dependent tests are worse than failing ones, and this app is about to
	be published, so a contributor running the suite in a different order must not see them.
	"""

	def setUp(self):
		super().setUp()
		self.saved_settings = _snapshot_settings()
		self.addCleanup(_restore_settings, self.saved_settings)
		frappe.db.delete("Singles", {"doctype": SETTINGS})
		# Rejection OFF: this module is about MIGRATING pre-existing rows, whatever business
		# they carry. Refusing them is the paired-site behaviour, covered in test_tenancy.
		for field, value in (("enabled", 1), ("reject_business_mismatch", 0)):
			frappe.db.sql(
				"""insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)""",
				(SETTINGS, field, value),
			)
		frappe.clear_document_cache(SETTINGS, SETTINGS)


def _make_lead(**legacy):
	lead = frappe.new_doc("Lead")
	lead.first_name = _uniq("LegacyTest")
	lead.company_name = "VC Legacy Test Co"
	lead.country = "India"

	emails = legacy.pop("emails", None) or []
	mobiles = legacy.pop("mobiles", None) or []
	for email in emails:
		lead.append("custom_email_ids", {"email_id": email})
	for mobile in mobiles:
		lead.append("custom_mobile_numbers", {"mobile_number": mobile})
	if not emails and not mobiles:
		lead.email_id = _uniq("legacy") + "@vclegacy.example"

	for fieldname, value in legacy.items():
		lead.set(fieldname, value)

	lead.insert(ignore_permissions=True)
	return lead


def _contact_of(lead):
	name = migrate._linked_document("Contact", lead)
	return frappe.get_doc("Contact", name) if name else None


def _add_legacy_row(lead, parentfield, child_doctype, values):
	"""Write a legacy child row the way a client's OLD data got there: straight to the table.

	Not through ``Lead.save``: this app's own Lead override projects row 1 onto the stock
	``email_id`` / ``mobile_no``, where ERPNext validates it and throws. The values these tests
	care about are precisely the ones that cannot pass that validation, and they are sitting in
	real clients' tables because they were written before any of this existed.
	"""
	row = frappe.new_doc(child_doctype)
	row.update(values)
	row.parent = lead
	row.parenttype = "Lead"
	row.parentfield = parentfield
	row.idx = frappe.db.count(child_doctype, {"parent": lead, "parentfield": parentfield}) + 1
	row.name = frappe.generate_hash(length=10)
	row.db_insert()
	return row.name


def _notes_of(lead, tag):
	return frappe.get_all(
		"CRM Note",
		filters={"parent": lead, "parenttype": "Lead", "parentfield": "notes", "note": ("like", f"%{tag}%")},
		pluck="note",
	)


def _phones_of(contact):
	return [row.phone for row in (contact.phone_nos or [])]


def _emails_of(contact):
	return [row.email_id for row in (contact.email_ids or [])]


def _attach(lead, content, field=None, file_name=None):
	"""A File row on this Lead. Identical CONTENT deliberately gives a shared file_url:
	frappe dedupes uploads by content hash, so this is the ordinary shape of two Leads
	scanned from the same card."""
	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name or (_uniq("card") + ".txt"),
			"attached_to_doctype": "Lead",
			"attached_to_name": lead,
			"attached_to_field": field,
			"is_private": 1,
			"content": content,
		}
	).insert(ignore_permissions=True)
	if field:
		frappe.db.set_value("Lead", lead, field, doc.file_url)
	return doc


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestPatchACopiesAndRepeats(LegacyMigrationTestCase):
	def test_an_unticked_check_is_not_a_legacy_value(self):
		"""custom_created_from_scan = 0 must not make a Lead look like it carries data.

		cstr(0) is "0", which is truthy, so this gave EVERY Lead on the site a mark row --
		200k rows in a table that is supposed to hold only the migrated ones.
		"""
		lead = _make_lead(custom_created_from_scan=0)
		# CustomLead.before_insert adopts the stock email_id into custom_email_ids, so a Lead
		# with truly nothing in the legacy fields has to have those rows removed first -- which
		# is exactly the shape of a client's ordinary, never-scanned Lead.
		frappe.db.delete("ERPNext Lead Email", {"parent": lead.name, "parenttype": "Lead"})
		frappe.db.delete("ERPNext Lead Mobile Number", {"parent": lead.name, "parenttype": "Lead"})

		self.assertIsNone(migrate.migrate_lead(lead.name))
		self.assertFalse(
			frappe.db.exists("Connector Legacy Field Archive", {"lookup_key": f"Lead|{lead.name}"})
		)

	def test_emails_and_phones_land_on_the_contact_once(self):
		email = _uniq("both") + "@vclegacy.example"
		lead = _make_lead(emails=[email], mobiles=["+91 98765 12345", "09876512345", "9876512345"])

		migrate.migrate_lead(lead.name)
		contact = _contact_of(lead.name)
		self.assertIsNotNone(contact)
		self.assertIn(email, [row.email_id for row in contact.email_ids])
		# Three spellings of one number are one phone.
		matching = [row.phone for row in contact.phone_nos if "9876512345" in row.phone.replace(" ", "")]
		self.assertEqual(len(matching), 1, [row.phone for row in contact.phone_nos])

		before = (len(contact.email_ids), len(contact.phone_nos))
		# A second pass with the mark row removed exercises the per-VALUE idempotency, not the
		# per-record skip -- that is the layer that stops a re-run duplicating child rows.
		frappe.db.delete("Connector Legacy Field Archive", {"reference_name": lead.name})
		migrate.migrate_lead(lead.name)
		contact = _contact_of(lead.name)
		self.assertEqual((len(contact.email_ids), len(contact.phone_nos)), before)

	def test_a_marked_lead_is_skipped_but_a_failed_one_is_retried(self):
		lead = _make_lead(custom_pincode="411001")
		self.assertIsNotNone(migrate.migrate_lead(lead.name))
		self.assertIsNone(migrate.migrate_lead(lead.name))

		row = frappe.db.get_value(
			"Connector Legacy Field Archive", {"lookup_key": f"Lead|{lead.name}"}, "name"
		)
		frappe.db.set_value("Connector Legacy Field Archive", row, "migration_status", "Failed")
		self.assertIsNotNone(
			migrate.migrate_lead(lead.name),
			"a Lead marked Failed must be retried, or Patch B's re-enqueue can never fix it",
		)

	def test_a_conflicting_pincode_keeps_both_and_logs(self):
		lead = _make_lead(custom_pincode="411045")
		migrate._ensure_address_template()
		existing = migrate._create_address(lead.name, {"lead_name": lead.name}, "560001")

		result = migrate.migrate_lead(lead.name)["custom_pincode"]
		self.assertEqual(result["outcome"], "conflict")
		self.assertEqual(frappe.db.get_value("Address", existing, "pincode"), "560001")

		held = {
			frappe.db.get_value("Address", name, "pincode")
			for name in migrate._linked_documents("Address", lead.name)
		}
		self.assertEqual(held, {"560001", "411045"}, "both pincodes must survive")
		self.assertTrue(
			frappe.db.exists(
				"Connector Log",
				{"request_id": f"legacy-migration:{lead.name}", "error_code": "PINCODE_CONFLICT"},
			)
		)

	def test_scanned_text_becomes_one_tagged_note(self):
		lead = _make_lead(custom_scanned_text="ACME Pvt Ltd\nJane Doe")
		migrate.migrate_lead(lead.name)

		notes = frappe.get_all(
			"CRM Note",
			filters={"parent": lead.name, "parenttype": "Lead", "note": ("like", f"%{SCANNED_TEXT_TAG}%")},
			pluck="name",
		)
		self.assertEqual(len(notes), 1)

		frappe.db.delete("Connector Legacy Field Archive", {"reference_name": lead.name})
		migrate.migrate_lead(lead.name)
		notes = frappe.get_all(
			"CRM Note",
			filters={"parent": lead.name, "parenttype": "Lead", "note": ("like", f"%{SCANNED_TEXT_TAG}%")},
			pluck="name",
		)
		self.assertEqual(len(notes), 1, "the tag must stop a re-run writing a second copy")

	def test_a_missing_file_row_is_logged_not_fatal(self):
		lead = _make_lead(custom_front_card_image="/private/files/vc-legacy-does-not-exist.png")
		notes = migrate.migrate_lead(lead.name)

		self.assertEqual(notes["custom_front_card_image"]["outcome"], "missing")
		self.assertTrue(
			frappe.db.exists(
				"Connector Log",
				{"request_id": f"legacy-migration:{lead.name}", "error_code": "CARD_FILE_MISSING"},
			)
		)
		# It must not block the drop for the whole site for ever.
		self.assertEqual(
			verify._verify_card_images(lead.name, migrate.read_legacy_lead(lead.name), notes), []
		)

	def test_a_real_attachment_is_reparented_not_reuploaded(self):
		lead = _make_lead()
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": _uniq("card") + ".txt",
				"attached_to_doctype": "Lead",
				"attached_to_name": lead.name,
				"attached_to_field": "custom_front_card_image",
				"is_private": 1,
				"content": _uniq("bytes").encode(),
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Lead", lead.name, "custom_front_card_image", file_doc.file_url)

		before = frappe.db.count("File", {"file_url": file_doc.file_url})
		notes = migrate.migrate_lead(lead.name)

		self.assertEqual(notes["custom_front_card_image"]["file"], file_doc.name)
		self.assertEqual(frappe.db.count("File", {"file_url": file_doc.file_url}), before, "re-uploaded")
		self.assertFalse(frappe.db.get_value("File", file_doc.name, "attached_to_field"))
		self.assertEqual(frappe.db.get_value("File", file_doc.name, "attached_to_name"), lead.name)

	def test_a_real_attribution_is_never_overwritten(self):
		source_fieldname = get_lead_source_fieldname()
		campaign = migrate.ensure_scan_source()  # any existing source will do
		lead = _make_lead(custom_created_from_scan=1)
		frappe.db.set_value("Lead", lead.name, source_fieldname, campaign)

		result = migrate.migrate_lead(lead.name)["custom_created_from_scan"]
		self.assertEqual(result["outcome"], "kept")
		self.assertEqual(frappe.db.get_value("Lead", lead.name, source_fieldname), campaign)

	def test_an_unattributed_scan_gets_the_scan_source(self):
		lead = _make_lead(custom_created_from_scan=1)
		migrate.migrate_lead(lead.name)
		self.assertEqual(
			frappe.db.get_value("Lead", lead.name, get_lead_source_fieldname()), SCAN_SOURCE_NAME
		)

	def test_business_id_becomes_a_link_row_with_no_uuid(self):
		lead = _make_lead(custom_business_id="legacy-biz")
		migrate.migrate_lead(lead.name)

		row = frappe.db.get_value(
			"Connector Lead Link", {"lead": lead.name}, ["vc_uuid", "business_id"], as_dict=True
		)
		self.assertIsNotNone(row)
		self.assertEqual(row.business_id, "legacy-biz")
		self.assertIsNone(row.vc_uuid, "a placeholder must be NULL, never '', or UNIQUE collides")

	def test_two_placeholders_can_coexist(self):
		"""The reason vc_uuid is NULL and not "": UNIQUE ignores NULL but not the empty string."""
		# A business id of its own: IntegrationTestCase rolls back once per CLASS, so rows
		# written by an earlier test in this class are still here.
		business = _uniq("legacy-biz")
		first = _make_lead(custom_business_id=business)
		second = _make_lead(custom_business_id=business)
		migrate.migrate_lead(first.name)
		migrate.migrate_lead(second.name)

		self.assertEqual(frappe.db.count("Connector Lead Link", {"business_id": business}), 2)


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestVerificationGate(LegacyMigrationTestCase):
	def test_a_non_empty_column_is_found_even_when_it_is_null_elsewhere(self):
		"""Regression guard for `("not in", ("", None))`, which matches nothing at all."""
		lead = _make_lead(custom_pincode="411099")
		self.assertIn(lead.name, verify._leads_with_legacy_values())
		self.assertIn(lead.name, archive._lead_names())

	def test_an_unmigrated_lead_fails_verification(self):
		lead = _make_lead(custom_scanned_text="not migrated yet")
		reasons = verify.verify_lead(lead.name)
		self.assertTrue(reasons)
		self.assertTrue(any("no Connector Legacy Field Archive mark row" in reason for reason in reasons))

	def test_a_migrated_lead_verifies(self):
		lead = _make_lead(
			emails=[_uniq("v") + "@vclegacy.example"],
			custom_scanned_text="scanned",
			custom_business_id="legacy-biz",
			custom_created_from_scan=1,
		)
		migrate.migrate_lead(lead.name)
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_undoing_the_migration_reopens_the_lead_for_a_retry(self):
		lead = _make_lead(custom_scanned_text="scanned")
		migrate.migrate_lead(lead.name)
		frappe.db.delete("CRM Note", {"parent": lead.name, "note": ("like", f"%{SCANNED_TEXT_TAG}%")})

		self.assertTrue(verify.verify_lead(lead.name))
		self.assertGreaterEqual(verify.reset_failed_marks(), 1)
		self.assertEqual(
			frappe.db.get_value(
				"Connector Legacy Field Archive", {"lookup_key": f"Lead|{lead.name}"}, "migration_status"
			),
			"Failed",
		)


class TestPatchBRefusesAndArchives(LegacyMigrationTestCase):
	def test_the_kept_field_is_never_in_the_drop_plan(self):
		self.assertNotIn(KEPT_LEAD_FIELD, LEGACY_LEAD_DATA_FIELDS)
		self.assertNotIn(KEPT_LEAD_FIELD, LEGACY_LEAD_LAYOUT_FIELDS)

	def test_the_drop_plan_is_the_declared_size(self):
		"""Nine data fields and four layout fields on Lead; seven and three on the source."""
		self.assertEqual(len(LEGACY_LEAD_DATA_FIELDS), 9)
		self.assertEqual(len(LEGACY_LEAD_LAYOUT_FIELDS), 4)
		self.assertEqual(len(LEGACY_SOURCE_DATA_FIELDS), 7)
		self.assertEqual(len(LEGACY_SOURCE_LAYOUT_FIELDS), 3)

	def test_it_refuses_while_patch_a_is_unfinished(self):
		"""Refusing writes nothing, which is why this one is safe to run on a real site."""
		previous = state.status()
		state.set_values(legacy_migration_status="Running")
		try:
			reason = drop._refusal_reason()
			self.assertIsNotNone(reason)
			self.assertIn("has not reported Complete", reason["reason"])
		finally:
			state.set_values(legacy_migration_status=previous or None)

	@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
	def test_it_refuses_while_a_lead_is_unverified(self):
		previous = state.status()
		lead = _make_lead(custom_scanned_text="never migrated")
		state.set_values(legacy_migration_status="Complete")
		try:
			reason = drop._refusal_reason()
			self.assertIsNotNone(reason)
			self.assertIn("verification found", reason["reason"])
			self.assertIn(lead.name, json.dumps(reason["failures"]))
		finally:
			state.set_values(legacy_migration_status=previous or None)

	@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
	def test_the_archive_captures_the_raw_values_and_the_child_rows(self):
		email = _uniq("arch") + "@vclegacy.example"
		lead = _make_lead(emails=[email], custom_pincode="411077", custom_business_id="legacy-biz")

		archive.write_lead(lead.name)
		row = frappe.db.get_value(
			"Connector Legacy Field Archive",
			{"lookup_key": f"Lead|{lead.name}"},
			["legacy_values", "archived_on"],
			as_dict=True,
		)
		self.assertTrue(row.archived_on)

		payload = json.loads(row.legacy_values)
		self.assertEqual(payload["fields"]["custom_pincode"], "411077")
		self.assertEqual(payload["fields"]["custom_business_id"], "legacy-biz")
		self.assertEqual(
			[r["email_id"] for r in payload["tables"]["custom_email_ids"]],
			[email],
			"the child rows are the only copy once the Table field is dropped",
		)


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestTheUndoButton(LegacyMigrationTestCase):
	"""The restore, at the one level that does not commit.

	``restore.restore()`` re-creates Custom Fields and commits, so a suite pointed at a real
	site must never call it. ``_restore_record`` does all the per-record work, writes through
	``frappe.db`` and rolls back with the class, so that is what is asserted here.

	This exists because the undo button had no test at all, and the one manual run of it
	reported ``records_restored: 0`` -- which looked like a broken restore and was in fact the
	correct answer to a question nobody had asked clearly. Both zeroes are pinned below.
	"""

	def _archived_payload(self, lead_name):
		archive.write_lead(lead_name)
		return json.loads(
			frappe.db.get_value(
				"Connector Legacy Field Archive", {"lookup_key": f"Lead|{lead_name}"}, "legacy_values"
			)
		)

	def test_a_wiped_record_is_rebuilt_from_the_archive_alone(self):
		"""The real undo: the values are GONE and the archive is the only copy left."""
		email = _uniq("undo") + "@vclegacy.example"
		lead = _make_lead(emails=[email], custom_pincode="411099", custom_business_id="undo-biz")
		payload = self._archived_payload(lead.name)

		# What `bench trim-tables` would leave behind after Patch B: no values, no child rows.
		frappe.db.set_value(
			"Lead", lead.name, {"custom_pincode": None, "custom_business_id": None}, update_modified=False
		)
		frappe.db.delete("ERPNext Lead Email", {"parent": lead.name, "parenttype": "Lead"})
		self.assertIsNone(frappe.db.get_value("Lead", lead.name, "custom_pincode"))

		restored, rows, already_present = restore._restore_record("Lead", lead.name, payload, 0, 0)

		self.assertTrue(restored)
		self.assertEqual(rows, 1)
		self.assertEqual(already_present, 0)
		self.assertEqual(frappe.db.get_value("Lead", lead.name, "custom_pincode"), "411099")
		self.assertEqual(frappe.db.get_value("Lead", lead.name, "custom_business_id"), "undo-biz")
		self.assertEqual(
			frappe.get_all(
				"ERPNext Lead Email",
				filters={"parent": lead.name, "parenttype": "Lead"},
				pluck="email_id",
			),
			[email],
		)

	def test_a_value_that_never_went_away_is_counted_not_rewritten(self):
		"""The other zero: Patch B deleted the FIELD, so the column still holds the value.

		A restore then writes nothing and must say so, rather than reporting the same
		``records_restored: 0`` an empty archive would.
		"""
		lead = _make_lead(custom_pincode="411100", custom_business_id="intact-biz")
		payload = self._archived_payload(lead.name)

		restored, rows, already_present = restore._restore_record("Lead", lead.name, payload, 0, 0)

		self.assertFalse(restored)
		self.assertEqual(rows, 0)
		self.assertGreaterEqual(already_present, 2)
		self.assertEqual(frappe.db.get_value("Lead", lead.name, "custom_pincode"), "411100")

	def test_force_overwrites_a_value_that_is_still_there(self):
		lead = _make_lead(custom_pincode="411100")
		payload = self._archived_payload(lead.name)
		frappe.db.set_value("Lead", lead.name, "custom_pincode", "999999", update_modified=False)

		restored, _rows, _present = restore._restore_record("Lead", lead.name, payload, 0, 1)

		self.assertTrue(restored)
		self.assertEqual(frappe.db.get_value("Lead", lead.name, "custom_pincode"), "411100")

	def test_a_dry_run_writes_nothing(self):
		lead = _make_lead(custom_pincode="411101")
		payload = self._archived_payload(lead.name)
		frappe.db.set_value("Lead", lead.name, "custom_pincode", None, update_modified=False)

		restore._restore_record("Lead", lead.name, payload, 1, 0)

		self.assertIsNone(frappe.db.get_value("Lead", lead.name, "custom_pincode"))

	def test_the_report_distinguishes_the_two_zeroes(self):
		"""Pure function, no DB: the sentence an operator reads has to differ."""
		empty = restore._explain(
			{
				"archive_rows": 0,
				"records_restored": 0,
				"records_already_intact": 0,
				"values_already_present": 0,
				"rows_restored": 0,
			}
		)
		intact = restore._explain(
			{
				"archive_rows": 21,
				"records_restored": 0,
				"records_already_intact": 21,
				"values_already_present": 30,
				"rows_restored": 0,
			}
		)
		did_work = restore._explain(
			{
				"archive_rows": 21,
				"records_restored": 21,
				"records_already_intact": 0,
				"values_already_present": 0,
				"rows_restored": 30,
			}
		)

		self.assertIn("archive is empty", empty)
		self.assertIn("Nothing needed writing", intact)
		self.assertIn("removes the field, not the column", intact)
		self.assertIn("Restored 21 record(s)", did_work)
		self.assertNotEqual(empty, intact)


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestAValueERPNextWillNotStore(LegacyMigrationTestCase):
	"""A business card is not a form. The card says "080-25551234 Ext 22" and the scanner
	stored it, so the client's table holds it -- and Frappe's PHONE_NUMBER_PATTERN refuses it.

	Before these tests, that exception rolled the Lead's whole savepoint back: its pincode,
	its scanned text and its card images were not migrated either, verification reported it
	for ever, and because there is no way to force-skip a Lead, Patch B could never drop on
	any client whose table holds one annotated number.
	"""

	def test_an_annotated_number_does_not_take_the_rest_of_the_lead_down(self):
		lead = _make_lead(custom_pincode="411045", custom_scanned_text="Ravi Kumar, Pune")
		_add_legacy_row(
			lead.name,
			"custom_mobile_numbers",
			"ERPNext Lead Mobile Number",
			{"mobile_number": "080-25551234 Ext 22"},
		)

		notes = migrate.migrate_lead(lead.name)

		self.assertIsNotNone(notes, "the Lead must migrate, not abort")
		contact = _contact_of(lead.name)
		self.assertIn("080-25551234", _phones_of(contact), _phones_of(contact))
		# ...and the rest of the Lead, which used to be rolled back with it:
		self.assertIn(notes["custom_pincode"]["outcome"], ("filled", "created", "already", "conflict"))
		self.assertTrue(_notes_of(lead.name, SCANNED_TEXT_TAG))
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_the_annotation_itself_is_written_where_a_human_can_read_it(self):
		""" "Ext 22" is not storable on a Contact Phone row and is about to lose the Custom
		Field it lives on. It has to land somewhere on the Lead, or the drop destroys it."""
		lead = _make_lead()
		_add_legacy_row(
			lead.name,
			"custom_mobile_numbers",
			"ERPNext Lead Mobile Number",
			{"mobile_number": "9876543210 (O)"},
		)

		migrate.migrate_lead(lead.name)

		notes = _notes_of(lead.name, UNSTORABLE_CONTACT_TAG)
		self.assertTrue(notes, "no tagged note carries the value as printed")
		self.assertIn("9876543210 (O)", "\n".join(notes))
		self.assertTrue(
			frappe.db.exists(
				"Connector Log",
				{
					"request_id": f"legacy-migration:{lead.name}",
					"error_code": "CONTACT_VALUE_UNSTORABLE",
				},
			)
		)

	def test_the_gate_refuses_while_that_note_is_missing(self):
		"""The note is not decoration: it is the only copy after the drop, so the gate checks
		for it. A gate that only looked at the Contact would pass and the value would be gone."""
		lead = _make_lead()
		_add_legacy_row(
			lead.name,
			"custom_mobile_numbers",
			"ERPNext Lead Mobile Number",
			{"mobile_number": "9876543210 (O)"},
		)
		migrate.migrate_lead(lead.name)
		self.assertEqual(verify.verify_lead(lead.name), [])

		frappe.db.delete("CRM Note", {"parent": lead.name, "note": ("like", f"%{UNSTORABLE_CONTACT_TAG}%")})
		reasons = verify.verify_lead(lead.name)

		self.assertTrue(any("tagged CRM Note" in reason for reason in reasons), reasons)
		# ...and a re-run repairs it, so this is not a dead end.
		frappe.db.set_value(
			"Connector Legacy Field Archive",
			{"lookup_key": f"Lead|{lead.name}"},
			"migration_status",
			"Failed",
		)
		migrate.migrate_lead(lead.name)
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_two_numbers_in_one_field_both_reach_the_contact(self):
		lead = _make_lead()
		_add_legacy_row(
			lead.name,
			"custom_mobile_numbers",
			"ERPNext Lead Mobile Number",
			{"mobile_number": "+91 98765 43210, +91 98765 43211"},
		)

		migrate.migrate_lead(lead.name)

		phones = _phones_of(_contact_of(lead.name))
		self.assertIn("+919876543210", phones, phones)
		self.assertIn("+919876543211", phones, phones)
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_a_number_that_cannot_be_read_at_all_is_a_note_not_a_crash(self):
		lead = _make_lead(custom_pincode="411046")
		_add_legacy_row(
			lead.name,
			"custom_mobile_numbers",
			"ERPNext Lead Mobile Number",
			{"mobile_number": "call reception"},
		)

		self.assertIsNotNone(migrate.migrate_lead(lead.name))
		self.assertIn("call reception", "\n".join(_notes_of(lead.name, UNSTORABLE_CONTACT_TAG)))
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_an_email_erpnext_refuses_does_not_abort_the_lead_either(self):
		lead = _make_lead(custom_pincode="411047")
		_add_legacy_row(
			lead.name, "custom_email_ids", "ERPNext Lead Email", {"email_id": "ravi(at)example.com"}
		)

		self.assertIsNotNone(migrate.migrate_lead(lead.name))
		self.assertIn("ravi(at)example.com", "\n".join(_notes_of(lead.name, UNSTORABLE_CONTACT_TAG)))
		self.assertEqual(verify.verify_lead(lead.name), [])


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestTwoNumbersAreNotOneNumber(LegacyMigrationTestCase):
	def test_a_country_code_is_part_of_the_number(self):
		"""+1 212 555 0100 and +44 20 2125 550100 share their last ten digits.

		The vendored match key keeps only those ten, so the second number was dropped on the
		floor -- and the verification gate, asking the same question, returned clean and let
		Patch B drop the field it was the last copy in."""
		lead = _make_lead()
		for number in ("+1 212 555 0100", "+44 20 2125 550100"):
			_add_legacy_row(
				lead.name, "custom_mobile_numbers", "ERPNext Lead Mobile Number", {"mobile_number": number}
			)

		migrate.migrate_lead(lead.name)

		phones = _phones_of(_contact_of(lead.name))
		self.assertIn("+12125550100", phones, phones)
		self.assertIn("+44202125550100", phones, phones)
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_the_gate_can_see_a_dropped_number(self):
		"""Proof the check is real: remove one number from the Contact and the gate reports it."""
		lead = _make_lead()
		for number in ("+1 212 555 0100", "+44 20 2125 550100"):
			_add_legacy_row(
				lead.name, "custom_mobile_numbers", "ERPNext Lead Mobile Number", {"mobile_number": number}
			)
		migrate.migrate_lead(lead.name)

		contact = _contact_of(lead.name)
		frappe.db.delete("Contact Phone", {"parent": contact.name, "phone": "+44202125550100"})
		frappe.clear_document_cache("Contact", contact.name)

		reasons = verify.verify_lead(lead.name)
		self.assertTrue(any("missing phone_nos" in reason for reason in reasons), reasons)

	def test_respellings_of_one_indian_number_still_collapse(self):
		"""The fix must not go the other way and write three rows for one number."""
		lead = _make_lead()
		for number in ("+91 98765 43219", "09876543219", "9876543219"):
			_add_legacy_row(
				lead.name, "custom_mobile_numbers", "ERPNext Lead Mobile Number", {"mobile_number": number}
			)

		migrate.migrate_lead(lead.name)

		phones = [phone for phone in _phones_of(_contact_of(lead.name)) if "9876543219" in phone]
		self.assertEqual(len(phones), 1, phones)


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestTwoLeadsScannedFromOneCard(LegacyMigrationTestCase):
	"""Frappe dedupes uploads by content hash: two Leads holding byte-identical card images
	have two File ROWS sharing one ``file_url``.

	Resolving the attachment by URL alone returned whichever row the database offered first.
	Migrating Lead A re-parented that row to A -- taking it off B -- and migrating B took it
	back. Verification then failed for whichever Lead was not holding it, Patch B reset the
	marks and re-enqueued, and the next pass flipped the pair again: an infinite repair loop
	in which the drop never happens and one of the two Leads has no card image at any moment.
	"""

	def test_each_lead_keeps_its_own_row(self):
		content = ("shared-card-" + frappe.generate_hash(length=8)).encode()
		first, second = _make_lead(), _make_lead()
		file_a = _attach(first.name, content, field="custom_front_card_image")
		file_b = _attach(second.name, content, field="custom_front_card_image")
		self.assertEqual(file_a.file_url, file_b.file_url, "frappe did not dedupe; test is void")
		self.assertNotEqual(file_a.name, file_b.name)

		migrate.migrate_lead(first.name)
		migrate.migrate_lead(second.name)

		self.assertEqual(frappe.db.get_value("File", file_a.name, "attached_to_name"), first.name)
		self.assertEqual(frappe.db.get_value("File", file_b.name, "attached_to_name"), second.name)
		self.assertFalse(frappe.db.get_value("File", file_a.name, "attached_to_field"))
		self.assertFalse(frappe.db.get_value("File", file_b.name, "attached_to_field"))
		self.assertEqual(verify.verify_lead(first.name), [])
		self.assertEqual(verify.verify_lead(second.name), [])

	def test_the_repair_cycle_settles_instead_of_flipping(self):
		"""Three rounds of the documented repair cycle. It used to alternate for ever."""
		content = ("loop-card-" + frappe.generate_hash(length=8)).encode()
		first, second = _make_lead(), _make_lead()
		_attach(first.name, content, field="custom_front_card_image")
		_attach(second.name, content, field="custom_front_card_image")

		for _round in range(3):
			for lead in (first.name, second.name):
				frappe.db.set_value(
					"Connector Legacy Field Archive",
					{"lookup_key": f"Lead|{lead}"},
					"migration_status",
					"Failed",
				)
				migrate.migrate_lead(lead)
			self.assertEqual(verify.verify_lead(first.name), [])
			self.assertEqual(verify.verify_lead(second.name), [])

	def test_a_lead_whose_only_row_belongs_to_another_record_gets_its_own(self):
		"""The URL is right, the bytes are right, and the one File row is someone else's.

		Taking it would break the other record. A second row pointing at the same bytes costs
		no disk -- it is exactly what frappe writes when the same image is uploaded twice."""
		content = ("claimed-card-" + frappe.generate_hash(length=8)).encode()
		owner, claimant = _make_lead(), _make_lead()
		owned = _attach(owner.name, content, field="custom_front_card_image")
		frappe.db.set_value("Lead", claimant.name, "custom_front_card_image", owned.file_url)

		notes = migrate.migrate_lead(claimant.name)

		self.assertEqual(notes["custom_front_card_image"]["outcome"], "copied")
		self.assertEqual(frappe.db.get_value("File", owned.name, "attached_to_name"), owner.name)
		self.assertEqual(verify.verify_lead(claimant.name), [])
		self.assertEqual(
			frappe.db.count("File", {"file_url": owned.file_url}), 2, "the bytes must not be re-uploaded"
		)
		self.assertTrue(
			frappe.db.exists(
				"Connector Log",
				{"request_id": f"legacy-migration:{claimant.name}", "error_code": "CARD_FILE_SHARED"},
			)
		)


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestADeletedLeadDoesNotBlockTheDrop(LegacyMigrationTestCase):
	"""Patch A's mark row has no ``archived_on``; Patch B fills it in later, and refuses to
	drop while any row lacks one. Deleting a migrated Lead -- an ordinary thing for a client to
	do -- left a row nothing could ever fill in: the archive only walks records that still
	exist. The refusal was permanent, self-healing did not apply to it, and the report named
	no row, so the operator had nothing to act on.
	"""

	def test_the_orphan_row_is_closed_out_rather_than_blocking_for_ever(self):
		lead = _make_lead(custom_pincode="411048")
		migrate.migrate_lead(lead.name)
		row = frappe.db.get_value(
			"Connector Legacy Field Archive", {"lookup_key": f"Lead|{lead.name}"}, "name"
		)
		self.assertIsNone(frappe.db.get_value("Connector Legacy Field Archive", row, "archived_on"))

		frappe.delete_doc("Lead", lead.name, force=True, delete_permanently=True, ignore_permissions=True)
		closed = archive.stamp_orphans()

		self.assertIn(row, closed)
		stamped = frappe.db.get_value(
			"Connector Legacy Field Archive",
			row,
			["archived_on", "migration_status", "migration_notes"],
			as_dict=True,
		)
		self.assertIsNotNone(stamped.archived_on, "still unarchived: Patch B would refuse for ever")
		self.assertEqual(stamped.migration_status, "Orphaned")
		self.assertIn("no longer exists", stamped.migration_notes)

	def test_a_record_that_still_exists_is_never_stamped(self):
		"""The escape hatch must not become a way to skip a Lead that is really there."""
		lead = _make_lead(custom_pincode="411049")
		migrate.migrate_lead(lead.name)
		row = frappe.db.get_value(
			"Connector Legacy Field Archive", {"lookup_key": f"Lead|{lead.name}"}, "name"
		)

		self.assertNotIn(row, archive.stamp_orphans())
		self.assertIsNone(frappe.db.get_value("Connector Legacy Field Archive", row, "archived_on"))

	def test_an_orphan_row_restores_nothing(self):
		"""It has no legacy_values -- there was nothing left to capture -- so the undo button
		must pass over it rather than trying to write to a record that is gone."""
		lead = _make_lead(custom_pincode="411050")
		migrate.migrate_lead(lead.name)
		frappe.delete_doc("Lead", lead.name, force=True, delete_permanently=True, ignore_permissions=True)
		archive.stamp_orphans()

		row = frappe.db.get_value(
			"Connector Legacy Field Archive", {"lookup_key": f"Lead|{lead.name}"}, "legacy_values"
		)
		self.assertFalse(row)


@unittest.skipUnless(LEGACY_PRESENT, "the legacy Custom Fields have already been dropped")
class TestTwoAddressesInOneLegacyField(LegacyMigrationTestCase):
	"""The other half of "two values in one legacy field".

	It was fixed for phones and left unfixed for emails, and emails are the worse case: a
	Contact Phone row THROWS on a value frappe cannot store, which is loud, while Contact
	Email.email_id accepts "ravi@a.com, ravi.k@b.com" without complaint. So the migration
	reported success, the gate -- asking frappe.utils.validate_email_address, which is truthy
	for that string -- agreed, and the Lead was left holding one row in which neither address
	can be found. After Patch B the legacy row is gone and the second address exists nowhere
	a lookup can reach.
	"""

	def test_a_comma_separated_pair_becomes_two_findable_rows(self):
		lead = _make_lead()
		_add_legacy_row(
			lead.name,
			"custom_email_ids",
			"ERPNext Lead Email",
			{"email_id": "ravi@a.example, ravi.k@b.example"},
		)

		migrate.migrate_lead(lead.name)

		contact = _contact_of(lead.name)
		stored = _emails_of(contact)
		self.assertIn("ravi@a.example", stored, stored)
		self.assertIn("ravi.k@b.example", stored, stored)

		# The point of splitting: each one is findable BY ITSELF.
		for email in ("ravi@a.example", "ravi.k@b.example"):
			with self.subTest(email=email):
				self.assertTrue(
					frappe.db.exists("Contact Email", {"parent": contact.name, "email_id": email}),
					f"{email} is in no row of its own",
				)

		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_a_display_name_is_not_stored_as_the_address(self):
		""" "Ravi Kumar <ravi2@a.example>" used to be stored verbatim -- lower-cased, angle
		brackets and all -- so a lookup for the address matched nothing."""
		lead = _make_lead()
		_add_legacy_row(
			lead.name, "custom_email_ids", "ERPNext Lead Email", {"email_id": "Ravi Kumar <ravi2@a.example>"}
		)

		migrate.migrate_lead(lead.name)

		contact = _contact_of(lead.name)
		stored = _emails_of(contact)
		self.assertIn("ravi2@a.example", stored, stored)
		# The value as PRINTED is not an address and must be on no Contact Email row.
		self.assertNotIn("ravi kumar <ravi2@a.example>", [value.lower() for value in stored], stored)
		# The display name is not contact data, but it IS about to lose the field it lives on.
		# The note stores it escaped -- a CRM Note is HTML -- which is the form the gate looks
		# for too, so asserting on the raw string here would pass a test the gate would fail.
		self.assertIn(
			frappe.utils.escape_html("Ravi Kumar <ravi2@a.example>"),
			"\n".join(_notes_of(lead.name, UNSTORABLE_CONTACT_TAG)),
		)
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_the_gate_refuses_while_the_second_address_is_unreachable(self):
		"""The gate has to be able to SEE the old bug, or it licenses the drop that makes it
		permanent. Written as the old code wrote it: one row holding both addresses."""
		lead = _make_lead()
		_add_legacy_row(
			lead.name,
			"custom_email_ids",
			"ERPNext Lead Email",
			{"email_id": "joint@a.example, joint.k@b.example"},
		)

		contact = migrate._get_or_create_contact(lead.name, {"lead_name": lead.lead_name})
		contact.append("email_ids", {"email_id": "joint@a.example, joint.k@b.example"})
		contact.insert(ignore_permissions=True) if contact.is_new() else contact.save(ignore_permissions=True)

		reasons = verify._verify_contacts(lead.name, migrate.read_legacy_lead(lead.name))

		self.assertTrue(reasons, "the gate passed a Contact neither address can be found in")
		self.assertIn("joint.k@b.example", " ".join(reasons))

	def test_a_number_too_long_to_store_does_not_fabricate_a_phone_number(self):
		"""The migration may lose a number. It may not invent one, and a fragment of an
		annotation is exactly what the client would then dial."""
		lead = _make_lead()
		_add_legacy_row(
			lead.name,
			"custom_mobile_numbers",
			"ERPNext Lead Mobile Number",
			{"mobile_number": "+91 (080) 2555-1234-5678 ext 9"},
		)

		migrate.migrate_lead(lead.name)

		contact = _contact_of(lead.name)
		stored = _phones_of(contact) if contact else []
		self.assertNotIn("9", stored, stored)
		self.assertEqual(stored, [], stored)
		# Nothing reached the Contact, so the value as printed MUST be readable on the Lead.
		self.assertIn(
			"+91 (080) 2555-1234-5678 ext 9", "\n".join(_notes_of(lead.name, UNSTORABLE_CONTACT_TAG))
		)
		self.assertEqual(verify.verify_lead(lead.name), [])

	def test_two_numbers_written_as_one_bracketed_run_both_reach_the_contact(self):
		lead = _make_lead()
		_add_legacy_row(
			lead.name,
			"custom_mobile_numbers",
			"ERPNext Lead Mobile Number",
			{"mobile_number": "7011477004(7011477005)"},
		)

		migrate.migrate_lead(lead.name)

		stored = _phones_of(_contact_of(lead.name))
		self.assertIn("7011477004", stored, stored)
		self.assertIn("7011477005", stored, stored)
		self.assertEqual(verify.verify_lead(lead.name), [])
