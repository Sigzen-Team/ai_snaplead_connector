# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Where the migration keeps its place.

The cursor lives in ``Connector Settings`` rather than in the patch, because a patch runs
once: ``bench migrate`` records it in ``tabPatch Log`` and never calls it again. A client with
200k Leads cannot have that one call walk the whole table inside ``bench migrate`` -- the
migrate would appear to hang, and a Ctrl-C would leave the work half done with nothing saying
how far it got. So ``execute()`` only enqueues, and the background job writes its position
here after every batch. An interrupted run resumes from that position instead of starting
over, and a finished run can be asked to re-scan from the top, because every write it makes
is idempotent.

Every write here goes through ``frappe.db.set_single_value``, never ``doc.save()``:
``Connector Settings.business_id`` is mandatory and is empty on a client who has not been
onboarded yet, so saving the document to record a cursor would throw a MandatoryError and
abort the migration for a reason that has nothing to do with it.
"""

import frappe
from frappe.utils import now_datetime

SETTINGS = "Connector Settings"

STATUS_QUEUED = "Queued"
STATUS_RUNNING = "Running"
STATUS_COMPLETE = "Complete"
STATUS_FAILED = "Failed"

FIELDS = (
	"legacy_migration_status",
	"legacy_migration_cursor",
	"legacy_migration_scanned",
	"legacy_migration_migrated",
	"legacy_migration_failed",
	"legacy_migration_conflicts",
	"legacy_migration_started_on",
	"legacy_migration_completed_on",
	"legacy_fields_dropped",
	"legacy_fields_dropped_on",
	"legacy_migration_message",
)


def available():
	"""False before model sync has created the Single's table (fresh install, pre_model_sync)."""
	return frappe.db.table_exists("Singles") and frappe.db.exists("DocType", SETTINGS)


def get(fieldname, default=None):
	if not available():
		return default
	try:
		value = frappe.db.get_single_value(SETTINGS, fieldname)
	except Exception:
		return default
	return default if value is None else value


def set_values(**values):
	if not available():
		return
	for fieldname, value in values.items():
		frappe.db.set_single_value(SETTINGS, fieldname, value)


def status():
	return (get("legacy_migration_status") or "").strip()


def cursor():
	return (get("legacy_migration_cursor") or "").strip()


def migration_is_complete():
	return status() == STATUS_COMPLETE


def fields_are_dropped():
	return bool(get("legacy_fields_dropped"))


def mark_queued(reset=False):
	"""Announce an enqueued run. ``reset`` rewinds the cursor to the start of the table."""
	values = {
		"legacy_migration_status": STATUS_QUEUED,
		"legacy_migration_message": None,
	}
	if reset:
		values["legacy_migration_cursor"] = None
		values["legacy_migration_scanned"] = 0
		values["legacy_migration_migrated"] = 0
		values["legacy_migration_failed"] = 0
		values["legacy_migration_conflicts"] = 0
	set_values(**values)


def mark_running():
	values = {"legacy_migration_status": STATUS_RUNNING}
	if not get("legacy_migration_started_on"):
		values["legacy_migration_started_on"] = now_datetime()
	set_values(**values)


def advance(last_name, scanned, migrated, failed, conflicts):
	"""Record the end of a batch. Called after the batch's own commit, never before."""
	set_values(
		legacy_migration_cursor=last_name,
		legacy_migration_scanned=scanned,
		legacy_migration_migrated=migrated,
		legacy_migration_failed=failed,
		legacy_migration_conflicts=conflicts,
	)


def mark_complete(scanned, migrated, failed, conflicts):
	set_values(
		legacy_migration_status=STATUS_COMPLETE,
		legacy_migration_completed_on=now_datetime(),
		legacy_migration_scanned=scanned,
		legacy_migration_migrated=migrated,
		legacy_migration_failed=failed,
		legacy_migration_conflicts=conflicts,
		legacy_migration_message=None,
	)


def mark_failed(message):
	set_values(
		legacy_migration_status=STATUS_FAILED,
		legacy_migration_message=(message or "")[:5000],
	)


def mark_dropped():
	set_values(legacy_fields_dropped=1, legacy_fields_dropped_on=now_datetime())
