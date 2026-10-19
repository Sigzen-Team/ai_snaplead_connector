# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""The connector's entire API surface: ONE write endpoint and two read endpoints.

Everything a central VC site is allowed to do to a client's ERPNext goes through
``upsert_lead_graph``. There is no second way in, and deliberately so:

* the previous design pushed each document separately over the generic REST resource API
  (``/api/resource/Lead``, then ``/api/resource/Contact``, then ``/api/resource/Address``),
  each in its own HTTP request and so in its own transaction. A failure on the third
  request left the client holding a Lead with no Address and no way to tell that from a
  Lead that legitimately has none. "Partially synced" was a representable state, and a
  retry then created duplicates of whatever HAD landed. One endpoint, one savepoint, one
  identity key removes that state entirely: after a call, the graph is either wholly there
  or wholly absent;
* it also required ``ai_snaplead`` to be installed on the CLIENT (the old probe looked for
  ``ai_snaplead.connector.remote_erp_sync``), which is exactly the coupling this app exists
  to remove. Nothing here imports ai_snaplead; the two normalisers it shares are VENDORED
  under ``ai_snaplead_connector/utils/`` with a fixture pinning them.

The validation order in ``upsert_lead_graph`` is fixed and every step runs BEFORE any write:

  1. authentication -- two factors (see ``_authenticate``);
  2. the connector is switched on;
  3. the payload's business is this site's business. This is the tenancy check, made ONCE,
     at the door. It replaces filtering every query by ``custom_business_id``, which was a
     check that had to be remembered at every call site and therefore was not;
  4. the protocol version is one we speak;
  5. the payload's shape, rejecting unknown keys instead of dropping them silently;
  6. normalisation of phones, emails, pincode and state.

Only then is a savepoint taken and the Lead, its Contact, its Address, its file attachments
and the Connector Lead Link row written inside it. Nothing here commits: a commit per document is
what made partial syncs possible in the first place, and the request's own commit at the end
of a successful response is the only one this app wants.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.utils import cint, cstr, now_datetime

from ai_snaplead_connector import __version__ as APP_VERSION
from ai_snaplead_connector.install import INTEGRATION_ROLE
from ai_snaplead_connector.legacy.schema import is_scanned_address_title, lead_has_contact_tables
from ai_snaplead_connector.tenancy import SETTINGS
from ai_snaplead_connector.utils.india_state import (
	canonical_state,
	derive_indian_state,
	normalize_pincode,
)
from ai_snaplead_connector.utils.phone import (
	dedupe_emails,
	dedupe_phones,
	normalize_email,
	normalize_mobile_number,
	phone_match_key,
)

PROTOCOL = "vc-connector/1"
SUPPORTED_PROTOCOLS = ("vc-connector/1",)
CONNECTOR_TOKEN_HEADER = "X-VC-Connector-Token"
SAVEPOINT = "vc_upsert_lead_graph"

DEFAULT_PULL_PAGE = 50
MAX_PULL_PAGE = 200

# Declared defaults. A mandatory field that this app fills in on the caller's behalf is
# always reported back in `defaults_applied`, so the central site can show what it did not
# send rather than discovering a "-" in a client's address book months later.
DEFAULT_ADDRESS_CITY = "-"
DEFAULT_ADDRESS_COUNTRY = "India"
DEFAULT_ADDRESS_TYPE = "Billing"
DEFAULT_LEAD_NAME_PREFIX = "Scanned Lead"

# The typed error contract. Every failure -- including an unexpected one -- leaves this
# endpoint as {"ok": false, "error": {...}} with one of these codes and this status.
ERROR_STATUS = {
	"UNAUTHENTICATED": 401,
	"TOKEN_INVALID": 401,
	"BUSINESS_MISMATCH": 403,
	"REMOTE_PERMISSION": 403,
	"CONNECTOR_DISABLED": 409,
	"CONFLICT": 409,
	"CONCURRENT_MODIFICATION": 409,
	"SETUP_REQUIRED": 409,
	"VALIDATION_FAILED": 422,
	"MANDATORY_MISSING": 422,
	"PROTOCOL_UNSUPPORTED": 426,
	"INTERNAL": 500,
}

# What "retryable" means here: re-sending THESE SAME BYTES, unchanged, can succeed later.
#
# Most failures cannot -- they are a decision about this payload or this configuration and
# will fail identically for ever, so a client that retries them is only generating load (the
# old pusher retried 422s in a loop). Three can:
#
#   INTERNAL                 -- an unexpected failure on the client; nothing says it recurs;
#   CONCURRENT_MODIFICATION  -- somebody saved the Lead in the Desk while this push was
#                               building it. That is a race, not a verdict, and the retry is
#                               the whole remedy. It used to be folded into CONFLICT, whose
#                               contract says never to retry, so a salesperson pressing
#                               Ctrl+S at the wrong moment permanently dropped a record;
#   SETUP_REQUIRED           -- something a human has to switch on at this client (today:
#                               a default Address Template). The payload is fine and will be
#                               accepted the moment the client is set up, so the central site
#                               must keep the record rather than mark it permanently failed.
RETRYABLE_CODES = frozenset({"INTERNAL", "CONCURRENT_MODIFICATION", "SETUP_REQUIRED"})


class ConnectorError(Exception):
	"""A refusal with a code from the contract above. Never leaks a traceback to the caller."""

	def __init__(self, code, message, field=None):
		super().__init__(message)
		self.code = code
		self.message = message
		self.field = field


# ── Payload schema ───────────────────────────────────────────────────────────────────────
# Unknown keys are REJECTED, not dropped. A dropped key is the worst possible outcome: the
# central site believes it sent a field, the client never stores it, and nothing anywhere
# says so. A 422 naming the key is how a protocol mismatch becomes visible on the day it
# starts instead of at the first audit.

TOP_LEVEL_SCHEMA = {
	"protocol": str,
	"request_id": str,
	"business_id": str,
	"vc_uuid": str,
	"legacy_remote_name": str,
	"lead": dict,
	"contact": dict,
	"address": dict,
	"attachments": list,
}
REQUIRED_TOP_LEVEL = ("protocol", "request_id", "business_id", "vc_uuid", "lead")

# Protocol name -> ERPNext fieldname. The protocol deliberately does NOT expose raw ERPNext
# fieldnames: `custom_address` and `custom_pincode` are this app's Custom Fields and renaming
# one must not break every central site in the field.
LEAD_FIELD_MAP = {
	"first_name": "first_name",
	"middle_name": "middle_name",
	"last_name": "last_name",
	"lead_name": "lead_name",
	"salutation": "salutation",
	"gender": "gender",
	"job_title": "job_title",
	"company_name": "company_name",
	"website": "website",
	"status": "status",
	"territory": "territory",
	"industry": "industry",
	"market_segment": "market_segment",
	"city": "city",
	"state": "state",
	"country": "country",
	"whatsapp_no": "whatsapp_no",
	"address_text": "custom_address",
	"pincode": "custom_pincode",
	"scanned_text": "custom_scanned_text",
	"created_from_scan": "custom_created_from_scan",
}

LEAD_SCHEMA = dict.fromkeys(LEAD_FIELD_MAP, str)
LEAD_SCHEMA["created_from_scan"] = (int, bool)
LEAD_SCHEMA["emails"] = list
LEAD_SCHEMA["phones"] = list
# `source` is mapped at runtime: ERPNext v16 renamed Lead.source to Lead.utm_source.
LEAD_SCHEMA["source"] = str

CONTACT_SCHEMA = {
	"first_name": str,
	"middle_name": str,
	"last_name": str,
	"salutation": str,
	"gender": str,
	"designation": str,
	"company_name": str,
}

ADDRESS_SCHEMA = {
	"address_title": str,
	"address_type": str,
	"address_line1": str,
	"address_line2": str,
	"city": str,
	"state": str,
	"country": str,
	"pincode": str,
}

ATTACHMENT_SCHEMA = {
	"file_name": str,
	"content_base64": str,
	"is_private": (int, bool),
	"kind": str,
}
ATTACHMENT_KINDS = {
	"front_card": "custom_front_card_image",
	"back_card": "custom_back_card_image",
}

# Writing as `ignore_permissions=True` is acceptable ONLY because steps 1-3 already decided
# the caller may write to this site. It is not a licence to write as somebody who was never
# granted the right: the check below runs first, for the AUTHENTICATED user, so a client
# whose integration user lacks the roles gets a 403 naming the missing right instead of a
# silent write that no ERPNext permission model can explain afterwards.
REQUIRED_PERMISSIONS = (
	("Lead", "create"),
	("Lead", "write"),
	("Contact", "create"),
	("Contact", "write"),
	("Address", "create"),
	("Address", "write"),
	("Connector Lead Link", "create"),
)


# ── Endpoints ────────────────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["GET", "POST"])
def ping():
	"""What this client can do, DETECTED -- never assumed.

	The central site used to guess: it assumed india_compliance was present because the
	client was Indian, and it assumed `Lead.source` because that is what ERPNext v14 called
	it. Both assumptions are wrong on some client, and each produced a push that failed on
	every record with a message about a field the central site had never heard of.
	"""
	settings = _settings_or_none()

	return {
		"ok": True,
		"protocol": PROTOCOL,
		"protocols_supported": list(SUPPORTED_PROTOCOLS),
		"app_version": APP_VERSION,
		"business_id": (settings.get("business_id") if settings else None) or None,
		"lead_source_field": _lead_source_fieldname(),
		"india_compliance": india_compliance_installed(),
		"enabled": bool(settings and cint(settings.get("enabled"))),
		"connector_token_configured": bool(settings and (settings.get("token_hash") or "").strip()),
		"default_address_template": _has_default_address_template(),
		"erpnext_version": _installed_version("erpnext"),
		"frappe_version": _installed_version("frappe"),
	}


@frappe.whitelist(methods=["POST"])
def upsert_lead_graph(payload: dict | str | None = None, **kwargs):
	"""Create or update one lead graph -- Lead, Contact, Address, attachments -- atomically."""
	started = time.monotonic()
	request_id = None
	vc_uuid = None

	try:
		data = _parse_payload(payload, kwargs)
		request_id = _peek(data, "request_id")
		vc_uuid = _peek(data, "vc_uuid")

		settings = _authenticate()  # 1
		_check_enabled(settings)  # 2
		_check_business(settings, _peek(data, "business_id"))  # 3
		_check_protocol(_peek(data, "protocol"))  # 4
		graph = _validate_payload(data)  # 5
		graph = _normalise_graph(graph)  # 6

		_check_permissions()

		response = _upsert(graph)
	except ConnectorError as exc:
		return _fail(exc.code, exc.message, exc.field, request_id, vc_uuid, started)
	except Exception as exc:
		code, message, field = _classify(exc)
		if code == "INTERNAL":
			frappe.log_error(
				title="ai_snaplead_connector.upsert_lead_graph",
				message=f"request_id={request_id} vc_uuid={vc_uuid}\n\n{frappe.get_traceback()}",
			)
		return _fail(code, message, field, request_id, vc_uuid, started)

	_touch_last_request()
	_log(
		outcome="Skipped" if (response.get("replayed") or response.get("unchanged")) else "Success",
		request_id=request_id,
		vc_uuid=vc_uuid,
		started=started,
		message=response.get("outcome"),
	)
	return response


