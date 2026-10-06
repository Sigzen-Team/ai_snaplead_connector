# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

import itertools
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from sigzen_ai_scan_crm_connector.legacy.schema import lead_has_contact_tables
from sigzen_ai_scan_crm_connector.tests.cleanup import LeavesNoContactsBehind

# Every value these tests store is a live claim in this app's duplicate guard, and the guard
# outlives the test run: the Lead is rolled back but the Contact ERPNext auto-creates from it
# is committed on a connection the class rollback does not cover. With fixed literals the
# SECOND `run-tests` on a site therefore failed against the FIRST run's orphans, reporting a
# duplicate against a Lead that no longer exists. The values are generated per RUN so a
# leftover can never be the value this run is about to claim.
_RUN = frappe.generate_hash(length=4)
_SEQUENCE = itertools.count(1)


def uniq_email(tag="lead"):
	return f"{tag}-{_RUN}-{next(_SEQUENCE)}@vctest.example"


def uniq_mobile():
	"""A well-formed Indian mobile unique to this run (Frappe validates the shape)."""
	return f"+91 9{int(_RUN, 16) % 10000:04d} {next(_SEQUENCE):05d}"


def _make_lead(**kwargs):
	values = {
		"doctype": "Lead",
		"first_name": frappe.generate_hash(length=8),
		"company_name": "VC Connector Test Co",
	}
	values.update(kwargs)
	return frappe.get_doc(values).insert(ignore_permissions=True)


# These describe the shape of a client who has NOT yet run the legacy field migration: the
# contact details live in this app's Custom child tables. After ``patches.v1_0.drop_legacy_lead_fields``
# those tables are gone from Lead and the details live on the Contact, so a test that asserts
# on ``custom_email_ids`` is asserting about a shape that site no longer has. It is skipped
# there rather than deleted -- most clients are still on the old shape, and it is the only
# thing testing it.
LEGACY_CONTACT_TABLES = lead_has_contact_tables()
SKIP_REASON = "this site has already run the legacy field migration; Lead has no contact child tables"


