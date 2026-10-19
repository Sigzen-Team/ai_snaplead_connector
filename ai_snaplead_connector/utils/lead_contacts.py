# Copyright (c) 2026, support@sigzen.com and contributors

import frappe
from frappe import _
from frappe.query_builder.functions import Lower
from frappe.utils import cstr

from ai_snaplead_connector.legacy.schema import lead_has_contact_tables
from ai_snaplead_connector.tenancy import get_site_business_id
from ai_snaplead_connector.utils.phone import phone_match_key


def _parse_contact_rows(rows):
	if isinstance(rows, str):
		rows = frappe.parse_json(rows)
	return rows if isinstance(rows, list) else []


def _extract_row_value(row, fieldname):
	if isinstance(row, dict):
		return row.get(fieldname)
	if hasattr(row, fieldname):
		return getattr(row, fieldname)
	return row


def normalize_email_list(email_rows):
	emails = []

	for row in _parse_contact_rows(email_rows):
		email = _extract_row_value(row, "email_id")
		email = (email or "").strip().lower()
		if email:
			emails.append(email)

	return emails


def normalize_mobile_list(mobile_rows):
	mobiles = []

	for row in _parse_contact_rows(mobile_rows):
		mobile = _extract_row_value(row, "mobile_number")
		mobile = (mobile or "").strip()
		if mobile:
			mobiles.append(mobile)

	return mobiles


def validate_duplicate_values_in_payload(values, contact_type):
	seen = set()

	for value in values:
		if value in seen:
			if contact_type == "email":
				frappe.throw(_("Duplicate email found: {0}").format(value))
			frappe.throw(_("Duplicate mobile number found: {0}").format(value))
		seen.add(value)


def normalize_contact_rows_in_place(doc):
	"""Canonicalise the contact child rows on a Lead before anything reads them.

	Emails are lower-cased and both lists are stripped, so the stored value is the same one
	the duplicate guard searches for. Without this, a row pushed as "A@B.com" is invisible to
	a later lookup for "a@b.com" on any database whose default collation is case sensitive
	(SQLite, PostgreSQL) -- which is precisely the client target.
	"""
	for row in doc.get("custom_email_ids") or []:
		row.email_id = (row.email_id or "").strip().lower()

	for row in doc.get("custom_mobile_numbers") or []:
		row.mobile_number = (row.mobile_number or "").strip()


def _matching_child_rows(child_doctype, fieldname, values, case_insensitive):
	"""Child rows of a Lead whose contact value is one of ``values``.

	Written with the query builder rather than raw SQL: the previous version interpolated a
	Python tuple into ``IN %(x)s``, which is a MariaDB/pymysql-only trick and raises on the
	SQLite and PostgreSQL sites this app is expected to install on.
	"""
	table = frappe.qb.DocType(child_doctype)
	column = table[fieldname]
	comparison = Lower(column) if case_insensitive else column

	return (
		frappe.qb.from_(table)
		.select(table.parent.as_("lead"), column.as_("value"))
		.where(table.parenttype == "Lead")
		.where(comparison.isin(values))
	).run(as_dict=True)


def _matching_lead_rows(fieldname, values, case_insensitive):
	"""Leads whose STOCK contact column is one of ``values``.

	The child tables only ever hold rows this app wrote. Every Lead that existed before
	ai_snaplead_connector was installed carries its contact details in ``Lead.email_id`` /
	``Lead.mobile_no`` and has no child rows at all, so a guard that reads the child tables
	alone is blind to the client's entire existing Lead book -- which is most of it.
	"""
	lead = frappe.qb.DocType("Lead")
	column = lead[fieldname]
	comparison = Lower(column) if case_insensitive else column

	return (
		frappe.qb.from_(lead)
		.select(lead.name.as_("lead"), column.as_("value"))
		.where(comparison.isin(values))
	).run(as_dict=True)


