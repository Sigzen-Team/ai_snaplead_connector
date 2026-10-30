# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Make the suite re-runnable by removing the Contacts it leaves behind.

WHY THIS EXISTS

``IntegrationTestCase`` rolls the class's transaction back, and that reclaims the Leads,
Addresses and link rows these tests write. It does NOT reclaim their Contacts: ERPNext's
``Lead.after_insert`` creates the Contact through a path that lands outside the transaction
the test class rolls back, so every run left a handful of Contact rows on the site carrying
the phone numbers and email addresses the tests use.

That was not cosmetic. This app refuses a second Lead that reuses a stored number, and the
fixed numbers in ``test_lead_contacts`` ("+91 90000 66661" and friends) are the same on every
run -- so a SECOND consecutive ``run-tests`` failed against the orphans the FIRST one left,
with an error naming a Lead that no longer exists. A reviewer running the suite twice saw a
failure that had nothing to do with the code.

ORDERING -- THE WHOLE DIFFICULTY

The sweep has to COMMIT, because the rows it deletes were themselves committed outside the
class transaction. So it must not run while that transaction still holds pending writes, or
the commit makes all of them permanent. The first version of this file swept from
``tearDownClass`` and did exactly that: Contacts went to 0 and NINE Leads appeared on the
site, because frappe had not rolled back yet.

frappe does not roll back in ``tearDownClass``. It registers the rollback in ``setUpClass``
with ``addClassCleanup(_rollback_db)`` (``frappe/tests/classes/integration_test_case.py``),
and class cleanups run AFTER ``tearDownClass``, in LIFO order. To land after frappe's
rollback, this sweep therefore has to be registered BEFORE ``super().setUpClass()`` -- last
registered is first to run, so registering first means running last.

Only rows that did not exist when the class started are touched, so pointing the suite at a
site that already has Contacts cannot delete any of them.
"""

import frappe

# The most a single test class should ever create. See the guard in _sweep_contacts.
SWEEP_LIMIT = 200


def _sweep_contacts(existing):
	"""Delete Contacts created since the class started. Runs after frappe's rollback."""
	frappe.set_user("Administrator")

	# limit_page_length=0 is load-bearing in BOTH queries. frappe.get_all returns 20 rows by
	# default, so without it the snapshot holds 20 names on a site with 89,000 Contacts and
	# every real customer Contact looks "leaked" -- a test helper that deletes the client's
	# address book. The snapshot below carries the same argument for the same reason.
	leaked = [
		name for name in frappe.get_all("Contact", pluck="name", limit_page_length=0) if name not in existing
	]
	if not leaked:
		return

	# A second line of defence behind limit_page_length=0. One class writes a handful of
	# Contacts; hundreds means the snapshot did not describe the site, and the right response
	# to "I think I should delete 4,000 Contacts" is to delete none of them and say so.
	if len(leaked) > SWEEP_LIMIT:
		frappe.logger().error(
			f"sigzenlead_connector tests: refusing to sweep {len(leaked)} Contacts (limit {SWEEP_LIMIT}). "
			"The class-start snapshot is wrong; nothing was deleted."
		)
		return

	for name in leaked:
		try:
			frappe.delete_doc("Contact", name, ignore_permissions=True, force=True, delete_permanently=True)
		except Exception:
			# A Contact something still links to is not worth failing an otherwise passing run
			# over; the next class's sweep collects it once that link is gone.
			frappe.clear_last_message()

	frappe.db.commit()


class LeavesNoContactsBehind:
	"""Mixin for an IntegrationTestCase whose Leads cause Contacts to be created.

	Mix in BEFORE the base class so this ``setUpClass`` runs first:
	``class TestX(LeavesNoContactsBehind, IntegrationTestCase)``.
	"""

	@classmethod
	def setUpClass(cls):
		existing = set(frappe.get_all("Contact", pluck="name", limit_page_length=0))
		# Registered BEFORE super(), which is what puts it AFTER frappe's rollback. See above.
		cls.addClassCleanup(_sweep_contacts, existing)
		super().setUpClass()
