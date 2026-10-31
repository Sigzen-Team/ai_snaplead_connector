# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""The Custom Field definitions, exactly as this app has always written them.

One copy, read by three callers that must not drift apart:

* ``install.py`` -- creates them on a client who has not migrated yet;
* ``sigzenlead_connector.legacy.restore`` -- puts them BACK, with their values, if a client wants the
  drop undone. A restore that re-created a field with a different fieldtype or a different
  child DocType would hand the client back a table their old data does not fit;
* ``sigzenlead_connector.legacy.drop`` -- knows which fields to remove by fieldname from
  ``legacy.schema``, and asserts against these definitions that it is not leaving one behind.

``custom_address`` is listed separately: it is the one field that is neither migrated nor
dropped, so install.py keeps creating it after the migration is finished.
"""

KEPT_LEAD_FIELD_DEFINITION = {
	"fieldname": "custom_address",
	"label": "Address",
	"fieldtype": "Small Text",
	"insert_after": "country",
	"reqd": 0,
}


def lead_legacy_fields(source_fieldname):
	"""The nine data fields and four layout fields this app put on Lead.

	``source_fieldname`` is ``source`` on ERPNext before v16 and ``utm_source`` from v16 on;
	``custom_business_id`` was always anchored next to whichever the client has.
	"""
	return [
		{
			"fieldname": "custom_email_ids",
			"label": "Email IDs",
			"fieldtype": "Table",
			"options": "ERPNext Lead Email",
			"insert_after": "contact_info_tab",
			"reqd": 0,
		},
		{
			"fieldname": "custom_mobile_numbers",
			"label": "Mobile Numbers",
			"fieldtype": "Table",
			"options": "ERPNext Lead Mobile Number",
			"insert_after": "custom_email_ids",
			"reqd": 0,
		},
		{
			"fieldname": "custom_business_id",
			"label": "Business ID",
			"fieldtype": "Data",
			"insert_after": source_fieldname,
			"read_only": 1,
			"reqd": 0,
		},
		{
			"fieldname": "custom_pincode",
			"label": "Pincode",
			"fieldtype": "Data",
			"insert_after": "custom_address",
			"reqd": 0,
		},
		{
			"fieldname": "scanner_details_section",
			"label": "Scanner Details",
			"fieldtype": "Section Break",
			"insert_after": "custom_pincode",
			"reqd": 0,
		},
		{
			"fieldname": "custom_scanned_text",
			"label": "Scanned Text",
			"fieldtype": "Long Text",
			"insert_after": "scanner_details_section",
			"reqd": 0,
		},
		{
			"fieldname": "custom_created_from_scan",
			"label": "Created From Scan",
			"fieldtype": "Check",
			"insert_after": "custom_scanned_text",
			"reqd": 0,
		},
		{
			"fieldname": "card_images_section",
			"label": "Card Images",
			"fieldtype": "Section Break",
			"insert_after": "custom_created_from_scan",
			"reqd": 0,
		},
		{
			"fieldname": "custom_front_card_image",
			"label": "Front Card Image",
			"fieldtype": "Attach Image",
			"insert_after": "card_images_section",
			"reqd": 0,
		},
		{
			"fieldname": "card_images_column_break",
			"fieldtype": "Column Break",
			"insert_after": "custom_front_card_image",
			"reqd": 0,
		},
		{
			"fieldname": "custom_back_card_image",
			"label": "Back Card Image",
			"fieldtype": "Attach Image",
			"insert_after": "card_images_column_break",
			"reqd": 0,
		},
		{
			"fieldname": "activity_log_section",
			"label": "Activity Log",
			"fieldtype": "Section Break",
			"insert_after": "custom_back_card_image",
			"reqd": 0,
		},
		{
			"fieldname": "custom_lead_activity_log",
			"label": "Lead Activity Log",
			"fieldtype": "Table",
			"options": "ERPNext Lead Activity Log",
			"insert_after": "activity_log_section",
			"reqd": 0,
		},
	]


def source_legacy_fields(is_utm):
	"""The seven data fields and three layout fields on Lead Source / UTM Source."""
	return [
		{
			"fieldname": "custom_business_id",
			"label": "Business ID",
			"fieldtype": "Data",
			"insert_after": "description" if is_utm else "source_name",
			"read_only": 1,
			"reqd": 0,
		},
		{
			"fieldname": "custom_status",
			"label": "Status",
			"fieldtype": "Select",
			"options": "Active\nInactive",
			"insert_after": "custom_business_id",
			"read_only": 0,
			"reqd": 0,
		},
		{
			"fieldname": "custom_is_default",
			"label": "Is Default",
			"fieldtype": "Check",
			"insert_after": "custom_status",
			"read_only": 0,
			"reqd": 0,
		},
		{
			"fieldname": "custom_created_by_user",
			"label": "Created By User",
			"fieldtype": "Link",
			"options": "User",
			"insert_after": "custom_is_default",
			"read_only": 0,
			"reqd": 0,
		},
		{
			"fieldname": "text_message_templates_section",
			"label": "Text Message Templates",
			"fieldtype": "Section Break",
			"insert_after": "custom_created_by_user" if is_utm else "details",
			"reqd": 0,
		},
		{
			"fieldname": "custom_text_messages",
			"label": "Text Messages",
			"fieldtype": "Table",
			"options": "Lead Source Text Message",
			"insert_after": "text_message_templates_section",
			"reqd": 0,
		},
		{
			"fieldname": "whatsapp_message_templates_section",
			"label": "WhatsApp Message Templates",
			"fieldtype": "Section Break",
			"insert_after": "custom_text_messages",
			"reqd": 0,
		},
		{
			"fieldname": "custom_whatsapp_messages",
			"label": "WhatsApp Messages",
			"fieldtype": "Table",
			"options": "Lead Source WhatsApp Message",
			"insert_after": "whatsapp_message_templates_section",
			"reqd": 0,
		},
		{
			"fieldname": "email_message_templates_section",
			"label": "Email Message Templates",
			"fieldtype": "Section Break",
			"insert_after": "custom_whatsapp_messages",
			"reqd": 0,
		},
		{
			"fieldname": "custom_email_messages",
			"label": "Email Messages",
			"fieldtype": "Table",
			"options": "Lead Source Email Message",
			"insert_after": "email_message_templates_section",
			"reqd": 0,
		},
	]