@frappe.whitelist(methods=["GET", "POST"])
def pull_changes(
	since: str | None = None,
	cursor: str | None = None,
	limit: int | str | None = None,
	business_id: str | None = None,
):
	"""Leads this connector owns that changed since ``since`` / after ``cursor``. Read only.

	The cursor is (Lead.modified, Lead.name), not an offset: rows keep changing while a pull
	is paging through them, and an OFFSET page silently skips a record every time an earlier
	one is re-saved. It is opaque to the caller by design.
	"""
	started = time.monotonic()

	try:
		settings = _authenticate()
		_check_enabled(settings)
		if business_id:
			_check_business(settings, business_id)

		page = _pull_page(settings, since=since, cursor=cursor, limit=limit)
	except ConnectorError as exc:
		return _fail(exc.code, exc.message, exc.field, None, None, started)
	except Exception as exc:
		code, message, field = _classify(exc)
		if code == "INTERNAL":
			frappe.log_error(title="ai_snaplead_connector.pull_changes", message=frappe.get_traceback())
		return _fail(code, message, field, None, None, started)

	return page


# ── Step 1-4: the door ───────────────────────────────────────────────────────────────────


def _settings_or_none():
	try:
		return frappe.get_cached_doc(SETTINGS)
	except Exception:
		return None


