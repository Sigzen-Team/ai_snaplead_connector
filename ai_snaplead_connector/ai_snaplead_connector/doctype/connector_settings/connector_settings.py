# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document

SETTINGS_DOCTYPE_NAME = "Connector Settings"


class ConnectorSettings(Document):
	def validate(self):
		self.business_id = (self.business_id or "").strip()
		self.central_site = (self.central_site or "").strip().rstrip("/")
		self.validate_business_id_change()

	def validate_business_id_change(self):
		"""business_id identifies this ERP site to central, and every Connector Lead Link,
		Connector Source Link and Lead.custom_business_id stamp carries it too. Changing only
		the setting left those rows on the old value, so the next push from central failed
		with BUSINESS_MISMATCH. A System Manager may change it; on_update then re-stamps every
		row that still carries the old value, so the site stays internally consistent."""
		previous = (frappe.db.get_single_value(SETTINGS_DOCTYPE_NAME, "business_id") or "").strip()
		if not previous or previous == self.business_id:
			return

		if "System Manager" not in frappe.get_roles():
			frappe.throw(
				_("Only a System Manager can change the Business ID (currently {0}).").format(previous),
				frappe.PermissionError,
			)

		self.flags.previous_business_id = previous

	def on_update(self):
		previous = self.flags.pop("previous_business_id", None)
		if not previous:
			return

		counts = restamp_business_rows(self.business_id, previous_business_id=previous)
		frappe.clear_cache()
		frappe.msgprint(
			_(
				"Business ID changed from {0} to {1}. Re-stamped {2} lead link(s), {3} source link(s)"
				" and {4} lead(s). On the central site, make sure the ERPNext Connector of business {1}"
				" points to this site and holds this site's connector token."
			).format(
				previous,
				self.business_id,
				counts["lead_links"],
				counts["source_links"],
				counts["leads"],
			),
			title=_("Business ID Changed"),
			indicator="green",
		)


def restamp_business_rows(business_id, previous_business_id=None):
	"""Point every connector row at business_id.

	With previous_business_id, only rows carrying that value move (a deliberate change from the
	form). Without it, every link row that does not already say business_id moves (the repair of
	a site whose stamps are mixed). Lead stamps that are empty are left alone: an untagged Lead
	is not a mismatch.

	update_modified=False: these rows are bookkeeping for pushes that already happened, and
	central reads `modified` to decide what to re-send.
	"""
	if previous_business_id:
		link_filters = {"business_id": previous_business_id}
		lead_filters = {"custom_business_id": previous_business_id}
	else:
		link_filters = {"business_id": ["!=", business_id]}
		lead_filters = {"custom_business_id": ["not in", ["", business_id]]}

	# One UPDATE per table, not one per row: a site with tens of thousands of leads made a
	# Business ID change a request-timeout. frappe.db.count and frappe.db.set_value with a
	# filters dict both build their WHERE through frappe.qb.get_query, the same engine get_all
	# used, so the counts reported are exactly the rows the UPDATE touches.
	counts = {}
	for key, doctype in (("lead_links", "Connector Lead Link"), ("source_links", "Connector Source Link")):
		counts[key] = _bulk_restamp(doctype, "business_id", link_filters, business_id)

	counts["leads"] = 0
	if frappe.get_meta("Lead").has_field("custom_business_id"):
		counts["leads"] = _bulk_restamp("Lead", "custom_business_id", lead_filters, business_id)

	return counts


def _bulk_restamp(doctype, fieldname, filters, business_id):
	count = frappe.db.count(doctype, filters)
	if count:
		frappe.db.set_value(doctype, filters, fieldname, business_id, update_modified=False)
	return count


@frappe.whitelist()
def preview_business_id_change(business_id: str):
	"""How many rows a change away from business_id would re-stamp, for the form's confirmation."""
	frappe.only_for("System Manager")

	business_id = (business_id or "").strip()
	counts = {
		"lead_links": frappe.db.count("Connector Lead Link", {"business_id": business_id}),
		"source_links": frappe.db.count("Connector Source Link", {"business_id": business_id}),
		"leads": 0,
	}
	if business_id and frappe.get_meta("Lead").has_field("custom_business_id"):
		counts["leads"] = frappe.db.count("Lead", {"custom_business_id": business_id})
	return counts


