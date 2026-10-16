# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""The inventory of every Custom Field this app ever put on a DocType ERPNext owns.

One list, read by all four things that need to agree about it: the migration (Patch A),
the verification gate and archive (Patch B), the drop, and the restore script. When they
disagree the failure is silent -- a field migrated but not dropped, or dropped but not
archived -- so they share this module rather than each holding their own copy.

The end state this app is migrating towards is ONE custom field on Lead: ``custom_address``,
the raw scanned address block, which has no standard ERPNext home. Everything else maps onto
a standard field and is dropped.
"""

import frappe

# The one Custom Field that survives. Everything about the target end state is this line.
KEPT_LEAD_FIELD = "custom_address"

# Lead: the nine data-carrying fields that are migrated and dropped.
LEGACY_LEAD_DATA_FIELDS = (
	"custom_email_ids",
	"custom_mobile_numbers",
	"custom_business_id",
	"custom_pincode",
	"custom_scanned_text",
	"custom_created_from_scan",
	"custom_front_card_image",
	"custom_back_card_image",
	"custom_lead_activity_log",
)

# Lead: the four layout-only fields that anchored them. They carry no value, but leaving
# them behind leaves four empty sections on every client's Lead form for ever.
LEGACY_LEAD_LAYOUT_FIELDS = (
	"scanner_details_section",
	"card_images_section",
	"card_images_column_break",
	"activity_log_section",
)

# Lead: which of the nine are Table fields, and the child DocType + value column of each.
# Read when the row values are archived and when the restore script puts them back.
LEGACY_LEAD_TABLE_FIELDS = {
	"custom_email_ids": ("ERPNext Lead Email", ("email_id",)),
	"custom_mobile_numbers": ("ERPNext Lead Mobile Number", ("mobile_number",)),
	"custom_lead_activity_log": ("ERPNext Lead Activity Log", ("source", "activity_date", "user")),
}

# Lead Source / UTM Source: seven data fields and three layout fields.
LEGACY_SOURCE_DATA_FIELDS = (
	"custom_business_id",
	"custom_status",
	"custom_is_default",
	"custom_created_by_user",
	"custom_text_messages",
	"custom_whatsapp_messages",
	"custom_email_messages",
)

LEGACY_SOURCE_LAYOUT_FIELDS = (
	"text_message_templates_section",
	"whatsapp_message_templates_section",
	"email_message_templates_section",
)

LEGACY_SOURCE_TABLE_FIELDS = {
	"custom_text_messages": ("Lead Source Text Message", None),
	"custom_whatsapp_messages": ("Lead Source WhatsApp Message", None),
	"custom_email_messages": ("Lead Source Email Message", None),
}

# The Property Setters install.py used to write, hiding stock Lead.email_id / mobile_no.
# `restore_stock_lead_contact_fields` already removes these; Patch B re-asserts it, because
# a client may have re-run an old install.py between the two patches.
HIDDEN_STOCK_FIELDS = ("email_id", "mobile_no")
HIDDEN_STOCK_PROPERTIES = ("hidden", "read_only")

# The UTM Source / Lead Source record that custom_created_from_scan becomes.
SCAN_SOURCE_NAME = "Business Card Scan"

# Marks the CRM Note row that custom_scanned_text became. Invisible in the rendered note and
# unambiguous in a LIKE query, which is what makes the note write idempotent: a re-run finds
# its own tag instead of appending a second copy of the same scan.
# The title suffix Patch A puts on an Address it had to CREATE because the Lead already had
# one with a different pincode. Shared with the live endpoint: that endpoint has to be able to
# tell such a row apart from the client's own address, or its next ordinary push overwrites
# the very row the conflict rule created to protect the scanned pincode.
SCANNED_ADDRESS_SUFFIX = " (Scanned)"

# Address.address_title is 100 characters. Appending the suffix and then truncating to 100 is
# how the marker gets cut off a long company name -- and a scanned address the endpoint cannot
# recognise is one it will overwrite. So the TITLE is truncated, never the suffix.
ADDRESS_TITLE_LIMIT = 100


def scanned_address_title(title):
	"""``title`` with the scanned suffix, in a form that always still carries the suffix."""
	title = (title or "").strip()[: ADDRESS_TITLE_LIMIT - len(SCANNED_ADDRESS_SUFFIX)]
	return f"{title}{SCANNED_ADDRESS_SUFFIX}"


def is_scanned_address_title(title):
	"""Did the migration create this Address to hold a pincode it refused to overwrite?"""
	return (title or "").strip().endswith(SCANNED_ADDRESS_SUFFIX.strip())

SCANNED_TEXT_TAG = "<!--vc-connector:scanned_text-->"

# Marks the CRM Note that carries a contact detail ERPNext refuses to store on a Contact --
# "9876543210 (O)", "080-25551234 Ext 22", an address that is not a valid email. The value is
# written down as it was printed on the card, because after the drop there is no Custom Field
# left holding it and the archive is not somewhere a salesperson looks. A separate tag from
# SCANNED_TEXT_TAG on purpose: the verification gate asks about each independently, and one
# note must never stand in for the other.
UNSTORABLE_CONTACT_TAG = "<!--vc-connector:unstorable_contact-->"

ARCHIVE_DOCTYPE = "Connector Legacy Field Archive"


def get_source_doctype():
	"""ERPNext v16 dropped Lead Source in favour of UTM Source; a client may be on either."""
	return "Lead Source" if frappe.db.exists("DocType", "Lead Source") else "UTM Source"


def get_lead_source_fieldname():
	"""v16 Lead has ``utm_source``; older ERPNext has ``source``."""
	return "source" if frappe.get_meta("Lead").has_field("source") else "utm_source"


def lead_has_legacy_fields():
	"""True while ANY of the nine still exists on Lead.

	Everything in this app that used to assume the child tables were there asks this first.
	After Patch B they are gone, and code that keeps reading them would either crash on
	``Document.append`` or -- worse -- match a duplicate against orphaned child rows that
	no form, list view or report can show any more.
	"""
	meta = frappe.get_meta("Lead")
	return any(meta.has_field(fieldname) for fieldname in LEGACY_LEAD_DATA_FIELDS)


def lead_has_contact_tables():
	"""True while the two contact child tables are still on Lead."""
	meta = frappe.get_meta("Lead")
	return meta.has_field("custom_email_ids") and meta.has_field("custom_mobile_numbers")


def existing_lead_fields(fieldnames):
	"""Those of ``fieldnames`` Lead actually has right now.

	A client may have deleted one by hand, and a half-dropped set must not make the whole
	migration raise on a SELECT naming a column that is not there.
	"""
	meta = frappe.get_meta("Lead")
	return [fieldname for fieldname in fieldnames if meta.has_field(fieldname)]