def _authenticate():
	"""Two factors, and they prove different things.

	The ERPNext API key identifies a USER on this site. It is a generic credential: it is
	handed out for integrations of every kind, it is frequently reused, and holding one says
	nothing about being the central VC site. The connector token is a secret shared with one
	central instance at onboarding, so presenting both says "a user of this site, acting as
	our central instance". Either one alone is not enough to write a whole lead graph with
	permissions bypassed.

	Only the SHA-256 of the token is stored, and the comparison is constant time: a plain
	``==`` on a secret leaks its prefix through timing to anyone allowed to call this at all.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		raise ConnectorError(
			"UNAUTHENTICATED",
			_(
				"This endpoint requires an authenticated ERPNext user. Send an API key/secret for the integration user."
			),
		)

	settings = _settings_or_none()
	if not settings:
		# The settings could not be LOADED -- typically mid-upgrade, between the code update and
		# `bench migrate` (the DocType's module is renamed by the migrate). That is not a
		# decision that this site is switched off, so it must not be the non-retryable
		# CONNECTOR_DISABLED: central would mark the lead permanently failed. A site whose
		# settings load and say enabled=0 still answers CONNECTOR_DISABLED (_check_enabled).
		raise ConnectorError(
			"SETUP_REQUIRED",
			_(
				"Connector Settings could not be loaded on this site. Run `bench --site <site> migrate` and configure it."
			),
		)

	stored = (settings.get("token_hash") or "").strip().lower()
	if not stored:
		raise ConnectorError(
			"TOKEN_INVALID",
			_(
				"This site has no connector token configured. Set the token hash in Connector Settings before the central site pushes."
			),
			field="token_hash",
		)

	presented = _connector_token_header()
	if not presented:
		raise ConnectorError(
			"TOKEN_INVALID",
			_("Missing {0} header.").format(CONNECTOR_TOKEN_HEADER),
			field=CONNECTOR_TOKEN_HEADER,
		)

	digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
	if not hmac.compare_digest(digest, stored):
		raise ConnectorError(
			"TOKEN_INVALID",
			_("The {0} header does not match this site's connector token.").format(CONNECTOR_TOKEN_HEADER),
			field=CONNECTOR_TOKEN_HEADER,
		)

	return settings


def _connector_token_header():
	try:
		value = frappe.get_request_header(CONNECTOR_TOKEN_HEADER)
	except Exception:
		# No HTTP request in scope (a background job, `bench execute`, a test).
		value = None

	return (value or "").strip()


def _check_enabled(settings):
	if cint(settings.get("enabled")):
		return

	raise ConnectorError(
		"CONNECTOR_DISABLED",
		_("The connector is switched off on this site. Enable it in Connector Settings."),
		field="enabled",
	)


def _check_business(settings, claimed):
	"""The tenancy check: once, at the door.

	Before this it was a per-query filter on ``Lead.custom_business_id`` -- a rule that every
	future query had to remember, which is not a rule. One site speaks for one business, that
	business is in Settings, and a payload naming another one never reaches a write.
	"""
	site_business = (settings.get("business_id") or "").strip()
	claimed = (claimed or "").strip()

	if not claimed:
		raise ConnectorError("VALIDATION_FAILED", _("business_id is required."), field="business_id")

	if not site_business:
		raise ConnectorError(
			"CONNECTOR_DISABLED",
			_("This site has no business configured. Set Business ID in Connector Settings first."),
			field="business_id",
		)

	if claimed != site_business:
		raise ConnectorError(
			"BUSINESS_MISMATCH",
			_("This payload is for business {0}, but this site belongs to business {1}.").format(
				claimed, site_business
			),
			field="business_id",
		)


def _check_protocol(claimed):
	if (claimed or "").strip() in SUPPORTED_PROTOCOLS:
		return

	raise ConnectorError(
		"PROTOCOL_UNSUPPORTED",
		_("Protocol {0} is not supported by this client. Supported: {1}.").format(
			claimed or "(none)", ", ".join(SUPPORTED_PROTOCOLS)
		),
		field="protocol",
	)


def _check_permissions():
	user = frappe.session.user

	for doctype, ptype in REQUIRED_PERMISSIONS:
		if frappe.has_permission(doctype, ptype, user=user):
			continue

		raise ConnectorError(
			"REMOTE_PERMISSION",
			_(
				"The integration user {0} does not have {1} permission on {2}. Give that user"
				" the {3} role this app installs -- alongside whatever sales role already"
				" grants create and write on Lead, Contact and Address, such as Sales User --"
				" or System Manager. The connector will not fall back to writing as"
				" Administrator."
			).format(user, ptype, doctype, INTEGRATION_ROLE),
			field=doctype,
		)


# ── Step 5: schema ───────────────────────────────────────────────────────────────────────


def _parse_payload(payload, kwargs):
	"""Accept {"payload": {...}} as JSON or as a form field, and a bare top-level body."""
	if payload is None:
		payload = kwargs.get("payload")

	if payload is None:
		# A caller that posted the graph as the request body itself.
		payload = {key: value for key, value in (kwargs or {}).items() if not key.startswith("_")}

	if isinstance(payload, bytes):
		payload = payload.decode("utf-8", "replace")

	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except ValueError:
			raise ConnectorError("VALIDATION_FAILED", _("payload is not valid JSON."), field="payload")

	if not isinstance(payload, dict) or not payload:
		raise ConnectorError("VALIDATION_FAILED", _("payload must be a JSON object."), field="payload")

	return payload


def _peek(data, key):
	"""Read one key before the schema has been validated, without trusting its type."""
	value = data.get(key) if isinstance(data, dict) else None
	return value.strip() if isinstance(value, str) else None


def _type_name(expected):
	names = {str: "a string", dict: "an object", list: "an array", int: "a number", bool: "a boolean"}
	if isinstance(expected, tuple):
		return " or ".join(names.get(item, str(item)) for item in expected)
	return names.get(expected, str(expected))


def _validate_object(data, schema, path):
	if not isinstance(data, dict):
		raise ConnectorError(
			"VALIDATION_FAILED",
			_("{0} must be an object.").format(path or "payload"),
			field=path or "payload",
		)

	clean = {}

	for key, value in data.items():
		field = f"{path}.{key}" if path else key

		if key not in schema:
			raise ConnectorError(
				"VALIDATION_FAILED",
				_(
					"Unknown field {0}. This client rejects unknown fields instead of dropping"
					" them silently; the fields accepted here are: {1}."
				).format(field, ", ".join(sorted(schema))),
				field=field,
			)

		if value is None:
			continue

		expected = schema[key]
		types = expected if isinstance(expected, tuple) else (expected,)

		# A pincode or a phone sent as a JSON number is a spelling of a string, not a type error.
		if str in types and isinstance(value, (int, float)) and not isinstance(value, bool):
			value = str(value)

		if not isinstance(value, types):
			raise ConnectorError(
				"VALIDATION_FAILED",
				_("{0} must be {1}.").format(field, _type_name(expected)),
				field=field,
			)

		clean[key] = value

	return clean


def _validate_contact_values(values, path):
	out = []

	for index, item in enumerate(values or []):
		if isinstance(item, dict):
			# Accept the child-row shape the old REST push used.
			item = item.get("email_id") or item.get("mobile_number") or item.get("phone") or item.get("value")
		if isinstance(item, (int, float)) and not isinstance(item, bool):
			item = str(item)
		if item is None or item == "":
			continue
		if not isinstance(item, str):
			raise ConnectorError(
				"VALIDATION_FAILED",
				_("{0}[{1}] must be a string.").format(path, index),
				field=f"{path}[{index}]",
			)
		out.append(item)

	return out


def _validate_payload(data):
	top = _validate_object(data, TOP_LEVEL_SCHEMA, "")

	for key in REQUIRED_TOP_LEVEL:
		if not top.get(key):
			raise ConnectorError("VALIDATION_FAILED", _("{0} is required.").format(key), field=key)

	lead = _validate_object(top.get("lead") or {}, LEAD_SCHEMA, "lead")
	contact = _validate_object(top.get("contact") or {}, CONTACT_SCHEMA, "contact")
	address = _validate_object(top.get("address") or {}, ADDRESS_SCHEMA, "address")

	attachments = []
	for index, item in enumerate(top.get("attachments") or []):
		attachment = _validate_object(item, ATTACHMENT_SCHEMA, f"attachments[{index}]")
		kind = (attachment.get("kind") or "").strip()
		if kind and kind not in ATTACHMENT_KINDS:
			raise ConnectorError(
				"VALIDATION_FAILED",
				_("attachments[{0}].kind must be one of: {1}.").format(
					index, ", ".join(sorted(ATTACHMENT_KINDS))
				),
				field=f"attachments[{index}].kind",
			)
		if not attachment.get("content_base64"):
			raise ConnectorError(
				"MANDATORY_MISSING",
				_("attachments[{0}].content_base64 is required.").format(index),
				field=f"attachments[{index}].content_base64",
			)
		attachments.append(attachment)

	return {
		"protocol": top["protocol"].strip(),
		"request_id": top["request_id"].strip(),
		"business_id": top["business_id"].strip(),
		"vc_uuid": top["vc_uuid"].strip(),
		"legacy_remote_name": (top.get("legacy_remote_name") or "").strip() or None,
		"lead": lead,
		"contact": contact,
		"address": address,
		"attachments": attachments,
		"emails_sent": "emails" in lead,
		"phones_sent": "phones" in lead,
	}


# ── Step 6: normalisation ────────────────────────────────────────────────────────────────


def _normalise_graph(graph):
	lead_in = dict(graph["lead"])

	emails = dedupe_emails(_validate_contact_values(lead_in.pop("emails", None), "lead.emails"))
	phones = dedupe_phones(_validate_contact_values(lead_in.pop("phones", None), "lead.phones"))

	source = lead_in.pop("source", None)

	lead_fields = {}
	for key, value in lead_in.items():
		fieldname = LEAD_FIELD_MAP[key]
		if isinstance(value, str):
			value = value.strip()
		if key == "created_from_scan":
			value = cint(value)
		lead_fields[fieldname] = value

	if source:
		lead_fields[_lead_source_fieldname()] = source.strip()

	address_text = " ".join(
		part
		for part in (
			(graph["address"].get("address_line1") or ""),
			(graph["address"].get("address_line2") or ""),
			(lead_fields.get("custom_address") or ""),
		)
		if part
	).strip()

	# The pincode is canonicalised the same way on both sides of the graph: a six-digit PIN
	# taken from the field when it is one, and otherwise read out of the address text.
	raw_pin = graph["address"].get("pincode") or lead_fields.get("custom_pincode") or ""
	pincode = normalize_pincode(raw_pin, address_text) or (str(raw_pin).strip() or None)
	if pincode:
		lead_fields.setdefault("custom_pincode", pincode)

	address = dict(graph["address"])
	if address:
		address["pincode"] = pincode

	return {
		**graph,
		"lead": lead_fields,
		"emails": emails if graph["emails_sent"] else None,
		"phones": phones if graph["phones_sent"] else None,
		"address": address,
		"address_text": address_text,
	}


def _payload_hash(graph):
	"""Identity of the CONTENT, so a re-push of the same graph is recognisably a no-op.

	`request_id` is excluded on purpose: a central site that retries after a timeout mints a
	new request id for the same content, and that must still be answered "unchanged" rather
	than re-writing every document and bumping every `modified` on the client.
	"""
	material = {key: value for key, value in graph.items() if key != "request_id"}
	return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ── The write ────────────────────────────────────────────────────────────────────────────


def _upsert(graph):
	payload_hash = _payload_hash(graph)

	replay = _find_replay(graph)
	if replay:
		return replay

	link_row = frappe.db.get_value(
		"Connector Lead Link",
		{"vc_uuid": graph["vc_uuid"]},
		[
			"name",
			"vc_uuid",
			"lead",
			"payload_hash",
			"last_request_id",
			"last_response",
			"client_held_contacts",
			"seen_request_ids",
		],
		as_dict=True,
	)

	# "Nothing changed" is only honest while the graph the stored response describes is still
	# THERE. This used to check the Lead alone, which is how a Lead whose Contact and Address
	# had since been deleted became permanently unrepairable: the hash never changes for
	# unchanged content, so every later push short-circuited here and answered with the names
	# of documents that no longer existed. A missing member of the graph falls through to the
	# write, which recreates it -- the promise at the top of this module is the WHOLE graph.
	if link_row and link_row.payload_hash == payload_hash and _graph_intact(link_row):
		return _unchanged_response(graph, link_row, payload_hash)

	created_files = []
	touched = []

	frappe.db.savepoint(SAVEPOINT)
	try:
		response = _write_graph(graph, link_row, payload_hash, created_files, touched)
	except Exception:
		# ONE rollback for the whole graph. Nothing survives a failure at any point: no Lead
		# without its Address, no Contact without its Lead, no link row pointing at a Lead
		# that was rolled out from under it.
		frappe.db.rollback(save_point=SAVEPOINT)
		_discard_files(created_files)
		_forget(touched)
		raise

	frappe.db.release_savepoint(SAVEPOINT)
	return response


def _write_graph(graph, link_row, payload_hash, created_files, touched):
	defaults = {}
	warnings = []

	_warn_if_history_incomplete(graph, link_row, warnings)

	deferred = _route_dropped_lead_fields(graph)
	lead, outcome = _upsert_lead(graph, link_row, defaults, warnings, touched)
	contact = _upsert_contact(lead, graph, touched)
	address = _upsert_address(lead, graph, defaults, warnings, touched)
	address = _settle_deferred_fields(lead, deferred, address, warnings) or address
	attachments = _attach_files(lead, graph, created_files, touched)

	response = {
		"ok": True,
		"protocol": PROTOCOL,
		"request_id": graph["request_id"],
		"vc_uuid": graph["vc_uuid"],
		"outcome": outcome,
		"lead": lead.name,
		"contact": contact,
		"address": address,
		"attachments": attachments,
		"defaults_applied": defaults,
		"warnings": warnings,
		"payload_hash": payload_hash,
		"replayed": False,
		"unchanged": False,
		"synced_on": str(now_datetime()),
	}

	response["link"] = _upsert_link(graph, lead, link_row, payload_hash, response, touched)
	return response


# Legacy Lead Custom Fields that the client may already have dropped, and where each value
# goes instead. The protocol name never changes -- a central site in the field must not have
# to know which half of the migration its client is on -- so the routing happens here.
DROPPABLE_LEAD_FIELDS = ("custom_pincode", "custom_scanned_text", "custom_created_from_scan")


def _route_dropped_lead_fields(graph):
	"""Take out of ``graph["lead"]`` any value whose Custom Field this client no longer has.

	``Document.set`` on a fieldname the meta does not know does not raise: it sets an
	attribute that ``get_valid_dict`` then discards. So without this, a client who has
	finished the migration would go on accepting ``pincode``, ``scanned_text`` and
	``created_from_scan`` with a 200 and store none of them -- silent data loss on every
	push, which is the single worst outcome this protocol is designed to prevent.
	"""
	meta = frappe.get_meta("Lead")
	deferred = {}

	for fieldname in DROPPABLE_LEAD_FIELDS:
		if fieldname in graph["lead"] and not meta.has_field(fieldname):
			deferred[fieldname] = graph["lead"].pop(fieldname)

	pincode = (deferred.get("custom_pincode") or "").strip()
	block = graph.get("address") or {}
	if pincode and block and any((value or "") for value in block.values()):
		# An Address is being written anyway; the pincode belongs on it, not on a second one.
		if not (block.get("pincode") or "").strip():
			block["pincode"] = pincode
			graph["address"] = block
			deferred.pop("custom_pincode")

	return deferred


def _settle_deferred_fields(lead, deferred, address, warnings):
	"""Write the deferred values to their standard homes, by the migration's own rules.

	Identical helpers, deliberately: a pincode that arrives today must land exactly where the
	migration put the ones that arrived last year, including refusing to overwrite a pincode
	somebody typed by hand.
	"""
	if not deferred:
		return None

	from ai_snaplead_connector.legacy.migrate import (
		ensure_pincode_on_address,
		set_scan_source_if_empty,
		write_scanned_note,
	)

	written_address = None

	pincode = (deferred.get("custom_pincode") or "").strip()
	if pincode:
		result = ensure_pincode_on_address(lead.name, pincode)
		written_address = result.get("address")
		if result.get("outcome") == "conflict":
			warnings.append(
				"pincode {0} was kept on a second Address ({1}): this Lead already had an"
				" address with a different pincode, which was not overwritten.".format(
					pincode, written_address
				)
			)

	text = (deferred.get("custom_scanned_text") or "").strip()
	if text:
		# update_existing=True, and the result is READ. Both matter. On a post-Patch-B client
		# -- which is every client, since a fresh install starts at legacy_fields_dropped=1 --
		# this is the only home scanned text has, so a second push carrying corrected text
		# used to find the tagged note already there, return "already" and write nothing,
		# and the discarded return value meant the caller was answered ok:true with no
		# warning at all. That is the silent data loss this whole routing layer exists to
		# prevent, arriving through the routing layer itself.
		result = write_scanned_note(lead.name, text, update_existing=True)
		if result.get("outcome") == "unavailable":
			warnings.append(
				"scanned_text could not be stored: this ERPNext has no Lead.notes table, and"
				" the custom_scanned_text field this client used to carry has been dropped."
			)

	if cint(deferred.get("custom_created_from_scan")):
		result = set_scan_source_if_empty(lead.name)
		if result.get("outcome") == "kept":
			warnings.append(
				"created_from_scan was not applied: this Lead is already attributed to {0}.".format(
					result.get("existing")
				)
			)

	return written_address


def _warn_if_history_incomplete(graph, link_row, warnings):
	"""Say so when this write cannot be proven not to be a re-delivery.

	The endpoint is about to apply a payload to a record whose request-id window has already
	overflowed. If this request_id is one of the ids the row has forgotten, the payload is
	older than what is stored and this write is a revert -- and there is no longer any way
	to tell. Writing is still the right answer (refusing would refuse every new push on a
	record with more than REQUEST_HISTORY_LIMIT pushes behind it), but reporting it as an
	ordinary clean update is not.
	"""
	if not link_row:
		return

	forgotten = _forgotten_request_count(link_row.get("seen_request_ids"))
	if not forgotten:
		return

	if graph["request_id"] in _seen_request_ids(link_row.get("seen_request_ids")):
		# Handled by the replay path long before this; here only for completeness.
		return

	warnings.append(
		"request_id {0} could not be checked against this record's whole history: the {1}"
		" oldest request ids it has seen are no longer remembered (the window is {2}). If"
		" this is a re-delivery of one of them, this write has just applied a payload older"
		" than the stored one.".format(graph["request_id"], forgotten, REQUEST_HISTORY_LIMIT)
	)


def _upsert_lead(graph, link_row, defaults, warnings, touched):
	"""Resolve the Lead by vc_uuid, then by the name it had before this protocol, then create.

	The middle step is what makes this app installable on a client that a central site has
	ALREADY been pushing to over the generic REST API. Those Leads exist and are real; without
	adoption the first push under the new protocol would create a second copy of every one of
	them, and the duplicate guard would refuse most of those, so the client's first experience
	of the upgrade would be a wall of errors over a half-duplicated Lead table.
	"""
	outcome = "updated"
	lead = None

	if link_row and link_row.lead and frappe.db.exists("Lead", link_row.lead):
		lead = frappe.get_doc("Lead", link_row.lead)
	elif graph["legacy_remote_name"] and frappe.db.exists("Lead", graph["legacy_remote_name"]):
		lead = frappe.get_doc("Lead", graph["legacy_remote_name"])
		outcome = "adopted"
		_assert_adoptable(lead, graph)
	else:
		lead = frappe.new_doc("Lead")
		outcome = "created"

	source_fieldname = _lead_source_fieldname()
	if graph["lead"].get(source_fieldname):
		# Before the Lead is written, not after: the link is validated on save.
		ensure_lead_source(graph["lead"][source_fieldname])

	for fieldname, value in graph["lead"].items():
		lead.set(fieldname, value)

	if frappe.get_meta("Lead").has_field("custom_business_id"):
		# After the migration the stamp lives on Connector Lead Link, which _upsert_link writes.
		# Setting a fieldname the meta no longer knows does not raise -- it is simply dropped
		# by get_valid_dict -- so the only harm in leaving this unguarded is that it reads as
		# if the stamp were still being written. It is not.
		lead.custom_business_id = graph["business_id"]

	# Adoption is the one case where the client's row PREDATES this protocol, so the client
	# can hold contact details the central record has never seen. Replacing the tables would
	# delete them with no undo. The payload's order is kept -- the central record's first email
	# is still the primary one -- and whatever the client already had is appended.
	#
	# The merge is REMEMBERED, and that is the whole of this fix. Doing it only on the adopting
	# push preserved those values for exactly one call: the link row stores the hash of the
	# payload as SENT, so the next push that differed in any field at all ran as an ordinary
	# "updated" and _upsert_contact replaced email_ids/phone_nos wholesale with the payload's
	# own lists. Everything the merge had just saved was deleted, with no warning, no log row
	# and -- after Patch B -- no copy outside the archive JSON.
	preserved = _preserved_contacts(lead, graph, link_row, outcome)
	graph["client_held"] = preserved
	graph["emails"] = _merge_values(graph["emails"], preserved["emails"], dedupe_emails)
	graph["phones"] = _merge_values(graph["phones"], preserved["phones"], dedupe_phones)

	if lead_has_contact_tables():
		if graph["emails"] is not None:
			_replace_rows(lead, "custom_email_ids", "email_id", graph["emails"])
		if graph["phones"] is not None:
			_replace_rows(lead, "custom_mobile_numbers", "mobile_number", graph["phones"])
	else:
		# The tables are gone. Contact.email_ids / phone_nos hold the full list (written by
		# _upsert_contact moments from now); these two stock fields are the single-value
		# projection every ERPNext list view, report and email workflow reads, and writing
		# them here is what stops that projection going blank on a migrated client.
		#
		# The test is `is not None`, not truthiness, and that is the fix. `lead.emails: []`
		# is a payload that CLEARS the list: _upsert_contact empties every Contact Email row
		# for it a few lines below (it tests `is not None` too). Guarding this half on
		# truthiness left Lead.email_id holding an address the central record had just
		# deleted, permanently and with no warning -- and the Lead is what every stock list
		# view, report and email workflow reads, and what the duplicate guard keeps
		# reserving that address for. The two halves of one record disagreed for ever.
		if graph["emails"] is not None:
			_project_cleared_contact(lead, "email_id", graph["emails"], warnings)
		if graph["phones"] is not None:
			_project_cleared_contact(lead, "mobile_no", graph["phones"], warnings)

		# The Lead can hold one of each; the rest go on the Contact a few lines below, which
		# does not exist yet when Lead.validate runs its duplicate guard. Declaring the whole
		# set here is what stops the guard comparing primary against primary and letting the
		# same card through a second time under a new vc_uuid with its emails reordered.
		lead.flags.vc_contact_values = {
			"emails": list(graph["emails"] or []),
			"phones": list(graph["phones"] or []),
		}

	if not (lead.get("lead_name") or lead.get("first_name") or lead.get("company_name")):
		# Lead.lead_name is mandatory in effect: ERPNext throws "A Lead requires either a
		# person's name or an organization's name". A scanned card frequently has neither.
		# Filling it from a declared default is the difference between a lead that lands and
		# a lead that is lost -- but the caller is TOLD, in defaults_applied.
		lead.lead_name = _default_lead_name(graph)
		defaults["lead.lead_name"] = lead.lead_name

	# _insert_doc / _save_doc carry ignore_permissions AND ignore_mandatory; see _insert_doc
	# for why a client-side mandatory field must not be allowed to lose a lead the app has
	# already reported as sent.
	is_new = lead.is_new()
	if is_new:
		_apply_lead_routing(lead, graph, defaults)
		_insert_doc(lead)
	else:
		_save_doc(lead)

	if is_new:
		_assign_new_lead(lead, warnings)

	touched.append(("Lead", lead.name))
	return lead, outcome


def _lead_routing_settings():
	"""Read the routing settings without letting a pre-migration client break the push.

	The two fields are new, so a client that has pulled the code but not yet run
	`bench migrate` has the doctype without them. db.get_value on a missing column raises,
	and losing a lead over a cosmetic setting would be absurd -- so this returns the
	do-nothing answer instead.
	"""
	try:
		meta = frappe.get_meta("Connector Settings")
		if not meta.has_field("default_lead_owner"):
			return None, False
		settings = frappe.get_cached_doc("Connector Settings")
	except Exception:
		return None, False

	owner = cstr(settings.get("default_lead_owner")).strip()
	if not owner or not frappe.db.exists("User", owner):
		return None, False
	return owner, bool(settings.get("assign_new_leads"))


def _apply_lead_routing(lead, graph, defaults):
	"""Point a newly created Lead at a real person.

	Without this the Lead is owned by the integration user and assigned to nobody, so it is
	readable but effectively invisible: it is in no salesperson's queue and matches none of
	the "my leads" filters the client's team actually works from. The central push reports
	success and the client swears the lead never arrived. Both are true.

	Only on create, and only when the payload did not name an owner itself -- a central site
	that starts sending lead_owner must win over a site-wide default.
	"""
	owner, _assign = _lead_routing_settings()
	if not owner:
		return
	# The test is against the PAYLOAD, not against lead.lead_owner. ERPNext declares
	# `"default": "__user"` on that field, so a brand new Lead already carries the integration
	# user before anything is set -- reading the doc here would make this branch bail every
	# single time and the setting would appear to do nothing at all.
	if cstr(graph["lead"].get("lead_owner")).strip():
		return
	lead.lead_owner = owner
	defaults["lead.lead_owner"] = owner


def _assign_new_lead(lead, warnings):
	"""Create the ToDo, after the insert, because an assignment needs a docname.

	Never fatal: the Lead is already committed to this transaction and an assignment that
	could not be created is worth a warning, not a rollback of real data.
	"""
	owner, assign = _lead_routing_settings()
	if not (owner and assign):
		return
	try:
		from frappe.desk.form.assign_to import add as assign_to

		assign_to(
			{
				"assign_to": [owner],
				"doctype": "Lead",
				"name": lead.name,
				"description": _("New lead synced from AI SnapLead."),
			}
		)
	except Exception as exc:
		warnings.append(
			"Lead {0} was created but could not be assigned to {1}: {2}".format(lead.name, owner, exc)
		)


def _project_cleared_contact(lead, fieldname, values, warnings):
	"""Keep Lead.email_id / Lead.mobile_no in step with the list the payload sent.

	An empty list is an instruction, so the projection is cleared with it; the caller is
	told, because clearing a Lead's email address changes what every stock ERPNext view of
	that Lead shows and is not something to do silently.
	"""
	if values:
		lead.set(fieldname, values[0])
		return

	held = cstr(lead.get(fieldname)).strip()
	lead.set(fieldname, None)
	if held:
		warnings.append(
			"Lead.{0} was cleared: this payload sent an empty list, so {1} no longer appears"
			" on the Lead or on its Contact.".format(fieldname, held)
		)


def _assert_adoptable(lead, graph):
	"""Prove the named Lead really is this caller's record before overwriting it.

	``legacy_remote_name`` is a primary key the CALLER supplies, and ERPNext Lead names are
	sequential and guessable (CRM-LEAD-2026-00001, -00002, ...). This used to check only that
	the named Lead was not already linked to a different vc_uuid, which left every unlinked
	Lead on the site adoptable by anyone holding a connector token -- including Leads stamped
	for a DIFFERENT business, which were then re-stamped for this one and overwritten. Lead's
	own ``reject_business_mismatch`` guard cannot catch that, because _upsert_lead sets
	``custom_business_id`` to this connector's business before validate runs, so the check has
	to happen here, against the stamp the Lead still carries.

	Two things are required, and both are checked before a single field is set:

	* the Lead is untagged (a genuine pre-protocol row) or already stamped for the business
	  this payload declares -- never another tenant's;
	* the payload corroborates the claim with a contact detail the Lead already holds. A
	  guessed name is thereby not enough: the caller must also know who the record is.
	"""
	stamped = _lead_business_stamp(lead)
	if stamped and stamped != graph["business_id"]:
		raise ConnectorError(
			"BUSINESS_MISMATCH",
			_(
				"Lead {0} is stamped for business {1}, not {2}, so it cannot be adopted. Nothing was written."
			).format(lead.name, stamped, graph["business_id"]),
			field="legacy_remote_name",
		)

	existing = frappe.db.get_value("Connector Lead Link", {"lead": lead.name}, ["vc_uuid"], as_dict=True)
	# A row with NO vc_uuid is a placeholder the legacy field migration wrote so that
	# custom_business_id would survive that field being dropped. It names no central record,
	# so it cannot conflict with one -- and reading it as a conflict is exactly what would
	# make every migrated Lead on the client permanently un-adoptable, which is the opposite
	# of what writing it was for.
	if existing and (existing.vc_uuid or "").strip() and existing.vc_uuid != graph["vc_uuid"]:
		raise ConnectorError(
			"CONFLICT",
			_("Lead {0} is already linked to central record {1}, so it cannot be adopted by {2}.").format(
				lead.name, existing.vc_uuid, graph["vc_uuid"]
			),
			field="legacy_remote_name",
		)

	_assert_adoption_is_corroborated(lead, graph)


def _assert_adoption_is_corroborated(lead, graph):
	"""Refuse to adopt a Lead the payload cannot identify by anything but its name.

	A Lead that holds no contact detail at all has nothing to corroborate against and is
	adopted on the name alone -- there is no better evidence available for it -- but a Lead
	with an email address or a phone number must share at least one of them with the payload.
	Phones are compared on their match key, so a re-spelling ("+91 98765 43210" against
	"09876543210") still corroborates.
	"""
	stored_emails, stored_phones = _held_lead_contacts(lead)
	held_emails = {
		normalize_email(value) for value in [*stored_emails, lead.get("email_id")] if normalize_email(value)
	}
	held_phones = {
		normalize_mobile_number(value)
		for value in [*stored_phones, lead.get("mobile_no"), lead.get("whatsapp_no")]
		if normalize_mobile_number(value)
	}

	if not held_emails and not held_phones:
		return

	sent_emails = {normalize_email(value) for value in (graph.get("emails") or [])}
	sent_phones = {normalize_mobile_number(value) for value in (graph.get("phones") or [])}

	if (held_emails & sent_emails) or (held_phones & sent_phones):
		return

	raise ConnectorError(
		"CONFLICT",
		_(
			"Lead {0} holds none of the email addresses or phone numbers in this payload, so it"
			" cannot be confirmed to be central record {1}. Send a payload carrying one of that"
			" Lead's existing contact details, or omit legacy_remote_name to create a new Lead."
			" Nothing was written."
		).format(lead.name, graph["vc_uuid"]),
		field="legacy_remote_name",
	)


def _row_values(doc, fieldname, row_fieldname):
	return [(row.get(row_fieldname) or "").strip() for row in (doc.get(fieldname) or [])]


def _held_lead_contacts(lead):
	"""Every email and phone this Lead already holds, in whichever shape this site is in.

	Before the legacy field migration that is Lead's own child tables. After it they are gone
	and the Contact is the record -- and the child TABLES still physically hold the old rows,
	unreachable from any form, so reading them there would answer with data that is both
	stale and invisible. Three callers depend on this answer and all three get it wrong in an
	expensive way: adoption would silently delete the contact details the payload did not
	mention, the corroboration check would refuse a legitimate adoption, and ``pull_changes``
	would report a contact list the client cannot see.
	"""
	if lead_has_contact_tables():
		return (
			_row_values(lead, "custom_email_ids", "email_id"),
			_row_values(lead, "custom_mobile_numbers", "mobile_number"),
		)

	held = _stored_lead_contacts(lead.get("name"))
	if held != ([], []):
		return held

	return (
		[value for value in [(lead.get("email_id") or "").strip()] if value],
		[value for value in [(lead.get("mobile_no") or "").strip()] if value],
	)


def _stored_lead_contacts(lead_name):
	"""The same answer, read entirely from the database, for a caller holding only a name.

	``pull_changes`` walks rows from a JOIN, not loaded documents, so it cannot use the
	in-memory child tables the adoption path relies on.
	"""
	if not lead_name:
		return ([], [])

	if lead_has_contact_tables():
		emails = frappe.get_all(
			"ERPNext Lead Email",
			filters={"parent": lead_name, "parenttype": "Lead"},
			pluck="email_id",
			order_by="idx asc",
		)
		phones = frappe.get_all(
			"ERPNext Lead Mobile Number",
			filters={"parent": lead_name, "parenttype": "Lead"},
			pluck="mobile_number",
			order_by="idx asc",
		)
	else:
		contact = _linked_document("Contact", lead_name)
		if not contact:
			return ([], [])
		emails = frappe.get_all(
			"Contact Email",
			filters={"parent": contact, "parenttype": "Contact"},
			pluck="email_id",
			order_by="idx asc",
		)
		phones = frappe.get_all(
			"Contact Phone",
			filters={"parent": contact, "parenttype": "Contact"},
			pluck="phone",
			order_by="idx asc",
		)

	return (
		[(value or "").strip() for value in emails if (value or "").strip()],
		[(value or "").strip() for value in phones if (value or "").strip()],
	)


def _lead_business_stamp(lead):
	"""Which business this Lead is already claimed by.

	``Lead.custom_business_id`` while that Custom Field exists; afterwards the Connector Lead Link
	row, which is where the migration moved the stamp to. Reading only the field would leave
	``_assert_adoptable``'s cross-tenant check reading an empty string on every migrated
	client -- i.e. switched off, on exactly the sites this app has been running longest on.
	"""
	if frappe.get_meta("Lead").has_field("custom_business_id"):
		return (lead.get("custom_business_id") or "").strip()

	if not lead.get("name"):
		return ""

	return (frappe.db.get_value("Connector Lead Link", {"lead": lead.name}, "business_id") or "").strip()


def _merge_values(sent, held, dedupe):
	"""The payload's values first, then anything the client already had that they lack."""
	if sent is None:
		return None
	return dedupe(list(sent) + [value for value in held if value])


