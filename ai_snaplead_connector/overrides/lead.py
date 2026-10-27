# Copyright (c) 2026, support@sigzen.com and contributors

import frappe
from erpnext.crm.doctype.lead.lead import Lead
from frappe import _

from ai_snaplead_connector.legacy.schema import lead_has_contact_tables
from ai_snaplead_connector.tenancy import (
	get_business_id,
	get_duplicate_scope_business_ids,
	is_business_mismatch,
	reject_business_mismatch_enabled,
	site_accepts_multiple_businesses,
)
from ai_snaplead_connector.utils.lead_contacts import (
	adopt_legacy_parent_contact_fields,
	find_duplicate_lead_by_contacts,
	lead_had_contact_details,
	newly_introduced_contacts,
	normalize_contact_rows_in_place,
	normalize_email_list,
	normalize_mobile_list,
	release_dropped_contact_values,
	sync_legacy_parent_contact_fields,
	validate_duplicate_values_in_payload,
)


class CustomLead(Lead):
	def reconcile_contact_fields(self):
		"""Make the stock fields and the child tables agree, in both directions.

		Canonicalise the child rows first, so the adopt step compares like with like; take
		email_id / mobile_no into the child tables, so a Lead pushed by a client that only
		knows standard ERPNext fields is accepted instead of refused and its contact details
		are not thrown away; then project row 1 of each table back onto the stock fields so a
		stock ERPNext list view / report / email workflow still sees a contact, and so both
		push transports leave an identical row behind.
		"""
		normalize_contact_rows_in_place(self)
		adopt_legacy_parent_contact_fields(self)
		sync_legacy_parent_contact_fields(self)

	def before_insert(self):
		"""Reconcile BEFORE ERPNext builds the auto-created Contact.

		ERPNext's Lead.before_insert calls create_contact(), which reads self.email_id and
		self.mobile_no -- and before_insert runs before validate. A Lead pushed through this
		app's child tables alone therefore used to get a Contact with no email address and no
		phone number, permanently: nothing re-syncs it afterwards, so every stock contact
		lookup and email workflow hanging off that Contact was blank.
		"""
		self.reconcile_contact_fields()
		super().before_insert()

	def on_update(self):
		"""Let the Lead's Contact go of a contact detail the Lead just dropped.

		ERPNext creates the Contact in before_insert and never touches it again, but the
		duplicate guard reads Contact Phone / Contact Email too -- so a corrected number went
		on being claimed by the stale copy and the Lead that really owned it could never be
		saved. See release_dropped_contact_values for the full reasoning and its limits.
		"""
		super_on_update = getattr(super(), "on_update", None)
		if super_on_update:
			super_on_update()
		release_dropped_contact_values(self)

	def validate(self):
		self.reconcile_contact_fields()
		self.validate_business()
		self.validate_contact_child_tables()
		self.validate_duplicate_lead_contacts()
		# Stock validation runs LAST, on the projected values. A Lead pushed through the
		# child tables alone has an email_id by now, so ERPNext's own guards (uniqueness,
		# address syntax) see it instead of being skipped on an empty field.
		super().validate()

	def check_email_id_is_unique(self):
		"""Scope ERPNext's GLOBAL email-uniqueness guard to the business -- but only when
		this site actually holds more than one business.

		Stock ERPNext refuses any second Lead that reuses an email address anywhere on the
		site. Skipping that for every configured site was wrong: a site with
		reject_business_mismatch on refuses foreign stamps outright, so "anywhere on the
		site" already means "within this business" and the stock rule is exactly right.
		Turning the connector on used to switch it off, which removed the only duplicate
		protection covering the client's pre-existing Lead book.

		Only when an admin deliberately allows mismatched stamps does the site hold several
		businesses; then validate_duplicate_lead_contacts is the authority, and it checks
		both child tables AND the stock columns, case-insensitively.
		"""
		if site_accepts_multiple_businesses() and get_business_id(self):
			return
		# Pushes land duplicates on purpose (api/v1/sync.py, DUPLICATE_GUARDS). Stock ERPNext
		# re-checks on every save, so both Leads would become unsaveable on the Desk; a save
		# that does not change the address is not introducing a duplicate.
		if not self.is_new() and not self.has_value_changed("email_id"):
			return
		super().check_email_id_is_unique()

	def validate_contact_child_tables(self):
		email_ids = normalize_email_list(self.get("custom_email_ids"))
		mobile_numbers = normalize_mobile_list(self.get("custom_mobile_numbers"))

		# email_id / mobile_no have already been adopted into the child tables by
		# adopt_legacy_parent_contact_fields, but a direct caller of this method may not have
		# run that, so accept the standard fields here as well.
		if (
			not email_ids
			and not mobile_numbers
			and not self.whatsapp_no
			and not self.get("email_id")
			and not self.get("mobile_no")
		):
			# Stock ERPNext accepts a Lead with a name and nothing else, and every client's
			# book holds some: a company noted at a trade show, a referral with a callback
			# promised. Requiring a contact detail is right for a Lead this app creates and
			# wrong for one that was already on the client, and enforcing it on every save
			# left those rows unsaveable for ever -- a spelling could not be corrected on
			# them. A row that never had a contact detail is left alone; a row that HAD one
			# is still not allowed to have its last one deleted, because that is a change
			# this save is making.
			if self.is_new() or lead_had_contact_details(self.name):
				frappe.throw(
					_("At least one contact detail is required: Email ID, Mobile Number, or WhatsApp No")
				)

		validate_duplicate_values_in_payload(email_ids, "email")
		validate_duplicate_values_in_payload(mobile_numbers, "mobile")

	def contact_values_for_duplicate_check(self):
		"""The contact details this save is claiming, whichever shape the site is in.

		Before Patch B that is the child tables. Afterwards they are gone, and reading them
		would leave the mobile-number half of the duplicate guard permanently switched off:
		ERPNext's own guard covers email_id only, so a second Lead reusing a phone number
		would be accepted silently.

		After Patch B the Lead itself has room for ONE email address and ONE phone number;
		the rest of a scanned card goes onto the Lead's Contact, which this document has not
		written yet at validate time. A caller that knows the whole set -- the connector
		endpoint does, it is holding the payload -- declares it in
		``flags.vc_contact_values``, and then all of it is checked rather than row 1 alone.
		"""
		if lead_has_contact_tables():
			return (
				normalize_email_list(self.get("custom_email_ids")),
				normalize_mobile_list(self.get("custom_mobile_numbers")),
			)

		declared = self.flags.get("vc_contact_values") or {}
		emails = [(value or "").strip().lower() for value in (declared.get("emails") or [])]
		mobiles = [(value or "").strip() for value in (declared.get("phones") or [])]

		email = (self.get("email_id") or "").strip().lower()
		mobile = (self.get("mobile_no") or "").strip()
		if email and email not in emails:
			emails.insert(0, email)
		if mobile and mobile not in mobiles:
			mobiles.insert(0, mobile)

		return ([value for value in emails if value], [value for value in mobiles if value])

	def validate_business(self):
		"""Refuse a Lead stamped for another business.

		Tenancy is configured in Connector Settings; a row arriving with a different
		custom_business_id means the pusher is talking to the wrong ERP site. Silently
		accepting it would mix two tenants' leads in one table.
		"""
		if not is_business_mismatch(self):
			return

		if not reject_business_mismatch_enabled():
			return

		frappe.throw(
			_("This Lead belongs to business {0}, but this site is configured for business {1}.").format(
				self.get("custom_business_id"), get_business_id()
			),
			frappe.PermissionError,
		)

	def validate_duplicate_lead_contacts(self):
		"""Refuse a Lead that reuses a contact another Lead in scope already holds.

		This used to return early whenever no business could be resolved, which switched the
		guard off completely on a site whose Connector Settings has not been filled in --
		the state every upgraded client is in the moment `bench migrate` finishes. Scoping is
		now done by find_duplicate_lead_by_contacts, which treats "no business known" as "do
		not exclude anything" instead of "exclude everything".

		Only the contact details THIS SAVE INTRODUCES are checked. `Lead.validate` runs on
		every Desk save, not only on connector pushes, and the guard covers phone numbers,
		which stock ERPNext never deduplicated. Checking every value the document holds
		therefore made two pre-existing Leads that legitimately share a number -- a shared
		office landline, a switchboard, a receptionist's mobile -- mutually unsaveable: each
		one named the other, on a save that had not touched the number, and the only way out
		was to delete data that was correct. Refusing the save does not un-share the number;
		it only stops the row being edited. A value the Lead already held is left alone, and
		a value this save ADDS is refused exactly as before.
		"""
		email_ids, mobile_numbers = self.contact_values_for_duplicate_check()

		if not self.is_new():
			email_ids, mobile_numbers = newly_introduced_contacts(self.name, email_ids, mobile_numbers)

		if not email_ids and not mobile_numbers:
			return

		duplicate = find_duplicate_lead_by_contacts(
			business_ids=get_duplicate_scope_business_ids(self),
			email_ids=email_ids,
			mobile_numbers=mobile_numbers,
			exclude_lead=self.name,
		)
		if not duplicate:
			return

		frappe.throw(_duplicate_message(duplicate))