@unittest.skipUnless(LEGACY_CONTACT_TABLES, SKIP_REASON)
class TestLeadContacts(LeavesNoContactsBehind, IntegrationTestCase):
	def test_stock_fields_are_adopted_and_projected_back(self):
		email = uniq_email("Mixed.Case").replace("mixed.case", "Mixed.Case")
		mobile = uniq_mobile()
		lead = _make_lead(email_id=email.upper(), mobile_no=f" {mobile} ")

		self.assertEqual([r.email_id for r in lead.custom_email_ids], [email.lower()])
		self.assertEqual([r.mobile_number for r in lead.custom_mobile_numbers], [mobile])
		# the stock fields stay populated so stock list views / reports still work
		self.assertEqual(lead.email_id, email.lower())
		self.assertEqual(lead.mobile_no, mobile)

	def test_editing_the_stock_field_promotes_it_without_losing_the_old_value(self):
		"""The edit must stick (not revert) AND must not delete a stored contact.

		The stock field is the projection of child row 1, so a new value has to become row 1
		or sync_legacy_parent_contact_fields projects the old row straight back over it. It is
		promoted by insertion, never by overwriting row 1: the same document shape arrives
		from a machine pusher that sanitizes its payload down to stock ERPNext fields, and
		overwriting silently deleted a contact that write never mentioned.
		"""
		first, second, edited = uniq_email("first"), uniq_email("second"), uniq_email("edited")
		lead = _make_lead(email_id=first, mobile_no=uniq_mobile())
		# ERPNext's Lead.after_insert writes back to the row, so re-read before editing.
		lead.reload()
		lead.append("custom_email_ids", {"email_id": second})
		lead.save(ignore_permissions=True)

		lead = frappe.get_doc("Lead", lead.name)
		lead.email_id = edited
		lead.save(ignore_permissions=True)

		lead.reload()
		self.assertEqual(lead.email_id, edited)
		self.assertEqual(
			[r.email_id for r in lead.custom_email_ids],
			[edited, first, second],
		)

	def test_a_stock_only_update_never_deletes_a_stored_number(self):
		"""The shape a stock-fields-only pusher sends must not destroy the child table."""
		one, two, three, fresh = uniq_mobile(), uniq_mobile(), uniq_mobile(), uniq_mobile()
		lead = _make_lead(mobile_no=one)
		lead.reload()
		lead.append("custom_mobile_numbers", {"mobile_number": two})
		lead.append("custom_mobile_numbers", {"mobile_number": three})
		lead.save(ignore_permissions=True)
		lead.reload()
		self.assertEqual(
			[r.mobile_number for r in lead.custom_mobile_numbers], [one, two, three]
		)

		lead = frappe.get_doc("Lead", lead.name)
		lead.mobile_no = fresh
		lead.save(ignore_permissions=True)

		lead.reload()
		self.assertEqual(lead.mobile_no, fresh)
		stored = [r.mobile_number for r in lead.custom_mobile_numbers]
		self.assertEqual(stored, [fresh, one, two, three])
		self.assertIn(one, stored)

	def test_a_legacy_lead_with_empty_stock_fields_can_still_be_edited(self):
		"""The state the removed bulk rewrite left every Lead in on an existing client.

		restore_stock_lead_contact_fields makes Lead.email_id / mobile_no visible and writable
		again; if the adopt step ignores a NULL stored value, they are writable but the write
		never sticks.
		"""
		old_email, old_mobile = uniq_email("legacyedit"), uniq_mobile()
		new_email, new_mobile = uniq_email("new"), uniq_mobile()
		lead = _make_lead(email_id=old_email, mobile_no=old_mobile)
		frappe.db.set_value(
			"Lead", lead.name, {"email_id": None, "mobile_no": None}, update_modified=False
		)

		lead = frappe.get_doc("Lead", lead.name)
		self.assertIsNone(lead.email_id)
		lead.email_id = new_email
		lead.mobile_no = new_mobile
		lead.save(ignore_permissions=True)

		lead.reload()
		self.assertEqual(lead.email_id, new_email)
		self.assertEqual(lead.mobile_no, new_mobile)
		self.assertEqual(
			[r.email_id for r in lead.custom_email_ids],
			[new_email, old_email],
		)
		self.assertEqual(
			[r.mobile_number for r in lead.custom_mobile_numbers], [new_mobile, old_mobile]
		)

	def test_a_child_table_only_push_still_gets_a_contact_with_contact_details(self):
		"""ERPNext builds the auto-created Contact in before_insert, before validate ran."""
		if not frappe.db.get_single_value("CRM Settings", "auto_creation_of_contact"):
			self.skipTest("auto_creation_of_contact is off on this site")

		email, mobile = uniq_email("childonly"), uniq_mobile()
		lead = _make_lead(
			custom_email_ids=[{"email_id": email}],
			custom_mobile_numbers=[{"mobile_number": mobile}],
		)
		lead.reload()
		self.assertEqual(lead.email_id, email)

		contact = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Lead", "link_name": lead.name, "parenttype": "Contact"},
			"parent",
		)
		self.assertTrue(contact, "no Contact was auto-created for the Lead")

		emails = frappe.db.get_all("Contact Email", filters={"parent": contact}, pluck="email_id")
		phones = frappe.db.get_all("Contact Phone", filters={"parent": contact}, pluck="phone")
		self.assertEqual(emails, [email])
		self.assertEqual(phones, [mobile])

	def test_resaving_without_changes_does_not_grow_the_child_tables(self):
		lead = _make_lead(email_id=uniq_email("stable"), mobile_no=uniq_mobile())

		for _ in range(3):
			lead = frappe.get_doc("Lead", lead.name)
			lead.save(ignore_permissions=True)

		lead.reload()
		self.assertEqual(len(lead.custom_email_ids), 1)
		self.assertEqual(len(lead.custom_mobile_numbers), 1)

	def test_a_lead_needs_at_least_one_contact_detail(self):
		with self.assertRaises(frappe.ValidationError):
			_make_lead()

	def test_deleting_the_last_row_of_the_grid_is_not_undone(self):
		"""Emptying a contact grid in the Desk must stick.

		The Desk sends the child table empty and leaves the stock field alone, so the adopt
		step used to re-append the stock value "because the table is empty" and the row came
		straight back. A contact could not be removed through the grid at all.
		"""
		mobile = uniq_mobile()
		lead = _make_lead(email_id=uniq_email("deleteme"), mobile_no=mobile)
		lead.reload()
		self.assertEqual(len(lead.custom_email_ids), 1)

		lead.set("custom_email_ids", [])
		lead.save(ignore_permissions=True)

		lead.reload()
		self.assertEqual([r.email_id for r in lead.custom_email_ids], [])
		self.assertIsNone(lead.email_id)
		# the other grid is untouched
		self.assertEqual([r.mobile_number for r in lead.custom_mobile_numbers], [mobile])

	def test_a_pre_install_lead_adopts_its_stock_contact_on_first_save(self):
		"""A Lead that predates this app has no child rows and must not be blanked.

		"Grid emptied by the user" and "never had a child row" are the same document, so the
		difference has to come from the database. Getting it wrong the other way would null
		email_id / mobile_no on every existing Lead the first time it is saved.
		"""
		email, mobile = uniq_email("preinstall"), uniq_mobile()
		lead = _make_lead(email_id=email, mobile_no=mobile)
		frappe.db.delete("ERPNext Lead Email", {"parent": lead.name})
		frappe.db.delete("ERPNext Lead Mobile Number", {"parent": lead.name})

		lead = frappe.get_doc("Lead", lead.name)
		self.assertEqual(lead.custom_email_ids, [])
		lead.save(ignore_permissions=True)

		lead.reload()
		self.assertEqual(lead.email_id, email)
		self.assertEqual(lead.mobile_no, mobile)
		self.assertEqual([r.email_id for r in lead.custom_email_ids], [email])
		self.assertEqual(
			[r.mobile_number for r in lead.custom_mobile_numbers], [mobile]
		)

	def test_correcting_a_leads_only_contact_releases_the_old_value(self):
		"""A mistyped number must not keep blocking the Lead that really owns it.

		Every stored child row is a live claim in the duplicate guard. When the Lead holds a
		single contact of that kind and its stock projection is corrected, the old value is
		a mistake, not a second contact -- keeping it refuses the rightful Lead forever, with
		a message naming a Lead whose visible number is something else.
		"""
		mistyped, corrected = uniq_mobile(), uniq_mobile()
		alpha = _make_lead(mobile_no=mistyped)
		alpha.reload()
		alpha.mobile_no = corrected
		alpha.save(ignore_permissions=True)

		alpha.reload()
		self.assertEqual(alpha.mobile_no, corrected)
		self.assertEqual([r.mobile_number for r in alpha.custom_mobile_numbers], [corrected])

		beta = _make_lead(mobile_no=mistyped)
		self.assertTrue(frappe.db.exists("Lead", beta.name))