def _client_only(sent, held, keyer):
	"""The held values this payload does not mention, in the order the client holds them.

	Keyed, not compared as strings: "+91 98765 43210" and "09876543210" are the same number,
	and recording the second as client-only would re-add it beside the first for ever.
	"""
	sent_keys = {keyer(value) or value for value in (sent or []) if value}

	kept = []
	seen = set()
	for value in held or []:
		if not value:
			continue
		key = keyer(value) or value
		if key in sent_keys or key in seen:
			continue
		seen.add(key)
		kept.append(value)

	return kept


def _preserved_contacts(lead, graph, link_row, outcome):
	"""Contact details that belong to the CLIENT, not to the central record.

	A value gets in here exactly once -- on the push that adopted a pre-protocol Lead, where it
	was already on the client's row and the payload did not mention it -- and it then survives
	every later push, because the merge is recomputed from this record rather than from the
	adoption that is now in the past.

	Two ways out, and both matter:

	* the client deletes the value by hand. It is no longer held, so it drops out and is not
	  resurrected on the next push. A merge that re-added it would make the value impossible to
	  remove;
	* the central record starts sending it. It is now the central record's value, so it leaves
	  this set and an ordinary update may later remove it -- which is the protocol's rule for
	  everything the central record owns.
	"""
	if outcome == "adopted":
		held_emails, held_phones = _held_lead_contacts(lead)
	else:
		carried = _carried_contacts(link_row)
		if not (carried["emails"] or carried["phones"]):
			return {"emails": [], "phones": []}

		# Only what the client STILL holds; a value removed on the client stays removed.
		lead_emails, lead_phones = _held_lead_contacts(lead)
		held_email_keys = {normalize_email(value) for value in lead_emails if value}
		held_phone_keys = {phone_match_key(value) or value for value in lead_phones if value}
		held_emails = [value for value in carried["emails"] if normalize_email(value) in held_email_keys]
		held_phones = [
			value for value in carried["phones"] if (phone_match_key(value) or value) in held_phone_keys
		]

	return {
		"emails": _client_only(graph["emails"], held_emails, normalize_email),
		"phones": _client_only(graph["phones"], held_phones, phone_match_key),
	}