def _duplicate_message(duplicate):
	"""Name a place on THIS site where the clashing value can actually be removed.

	The old text always said "its Email IDs table" / "its Mobile Numbers table". Those are
	the legacy Custom Field child tables, and they are exactly what the migration drops: on
	any migrated or freshly installed client the Lead form has one Custom Field
	(`custom_address`) and no such table, so the refusal instructed the user to edit
	something that does not exist anywhere in their Desk.
	"""
	if duplicate["type"] == "email":
		if lead_has_contact_tables():
			return _(
				"A lead with Email ID {0} already exists in this business: {1}."
				" If that lead no longer uses this address, remove it from its Email IDs"
				" table first."
			).format(duplicate["value"], duplicate["lead"])

		return _(
			"A lead with Email ID {0} already exists in this business: {1}."
			" If that lead no longer uses this address, clear it from that lead's Email ID"
			" field and from the Contact linked to it first."
		).format(duplicate["value"], duplicate["lead"])

	if lead_has_contact_tables():
		return _(
			"A lead with Mobile No {0} already exists in this business: {1}."
			" If that lead no longer uses this number, remove it from its Mobile Numbers"
			" table first."
		).format(duplicate["value"], duplicate["lead"])

	return _(
		"A lead with Mobile No {0} already exists in this business: {1}."
		" If that lead no longer uses this number, clear it from that lead's Mobile No field"
		" and from the Contact linked to it first."
	).format(duplicate["value"], duplicate["lead"])