def _matching_contact_rows(contact_doctype, fieldname, values, case_insensitive):
	"""Leads whose linked CONTACT holds one of ``values``.

	After Patch B this is the only place a Lead's SECOND email address or SECOND phone
	number exists. The drop moves them onto the Lead's Contact (``Contact Email`` /
	``Contact Phone``) and removes the Custom Field child tables, so a guard that reads the
	child tables and the two stock Lead columns sees row 1 and nothing else: every secondary
	contact a Lead holds became invisible to the duplicate check, and re-pushing the same
	business card with the addresses in a different order created a second Lead for the same
	person, with one address sitting on two Leads and two Contacts.

	The Lead is reached through ``Dynamic Link``, which is how a Contact is attached to a
	Lead in stock ERPNext -- including the Contact that ``Lead.before_insert`` creates by
	itself, so this covers Leads that were never touched by this app.
	"""
	contact = frappe.qb.DocType(contact_doctype)
	link = frappe.qb.DocType("Dynamic Link")
	column = contact[fieldname]
	comparison = Lower(column) if case_insensitive else column

	return (
		frappe.qb.from_(contact)
		.inner_join(link)
		.on(link.parent == contact.parent)
		.select(link.link_name.as_("lead"), column.as_("value"))
		.where(contact.parenttype == "Contact")
		.where(link.parenttype == "Contact")
		.where(link.link_doctype == "Lead")
		.where(comparison.isin(values))
	).run(as_dict=True)


def _candidate_business_stamp(lead_name):
	"""Which business a candidate Lead is stamped for -- wherever this site keeps that.

	Two traps, and either one switches the duplicate guard off completely.

	``custom_business_id`` is one of the Custom Fields Patch B drops, and
	``frappe.db.get_value`` does NOT fail on a fieldname the DocType no longer has: it hands
	back the FIELDNAME ITSELF as a literal. So on every migrated site this read returned the
	string ``"custom_business_id"``, which is not any site's business id, every candidate was
	judged out of scope, and a configured client was left with no duplicate protection at all
	-- the exact state the drop is supposed to leave a client in. Measured, not reasoned:
	``frappe.db.get_value("Lead", name, "custom_business_id")`` on a dropped site returns
	``'custom_business_id'``.

	And once the column is gone the stamp lives on the Lead's ``Connector Lead Link`` row, which is
	where the legacy migration deliberately put it so it would survive the drop. So that is
	where it is read from afterwards.

	Returns None for "this Lead names no business", which ``_candidate_is_in_scope`` reads as
	"it belongs to whichever business this site speaks for" -- never as "a different one".
	"""
	if frappe.get_meta("Lead").has_field("custom_business_id"):
		stamp = cstr(frappe.db.get_value("Lead", lead_name, "custom_business_id")).strip()
		if stamp:
			return stamp

	try:
		if not frappe.db.table_exists("Connector Lead Link"):
			return None
		return (
			cstr(frappe.db.get_value("Connector Lead Link", {"lead": lead_name}, "business_id")).strip()
			or None
		)
	except Exception:
		# Lead.validate also runs during install and migrate, where this app's own tables may
		# not exist yet. A missing link table is "no stamp", never a failed save.
		return None


def _candidate_is_in_scope(candidate_stamp, business_ids, site_business_id):
	"""Is a candidate Lead one of the Leads this check is allowed to match?

	Either side of the comparison can be unknown, and "unknown" must never be read as
	"different business", because that silently switches the guard off:

	* the candidate carries no stamp -- it belongs to whichever business this site speaks
	  for, and if the site is not configured either then its business is simply unknown;
	* the scope is empty -- the site has no business configured and the incoming row names
	  none, so there is no tenancy to scope by at all.

	That second case is the DEFAULT state of an upgraded client right after ``bench
	migrate``: Connector Settings is a brand new Single and nothing prompts anyone to
	fill it in. Reading unknown as "different" left that client's entire pre-existing Lead
	book -- every row of it -- with no duplicate protection whatsoever, because the stock
	``Lead.email_id`` column of those rows was nulled by the bulk rewrite this release
	removed, so ERPNext's own guard finds nothing either.
	"""
	if not business_ids:
		return True

	candidate = (candidate_stamp or "").strip() or site_business_id
	if not candidate:
		return True

	return candidate in business_ids