def _carried_contacts(link_row):
	"""The preserved set this Lead's link row is carrying, or empty lists."""
	empty = {"emails": [], "phones": []}
	if not link_row:
		return empty

	raw = (link_row.get("client_held_contacts") or "").strip()
	if not raw:
		return empty

	try:
		stored = json.loads(raw)
	except ValueError:
		return empty

	if not isinstance(stored, dict):
		return empty

	return {
		key: [value for value in (stored.get(key) or []) if isinstance(value, str) and value]
		for key in ("emails", "phones")
	}


def _default_lead_name(graph):
	for email in graph.get("emails") or []:
		local = email.split("@")[0].strip()
		if local:
			return local

	for phone in graph.get("phones") or []:
		if phone:
			return phone

	return f"{DEFAULT_LEAD_NAME_PREFIX} {graph['vc_uuid'][:8]}"


def _replace_rows(doc, fieldname, row_fieldname, values):
	doc.set(fieldname, [])
	for value in values:
		doc.append(fieldname, {row_fieldname: value})


def _upsert_contact(lead, graph, touched):
	"""Enrich the Contact instead of creating a second one.

	ERPNext creates a Contact itself, inside Lead.before_insert, from the single email_id and
	the single mobile_no. Creating our own next to it is how a client ends up with two
	Contacts per lead, and it is also how the other three phone numbers on a scanned card got
	lost: whatever is not in `email_ids` / `phone_nos` here does not exist for any stock
	ERPNext contact lookup, report or email workflow.
	"""
	name = _linked_document("Contact", lead.name)
	contact = frappe.get_doc("Contact", name) if name else frappe.new_doc("Contact")

	fields = graph["contact"]
	contact.first_name = (
		fields.get("first_name")
		or contact.get("first_name")
		or lead.get("first_name")
		or lead.get("lead_name")
		or lead.name
	)

	for key, lead_field in (
		("middle_name", "middle_name"),
		("last_name", "last_name"),
		("salutation", "salutation"),
		("gender", "gender"),
		("designation", "job_title"),
		("company_name", "company_name"),
	):
		value = fields.get(key) or lead.get(lead_field)
		if value:
			contact.set(key, value)

	if graph["emails"] is not None:
		contact.set("email_ids", [])
		for index, email in enumerate(graph["emails"]):
			contact.append("email_ids", {"email_id": email, "is_primary": 1 if index == 0 else 0})

	if graph["phones"] is not None:
		contact.set("phone_nos", [])
		for index, phone in enumerate(graph["phones"]):
			contact.append(
				"phone_nos",
				{
					"phone": phone,
					"is_primary_phone": 1 if index == 0 else 0,
					"is_primary_mobile_no": 1 if index == 0 else 0,
				},
			)

	if not contact.get("links") or not any(
		row.link_doctype == "Lead" and row.link_name == lead.name for row in contact.get("links")
	):
		contact.append("links", {"link_doctype": "Lead", "link_name": lead.name})

	if contact.is_new():
		_insert_doc(contact)
	else:
		_save_doc(contact)

	touched.append(("Contact", contact.name))
	return contact.name


