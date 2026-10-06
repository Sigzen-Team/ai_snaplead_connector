# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from sigzen_ai_scan_crm_connector.tenancy import stamp_and_validate_business


class ConnectorLeadLink(Document):
	def validate(self):
		# NULL, never "". The UNIQUE index on vc_uuid ignores NULLs on every supported
		# backend, so any number of placeholder rows can coexist -- but two rows holding ""
		# would collide on the second insert, which is how a 200k-Lead migration would abort
		# on its second unpushed Lead.
		self.vc_uuid = (self.vc_uuid or "").strip() or None
		# This table is the connector's identity map. A row stamped for a business this site
		# is not configured for is a cross-tenant write, and until now reject_business_mismatch
		# was consulted for Lead only -- the link tables accepted anything.
		stamp_and_validate_business(self)
		self.validate_one_link_per_lead()

	def validate_one_link_per_lead(self):
		"""One central record maps to exactly one Lead, and one Lead to exactly one record.

		The UNIQUE index on vc_uuid only covers the first direction. Without this, N different
		central records could each claim the same Lead and the map stops being a map.

		This is a validate-level guard rather than a UNIQUE index on purpose: adding a unique
		index to `lead` would make `bench migrate` abort on any existing site that already has
		two rows for one Lead, and there is no data-migration phase here to clean that up.
		"""
		if not self.lead:
			return

		existing = frappe.db.get_value(
			"Connector Lead Link",
			{"lead": self.lead, "name": ("!=", self.name or "")},
			["name", "vc_uuid"],
			as_dict=True,
		)
		if not existing:
			return

		frappe.throw(
			_("Lead {0} is already linked to central record {1}.").format(self.lead, existing.vc_uuid),
			frappe.DuplicateEntryError,
		)
