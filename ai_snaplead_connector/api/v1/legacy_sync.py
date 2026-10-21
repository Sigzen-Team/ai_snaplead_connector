# Copyright (c) 2026, support@sigzen.com and contributors
"""The write endpoint central falls back to when ai_snaplead is NOT on this client.

WHY THIS EXISTS

Central's remote_erp_client tries `ai_snaplead.connector.remote_erp_sync.upsert_document`
first -- an endpoint that only exists if the CENTRAL app is also installed on the client,
which is exactly the coupling ai_snaplead_connector was built to remove. When it is absent the probe
fails and central falls back to plain REST `api/resource/<doctype>`, which is permission- and
mandatory-checked like any Desk write. On a client whose integration user had not been granted
create rights that produced:

    "cannot create UTM Source - permission"
    "email mandatory"

and the lead never arrived, although the mobile app had already reported it as sent. Sources,
Contacts and Addresses all travel this same path, so upsert_lead_graph's own guards never got
a chance to help: central was never calling it for those documents.

This module provides the same contract under the ai_snaplead_connector namespace, so central has
something to reach that is not the permission-checked REST fallback.

ON IGNORING PERMISSIONS AND MANDATORY FIELDS

Deliberate, and the owner's explicit instruction: the write must land. Frappe has already
authenticated the caller (this is a whitelisted method -- an API key or session is required to
reach it at all), and the connector settings must be enabled. Beyond that door the client's
own role matrix and its mandatory fields are treated as setup that must not silently lose a
customer's lead. ALLOWED_DOCTYPES bounds the blast radius to the five documents this
integration actually writes -- it is not a general-purpose remote write API.
"""

import hashlib
import json
import re

import frappe
from frappe import _

from ai_snaplead_connector.install import INTEGRATION_ROLE
from ai_snaplead_connector.tenancy import SETTINGS

#: Who may write through upsert_document: central authenticates as the client's integration
#: user, which is a System Manager or holds the connector's own role. Being logged in is NOT
#: enough -- a portal user or a low-rights employee must not overwrite a Lead, Contact or
#: Address with permissions and mandatory checks switched off.
WRITE_ROLES = ("System Manager", INTEGRATION_ROLE)

ALLOWED_DOCTYPES = {
	"Lead",
	"Lead Source",
	"UTM Source",
	"Contact",
	"Address",
}


def _parse_data(data):
	if isinstance(data, str):
		data = json.loads(data) if data else {}
	return data if isinstance(data, dict) else {}


def _assert_enabled():
	"""The connector must be switched on, exactly as upsert_lead_graph requires."""
	try:
		enabled = frappe.db.get_single_value(SETTINGS, "enabled")
	except Exception:
		enabled = None
	if not enabled:
		frappe.throw(
			_("The connector is disabled on this site. Enable it in Connector Settings."),
			frappe.ValidationError,
		)


@frappe.whitelist()
def ping_remote_sync():
	"""Health check. Central probes this to decide whether this endpoint is reachable.

	The shape -- {"ok": True, "site": ...} -- matches what central already expects from the
	ai_snaplead endpoint, so central needs no new parsing.
	"""
	# code_version is a DEPLOYMENT marker, not decoration. Three separate rounds of "I pulled
	# and restarted" were spent on a client that was still serving older code, with no way to
	# see that from the outside: the fixes in question add no endpoint to probe for. Reporting
	# what is actually loaded turns that into one call.
	from ai_snaplead_connector.utils.phone import is_storable_phone

	return {
		"ok": True,
		"site": frappe.local.site,
		"provider": "ai_snaplead_connector",
		"app_version": _installed_version(),
		"capabilities": {
			# Named after the behaviour, so a caller can tell whether the fix it depends on is
			# live rather than inferring it from a version string nobody bumps.
			"drops_unstorable_phones": not is_storable_phone("+9174051617l3"),
			"drops_unresolvable_links": hasattr(
				frappe.get_module("ai_snaplead_connector.api.v1.sync"), "_drop_unresolvable_link"
			),
		},
	}


def _installed_version():
	try:
		return frappe.get_attr("ai_snaplead_connector.__version__")
	except Exception:
		return None