def _upsert_address(lead, graph, defaults, warnings, touched):
	block = graph["address"]
	if not block or not any((value or "") for value in block.values()):
		return None

	line1 = (block.get("address_line1") or "").strip()
	if not line1:
		raise ConnectorError(
			"MANDATORY_MISSING",
			_("address.address_line1 is required when an address is sent."),
			field="address.address_line1",
		)

	# The stored row is read BEFORE anything is resolved, because "the payload did not send
	# this field" and "the payload cleared this field" are different instructions and only
	# the stored row can tell them apart. That rule used to be implemented for `pincode`
	# alone: `address_line2` was written unconditionally from the payload, so an omitted
	# line2 blanked a stored one; `city` fell back to the DEFAULT "-" and overwrote a real
	# stored city, reported as a "default applied" rather than as the overwrite it was; and
	# `state` was blanked to "" -- on a non-India country without even a warning. A push
	# that narrowed a Lead's street address therefore destroyed the rest of its address, and
	# the response warned about the pincode it had KEPT while saying nothing about the three
	# fields it had just erased.
	name = _address_for_lead(lead.name)
	address = frappe.get_doc("Address", name) if name else frappe.new_doc("Address")

	held = {}
	if not address.is_new():
		held = {
			fieldname: cstr(address.get(fieldname)).strip()
			for fieldname in ("address_line2", "city", "state", "country")
		}
	kept = []

	country = (block.get("country") or "").strip()
	if not country and held.get("country"):
		country = held["country"]
		kept.append("country")
	if not country:
		country = DEFAULT_ADDRESS_COUNTRY
		defaults["address.country"] = country

	city = (block.get("city") or "").strip()
	if not city and held.get("city") and held["city"] != DEFAULT_ADDRESS_CITY:
		city = held["city"]
		kept.append("city")
	if not city:
		city = DEFAULT_ADDRESS_CITY
		defaults["address.city"] = city

	# A client may have turned Address.city into a LINK (to a City/Town master). This app has
	# always assumed free text, so the "-" placeholder -- and any real city the card happens to
	# carry that the client has no master row for -- fails link validation and takes the WHOLE
	# lead down:
	#
	#     [VALIDATION_FAILED] Could not find City/Town: -
	#
	# A city we cannot satisfy is not worth losing a lead over. Drop it and say so; the
	# address still lands with its street, pincode, state and country.
	city, dropped_city = _drop_unresolvable_link("Address", "city", city)
	if dropped_city:
		defaults.pop("address.city", None)
		warnings.append(
			_(
				"Address.city {0} was dropped: this site links city to a master record and has no row for it."
			).format(dropped_city)
		)

	line2 = (block.get("address_line2") or "").strip()
	if not line2 and held.get("address_line2"):
		line2 = held["address_line2"]
		kept.append("address_line2")

	state = (block.get("state") or "").strip()

	if country.strip().lower() == "india":
		derived, source = derive_indian_state(
			graph["address_text"],
			city=block.get("city"),
			state=state,
			pincode=block.get("pincode"),
		)
		if derived:
			if source != "given":
				defaults["address.state"] = derived
			state = derived
		elif canonical_state(state):
			state = canonical_state(state)

	if not state and held.get("state"):
		# Nothing in THIS payload names a state, and the row already holds one. A stored
		# state is a fact; a derivation is a guess, and there is no guess to make here.
		state = held["state"]
		kept.append("state")
		defaults.pop("address.state", None)

	if not state and country.strip().lower() == "india":
		if india_compliance_installed():
			# india_compliance refuses an Indian Address without a state, and a WRONG
			# state on a GST address is worse than no address at all -- so this is a
			# refusal naming the field, not a guess.
			raise ConnectorError(
				"MANDATORY_MISSING",
				_(
					"address.state could not be determined for this Indian address and"
					" india_compliance is installed on this site, which requires it. Send"
					" address.state, or a six-digit pincode this client can derive it from."
				),
				field="address.state",
			)
		warnings.append(
			"Address stored without a state: none was sent and none could be derived from the address or pincode."
		)

	if kept:
		warnings.append(
			"Address {0} kept its stored {1}: this payload sent an address block that did not"
			" carry {2}, and an omitted field is not a cleared field.".format(
				address.name, ", ".join(kept), "them" if len(kept) > 1 else "it"
			)
		)

	# Everything above is about the payload. This is about the CLIENT, and it has to be asked
	# before the first write: without an Address Template frappe refuses to save any Address
	# at all, the savepoint rolls the whole graph back -- Lead included -- and the failure
	# used to surface as a bare ERPNext ValidationError, which _classify reads as
	# VALIDATION_FAILED: a 422 the contract marks NOT retryable. A conforming central site
	# then marks every card-scan carrying an address permanently failed, for a client-side
	# setup gap a human fixes in one minute.
	_require_address_template(country)

	values = {
		"address_title": (block.get("address_title") or "").strip() or lead.get("lead_name") or lead.name,
		"address_type": (block.get("address_type") or "").strip() or DEFAULT_ADDRESS_TYPE,
		"address_line1": line1[:240],
		"address_line2": line2,
		"city": city,
		"state": state,
		"country": country,
	}

	# A pincode the payload does not carry is NOT a pincode the payload deleted. This wrote
	# `block.get("pincode") or ""` unconditionally, so any push carrying an address block and
	# no pincode blanked the one already on the row -- including one the migration had just
	# moved off `custom_pincode`, which after the drop exists nowhere but the archive JSON.
	# Nothing was warned about; the response said "adopted", warnings [].
	sent_pincode = cstr(block.get("pincode")).strip()
	held_pincode = cstr(address.get("pincode")).strip()
	if sent_pincode:
		values["pincode"] = sent_pincode
	elif address.is_new():
		values["pincode"] = ""
	elif held_pincode:
		warnings.append(
			"Address {0} kept its existing pincode {1}: this payload sent an address with no"
			" pincode, which is not a request to remove one.".format(address.name, held_pincode)
		)

	address.update(values)

	if not address.get("links") or not any(
		row.link_doctype == "Lead" and row.link_name == lead.name for row in address.get("links")
	):
		address.append("links", {"link_doctype": "Lead", "link_name": lead.name})

	if address.is_new():
		_insert_doc(address)
	else:
		_save_doc(address)

	touched.append(("Address", address.name))
	return address.name


def _require_address_template(country):
	"""Refuse early, with a code that says whose problem it is and that it is retryable.

	`frappe.contacts.doctype.address.address.get_address_templates` looks for a template for
	the address's own country and then for the default one, and throws if it finds neither.
	A site created without the setup wizard -- which is every site a developer or a Frappe
	Cloud restore makes, and some real clients -- has no Address Template at all.

	This app does not create one: a marketplace app does not invent print formats on a
	stranger's site, and `ping()` already reports `default_address_template: false` so the
	central site can see the gap before it pushes ten thousand records. What it must not do
	is let the gap look like a bad payload.
	"""
	if _address_template_exists(country):
		return

	raise ConnectorError(
		"SETUP_REQUIRED",
		_(
			"This site has no Address Template, so ERPNext refuses to save any Address and"
			" nothing in this push was written. Create one at Setup > Printing and Branding >"
			" Address Template (a default one, or one for {0}) and re-send this payload"
			" unchanged. ping() reports this as default_address_template: false."
		).format(country or DEFAULT_ADDRESS_COUNTRY),
		field="address",
	)


def _address_template_exists(country=None):
	"""The same question `get_address_templates` asks, in the same order."""
	try:
		if country and frappe.db.exists("Address Template", {"country": country}):
			return True
		return bool(frappe.db.exists("Address Template", {"is_default": 1}))
	except Exception:
		# Never let the gate itself be the reason a push fails; the save below will decide.
		return True


def _attachment_already_here(lead_name, content):
	"""The File row already holding these exact bytes on this Lead, if there is one.

	frappe identifies an uploaded file by ``content_hash`` -- that is how it avoids writing the
	same bytes to disk twice -- so this asks the identical question about the ROW, which is the
	half the endpoint was not asking.
	"""
	try:
		from frappe.core.doctype.file.utils import get_content_hash
	except ImportError:  # pragma: no cover -- frappe moved it; fall through to inserting
		return None

	content_hash = get_content_hash(content)
	if not content_hash:
		return None

	return frappe.db.get_value(
		"File",
		{
			"attached_to_doctype": "Lead",
			"attached_to_name": lead_name,
			"content_hash": content_hash,
		},
		["name", "file_url"],
		as_dict=True,
		order_by="creation asc",
	)


def _attach_files(lead, graph, created_files, touched):
	"""Card images and any other attachment, inside the same savepoint as the rest.

	A savepoint rolls back the DATABASE, not the disk. So every File written here is tracked
	and the bytes are removed by hand if the graph is rolled back -- otherwise a failed push
	would leave an orphan file on the client's disk on every retry.
	"""
	attachments = graph["attachments"]
	if not attachments:
		return []

	urls = []
	card_fields = {}

	for index, item in enumerate(attachments):
		try:
			content = base64.b64decode(item["content_base64"], validate=True)
		except (binascii.Error, ValueError):
			raise ConnectorError(
				"VALIDATION_FAILED",
				_("attachments[{0}].content_base64 is not valid base64.").format(index),
				field=f"attachments[{index}].content_base64",
			)

		# Which Attach field, if any, this card belongs in. After the migration there is no
		# custom_front_card_image to point at, and none is needed: the File row is already
		# attached_to this Lead, which is the shape the migration re-parented the old ones into.
		kind = (item.get("kind") or "").strip()
		fieldname = ATTACHMENT_KINDS[kind] if kind else None
		if fieldname and not frappe.get_meta("Lead").has_field(fieldname):
			fieldname = None

		held = _attachment_already_here(lead.name, content)
		if held:
			# These exact bytes are already attached to this Lead. Inserting a second File row
			# for them is what made the card image pile up: frappe dedupes the BYTES by content
			# hash, so every extra row pointed at the same file_url and the only visible effect
			# was one more copy of the same card in the Lead's sidebar on every push that
			# changed any unrelated field. It also inflated the row count _discard_files reads
			# when deciding whether rolled-back bytes may be removed.
			urls.append(held.file_url)
			if fieldname:
				card_fields[fieldname] = held.file_url
			continue

		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": (item.get("file_name") or "").strip() or f"{graph['vc_uuid']}-{index}",
				"attached_to_doctype": "Lead",
				"attached_to_name": lead.name,
				# Claim the Attach field on the row this endpoint writes. frappe's own
				# `attach_files_to_document` runs on EVERY save of the Lead and creates a File
				# row for an Attach field value that has none -- and its guard matches on
				# (file_url, attached_to_field), which an unclaimed row never satisfies. So a
				# client that still has the card Custom Fields grew one further File row per
				# push from frappe's side too, for any push at all, not only one carrying a
				# card. Claiming the field here is what that guard is looking for.
				"attached_to_field": fieldname,
				"is_private": cint(item.get("is_private", 1)),
				"content": content,
			}
		)
		try:
			_insert_doc(file_doc)
		except ConnectorError:
			raise
		except Exception as exc:
			# frappe inspects and optimises an uploaded image, so bytes that are not the
			# image their file name claims ("cannot identify image file") fail here. That is
			# a fact about THIS payload -- it will fail identically forever -- so it must not
			# be reported as a retryable 500 that the central site then retries in a loop.
			raise ConnectorError(
				"VALIDATION_FAILED",
				_("attachments[{0}] could not be stored: {1}").format(index, _exception_message(exc)),
				field=f"attachments[{index}]",
			)
		created_files.append(file_doc)
		touched.append(("File", file_doc.name))
		urls.append(file_doc.file_url)

		if fieldname:
			card_fields[fieldname] = file_doc.file_url

	if card_fields:
		# Attaching a File touches the Lead row, so the copy in hand is now stale and saving
		# it raises TimestampMismatchError -- which, before the reload, turned every push
		# that carried a card image into a failed push that rolled the whole graph back.
		lead.reload()
		changed = {
			fieldname: value
			for fieldname, value in card_fields.items()
			if (lead.get(fieldname) or "") != value
		}
		if changed:
			for fieldname, value in changed.items():
				lead.set(fieldname, value)
			_save_doc(lead)

	return urls


def _upsert_link(graph, lead, link_row, payload_hash, response, touched):
	"""The identity row: UNIQUE vc_uuid, so idempotency is enforced by the database.

	`idempotency_key == lead.vc_uuid`. Keeping it in a table with a UNIQUE index rather than
	in application logic is the point: two concurrent pushes of the same central record race,
	and only the database can settle that race.
	"""
	link = _claim_link_row(lead, link_row)

	link.vc_uuid = graph["vc_uuid"]
	link.lead = lead.name
	link.business_id = graph["business_id"]
	link.seen_request_ids = _extended_request_history(link.get("seen_request_ids"), graph["request_id"])
	link.last_request_id = graph["request_id"]
	link.payload_hash = payload_hash
	link.last_synced_on = now_datetime()
	link.migrated_fields = ",".join(sorted(graph["lead"]))
	link.client_held_contacts = json.dumps(graph.get("client_held") or {"emails": [], "phones": []})
	link.last_response = json.dumps(response, default=str)

	if link.is_new():
		_insert_doc(link)
	else:
		_save_doc(link)

	touched.append(("Connector Lead Link", link.name))
	return link.name


def _claim_link_row(lead, link_row):
	"""The Connector Lead Link row this push should write to.

	Three cases. The third is the one the legacy field migration created:

	* the push carries a vc_uuid already in the table -- that row;
	* nothing known about this Lead -- a new row;
	* a PLACEHOLDER row: the migration wrote it to carry ``custom_business_id`` across the
	  drop of that Custom Field, with vc_uuid NULL because the Lead has never been pushed
	  under this protocol. This push is its first, so it fills the UUID in rather than
	  inserting a second row -- which ``validate_one_link_per_lead`` would refuse anyway,
	  turning every migrated Lead's first real push into a hard 409.
	"""
	if link_row:
		return frappe.get_doc("Connector Lead Link", link_row.name)

	placeholder = frappe.db.get_value(
		"Connector Lead Link", {"lead": lead.name}, ["name", "vc_uuid"], as_dict=True
	)
	if placeholder and not (placeholder.vc_uuid or "").strip():
		return frappe.get_doc("Connector Lead Link", placeholder.name)

	return frappe.new_doc("Connector Lead Link")


