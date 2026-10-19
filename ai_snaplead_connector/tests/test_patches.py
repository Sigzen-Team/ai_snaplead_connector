# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now

from ai_snaplead_connector.patches.v1_0.adopt_file_backed_doctypes import preserve_client_added_fields

CHILD_DOCTYPE = "ERPNext Lead Email"
CLIENT_FIELD = "client_added_note"


class TestAdoptFileBackedDoctypes(IntegrationTestCase):
	def _drop_client_field(self):
		for name in frappe.get_all(
			"Custom Field", filters={"dt": CHILD_DOCTYPE, "fieldname": CLIENT_FIELD}, pluck="name"
		):
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)

		frappe.db.delete(
			"DocField", {"parent": CHILD_DOCTYPE, "parenttype": "DocType", "fieldname": CLIENT_FIELD}
		)
		# Inserting a Custom Field alters the table, and the schema change is committed out
		# from under the test transaction -- so the row survives an ordinary test rollback
		# and the cleanup has to be committed too, or the suite leaves the field behind on
		# whatever site it ran against.
		frappe.db.commit()

		# The client's column, which this test added by hand. Dropping it is best-effort:
		# the point of the cleanup is that nothing of the fake field is left in the meta.
		try:
			frappe.db.sql_ddl(f"alter table `tab{CHILD_DOCTYPE}` drop column `{CLIENT_FIELD}`")
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()

		frappe.clear_cache(doctype=CHILD_DOCTYPE)

	def test_a_client_added_field_survives_adoption(self):
		"""These DocTypes shipped with custom = 1, so a System Manager could add fields to
		them from the Desk with no developer mode -- an ordinary, supported thing to do.

		Adoption clears migration_hash and model sync then re-imports the shipped JSON over
		the row, deleting every DocField the client added. The column and its values stay in
		the table but vanish from every form, list view, report and REST response, silently.
		"""
		self.addCleanup(self._drop_client_field)
		self._drop_client_field()

		frappe.db.sql(
			"""insert into `tabDocField`
			(name, parent, parenttype, parentfield, idx, fieldname, label, fieldtype,
			 creation, modified, owner, modified_by, docstatus)
			values (%s, %s, 'DocType', 'fields', 99, %s, 'Client Added Note', 'Data',
			        %s, %s, 'Administrator', 'Administrator', 0)""",
			(frappe.generate_hash(length=10), CHILD_DOCTYPE, CLIENT_FIELD, now(), now()),
		)
		# the client's field has a real column behind it, exactly as adding it from the Desk
		# would have created one
		frappe.db.sql_ddl(f"alter table `tab{CHILD_DOCTYPE}` add column `{CLIENT_FIELD}` varchar(140)")
		frappe.db.commit()

		frappe.clear_cache(doctype=CHILD_DOCTYPE)
		self.assertTrue(frappe.get_meta(CHILD_DOCTYPE).has_field(CLIENT_FIELD))

		carried_over = preserve_client_added_fields(CHILD_DOCTYPE)

		self.assertEqual(carried_over, [CLIENT_FIELD])
		self.assertTrue(frappe.db.exists("Custom Field", {"dt": CHILD_DOCTYPE, "fieldname": CLIENT_FIELD}))

		# it is still a field of the DocType, which is the whole point: a Custom Field is the
		# only carrier that survives the re-import of the shipped JSON.
		meta = frappe.get_meta(CHILD_DOCTYPE, cached=False)
		self.assertTrue(meta.has_field(CLIENT_FIELD))
		field = meta.get_field(CLIENT_FIELD)
		self.assertEqual(field.fieldtype, "Data")
		self.assertEqual(field.label, "Client Added Note")

		# the shipped field is left exactly as it is
		self.assertTrue(meta.has_field("email_id"))

	def test_shipped_fields_are_not_duplicated_as_custom_fields(self):
		carried_over = preserve_client_added_fields(CHILD_DOCTYPE)

		self.assertEqual(carried_over, [])
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": CHILD_DOCTYPE, "fieldname": "email_id"}))
