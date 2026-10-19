# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""PATCH A -- copy the legacy Custom Field values onto standard ERPNext fields.

Drops nothing. The work itself is in ``ai_snaplead_connector.legacy.migrate``; this file exists only
to start it and get out of the way.

``execute()`` enqueues and returns. It does not do the work, and that is the point: a client
with 200k Leads would otherwise watch ``bench migrate`` sit silent for an hour with no way to
tell it from a hang, and a Ctrl-C would leave the migration half done with nothing recording
how far it got. The cursor in Connector Settings makes the background run resumable, and
the per-Lead mark row in Connector Legacy Field Archive makes a re-run a no-op.

A site with no background workers -- which is most test sites and some small clients -- will
see the job queued and never run. That is recorded in ``legacy_migration_status`` and the
supported answer is one command:

	bench --site <site> execute ai_snaplead_connector.legacy.migrate.run_now
"""

import frappe

from ai_snaplead_connector.legacy import state
from ai_snaplead_connector.legacy.migrate import enqueue


def execute():
	if not frappe.db.exists("DocType", "Connector Legacy Field Archive"):
		# post_model_sync, so this should not happen; if it does, the mark row has nowhere to
		# live and the migration would not be re-runnable. Better to do nothing.
		return

	if state.fields_are_dropped():
		# Patch B has already run on this site. There is nothing left to migrate, and a
		# re-scan would only look for columns that are gone.
		return

	enqueue(reset=False)
