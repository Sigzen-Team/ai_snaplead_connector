# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from sigzen_ai_scan_crm_connector.tenancy import stamp_and_validate_business


class ConnectorSourceLink(Document):
	def validate(self):
		self.vc_uuid = (self.vc_uuid or "").strip()
		# Same reason as Connector Lead Link: this row is connector identity, so it is subject to
		# reject_business_mismatch just like the Lead it came in with.
		stamp_and_validate_business(self)
