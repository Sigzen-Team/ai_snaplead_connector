# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""PATCH B -- verify, archive, then drop the legacy Custom Fields.

Runs AFTER Patch A in patches.txt, but ordering is not the guarantee: Patch A only enqueues,
so on a normal ``bench migrate`` this patch runs while the migration is still queued. It
therefore refuses -- logs a Connector Log row, re-enqueues Patch A, and returns having
dropped nothing. The drop then happens on a later migrate, once the data is actually ready,
or immediately when an operator runs:

	bench --site <site> execute sigzen_ai_scan_crm_connector.legacy.drop.run

That is deliberate. "The fields are dropped only after a verification pass proves every value
has a copy on a standard field" is a stronger promise than any ordering between two patches,
and it holds however the two are invoked.

Never raises. A patch that raises aborts ``bench migrate`` for the whole site, and "this
client's data is not ready yet" is not a reason to break their upgrade.
"""

import frappe

from sigzen_ai_scan_crm_connector.legacy.drop import run


def execute():
	if not frappe.db.exists("DocType", "Connector Legacy Field Archive"):
		return

	try:
		run()
	except Exception:
		# Whatever went wrong, it went wrong while trying to DELETE things. Log it and leave
		# the client's fields where they are; the next migrate will try again.
		frappe.log_error(
			title="sigzen_ai_scan_crm_connector: legacy field drop failed",
			message=frappe.get_traceback(),
		)