def find_duplicate_lead_by_contacts(business_ids, email_ids=None, mobile_numbers=None, exclude_lead=None):
	"""Find another Lead in one of these businesses that already holds one of these contacts.

	``business_ids`` is a list, not a single id: on a site that deliberately holds several
	businesses the check has to cover both the business the incoming row names AND the
	site's own, or a retried push stamped for a foreign business is matched against nothing.
	An empty list means "no tenancy configured anywhere", and then nothing is excluded.

	Searches three places, and needs all three:

	* the connector's child tables, while the site still has them (before Patch B);
	* the stock ``Lead.email_id`` / ``Lead.mobile_no`` columns -- every Lead that predates
	  this app carries its contact details there and has no child rows at all;
	* the Lead's linked ``Contact``, which after Patch B is the ONLY home of a Lead's second
	  and third email address and phone number. Reading the first two alone left the guard
	  comparing primary against primary, so the same card pushed with its addresses in a
	  different order produced a second Lead for the same person.

	Emails are matched case-insensitively: rows written before this release were stored
	verbatim, so an exact-match lookup for "a@b.com" never sees a stored "A@B.com" on a
	case-sensitive database (SQLite, PostgreSQL) -- the client target.
	"""
	email_ids = [value for value in (email_ids or []) if value]
	mobile_numbers = [value for value in (mobile_numbers or []) if value]
	site_business_id = get_site_business_id()

	candidates = (
		(email_ids, "ERPNext Lead Email", "email_id", "email_id", "Contact Email", "email_id", "email", True),
		(
			mobile_numbers,
			"ERPNext Lead Mobile Number",
			"mobile_number",
			"mobile_no",
			"Contact Phone",
			"phone",
			"mobile",
			False,
		),
	)

	# Once the legacy Custom Fields have been dropped, the child tables still HOLD rows -- the
	# drop deliberately leaves them as a second copy of the archive -- but no form, list view
	# or report can reach them any more. Matching against them would refuse a perfectly good
	# new Lead and name a "duplicate" whose visible contact details are something else
	# entirely, so they stop being evidence the moment they stop being reachable.
	search_child_tables = lead_has_contact_tables()

	for (
		values,
		child_doctype,
		child_field,
		lead_field,
		contact_doctype,
		contact_field,
		contact_type,
		case_insensitive,
	) in candidates:
		if not values:
			continue

		rows = (
			_matching_child_rows(child_doctype, child_field, values, case_insensitive)
			if search_child_tables
			else []
		)
		rows += _matching_lead_rows(lead_field, values, case_insensitive)
		rows += _matching_contact_rows(contact_doctype, contact_field, values, case_insensitive)

		for row in rows:
			if not row.lead or (exclude_lead and row.lead == exclude_lead):
				continue

			# A Lead saved before this site was onboarded carries no stamp. Settings is the
			# authority for "which business is this site", so an unstamped Lead belongs to it --
			# otherwise configuring Settings would silently switch the dedupe guard off.
			stamp = _candidate_business_stamp(row.lead)
			if not _candidate_is_in_scope(stamp, business_ids, site_business_id):
				continue

			return {
				"lead": row.lead,
				"type": contact_type,
				"value": row.value,
			}

	return None


def _linked_contact_names(lead_name):
	"""Every Contact attached to this Lead, the stock ERPNext way (Dynamic Link)."""
	return frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Contact", "link_doctype": "Lead", "link_name": lead_name},
		pluck="parent",
	)


