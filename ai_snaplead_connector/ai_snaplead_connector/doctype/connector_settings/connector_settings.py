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
		self.validate_business_id_is_immutable()

	def validate_business_id_is_immutable(self):
		"""business_id identifies this ERP site to central. Re-pointing a site at a
		different business would silently re-tenant every row already linked through
		Connector Lead Link, so the field is write-once."""
		previous = frappe.db.get_single_value("Connector Settings", "business_id")
		if previous and previous != self.business_id:
			frappe.throw(
				_("Business ID is already set to {0} and cannot be changed.").format(previous),
				frappe.ValidationError,
			)


@frappe.whitelist()
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


@frappe.whitelist()
def repair_business_id(business_id: str, confirm_relink: int | str = 0):
	"""Correct a business_id that was set wrongly, and re-stamp the rows it orphaned.

	WHY AN ESCAPE HATCH IS NEEDED

	validate_business_id_is_immutable makes the field write-once, and the reason is sound: a
	silent re-point would re-tenant every row already linked through Connector Lead Link. But
	write-once with no supported correction means ONE wrong value -- a typo at onboarding, or
	a test suite writing over a live site -- is permanent, and every push afterwards fails
	with BUSINESS_MISMATCH and no way out:

	    [BUSINESS_MISMATCH] This payload is for business ORG-00103, but this site belongs to
	    business VCTEST-BIZ.

	Observed exactly that: a client whose 39 of 42 link rows said ORG-00103 while the setting
	said VCTEST-BIZ, because the connector's own test suite had written the test value onto a
	real site and the guard then froze it there.

	WHAT THIS DOES ABOUT THE LINK ROWS

	It reports how many Connector Lead Link rows carry a DIFFERENT business and refuses until the
	caller passes confirm_relink, because re-stamping them is the part that actually moves
	data between tenants. When confirmed it re-stamps them to match, so the site is left
	internally consistent rather than half-migrated.

	update_modified=False: these rows are bookkeeping for a push that already happened, and
	central reads `modified` to decide what to re-send.
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

	# A direct single-value write, deliberately: the document API would run the immutability guard, which is
	# the thing being overridden here under an explicit confirmation.
	frappe.db.set_single_value(SETTINGS_DOCTYPE_NAME, "business_id", business_id)

	relinked = 0
	if mismatched:
		for name in frappe.db.sql_list(
			"select name from `tabVC Lead Link` where ifnull(business_id,'') != %s", business_id
		):
			frappe.db.set_value("Connector Lead Link", name, "business_id", business_id, update_modified=False)
			relinked += 1

	frappe.db.commit()  # nosemgrep - an operator repair run from the console, outside any request
	frappe.clear_cache()
	return {"changed": True, "from": current, "business_id": business_id, "relinked": relinked}