@unittest.skipUnless(LEGACY_CONTACT_TABLES, SKIP_REASON)
class TestTheLeadsContactReleasesWhatTheLeadDropped(LeavesNoContactsBehind, IntegrationTestCase):
	"""ERPNext builds a Lead's Contact once and never re-syncs it, but the duplicate guard
	reads Contact Phone / Contact Email too -- so a corrected number went on being claimed by
	the stale copy for ever. These pin the release AND the two things it must not do.
	"""

	def _contact_of(self, lead_name):
		return frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Lead", "link_name": lead_name, "parenttype": "Contact"},
			"parent",
		)

	def _contact_phones(self, lead_name):
		contact = self._contact_of(lead_name)
		if not contact:
			return []
		return frappe.get_all("Contact Phone", filters={"parent": contact}, pluck="phone")

	def test_a_corrected_number_stops_being_claimed_by_the_contact(self):
		"""THE bug: the stale value kept the rightful Lead permanently unsaveable."""
		mistyped, corrected = uniq_mobile(), uniq_mobile()

		alpha = _make_lead(mobile_no=mistyped)
		self.assertEqual(self._contact_phones(alpha.name), [mistyped])

		alpha.reload()
		alpha.mobile_no = corrected
		alpha.save(ignore_permissions=True)

		self.assertEqual(
			self._contact_phones(alpha.name),
			[corrected],
			"the Contact still holds the number the Lead corrected away",
		)

		# The whole point: the Lead that really owns the number can now be saved.
		beta = _make_lead(mobile_no=mistyped)
		self.assertTrue(frappe.db.exists("Lead", beta.name))

	def test_a_value_typed_straight_onto_the_contact_is_not_deleted(self):
		"""The release is a diff, never a resync: it may only remove what the Lead dropped."""
		original, corrected = uniq_mobile(), uniq_mobile()
		hand_added = uniq_mobile()

		lead = _make_lead(mobile_no=original)
		contact = frappe.get_doc("Contact", self._contact_of(lead.name))
		contact.append("phone_nos", {"phone": hand_added})
		contact.save(ignore_permissions=True)

		lead.reload()
		lead.mobile_no = corrected
		lead.save(ignore_permissions=True)

		phones = self._contact_phones(lead.name)
		self.assertIn(hand_added, phones, "a number the Lead never held must not be removed")
		self.assertIn(corrected, phones)
		self.assertNotIn(original, phones)

	def test_a_contact_shared_with_another_record_is_left_alone(self):
		"""A converted Lead shares its Contact with the Customer. Editing it from a Lead save
		would be editing the client's customer record."""
		original, corrected = uniq_mobile(), uniq_mobile()
		lead = _make_lead(mobile_no=original)

		contact = frappe.get_doc("Contact", self._contact_of(lead.name))
		contact.append("links", {"link_doctype": "Lead", "link_name": _make_lead(
			mobile_no=uniq_mobile()
		).name})
		contact.save(ignore_permissions=True)

		lead.reload()
		lead.mobile_no = corrected
		lead.save(ignore_permissions=True)

		phones = frappe.get_all("Contact Phone", filters={"parent": contact.name}, pluck="phone")
		self.assertIn(original, phones, "a shared Contact must not be edited by a Lead save")

	def test_an_email_the_lead_dropped_is_released_too(self):
		first, second = uniq_email("rel"), uniq_email("rel")
		lead = _make_lead(email_id=first)

		lead.reload()
		lead.set("custom_email_ids", [{"email_id": second}])
		lead.email_id = second
		lead.save(ignore_permissions=True)

		contact = self._contact_of(lead.name)
		emails = frappe.get_all("Contact Email", filters={"parent": contact}, pluck="email_id")
		self.assertNotIn(first, emails)
		self.assertIn(second, emails)