def stored_contact_values(lead_name):
	"""The contact details this Lead ALREADY holds in the database, wherever they live.

	This is what tells "this save is introducing a contact detail" apart from "this Lead has
	carried that detail all along". The duplicate guard needs the difference, because it runs
	on every Desk save and not only on connector pushes.

	Two pre-existing Leads that legitimately share a number -- a shared office landline, a
	switchboard, a receptionist's mobile -- each name the other, so switching the guard on
	made both of them permanently unsaveable: every save was refused for a value neither save
	had touched, and the client's only way out was to edit data that was correct. Stock
	ERPNext never deduplicated phone numbers at all, so for a migrating client that is not a
	tightened rule, it is a wall.

	Returns (emails, mobiles) as sets. Mobiles carry BOTH the stored spelling and its match
	key, so "+91 98765 43210" and "09876543210" count as the same held number.
	"""
	emails = set()
	mobiles = set()

	if not lead_name:
		return emails, mobiles

	def remember_email(value):
		value = cstr(value).strip().lower()
		if value:
			emails.add(value)

	def remember_mobile(value):
		value = cstr(value).strip()
		if not value:
			return
		mobiles.add(value)
		key = phone_match_key(value)
		if key:
			mobiles.add(key)

	stored = frappe.db.get_value("Lead", lead_name, ["email_id", "mobile_no"], as_dict=True)
	if not stored:
		# Not in the database yet: nothing is "already held", so every value is new.
		return emails, mobiles

	remember_email(stored.get("email_id"))
	remember_mobile(stored.get("mobile_no"))

	if lead_has_contact_tables():
		for value in frappe.get_all(
			"ERPNext Lead Email",
			filters={"parent": lead_name, "parenttype": "Lead"},
			pluck="email_id",
		):
			remember_email(value)
		for value in frappe.get_all(
			"ERPNext Lead Mobile Number",
			filters={"parent": lead_name, "parenttype": "Lead"},
			pluck="mobile_number",
		):
			remember_mobile(value)

	contacts = _linked_contact_names(lead_name)
	if contacts:
		for value in frappe.get_all(
			"Contact Email", filters={"parent": ("in", contacts), "parenttype": "Contact"}, pluck="email_id"
		):
			remember_email(value)
		for value in frappe.get_all(
			"Contact Phone", filters={"parent": ("in", contacts), "parenttype": "Contact"}, pluck="phone"
		):
			remember_mobile(value)

	return emails, mobiles


def lead_had_contact_details(lead_name):
	"""Did the row in the database already carry a contact detail of its own?

	Read on the LEAD, not on its Contact: this answers "was this row ever within the rule
	this app adds", and the rule is about the Lead's own fields.

	Stock ERPNext is happy with a Lead that has a name and nothing else, and a client's book
	is full of them -- a company noted at a trade show, a referral with a callback promised.
	This app requires a contact detail, which is right for a Lead this app CREATES and wrong
	for one that was already there: applying it on every save made those rows permanently
	unsaveable, so a user could not so much as correct a spelling on them. Two of the four
	Leads on the review site were in exactly that state.
	"""
	if not lead_name:
		return False

	stored = frappe.db.get_value("Lead", lead_name, ["email_id", "mobile_no", "whatsapp_no"], as_dict=True)
	if not stored:
		return False

	if any(cstr(stored.get(fieldname)).strip() for fieldname in ("email_id", "mobile_no", "whatsapp_no")):
		return True

	if lead_has_contact_tables():
		for doctype in ("ERPNext Lead Email", "ERPNext Lead Mobile Number"):
			if frappe.db.exists(doctype, {"parent": lead_name, "parenttype": "Lead"}):
				return True

	return False


def newly_introduced_contacts(lead_name, email_ids, mobile_numbers):
	"""The subset of these values that this Lead does not already hold.

	A duplicate the Lead has carried since before this app was installed is a fact about the
	client's existing data, not a decision this save is making. Refusing the save does not
	undo it -- it only stops the row being edited at all.
	"""
	held_emails, held_mobiles = stored_contact_values(lead_name)

	def already_held_mobile(value):
		if cstr(value).strip() in held_mobiles:
			return True
		key = phone_match_key(value)
		return bool(key) and key in held_mobiles

	new_emails = [value for value in email_ids if cstr(value).strip().lower() not in held_emails]
	new_mobiles = [value for value in mobile_numbers if not already_held_mobile(value)]

	return new_emails, new_mobiles


# How many clashes the pre-flight report names before it stops counting. The report exists to
# tell a client whether installing this app will change how their Lead form behaves, and a
# hundred examples answer that as well as ten thousand do.
DUPLICATE_REPORT_LIMIT = 200


def _report_pairs(rows, normalise):
	"""Group by the comparison key, report the value a human typed.

	Phones are grouped by their match key, which is a canonical form nobody has ever seen on
	a business card (":2212345678"). A report naming that is a report nobody can act on, so
	the spellings actually stored are carried alongside and reported instead.
	"""
	grouped = {}

	for row in rows:
		lead = row.get("lead")
		raw = cstr(row.get("value")).strip()
		value = normalise(row.get("value"))
		if not lead or not value:
			continue
		entry = grouped.setdefault(value, {"leads": set(), "spellings": set()})
		entry["leads"].add(lead)
		if raw:
			entry["spellings"].add(raw)

	return grouped