@frappe.whitelist(methods=["POST"])
def upsert_document(
	doctype: str | None = None,
	data: dict | str | None = None,
	name: str | None = None,
	ignore_mandatory: int | str = 1,
):
	"""Create or update one document, without letting client setup lose the write.

	POST only, so a cross-site GET (an <img src>, a link) cannot reach it with the victim's
	session; central always POSTs (remote_erp_client.remote_upsert_document).
	"""
	frappe.only_for(WRITE_ROLES)
	_assert_enabled()

	doctype = (doctype or "").strip()
	if doctype not in ALLOWED_DOCTYPES:
		frappe.throw(_("Doctype {0} is not allowed for remote sync.").format(doctype))

	payload = _parse_data(data)
	if not payload and not name:
		frappe.throw(_("Data is required to create {0}.").format(doctype))

	# Unknown fieldnames are DROPPED rather than refused. A central site may know a field this
	# client's version does not have, and refusing the whole document over one stray key would
	# lose the lead for a difference that does not matter.
	meta = frappe.get_meta(doctype)
	clean = {k: v for k, v in payload.items() if k == "name" or meta.has_field(k)}

	force_mandatory = bool(int(ignore_mandatory or 0))

	if name and frappe.db.exists(doctype, name):
		doc = frappe.get_doc(doctype, name)
		doc.update(clean)
		doc.flags.ignore_mandatory = force_mandatory
		doc.save(ignore_permissions=True)
		operation = "updated"
	else:
		values = {"doctype": doctype, **clean}
		if name:
			# UTM Source autonames by prompt, so the NAME is the value and has to be supplied.
			values["name"] = name
		doc = frappe.get_doc(values)
		doc.flags.ignore_mandatory = force_mandatory
		doc.insert(ignore_permissions=True, ignore_mandatory=force_mandatory, ignore_if_duplicate=True)
		operation = "created"

	frappe.db.commit()  # nosemgrep - the write must land even if a later hook in this request fails
	return {"name": doc.name, "doctype": doctype, "operation": operation}


#: What a central business id looks like. Central names Business by the naming series
#: ``ORG-.#####`` (ORG-00103), but the series is editable there, so this accepts any short
#: identifier-shaped value rather than that one pattern -- and refuses whitespace, quotes,
#: markup and anything long, which no business id ever contains.
BUSINESS_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,139}$")


def _token_refusal(code, message, field="business_id"):
	"""Refuse in the connector's own error envelope (see sync.ERROR_STATUS), minting nothing."""
	from ai_snaplead_connector.api.v1.sync import ERROR_STATUS

	frappe.local.response["http_status_code"] = ERROR_STATUS.get(code, 422)
	return {"ok": False, "error": {"code": code, "message": message, "field": field, "retryable": False}}


@frappe.whitelist(methods=["POST"])
def issue_connector_token(business_id: str | None = None):
	"""Mint the connector token FOR central, during onboarding, and return it once.

	WHY THIS IS AUTOMATIC

	The owner's requirement is that a client enters three things and nothing more: URL,
	username, password. The connector token is a second factor -- the API key says WHICH USER,
	the token says WHICH CENTRAL SITE -- but there is no reason a human has to carry it
	between two sites. Central is already authenticated here with those same credentials when
	it mints API keys, so it can mint the token in the same exchange.

	Requiring a copy-paste step instead is what produced, on a real QA site:

	    "This connector has no Connector Token. Paste the token issued by the client's
	     ERPNext (Connector Settings) into the ERPNext Connector before pushing."

	-- a dead end for anyone who did not already know the token had to be generated by hand,
	hashed by hand, and split across two forms.

	Only the SHA-256 is stored. The plaintext is returned once, to the caller that just
	authenticated as a System Manager on this site, and never again. Calling this REPLACES any
	existing token, so it is only safe from the onboarding exchange -- which is exactly the
	moment central is about to store the new value.

	BUSINESS ID (1.1.1)

	Central sends the business it is connecting for, so nobody has to open this site to type
	it. A blank Business ID is filled in; the same one is a no-op. A DIFFERENT one is refused
	and no token is minted: this site already belongs to another AI SnapLead business, and
	moving it is the deliberate System Manager change in Connector Settings (which re-stamps
	the rows that carry the old id) -- never a side effect of someone else's onboarding.
	Omitted, the endpoint behaves exactly as in 1.1.0.
	"""
	import secrets

	# Same bar as frappe's own generate_keys, which central calls moments earlier in the same
	# exchange: minting a credential that bypasses permissions is a System Manager action.
	frappe.only_for("System Manager")

	settings = frappe.get_single(SETTINGS)

	if business_id is not None:
		claimed = business_id.strip() if isinstance(business_id, str) else ""
		if not BUSINESS_ID_PATTERN.match(claimed):
			return _token_refusal(
				"VALIDATION_FAILED", _("business_id is not a valid AI SnapLead business ID.")
			)

		current = (settings.business_id or "").strip()
		if current and current != claimed:
			return _token_refusal(
				"BUSINESS_MISMATCH",
				_(
					"This ERPNext is already linked to another AI SnapLead business ({0}), not {1}."
					" An ERPNext System Manager must change Business ID in Connector Settings"
					" before it can be connected to this business."
				).format(current, claimed),
			)
		settings.business_id = claimed

	token = secrets.token_urlsafe(32)
	settings.token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
	settings.flags.ignore_permissions = True
	# Onboarding runs before anyone has typed a Business ID, and business_id is mandatory on
	# the form: without this the save refused and central's onboarding got no token at all.
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep - the token is returned once; its hash must be stored first

	return {"token": token, "business_id": settings.business_id}