def _insert_doc(doc):
	"""The single seam every insert in this endpoint passes through.

	Having one makes "prove the whole graph rolls back when the Address fails" a test that
	can be written honestly, instead of one that has to corrupt real data to provoke it.

	``ignore_permissions`` is safe because the caller was already authorised at the door.
	``ignore_mandatory`` is a DELIBERATE product decision, changed 2026-09-25: a client site
	can mark any field mandatory -- including a custom field this protocol has never heard of
	-- and when it did, the push failed after the mobile app had already told the user the
	lead was sent. The lead then existed nowhere. Landing the record and letting the client
	fill the gap afterwards loses less than refusing it. The connector's OWN schema checks
	still run first, so a payload that is structurally wrong is still rejected as
	VALIDATION_FAILED / MANDATORY_MISSING before anything is written.
	"""
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	return doc


def _save_doc(doc):
	doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)
	return doc


def _linked_document(doctype, lead_name):
	"""The document of this type linked to this Lead -- the OLDEST one, always the same one.

	``order_by`` is not decoration. Without it frappe applies the DocType's own default sort,
	which is ``modified desc``, so "the Contact of this Lead" was whichever linked document had
	been touched last -- a different answer on a different day, and a different answer again
	the moment this endpoint wrote to one of them.
	"""
	return frappe.db.get_value(
		"Dynamic Link",
		{"parenttype": doctype, "link_doctype": "Lead", "link_name": lead_name},
		"parent",
		order_by="creation asc",
	)


def _linked_documents(doctype, lead_name):
	"""Every document of this type linked to this Lead, oldest first, without repeats."""
	names = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": doctype, "link_doctype": "Lead", "link_name": lead_name},
		pluck="parent",
		order_by="creation asc",
	)
	return list(dict.fromkeys(name for name in names if name))


def _address_for_lead(lead_name):
	"""Which Address an incoming push may rewrite. Never the one Patch A created to protect.

	A Lead can hold two addresses because the migration deliberately made it so: when the Lead
	already had a human-entered address with a DIFFERENT pincode, Patch A refused to overwrite
	it and kept the scanned pincode on a second "(Scanned)" Address instead, logging
	PINCODE_CONFLICT. That second row exists for one reason -- to hold a value nothing else
	holds after the legacy field is dropped.

	Resolving "the Address of this Lead" by ``modified desc`` handed the push exactly that row,
	so the next ordinary push overwrote its line, its city, its type and its pincode, undoing
	the conflict rule in one call and leaving the human address -- the row the rule was
	protecting -- untouched and irrelevant. So a scanned row is chosen only when it is the ONLY
	address there is, which is the simple case where it is also the client's real address.
	"""
	linked = _linked_documents("Address", lead_name)
	if not linked:
		return None

	for name in linked:
		if not is_scanned_address_title(frappe.db.get_value("Address", name, "address_title")):
			return name

	return linked[0]


def _discard_files(created_files):
	"""Remove the bytes a rolled-back push wrote -- and ONLY the bytes it wrote.

	Called after the savepoint rollback, which has already removed the File ROWS this push
	inserted. What it cannot undo is the write to disk, hence this.

	The rule that matters: frappe dedupes uploads by content hash. A second upload of bytes
	that are already on the site creates a new File row but writes NOTHING new to disk and
	points that row at the EXISTING file_url. Deleting that path here deleted the attachment
	of every other record sharing it -- their File rows survived the rollback and now point at
	bytes that are gone, with no archive and no undo. A truncated base64 field in one payload,
	which is an ordinary transport failure that the central site then retries, was enough.

	So a path is removed only when no File row is left pointing at its URL. After the rollback
	the only rows that can still point at it are rows this push did not create, and one of
	those is exactly the case where the bytes must stay.
	"""
	for file_doc in created_files:
		url = file_doc.get("file_url")
		try:
			if url and frappe.db.count("File", {"file_url": url}):
				continue

			path = file_doc.get_full_path()
			if path and os.path.exists(path):
				os.remove(path)
		except Exception:
			frappe.logger("ai_snaplead_connector").warning(
				f"could not remove rolled-back attachment {file_doc.get('file_url')}"
			)


def _forget(touched):
	"""Drop the in-process caches for documents a savepoint rollback has just un-written.

	`rollback to savepoint` does not reach frappe's document cache, so without this the next
	`frappe.get_doc` in the same request happily returns a Lead that no longer exists in the
	database.
	"""
	for doctype, name in touched:
		try:
			frappe.clear_document_cache(doctype, name)
		except Exception:
			pass

	try:
		frappe.db.value_cache.clear()
	except Exception:
		pass


# ── Idempotency ──────────────────────────────────────────────────────────────────────────


# How many request ids a link row remembers. Every one of them is a request this endpoint
# has already applied to that record, and re-applying any of them would revert it to an
# older state.
#
# This is a WINDOW, not a promise about all of history, and the difference is the whole of
# this fix. At fifty ids the window was silent about its own edge: the fifty-first push
# pushed the first request id out of the list, and a re-delivery of that request then missed
# the replay check, failed the payload-hash comparison (the stored hash belongs to the newer
# payload), fell through to the write and re-applied the stale payload over the newer one --
# answering ok:true, outcome:"updated", replayed:false, warnings:[]. Fifty pushes of work
# silently reverted and reported as a success, which is verbatim the failure the history was
# added to prevent.
#
# Two things changed. The window is an order of magnitude larger, and the row now counts the
# ids it has FORGOTTEN, so the endpoint can tell "this request id is new" apart from "this
# request id is one I can no longer rule out". The second case is still written -- refusing
# it would refuse every genuinely new push on a busy record -- but it is reported, in
# `warnings`, naming what could not be checked. A caller is never again told that a revert
# was a clean update.
REQUEST_HISTORY_LIMIT = 500


def _request_history(stored):
	"""Parse the stored history in either shape it has ever had.

	Rows written before this release hold a bare JSON array of ids and know nothing about
	what they dropped; they are read as "nothing forgotten", which is what they believed.
	"""
	try:
		value = json.loads(stored or "[]")
	except ValueError:
		return [], 0

	if isinstance(value, list):
		return [str(item) for item in value if item], 0

	if isinstance(value, dict):
		ids = value.get("ids")
		ids = [str(item) for item in ids if item] if isinstance(ids, list) else []
		return ids, cint(value.get("dropped"))

	return [], 0


def _seen_request_ids(stored):
	return _request_history(stored)[0]


def _forgotten_request_count(stored):
	return _request_history(stored)[1]


def _extended_request_history(stored, request_id):
	history, dropped = _request_history(stored)
	history = [item for item in history if item != request_id]
	history.append(request_id)

	overflow = len(history) - REQUEST_HISTORY_LIMIT
	if overflow > 0:
		history = history[overflow:]
		dropped += overflow

	return json.dumps({"ids": history, "dropped": dropped})


def _find_replay(graph):
	"""The stored answer to THIS request -- and only when it was THIS record's request.

	The lookup key is a request_id the central site mints, and it used to be trusted on its
	own: a central site that reused one across two different records was handed the FIRST
	record's response with ok/replayed true, so it marked the second record synced although
	nothing had been written for it and no link row existed for its vc_uuid. The row carries
	the vc_uuid it belongs to, so that is compared here; a reused id is now a refusal naming
	both records rather than a silently discarded record.

	`last_request_id` answers only for the MOST RECENT request of a record, and that was the
	whole idempotency guarantee until now. An at-least-once transport -- which is the only
	reason request_id exists -- reorders and redelivers routinely, so a duplicate delivery of
	an EARLIER request arrived with a request_id the row no longer held and a payload_hash
	that no longer matched, fell through to the write, and silently re-applied a stale
	payload over the newer one, answering ok:true outcome:"updated" replayed:false. The row
	now remembers the ids it has already applied, and a re-delivery of any of them is
	answered without writing -- see `_superseded_replay`.
	"""
	row = frappe.db.get_value(
		"Connector Lead Link",
		{"last_request_id": graph["request_id"]},
		["name", "vc_uuid", "lead", "last_response"],
		as_dict=True,
	)
	if not row:
		return _superseded_replay(graph)

	if (row.vc_uuid or "") != graph["vc_uuid"]:
		raise ConnectorError(
			"CONFLICT",
			_(
				"request_id {0} has already been used by central record {1}, so it cannot be"
				" reused for {2}. Mint a new request_id for every attempt; nothing was written"
				" for {2}."
			).format(graph["request_id"], row.vc_uuid, graph["vc_uuid"]),
			field="request_id",
		)

	if not row.last_response or not _graph_intact(row):
		# The stored response names documents that are no longer there. Replaying it would
		# report a graph that does not exist; falling through writes the payload and restores
		# it, which is the only repair the protocol can offer.
		return None

	try:
		stored = json.loads(row.last_response)
	except ValueError:
		return None

	stored["ok"] = True
	stored["replayed"] = True
	stored["unchanged"] = False
	return stored


def _superseded_replay(graph):
	"""A re-delivery of a request this record has already moved on from.

	Returns the record's CURRENT state, marked `replayed` and `superseded`, and writes
	nothing. The stored answer to that particular request is gone -- only the latest one is
	kept -- and inventing one would be worse than useless, so the answer describes what is
	actually on this client now and names the request that produced it.

	The one thing this must never do is fall through to the write. The payload attached to
	an old request_id is, by definition, older than what is on the record, and applying it
	is a silent revert of whatever came after it.
	"""
	row = frappe.db.get_value(
		"Connector Lead Link",
		{"vc_uuid": graph["vc_uuid"]},
		["name", "vc_uuid", "lead", "last_request_id", "last_response", "seen_request_ids"],
		as_dict=True,
	)
	if not row or graph["request_id"] not in _seen_request_ids(row.seen_request_ids):
		return None

	try:
		stored = json.loads(row.last_response or "{}")
	except ValueError:
		stored = {}
	if not isinstance(stored, dict):
		stored = {}

	warnings = list(stored.get("warnings") or [])
	warnings.append(
		"request_id {0} had already been applied to {1} and has since been superseded by {2}."
		" Nothing was written: this answer describes the record as it stands now.".format(
			graph["request_id"], graph["vc_uuid"], row.last_request_id or "a later request"
		)
	)

	stored.update(
		{
			"ok": True,
			"protocol": PROTOCOL,
			"request_id": graph["request_id"],
			"vc_uuid": graph["vc_uuid"],
			"lead": row.lead,
			"replayed": True,
			"unchanged": True,
			"superseded": True,
			"superseded_by": row.last_request_id or None,
			"warnings": warnings,
		}
	)
	return stored


