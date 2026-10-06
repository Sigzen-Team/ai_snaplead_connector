# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Where "which business is this?" is answered on the ERP side.

Before this release the answer lived only in ``Lead.custom_business_id``: a Custom Field on a
DocType ERPNext owns, stamped per row by whatever happened to push the Lead. That made the
site's own identity implicit, unverifiable and impossible to configure before the first push.

It now lives in the ``Connector Settings`` single. ``Lead.custom_business_id`` is kept as a
denormalised per-row stamp -- existing client sites hold real data in that column and the
duplicate guard filters on it -- but Settings is the authority.
"""

import frappe
from frappe import _

SETTINGS = "Connector Settings"


def get_settings():
	"""Return the settings single, or None when the DocType is not synced yet.

	Called from Lead.validate, which also runs during install/migrate before this app's
	own DocTypes exist, so a missing table must not be fatal.
	"""
	try:
		return frappe.get_cached_doc(SETTINGS)
	except Exception:
		return None


def get_site_business_id():
	"""The business this ERP site belongs to, or empty string."""
	try:
		return (frappe.db.get_single_value(SETTINGS, "business_id") or "").strip()
	except Exception:
		return ""


def get_business_id(doc=None):
	"""Resolve the business for a document.

	Settings wins. The per-row stamp is only a fallback, for sites onboarded before
	Connector Settings existed and not yet configured.
	"""
	site_business_id = get_site_business_id()
	if site_business_id:
		return site_business_id

	if doc is not None:
		return (doc.get("custom_business_id") or "").strip() or None

	return None


def is_business_mismatch(doc, fieldname="custom_business_id"):
	"""True when a document is stamped for a different business than this site's.

	Only meaningful once the site has been configured; an unstamped document is not a
	mismatch, it is simply untagged.
	"""
	site_business_id = get_site_business_id()
	if not site_business_id:
		return False

	stamped = (doc.get(fieldname) or "").strip()
	if not stamped:
		return False

	return stamped != site_business_id


def reject_business_mismatch_enabled():
	try:
		return bool(frappe.db.get_single_value(SETTINGS, "reject_business_mismatch"))
	except Exception:
		return False


def stamp_and_validate_business(doc, fieldname="business_id"):
	"""Fill in this site's business on a connector row, and refuse a foreign stamp.

	Used by the connector's own link tables. They hold the identity map between the central
	site and this ERP, so a row stamped for a business this site is not configured for is a
	cross-tenant write -- exactly what ``reject_business_mismatch`` exists to stop. Before
	this, the switch was consulted for Lead only and the link tables accepted anything.
	"""
	stamped = (doc.get(fieldname) or "").strip()
	site_business_id = get_site_business_id()

	if doc.flags.get("ignore_business_mismatch"):
		# Set by the legacy field migration only. It is copying a stamp the client's own data
		# already carries, from a Lead that predates Connector Settings entirely. Refusing
		# it would abort that Lead's migration for ever -- and because Patch B will not drop
		# anything until every Lead verifies, one such Lead would block the whole client.
		doc.set(fieldname, stamped or site_business_id or None)
		return

	if not stamped:
		doc.set(fieldname, site_business_id or None)
		return

	doc.set(fieldname, stamped)

	if not is_business_mismatch(doc, fieldname):
		return

	if not reject_business_mismatch_enabled():
		return

	frappe.throw(
		_("This {0} belongs to business {1}, but this site is configured for business {2}.").format(
			doc.doctype, stamped, site_business_id
		),
		frappe.PermissionError,
	)


def site_accepts_multiple_businesses():
	"""True only when this site is deliberately configured to hold more than one business.

	A configured site that rejects mismatched stamps holds exactly one business, so stock
	ERPNext's site-wide duplicate guards are correctly scoped for it and must stay on. Only
	when an admin switches ``reject_business_mismatch`` off does "site-wide" stop meaning
	"within this business", and only then may a business-scoped guard replace a stock one.
	"""
	if not get_site_business_id():
		return False

	return not reject_business_mismatch_enabled()


def get_duplicate_scope_business_ids(doc=None):
	"""Every business whose Leads a duplicate check for ``doc`` has to cover.

	``get_business_id`` answers "which business does this site speak for", and Settings
	deliberately outranks the per-row stamp there. That answer is WRONG for the duplicate
	guard on a site that holds more than one business (``reject_business_mismatch`` off):
	a push stamped for a foreign business was checked against this site's own business
	only, so every Lead of that foreign business was discarded as a candidate and an
	identical retried push created a second Lead -- with nothing else covering it, because
	``CustomLead.check_email_id_is_unique`` hands ERPNext's site-wide guard over in exactly
	that mode.

	The scope is the union of the two: this site's own business (which is the business an
	unstamped Lead belongs to) plus the business the incoming row names. It is deliberately
	a union and not a swap -- a foreign-stamped push must not be allowed to reuse a contact
	this site's own business already holds.

	An empty list means the site has no business configured and the row names none either.
	No Lead can then be excluded on business grounds, so the guard searches the whole table
	-- which is what a single-tenant site wants, and what an upgraded client gets by default
	until somebody fills Connector Settings in.
	"""
	scope = []

	site_business_id = get_site_business_id()
	if site_business_id:
		scope.append(site_business_id)

	if doc is not None:
		stamped = (doc.get("custom_business_id") or "").strip()
		if stamped and stamped not in scope:
			scope.append(stamped)

	return scope
