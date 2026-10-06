# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Guards on the shape of the app itself.

These are the invariants that make sigzen_ai_scan_crm_connector safe to install on a site whose data belongs
to somebody else. They are cheap, and each one has actually been violated by this app before.
"""

import frappe
from frappe.tests import IntegrationTestCase

from sigzen_ai_scan_crm_connector.patches.v1_0.adopt_file_backed_doctypes import ADOPTED_DOCTYPES

OWNED_DOCTYPES = (
	*ADOPTED_DOCTYPES,
	"Connector Settings",
	"Connector Lead Link",
	"Connector Source Link",
	"Connector Log",
)


class TestConnectorSchema(IntegrationTestCase):
	def test_every_owned_doctype_is_file_backed(self):
		"""custom = 1 means "no app owns this": unversioned, invisible to migrate."""
		for doctype in OWNED_DOCTYPES:
			with self.subTest(doctype=doctype):
				row = frappe.db.get_value("DocType", doctype, ["custom", "module"], as_dict=True)
				self.assertIsNotNone(row, f"{doctype} was not created by model sync")
				self.assertEqual(row.custom, 0, f"{doctype} is a custom DocType")
				self.assertEqual(row.module, "Sigzen - AI Scan CRM Connector")

	def test_install_does_not_hide_stock_lead_contact_fields(self):
		"""Lead.email_id / Lead.mobile_no belong to ERPNext. We do not hide them."""
		meta = frappe.get_meta("Lead")
		for fieldname in ("email_id", "mobile_no"):
			with self.subTest(fieldname=fieldname):
				df = meta.get_field(fieldname)
				self.assertFalse(df.hidden, f"Lead.{fieldname} is hidden")
				self.assertFalse(df.read_only, f"Lead.{fieldname} is read only")

	def test_install_module_has_no_bulk_record_rewrite(self):
		"""The install hook must never load-and-save every Lead on the client."""
		from sigzen_ai_scan_crm_connector import install

		for removed in (
			"create_child_doctypes",
			"repair_activity_log_source_link",
			"hide_legacy_lead_contact_fields",
			"migrate_existing_lead_contacts",
		):
			with self.subTest(function=removed):
				self.assertFalse(
					hasattr(install, removed),
					f"install.{removed} is back; it mutates records this app does not own",
				)

	def test_vc_uuid_is_unique(self):
		"""Without a UNIQUE index a retried push creates two Leads for one central record."""
		for doctype in ("Connector Lead Link", "Connector Source Link"):
			with self.subTest(doctype=doctype):
				df = frappe.get_meta(doctype).get_field("vc_uuid")
				self.assertTrue(df.unique, f"{doctype}.vc_uuid is not unique")

	def test_vc_lead_link_accepts_a_placeholder_without_a_uuid(self):
		"""Connector Lead Link.vc_uuid is deliberately NOT mandatory, and Connector Source Link's still is.

		The legacy field migration writes a link row for a Lead that has a business_id but has
		never been pushed under protocol 1, so that ``custom_business_id`` survives that Custom
		Field being dropped. Such a row names no central record yet; the first push adopts it
		and fills the UUID in. A mandatory vc_uuid would make that row impossible to write, and
		the alternative -- storing "" -- collides with the UNIQUE index on the second such Lead.
		So the value must be NULL, which means not mandatory, and the "one link row per Lead"
		half of the invariant is enforced by ConnectorLeadLink.validate instead.
		"""
		self.assertFalse(
			frappe.get_meta("Connector Lead Link").get_field("vc_uuid").reqd,
			"Connector Lead Link.vc_uuid is mandatory again; the migration cannot write a placeholder",
		)
		self.assertTrue(frappe.get_meta("Connector Source Link").get_field("vc_uuid").reqd)
