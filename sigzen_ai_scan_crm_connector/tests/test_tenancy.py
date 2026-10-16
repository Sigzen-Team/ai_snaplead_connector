# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.tests import IntegrationTestCase

from sigzen_ai_scan_crm_connector.legacy.schema import lead_has_contact_tables
from sigzen_ai_scan_crm_connector.tenancy import SETTINGS, get_business_id, get_site_business_id
from sigzen_ai_scan_crm_connector.tests.cleanup import LeavesNoContactsBehind


def _snapshot_settings():
	# "Singles" is a raw table, not a DocType, so it has to be read with SQL.
	return frappe.db.sql(
		"""select field, value from `tabSingles` where doctype = %s order by field""",
		(SETTINGS,),
		as_dict=True,
	)


def _clear_settings():
	frappe.db.delete("Singles", {"doctype": SETTINGS})
	frappe.db.commit()
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	frappe.clear_cache()


def _restore_settings(rows):
	frappe.db.delete("Singles", {"doctype": SETTINGS})

	for row in rows:
		frappe.db.sql(
			"""insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)""",
			(SETTINGS, row.field, row.value),
		)

	frappe.db.commit()
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	frappe.clear_cache()


class TestTenancy(LeavesNoContactsBehind, IntegrationTestCase):
	def setUp(self):
		# Connector Settings is a Single, so it outlives an ordinary test rollback and the
		# suite has to clear it by hand. Clearing it WITHOUT keeping a copy destroyed the real
		# configuration of whatever site the suite was run on -- including token_hash, which is
		# a SHA-256 of a shared secret no admin can recover. Snapshot first, restore always.
		self.saved_settings = _snapshot_settings()
		self.addCleanup(self._put_the_sites_settings_back)
		_clear_settings()

	def _put_the_sites_settings_back(self):
		_restore_settings(self.saved_settings)

	def test_the_suite_restores_the_sites_own_settings(self):
		"""Guard on the guard: prove the snapshot/restore round-trips before trusting it."""
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-snapshot"
		settings.token_hash = "a" * 64
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		pretend_site_config = _snapshot_settings()
		self.assertTrue(pretend_site_config)

		_clear_settings()
		self.assertEqual(get_site_business_id(), "")

		_restore_settings(pretend_site_config)
		self.assertEqual(get_site_business_id(), "biz-snapshot")
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "token_hash"), "a" * 64)

		_clear_settings()

	def test_changing_the_business_id_restamps_rows_carrying_the_old_one(self):
		"""Changing only the setting left every link row and Lead stamp on the old value, so the
		next push from central failed with BUSINESS_MISMATCH. The save now moves them along."""
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Moves Business",
				"company_name": "VC Connector Test Co",
				"email_id": "movesbusiness@example.com",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", lead.name, force=True, ignore_permissions=True)
		lead_link = frappe.get_doc(
			{"doctype": "Connector Lead Link", "vc_uuid": frappe.generate_hash(length=12), "lead": lead.name}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Connector Lead Link", lead_link.name, force=True, ignore_permissions=True)
		# UTM Source on ERPNext v16, Lead Source up to v15.
		source_doctype = frappe.get_meta("Connector Source Link").get_field("source").options
		source_name = f"Moves Business {frappe.generate_hash(length=6)}"
		frappe.get_doc({"doctype": source_doctype, "name": source_name, "source_name": source_name}).insert(
			ignore_permissions=True
		)
		self.addCleanup(frappe.delete_doc, source_doctype, source_name, force=True, ignore_permissions=True)
		source_link = frappe.get_doc(
			{"doctype": "Connector Source Link", "vc_uuid": frappe.generate_hash(length=12), "source": source_name}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "Connector Source Link", source_link.name, force=True, ignore_permissions=True
		)
		has_stamp = frappe.get_meta("Lead").has_field("custom_business_id")
		if has_stamp:
			frappe.db.set_value("Lead", lead.name, "custom_business_id", "biz-alpha", update_modified=False)

		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-beta"
		settings.save(ignore_permissions=True)

		self.assertEqual(get_site_business_id(), "biz-beta")
		self.assertEqual(frappe.db.get_value("Connector Lead Link", lead_link.name, "business_id"), "biz-beta")
		self.assertEqual(frappe.db.get_value("Connector Source Link", source_link.name, "business_id"), "biz-beta")
		if has_stamp:
			self.assertEqual(frappe.db.get_value("Lead", lead.name, "custom_business_id"), "biz-beta")

	def test_only_a_system_manager_can_change_the_business_id(self):
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.save(ignore_permissions=True)

		original_get_roles = frappe.get_roles
		frappe.get_roles = lambda *args, **kwargs: ["Connector Integration"]
		self.addCleanup(setattr, frappe, "get_roles", original_get_roles)

		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-beta"
		with self.assertRaises(frappe.PermissionError):
			settings.save(ignore_permissions=True)

	def test_settings_outrank_the_per_row_stamp(self):
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.save(ignore_permissions=True)

		legacy_stamped_doc = frappe._dict({"custom_business_id": "biz-legacy"})
		self.assertEqual(get_business_id(legacy_stamped_doc), "biz-alpha")

	def test_duplicate_contact_in_the_same_business_is_refused(self):
		"""The in-business dedupe guard must keep working once Settings is the authority.

		Leads saved before onboarding carry no custom_business_id stamp; scoping the guard
		strictly to the stamp would silently switch it off on exactly those rows.
		"""
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		first = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Dup One",
				"company_name": "VC Connector Test Co",
				"email_id": "dupe@example.com",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", first.name, force=True, ignore_permissions=True)

		# frappe.DuplicateEntryError (stock ERPNext's guard) is a NameError, not a
		# ValidationError, so both have to be accepted here.
		with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
			frappe.get_doc(
				{
					"doctype": "Lead",
					"first_name": "Dup Two",
					"company_name": "VC Connector Test Co",
					"email_id": "dupe@example.com",
				}
			).insert(ignore_permissions=True)

	def test_configuring_a_business_does_not_switch_off_the_stock_duplicate_guard(self):
		"""A Lead that predates this app has no child rows. It must still block a duplicate.

		This is the shape every real client is in: the bulk backfill that used to create
		child rows for existing Leads was deleted, so the connector's own tables are blind to
		the client's entire existing Lead book.
		"""
		legacy = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Legacy Pre Install",
				"company_name": "VC Connector Test Co",
				"email_id": "legacy@example.com",
				"mobile_no": "+91 55550 00001",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", legacy.name, force=True, ignore_permissions=True)

		# strip the child rows so the row looks exactly like a pre-install Lead
		frappe.db.delete("ERPNext Lead Email", {"parent": legacy.name})
		frappe.db.delete("ERPNext Lead Mobile Number", {"parent": legacy.name})

		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
			frappe.get_doc(
				{
					"doctype": "Lead",
					"first_name": "Retry Of Legacy",
					"company_name": "VC Connector Test Co",
					"email_id": "legacy@example.com",
					"mobile_no": "+91 55550 00001",
				}
			).insert(ignore_permissions=True)

	def test_a_differently_cased_stored_email_is_still_a_duplicate(self):
		"""Rows written before this release were stored verbatim; the lookup lower-cases."""
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Mixed Case",
				"company_name": "VC Connector Test Co",
				"email_id": "holder@example.com",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", lead.name, force=True, ignore_permissions=True)

		# a pre-release child row, stored with its original casing
		frappe.db.sql(
			"""update `tabERPNext Lead Email` set email_id = %s where parent = %s""",
			("Mixed.Case@Example.com", lead.name),
		)
		frappe.db.set_value("Lead", lead.name, "email_id", "Mixed.Case@Example.com", update_modified=False)

		with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
			frappe.get_doc(
				{
					"doctype": "Lead",
					"first_name": "Lower Case Push",
					"company_name": "VC Connector Test Co",
					"email_id": "mixed.case@example.com",
				}
			).insert(ignore_permissions=True)

	def test_a_link_row_stamped_for_another_business_is_refused(self):
		"""Connector Lead Link / Connector Source Link are connector identity, not free-form rows."""
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.reject_business_mismatch = 1
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Link Target",
				"company_name": "VC Connector Test Co",
				"email_id": "linktarget@example.com",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", lead.name, force=True, ignore_permissions=True)

		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Connector Lead Link",
					"vc_uuid": frappe.generate_hash(length=12),
					"lead": lead.name,
					"business_id": "biz-foreign",
				}
			).insert(ignore_permissions=True)

		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Connector Source Link",
					"vc_uuid": frappe.generate_hash(length=12),
					"business_id": "biz-foreign",
				}
			).insert(ignore_permissions=True)

	def test_one_lead_cannot_be_claimed_by_two_central_records(self):
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Single Claim",
				"company_name": "VC Connector Test Co",
				"email_id": "singleclaim@example.com",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", lead.name, force=True, ignore_permissions=True)

		first = frappe.get_doc(
			{"doctype": "Connector Lead Link", "vc_uuid": frappe.generate_hash(length=12), "lead": lead.name}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Connector Lead Link", first.name, force=True, ignore_permissions=True)
		self.assertEqual(first.business_id, "biz-alpha")

		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc(
				{"doctype": "Connector Lead Link", "vc_uuid": frappe.generate_hash(length=12), "lead": lead.name}
			).insert(ignore_permissions=True)

	def test_a_multi_business_site_still_refuses_a_duplicate(self):
		"""Switching reject_business_mismatch off hands the guard over, it does not remove it.

		With mismatches allowed, CustomLead.check_email_id_is_unique steps aside for stock
		ERPNext's site-wide rule and validate_duplicate_lead_contacts becomes the authority.
		If that handover ever leaves a gap, a retried push creates a second Lead.
		"""
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.reject_business_mismatch = 0
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		first = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Multi Biz A",
				"company_name": "VC Connector Test Co",
				"email_id": "multibiz@example.com",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", first.name, force=True, ignore_permissions=True)

		with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
			frappe.get_doc(
				{
					"doctype": "Lead",
					"first_name": "Multi Biz B",
					"company_name": "VC Connector Test Co",
					"email_id": "multibiz@example.com",
					"custom_business_id": "biz-other",
				}
			).insert(ignore_permissions=True)

	def test_unconfigured_site_has_no_business(self):
		self.assertEqual(get_site_business_id(), "")

	def test_a_multi_business_site_refuses_a_retry_for_a_FOREIGN_business(self):
		"""The retry that actually happens: the same push, twice, for a business that is not
		this site's own.

		With reject_business_mismatch off, CustomLead.check_email_id_is_unique steps aside
		for stock ERPNext's site-wide rule, so validate_duplicate_lead_contacts is the only
		guard left -- and it used to resolve the business from Settings alone, which meant
		every Lead of the foreign business was discarded as a candidate and the second push
		created a second Lead. Nothing else covered it.
		"""
		settings = frappe.get_single(SETTINGS)
		settings.business_id = "biz-alpha"
		settings.reject_business_mismatch = 0
		settings.save(ignore_permissions=True)
		frappe.clear_cache()

		payload = {
			"doctype": "Lead",
			"first_name": "Foreign Retry",
			"company_name": "VC Connector Test Co",
			"email_id": "foreignretry@example.com",
			"mobile_no": "+91 60010 00001",
			"custom_business_id": "biz-other",
		}

		first = frappe.get_doc(dict(payload)).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", first.name, force=True, ignore_permissions=True)
		self.assertEqual(first.custom_business_id, "biz-other")

		with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
			frappe.get_doc(dict(payload)).insert(ignore_permissions=True)

	def test_an_unconfigured_site_still_refuses_a_duplicate(self):
		"""The state of every upgraded client the moment `bench migrate` finishes.

		Connector Settings is a brand new Single and nothing prompts anyone to fill it in,
		so business_id is empty. The guard used to return immediately in that state, and the
		client's whole pre-existing Lead book -- whose stock email_id / mobile_no the removed
		bulk rewrite had nulled -- was left with no duplicate protection from anything.
		"""
		self.assertEqual(get_site_business_id(), "")

		# Unique per run. A fixed address ties this test to the site's history: one run that
		# is interrupted (a locked database, a Ctrl-C) leaves the Lead behind, and every later
		# run then fails in its own SETUP, reporting a duplicate as if the code were broken.
		email = f"unconfigured-{frappe.generate_hash(length=8)}@example.com"
		mobile = "+91 60020 " + "".join(secrets.choice("0123456789") for _ in range(5))

		legacy = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": "Unconfigured Legacy",
				"company_name": "VC Connector Test Co",
				"email_id": email,
				"mobile_no": mobile,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Lead", legacy.name, force=True, ignore_permissions=True)

		retries = [
			{"email_id": email},
			{"mobile_no": mobile},
		]
		stamps = [{}]

		if lead_has_contact_tables():
			# The shape the removed bulk rewrite left behind: contacts in the child tables
			# only, with both stock fields nulled. It exists only before the legacy field
			# migration -- afterwards there are no child tables to hold them, and a Lead with
			# both stock fields nulled holds no contact detail at all, so there is nothing for
			# a second Lead to duplicate.
			frappe.db.set_value(
				"Lead", legacy.name, {"email_id": None, "mobile_no": None}, update_modified=False
			)
			retries += [
				{"custom_email_ids": [{"email_id": email}]},
				{"custom_mobile_numbers": [{"mobile_number": mobile}]},
			]
			stamps.append({"custom_business_id": "biz-one"})

		for extra in stamps:
			for contact in retries:
				payload = {
					"doctype": "Lead",
					"first_name": "Retry",
					"company_name": "VC Connector Test Co",
				}
				payload.update(contact)
				payload.update(extra)

				with self.assertRaises(
					(frappe.ValidationError, frappe.DuplicateEntryError), msg=repr(payload)
				):
					frappe.get_doc(payload).insert(ignore_permissions=True)