def duplicate_contacts_report(limit=DUPLICATE_REPORT_LIMIT):
	"""Which Leads on THIS site already share an email address or a phone number.

	Run it before installing, with::

	    bench --site <site> execute ai_snaplead_connector.install.report_duplicate_contacts_preflight

	Installing this app puts a duplicate guard on Lead.validate, and that guard covers phone
	numbers, which stock ERPNext never deduplicated. Every Lead named here is one whose next
	save could be refused if the save introduces the shared value; pre-existing sharing is
	left alone, but a client is entitled to see the list rather than discover it one refused
	save at a time.

	Returns {"emails": [...], "mobiles": [...], "truncated": bool}, each entry
	{"value": ..., "leads": [...]}.
	"""
	lead = frappe.qb.DocType("Lead")
	contact_link = frappe.qb.DocType("Dynamic Link")

	email_rows = (
		frappe.qb.from_(lead)
		.select(lead.name.as_("lead"), lead.email_id.as_("value"))
		.where(lead.email_id.notnull())
		.where(lead.email_id != "")
	).run(as_dict=True)

	mobile_rows = (
		frappe.qb.from_(lead)
		.select(lead.name.as_("lead"), lead.mobile_no.as_("value"))
		.where(lead.mobile_no.notnull())
		.where(lead.mobile_no != "")
	).run(as_dict=True)

	for doctype, fieldname, sink in (
		("Contact Email", "email_id", email_rows),
		("Contact Phone", "phone", mobile_rows),
	):
		child = frappe.qb.DocType(doctype)
		sink += (
			frappe.qb.from_(child)
			.inner_join(contact_link)
			.on(contact_link.parent == child.parent)
			.select(contact_link.link_name.as_("lead"), child[fieldname].as_("value"))
			.where(child.parenttype == "Contact")
			.where(contact_link.parenttype == "Contact")
			.where(contact_link.link_doctype == "Lead")
		).run(as_dict=True)

	if lead_has_contact_tables():
		for doctype, fieldname, sink in (
			("ERPNext Lead Email", "email_id", email_rows),
			("ERPNext Lead Mobile Number", "mobile_number", mobile_rows),
		):
			child = frappe.qb.DocType(doctype)
			sink += (
				frappe.qb.from_(child)
				.select(child.parent.as_("lead"), child[fieldname].as_("value"))
				.where(child.parenttype == "Lead")
			).run(as_dict=True)

	emails = _report_pairs(email_rows, lambda value: cstr(value).strip().lower())
	mobiles = _report_pairs(mobile_rows, lambda value: phone_match_key(value) or cstr(value).strip())

	report = {"emails": [], "mobiles": [], "truncated": False}
	for key, grouped in (("emails", emails), ("mobiles", mobiles)):
		clashes = [
			{
				"value": " / ".join(sorted(entry["spellings"])) or value,
				"key": value,
				"leads": sorted(entry["leads"]),
			}
			for value, entry in sorted(grouped.items())
			if len(entry["leads"]) > 1
		]
		if len(clashes) > limit:
			report["truncated"] = True
			clashes = clashes[:limit]
		report[key] = clashes

	return report


def _stored_parent_contacts(doc):
	"""The email_id / mobile_no currently on the row in the database.

	None when the document is being inserted. Used to tell "the user edited the stock
	field" apart from "the stock field still holds the projection we wrote last save".
	"""
	if not doc.get("name"):
		return None

	return frappe.db.get_value(doc.doctype, doc.name, ["email_id", "mobile_no"], as_dict=True)


def _has_stored_child_rows(doc, table_fieldname):
	"""Does the database hold any child row for this table on this document?

	The only way to tell "the user just deleted every row of the grid" from "this Lead has
	never had a child row" -- they are the same document, an empty child list.
	"""
	if not doc.get("name"):
		return False

	field = doc.meta.get_field(table_fieldname)
	if not field or not field.options:
		return False

	return bool(
		frappe.db.exists(
			field.options,
			{"parent": doc.name, "parenttype": doc.doctype, "parentfield": table_fieldname},
		)
	)