@frappe.whitelist(methods=["POST"])
def generate_connector_token():
	"""Mint the shared secret this site expects from central, and show it ONCE.

	WHY THIS EXISTS

	upsert_lead_graph requires two factors: an ERPNext API key (which says WHICH USER) and
	the X-VC-Connector-Token header (which says WHICH CENTRAL SITE). Only the SHA-256 of the
	token is stored here, so the plaintext cannot be read back later -- which is correct, and
	also meant there was no way to OBTAIN one. Setting a connector up required generating a
	random string by hand, hashing it by hand, pasting the hash into this form and the
	plaintext into central. Undocumented, and easy to get wrong in a way that surfaces only
	as "TOKEN_INVALID" on the first push.

	Returns the plaintext once. It is never stored and cannot be retrieved again; generating
	a new one invalidates the old, so an existing connector must be updated with the new value.
	"""
	import secrets

	frappe.only_for("System Manager")

	token = secrets.token_urlsafe(32)
	settings = frappe.get_single("Connector Settings")
	settings.token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
	settings.flags.ignore_permissions = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep - the token is shown once; its hash must be stored before it is returned

	return {
		"token": token,
		"business_id": settings.business_id,
		"note": (
			"Paste this token into the ERPNext Connector on the central site, in Connector"
			" Token. It is shown once and cannot be recovered."
		),
	}


@frappe.whitelist(methods=["POST"])
def repair_business_id(business_id: str, confirm_relink: int | str = 0):
	"""Correct a business_id that was set wrongly, and re-stamp the rows it orphaned.

	WHEN TO USE THIS INSTEAD OF THE FORM

	Changing Business ID in the form re-stamps only the rows that carry the OLD value. That is
	the normal case. This repair is for a site whose rows are MIXED -- some on the right
	business, some on a wrong one -- so every row that does not already say business_id is
	moved, which the form will not do. Left alone, every push fails with BUSINESS_MISMATCH:

	    [BUSINESS_MISMATCH] This payload is for business ORG-00103, but this site belongs to
	    business VCTEST-BIZ.

	Observed exactly that: a client whose 39 of 42 link rows said ORG-00103 while the setting
	said VCTEST-BIZ, because the connector's own test suite had written the test value onto a
	real site.

	WHAT THIS DOES ABOUT THE LINK ROWS

	It reports how many Connector Lead Link rows carry a DIFFERENT business and refuses until the
	caller passes confirm_relink, because re-stamping them is the part that actually moves
	data between tenants. When confirmed it re-stamps them to match, so the site is left
	internally consistent rather than half-migrated.
	"""
	frappe.only_for("System Manager")

	business_id = (business_id or "").strip()
	if not business_id:
		frappe.throw(_("A business_id is required."))

	settings = frappe.get_single(SETTINGS_DOCTYPE_NAME)
	current = (settings.business_id or "").strip()
	if current == business_id:
		return {"changed": False, "business_id": business_id, "relinked": 0}

	mismatched = frappe.db.count("Connector Lead Link", {"business_id": ["!=", business_id]})
	if mismatched and not int(confirm_relink or 0):
		frappe.throw(
			_(
				"{0} Connector Lead Link row(s) carry a different business. Re-stamping them moves "
				"records between tenants, so pass confirm_relink=1 to proceed."
			).format(mismatched)
		)

	# A direct single-value write, deliberately: the change is already confirmed above, and the
	# rows are re-stamped below for every business, not only the one the setting held.
	frappe.db.set_single_value(SETTINGS_DOCTYPE_NAME, "business_id", business_id)
	counts = restamp_business_rows(business_id)

	frappe.db.commit()  # nosemgrep - an operator repair run from the console, outside any request
	frappe.clear_cache()
	return {
		"changed": True,
		"from": current,
		"business_id": business_id,
		"relinked": counts["lead_links"],
		**counts,
	}