def _graph_intact(row):
	"""Is every document the stored response named still on this site?

	A "no change" or "replay" answer repeats names the endpoint wrote earlier. An ordinary
	user deleting the Contact or the Address afterwards is a thing that happens, and nothing
	in this app forbids it -- so the names are verified before they are repeated, and a graph
	that has lost a member is reported as not intact so the caller's next push rebuilds it.
	"""
	if not row.get("lead") or not frappe.db.exists("Lead", row.get("lead")):
		return False

	try:
		stored = json.loads(row.get("last_response") or "{}")
	except ValueError:
		return False

	if not isinstance(stored, dict):
		return False

	for doctype, key in (("Contact", "contact"), ("Address", "address")):
		name = stored.get(key)
		if name and not frappe.db.exists(doctype, name):
			return False

	return True


def _unchanged_response(graph, link_row, payload_hash):
	"""Same content, different request: answer without writing anything at all.

	Not even `last_request_id` is updated. Writing on a no-op would bump `modified` on the
	link row and make every "nothing changed" push look like activity in the log.
	"""
	try:
		stored = json.loads(link_row.last_response or "{}")
	except ValueError:
		stored = {}

	stored.update(
		{
			"ok": True,
			"protocol": PROTOCOL,
			"request_id": graph["request_id"],
			"vc_uuid": graph["vc_uuid"],
			"lead": link_row.lead,
			"payload_hash": payload_hash,
			"unchanged": True,
			"replayed": False,
		}
	)
	return stored


# ── Reads ────────────────────────────────────────────────────────────────────────────────


def _pull_page(settings, since=None, cursor=None, limit=None):
	page_size = cint(limit) or DEFAULT_PULL_PAGE
	page_size = max(1, min(page_size, MAX_PULL_PAGE))

	link = frappe.qb.DocType("Connector Lead Link")
	lead = frappe.qb.DocType("Lead")
	business_id = (settings.get("business_id") or "").strip()

	query = (
		frappe.qb.from_(link)
		.inner_join(lead)
		.on(link.lead == lead.name)
		.select(
			link.vc_uuid,
			link.lead,
			link.payload_hash,
			link.last_synced_on,
			lead.modified,
			lead.lead_name,
			lead.status,
			lead.email_id,
			lead.mobile_no,
		)
		.where((link.business_id == business_id) | link.business_id.isnull())
		.orderby(lead.modified, order=Order.asc)
		.orderby(lead.name, order=Order.asc)
		.limit(page_size + 1)
	)

	if since:
		query = query.where(lead.modified > since)

	if cursor:
		modified, _sep, name = str(cursor).partition("|")
		if not name:
			raise ConnectorError(
				"VALIDATION_FAILED", _("cursor is not a cursor issued by this client."), field="cursor"
			)
		query = query.where((lead.modified > modified) | ((lead.modified == modified) & (lead.name > name)))

	rows = query.run(as_dict=True)
	has_more = len(rows) > page_size
	rows = rows[:page_size]

	for row in rows:
		# Read from wherever this site actually keeps them. The child TABLES survive the
		# legacy field migration -- the drop leaves their rows alone on purpose, as a second
		# copy of the archive -- so a query against them on a migrated client answers with a
		# contact list frozen at migration time that no form on that site can show.
		row["emails"], row["phones"] = _stored_lead_contacts(row.lead)
		row["modified"] = str(row["modified"])
		row["last_synced_on"] = str(row["last_synced_on"]) if row.get("last_synced_on") else None

	next_cursor = f"{rows[-1]['modified']}|{rows[-1]['lead']}" if rows and has_more else None

	return {
		"ok": True,
		"protocol": PROTOCOL,
		"business_id": business_id,
		"count": len(rows),
		"records": rows,
		"cursor": next_cursor,
		"has_more": has_more,
	}


# ── Detection helpers ────────────────────────────────────────────────────────────────────


def india_compliance_installed():
	"""Detected, never assumed: it changes what an Address is allowed to be missing."""
	try:
		return "india_compliance" in frappe.get_installed_apps()
	except Exception:
		return False


def _has_default_address_template():
	"""Can this site save an Address at all?

	frappe refuses to save ANY Address on a site with no default Address Template -- "No
	default Address Template found" -- and a site created without the setup wizard has none.
	It is a client-side setup gap the central site cannot see and the connector must not
	paper over by creating one: a marketplace app does not invent print formats on a
	stranger's site. Reporting it here is how the central site learns that addresses will be
	refused BEFORE it pushes ten thousand of them.
	"""
	try:
		return bool(frappe.db.exists("Address Template", {"is_default": 1}))
	except Exception:
		return False


def ensure_lead_source(source_name):
	"""Create the Lead's source master record on the client if it is not there yet.

	THE BUG THIS CLOSES

	The payload carries the source as a plain STRING -- the central site's source name --
	while ``Lead.utm_source`` (``Lead.source`` before v16) is a LINK. If the client has no
	such row the push dies on link validation, and the older workaround (central creating the
	row first over plain REST, as the client's integration user) failed with a PERMISSION
	error on every client whose integration user had not been granted create rights on that
	DocType. Both failures lost a lead that the app had already reported as sent.

	Creating it here fixes both at once: it happens inside the same transaction as the Lead,
	so it rolls back with it, and with ``ignore_permissions`` so it cannot depend on a role
	the client forgot to grant.

	``ignore_if_duplicate`` matters because two pushes for the same new source can race; the
	loser must not turn a successful lead into a failed one.
	"""
	if not source_name:
		return None

	doctype = "UTM Source" if _lead_source_fieldname() == "utm_source" else "Lead Source"
	if not frappe.db.exists("DocType", doctype):
		return None
	if frappe.db.exists(doctype, source_name):
		return source_name

	doc = frappe.new_doc(doctype)
	# UTM Source autonames by prompt, so the NAME is the source name. Lead Source (pre-v16)
	# carries its own source_name field and autonames from it. Setting whichever exists keeps
	# this working on both.
	if doc.meta.get_field("source_name"):
		doc.source_name = source_name
	doc.name = source_name
	doc.flags.ignore_permissions = True
	try:
		doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		frappe.clear_last_message()
	return source_name


def _drop_unresolvable_link(doctype, fieldname, value):
	"""(value, dropped) -- blank a Link value the client has no record for.

	Only applies when the client has made the field a Link; on a stock site these are Data
	fields and the value passes through untouched. Returning it rather than raising is the
	point: one unmatched master row must not cost the whole lead.
	"""
	value = (value or "").strip()
	if not value:
		return value, None
	try:
		field = frappe.get_meta(doctype).get_field(fieldname)
	except Exception:
		return value, None
	if not field or field.fieldtype != "Link" or not field.options:
		return value, None
	if frappe.db.exists(field.options, value):
		return value, None
	return "", value


def _lead_source_fieldname():
	# ERPNext v16 renamed Lead.source to Lead.utm_source (Lead Source -> UTM Source). The
	# central site must be told which one this client has rather than guessing from a version
	# number it cannot see.
	try:
		return "source" if frappe.get_meta("Lead").has_field("source") else "utm_source"
	except Exception:
		return "utm_source"


def _installed_version(app):
	try:
		return frappe.get_attr(f"{app}.__version__")
	except Exception:
		return None


# ── Failure path ─────────────────────────────────────────────────────────────────────────


def _classify(exc):
	"""Turn a framework exception into the typed contract. Order matters: the specific
	subclasses of ValidationError have to be tested before ValidationError itself."""
	message = _exception_message(exc)

	if isinstance(exc, frappe.MandatoryError):
		return "MANDATORY_MISSING", message, _mandatory_field(exc)
	# A TimestampMismatchError means somebody else wrote this Lead between the get_doc that
	# loaded it and the save that tried to put it back -- a salesperson pressing Ctrl+S in
	# the Desk while the push was in flight. This used to be folded into CONFLICT, and
	# CONFLICT's contract (and the README's error table) says re-sending the same bytes will
	# not help. It is the one failure here where re-sending the same bytes is EXACTLY the
	# fix, so it gets its own code and is retryable. Keeping the 409 status: it is still a
	# conflict, just a transient one.
	timestamp_mismatch = getattr(frappe.exceptions, "TimestampMismatchError", None)
	if timestamp_mismatch is not None and isinstance(exc, timestamp_mismatch):
		return (
			"CONCURRENT_MODIFICATION",
			_(
				"This Lead was modified on the client while this push was being written, so"
				" nothing was saved: {0}. Re-send this payload unchanged."
			).format(message),
			None,
		)

	# A real CONFLICT is a duplicate: re-sending the same bytes will not fix it.
	conflict_types = tuple(
		exc_type
		for exc_type in (
			frappe.DuplicateEntryError,
			getattr(frappe, "UniqueValidationError", None),
		)
		if exc_type is not None
	)
	if isinstance(exc, conflict_types):
		return "CONFLICT", message, None
	if isinstance(exc, frappe.PermissionError):
		return "REMOTE_PERMISSION", message, None
	if isinstance(exc, frappe.LinkValidationError):
		return "VALIDATION_FAILED", message, None
	if isinstance(exc, frappe.ValidationError):
		return "VALIDATION_FAILED", message, None

	return "INTERNAL", _("The client could not process this push: {0}").format(message), None


def _exception_message(exc):
	message = str(exc or "").strip()

	if not message:
		try:
			messages = frappe.local.message_log or []
			if messages:
				last = messages[-1]
				message = last.get("message") if isinstance(last, dict) else str(last)
		except Exception:
			message = ""

	message = frappe.utils.strip_html(message or "").strip()
	return message or exc.__class__.__name__


def _mandatory_field(exc):
	# frappe.MandatoryError reads "[Lead, new-lead-1]: lead_name"
	text = str(exc or "")
	if ":" in text:
		return text.rsplit(":", 1)[-1].strip() or None
	return None


def _fail(code, message, field, request_id, vc_uuid, started):
	status = ERROR_STATUS.get(code, 500)
	frappe.local.response["http_status_code"] = status

	if code != "UNAUTHENTICATED":
		# An unauthenticated caller is not allowed to write a row of any kind on this site,
		# not even a log row: that is a free write amplifier for anyone who can reach the URL.
		_log(
			outcome="Error" if code == "INTERNAL" else "Rejected",
			request_id=request_id,
			vc_uuid=vc_uuid,
			started=started,
			message=message,
			error_code=code,
		)

	return {
		"ok": False,
		"error": {
			"code": code,
			"message": message,
			"field": field,
			"retryable": code in RETRYABLE_CODES,
			"request_id": request_id,
		},
	}


def _log(outcome, request_id, vc_uuid, started, message=None, error_code=None):
	try:
		frappe.get_doc(
			{
				"doctype": "Connector Log",
				"request_id": request_id,
				"vc_uuid": vc_uuid,
				"outcome": outcome,
				"error_code": error_code,
				"duration_ms": int((time.monotonic() - started) * 1000),
				"message": (message or "")[:5000],
			}
		).insert(ignore_permissions=True)
	except Exception:
		# Logging must never be the reason a push fails, or turn a 422 into a 500.
		frappe.logger("ai_snaplead_connector").warning(f"could not write Connector Log for {request_id}")


def _touch_last_request():
	try:
		frappe.db.set_single_value(SETTINGS, "last_request_on", now_datetime())
	except Exception:
		pass