def _adopt_one(doc, table_fieldname, row_fieldname, value, stored_value):
	"""Reconcile one stock contact field with its child table.

	Three cases, and the third is the one that has been wrong twice:

	* new document -- nothing stored yet, so the stock value is simply a contact to record;
	* stock field untouched (it still equals what is in the database) -- the child table is
	  authoritative, so re-adopting would resurrect a contact the user had just deleted.
	  This test comes BEFORE the empty-table test on purpose: emptying the grid is exactly
	  how the Desk deletes the last contact, and re-appending "because the table is empty"
	  undid that delete every time, which made the last row of either grid undeletable;
	* stock field carries a value the database does not hold -- somebody wrote it, and it
	  has to become the PRIMARY contact, because ``sync_legacy_parent_contact_fields``
	  projects child row 1 back over this field. Appending it at the end instead makes the
	  write silently revert.

	The promotion inserts; it does not overwrite row 1 by position. Overwriting row 1 was
	defensible for a Desk user editing the projection, but the same document shape arrives
	from any machine pusher that sanitizes its payload down to stock ERPNext fields, and for
	that caller it permanently deleted whichever contact happened to sit in row 1 -- on a
	write that never mentioned the child tables.

	The single exception is a Lead whose ONLY contact of this kind is the one the stock
	field was projecting. Then the field and the table describe the same single contact, so
	changing the field is a correction of it and the superseded value is dropped. Keeping it
	is not harmless: every stored value is a live claim in the duplicate guard, so a mistyped
	number left behind here permanently refuses the Lead that actually owns it, with a
	message naming a Lead whose visible number is something else.
	"""
	if not value:
		return False

	rows = doc.get(table_fieldname) or []
	values = [(row.get(row_fieldname) or "") for row in rows]
	is_new = stored_value is None

	if is_new:
		if value in values:
			return False

		doc.append(table_fieldname, {row_fieldname: value})
		return True

	if not rows and not _has_stored_child_rows(doc, table_fieldname):
		# A Lead that predates this app: it has a stock contact field and has never had a
		# child row, so this is its first adoption, not a deletion. Telling the two apart
		# needs the database -- an emptied grid and a never-populated one look identical on
		# the document -- and getting it wrong either resurrects a just-deleted contact or
		# blanks the stock field of every pre-install Lead on its first save.
		doc.append(table_fieldname, {row_fieldname: value})
		return True

	if stored_value == value:
		return False

	# NOTE: `stored_value` may legitimately be "" here -- that is exactly the state the
	# removed bulk rewrite (migrate_existing_lead_contacts) left every Lead in on an
	# already-damaged client. Requiring a non-empty stored value made this branch
	# unreachable for them, so their stock fields were editable but never actually changed.
	promoting_existing_row = value in values

	if promoting_existing_row:
		row = rows.pop(values.index(value))
	else:
		doc.append(table_fieldname, {row_fieldname: value})
		rows = doc.get(table_fieldname)
		row = rows.pop()

	# `rows` now holds everything except the row being promoted.
	supersedes_the_only_contact = (
		not promoting_existing_row
		and bool(stored_value)
		and len(rows) == 1
		and (rows[0].get(row_fieldname) or "") == stored_value
	)
	if supersedes_the_only_contact:
		rows.pop(0)

	# `rows` is the document's live child list, so these mutations land on the document.
	rows.insert(0, row)
	for idx, existing in enumerate(rows, start=1):
		existing.idx = idx

	return True


def adopt_legacy_parent_contact_fields(doc):
	"""Move standard Lead email_id / mobile_no into the custom child tables.

	Any client that does not know about this app - including ai_snaplead's connector, which
	sanitizes its outbound payload down to standard ERPNext fields - sends contact details in
	``email_id`` / ``mobile_no``. Without this the Lead was refused by
	``validate_contact_child_tables`` (HTTP 417) and, if it had been let through,
	``sync_legacy_parent_contact_fields`` had nothing to mirror back.

	This runs per-document, at save time, on documents that are being written anyway. It is
	deliberately NOT a bulk backfill: the install-time rewrite of every existing Lead was
	removed in this release.

	Returns True when the child tables were changed.
	"""
	if not lead_has_contact_tables():
		# Patch B has dropped them. The stock fields ARE the contact detail now, so there is
		# nothing to adopt into -- and ``Document.append`` on a fieldname the meta no longer
		# has raises, which would make every Lead on the site unsaveable.
		return False

	stored = _stored_parent_contacts(doc)

	adopted_email = _adopt_one(
		doc,
		"custom_email_ids",
		"email_id",
		(doc.get("email_id") or "").strip().lower(),
		None if stored is None else (stored.get("email_id") or "").strip().lower(),
	)
	adopted_mobile = _adopt_one(
		doc,
		"custom_mobile_numbers",
		"mobile_number",
		(doc.get("mobile_no") or "").strip(),
		None if stored is None else (stored.get("mobile_no") or "").strip(),
	)

	return adopted_email or adopted_mobile


