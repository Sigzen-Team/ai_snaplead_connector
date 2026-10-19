# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class ConnectorLog(Document):
	@staticmethod
	def clear_old_logs(days=90):
		"""Called by Log Settings (hooks.default_log_clearing_doctypes): one row per push adds up."""
		table = frappe.qb.DocType("Connector Log")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))