def sync_legacy_parent_contact_fields(doc):
	"""Mirror the FIRST contact child row back into the standard Lead.email_id / mobile_no.

	This used to null both fields, which made the remote row depend on the transport the
	pusher happened to use: ai_snaplead's remote_erp_sync transport re-wrote them by raw SQL
	after save(), while a plain REST /api/resource create left the Lead with email_id = NULL
	and mobile_no = NULL. Besides being inconsistent, a NULL email_id:

	  * blanks Lead.email_id / Lead.mobile_no for every stock ERPNext list view, report,
	    contact-lookup and email workflow, and
	  * disables ERPNext's own Lead.check_email_id_is_unique guard, which is what let a
	    duplicated push create a second Lead silently instead of being refused.

	The child tables stay authoritative for multi-contact leads; these two fields are the
	single-value projection stock ERPNext reads.
	"""
	if not lead_has_contact_tables():
		# There is no table left to project FROM, and "no rows" must not be read as "no
		# contact": doing so would null email_id and mobile_no on every Lead saved after the
		# drop -- the exact bulk data loss this release removed from install.py.
		return

	emails = normalize_email_list(doc.get("custom_email_ids"))
	mobiles = normalize_mobile_list(doc.get("custom_mobile_numbers"))

	doc.email_id = emails[0] if emails else None
	doc.mobile_no = mobiles[0] if mobiles else None


# Backwards-compatible alias: the old name described the old (data-losing) behaviour.
clear_legacy_parent_contact_fields = sync_legacy_parent_contact_fields


def _contact_is_only_this_leads(contact_name, lead_name):
	"""True when this Contact hangs off THIS Lead and nothing else.

	A Contact reached from a Lead can also be linked to a Customer, a Supplier or a second
	Lead -- ERPNext keeps one Contact and adds Dynamic Link rows when a Lead is converted.
	Editing such a Contact from a Lead save would be editing the client's customer record,
	so a Contact with any other link is left completely alone.
	"""
	for row in frappe.get_all(
		"Dynamic Link",
		filters={"parent": contact_name, "parenttype": "Contact"},
		fields=["link_doctype", "link_name"],
		limit_page_length=0,
	):
		if row.link_doctype != "Lead" or row.link_name != lead_name:
			return False
	return True


def release_dropped_contact_values(doc):
	"""Keep a Lead's auto-created Contact from hoarding a value the Lead no longer has.

	THE BUG THIS CLOSES

	ERPNext builds a Contact from a Lead in ``Lead.before_insert`` and, as this app's own
	override already noted, "nothing re-syncs it afterwards". The duplicate guard, however,
	reads ``Contact Phone`` and ``Contact Email`` as well as the Lead's own tables. So a
	mistyped number corrected on the Lead stayed on the Contact for ever, and went on
	claiming that number: the Lead that really owned it was refused, with a message naming a
	Lead whose visible number is something else and offering no way to clear it. Correcting a
	typo was impossible on a pre-migration client -- which is most clients.

	WHY IT ONLY RUNS ON THE PRE-MIGRATION SHAPE

	Here the Lead's child tables are the whole record and the Contact is a derived copy, so
	the Lead can say what the Contact should hold. After the migration that is reversed: the
	CONTACT is the store, holding every email and phone from the scanned card, while the Lead
	keeps a single-value projection. Reconciling from the Lead there would delete the rest of
	the card, so this does nothing in that shape -- ``upsert_lead_graph`` owns the Contact.

	WHY A DIFF AND NOT A RESYNC

	Only what THIS SAVE dropped is removed, and only what this save added is added. Making
	the Contact equal the Lead would silently delete a second number somebody typed straight
	onto the Contact in the Desk -- a value the Lead never had and this save never touched.
	"""
	if not lead_has_contact_tables():
		return

	before = doc.get_doc_before_save()
	if not before:
		# An insert: ERPNext has just built the Contact from the reconciled fields.
		return

	current_emails = normalize_email_list(doc.get("custom_email_ids"))
	current_mobiles = normalize_mobile_list(doc.get("custom_mobile_numbers"))
	previous_emails = normalize_email_list(before.get("custom_email_ids"))
	previous_mobiles = normalize_mobile_list(before.get("custom_mobile_numbers"))

	current_mobile_keys = {phone_match_key(value) for value in current_mobiles}
	previous_mobile_keys = {phone_match_key(value) for value in previous_mobiles}

	dropped_emails = {value for value in previous_emails if value not in current_emails}
	dropped_mobile_keys = {key for key in previous_mobile_keys if key and key not in current_mobile_keys}
	added_emails = [value for value in current_emails if value not in previous_emails]
	added_mobiles = [value for value in current_mobiles if phone_match_key(value) not in previous_mobile_keys]

	if not (dropped_emails or dropped_mobile_keys or added_emails or added_mobiles):
		return

	for contact_name in _linked_contact_names(doc.name):
		if not _contact_is_only_this_leads(contact_name, doc.name):
			continue
		_apply_contact_diff(
			doc, contact_name, dropped_emails, dropped_mobile_keys, added_emails, added_mobiles
		)


def _apply_contact_diff(doc, contact_name, dropped_emails, dropped_mobile_keys, added_emails, added_mobiles):
	"""Remove what the Lead released and add what it gained, on one Contact."""
	contact = frappe.get_doc("Contact", contact_name)
	changed = False

	if dropped_emails:
		kept = [
			row
			for row in (contact.get("email_ids") or [])
			if cstr(row.email_id).strip().lower() not in dropped_emails
		]
		if len(kept) != len(contact.get("email_ids") or []):
			contact.set("email_ids", kept)
			changed = True

	if dropped_mobile_keys:
		kept = [
			row
			for row in (contact.get("phone_nos") or [])
			if phone_match_key(cstr(row.phone).strip()) not in dropped_mobile_keys
		]
		if len(kept) != len(contact.get("phone_nos") or []):
			contact.set("phone_nos", kept)
			changed = True

	held_emails = {cstr(row.email_id).strip().lower() for row in (contact.get("email_ids") or [])}
	for value in added_emails:
		if value not in held_emails:
			contact.append("email_ids", {"email_id": value})
			changed = True

	held_keys = {phone_match_key(cstr(row.phone).strip()) for row in (contact.get("phone_nos") or [])}
	for value in added_mobiles:
		if phone_match_key(value) not in held_keys:
			contact.append("phone_nos", {"phone": value})
			changed = True

	if not changed:
		return

	_point_primary_mobile_at(contact, doc.get("mobile_no"))
	_ensure_one_primary(contact.get("email_ids"), "is_primary")

	contact.flags.ignore_permissions = True
	# ignore_mandatory is NOT set: a Contact that cannot be saved is a real problem and
	# should surface, not be forced through.
	contact.save(ignore_permissions=True)


def _point_primary_mobile_at(contact, mobile_no):
	"""Make the Contact's primary mobile the number the Lead is projecting.

	This is not cosmetic, it is what stops the save undoing itself. ERPNext registers
	``update_lead_phone_numbers`` on Contact.validate (erpnext/crm/utils.py): saving a Contact
	writes ``phone_nos[0].phone`` -- or the row flagged ``is_primary_mobile_no`` -- straight
	back onto ``Lead.mobile_no`` with ``db_set``. Editing the Contact from a Lead save without
	this therefore REVERTED the Lead to whichever row happened to sit first, so a corrected
	number came back as the old one and the correction looked like it had never been made.
	"""
	rows = contact.get("phone_nos") or []
	if not rows:
		return

	wanted = phone_match_key(cstr(mobile_no).strip())
	match = None
	if wanted:
		match = next((row for row in rows if phone_match_key(cstr(row.phone).strip()) == wanted), None)

	target = match or rows[0]
	for row in rows:
		row.set("is_primary_mobile_no", 1 if row is target else 0)


def _ensure_one_primary(rows, fieldname):
	if not rows:
		return
	if any(row.get(fieldname) for row in rows):
		return
	rows[0].set(fieldname, 1)
