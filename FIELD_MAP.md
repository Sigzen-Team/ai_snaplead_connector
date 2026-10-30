# Field map

Two different maps live in this file and they must not be confused:

1. **The protocol map** — what a central site sends over `vc-connector/1`, and which ERPNext
   field each key lands in. This is the contract a central site codes against. It is stable:
   the protocol deliberately does not expose raw ERPNext fieldnames, so renaming a field on
   this side does not break a central site in the field.
2. **The migration map** — every Custom Field earlier releases of this app put on a DocType
   ERPNext owns, and which *standard* field replaced it. Read this one if you are upgrading a
   site that ran an older release.

Both are generated from the code that actually performs the mapping:
`sigzenlead_connector/api/v1/sync.py` (`LEAD_FIELD_MAP`, `CONTACT_SCHEMA`, `ADDRESS_SCHEMA`,
`ATTACHMENT_KINDS`) and `sigzenlead_connector/legacy/schema.py` + `sigzenlead_connector/legacy/migrate.py`.

---

## 1. The protocol map

### Top level

| Payload key | Required | Lands on |
| --- | --- | --- |
| `protocol` | yes | Not stored. Checked against `SUPPORTED_PROTOCOLS`; a mismatch is `PROTOCOL_UNSUPPORTED`. |
| `request_id` | yes | `Connector Lead Link.last_request_id`, and `Connector Log.request_id`. Replay key. |
| `business_id` | yes | Checked against `Connector Settings.business_id`; written to `Connector Lead Link.business_id` (and to `Lead.custom_business_id` while that field still exists). |
| `vc_uuid` | yes | `Connector Lead Link.vc_uuid` — UNIQUE. This is the identity of the record. |
| `legacy_remote_name` | no | Not stored. The `Lead.name` this record had before the protocol existed; used once, to adopt that Lead instead of creating a duplicate. |
| `lead` | yes | See below. |
| `contact` | no | See below. |
| `address` | no | See below. |
| `attachments` | no | See below. |

Any key not in this table is **rejected** with `VALIDATION_FAILED` naming the key. It is never
silently dropped.

### `lead.*` → `Lead`

| Payload key | ERPNext field | Notes |
| --- | --- | --- |
| `first_name` | `Lead.first_name` | |
| `middle_name` | `Lead.middle_name` | |
| `last_name` | `Lead.last_name` | |
| `lead_name` | `Lead.lead_name` | Filled from a declared default if the payload leaves it, `first_name` and `company_name` all empty; the default is reported back in `defaults_applied`. |
| `salutation` | `Lead.salutation` | |
| `gender` | `Lead.gender` | |
| `job_title` | `Lead.job_title` | |
| `company_name` | `Lead.company_name` | |
| `website` | `Lead.website` | |
| `status` | `Lead.status` | |
| `territory` | `Lead.territory` | |
| `industry` | `Lead.industry` | |
| `market_segment` | `Lead.market_segment` | |
| `city` | `Lead.city` | |
| `state` | `Lead.state` | Normalised through `utils.india_state.canonical_state`. |
| `country` | `Lead.country` | |
| `whatsapp_no` | `Lead.whatsapp_no` | |
| `source` | `Lead.utm_source` **or** `Lead.source` | Resolved at runtime. ERPNext v16 renamed the field; `ping()` reports which one this site has as `lead_source_field`. |
| `address_text` | `Lead.custom_address` | The one Custom Field this app keeps. Raw scanned address block; it has no standard ERPNext home. Central sends Line 1 and Line 2 joined with ", ". |
| `pincode` | `Lead.custom_pincode`, **or** `Address.pincode` | While the legacy field exists it is written there. Once it has been dropped the value is routed to the Lead's Address — preferring the address the payload is already writing, otherwise via `ensure_pincode_on_address`, which never overwrites a different pincode a human typed. |
| `scanned_text` | `Lead.custom_scanned_text`, **or** a tagged `CRM Note` | Same rule: the Custom Field while it exists, a tagged note on the Lead once it is gone. |
| `created_from_scan` | `Lead.custom_created_from_scan`, **or** `Lead.utm_source` | Once the field is dropped, a true value sets `utm_source` to `Business Card Scan` **only if it is empty**; an existing attribution is kept and reported in `warnings`. |

### `lead.emails[]` and `lead.phones[]`

These are lists, not scalars, and they are the reason this app exists — a scanned card carries
more than one of each and stock `Lead` has room for exactly one.

| Payload key | Lands on | Notes |
| --- | --- | --- |
| `lead.emails[]` | `Contact.email_ids[].email_id` | Full list. Index 0 gets `is_primary = 1`. Normalised and deduped by `utils.phone.dedupe_emails`. |
| `lead.phones[]` | `Contact.phone_nos[].phone` | Full list. Index 0 gets `is_primary_phone` and `is_primary_mobile_no`. Deduped on `phone_match_key`, which includes the country code. |
| `lead.emails[0]` | `Lead.email_id` | The single-value projection stock ERPNext list views, reports and email workflows read. |
| `lead.phones[0]` | `Lead.mobile_no` | Same. |
| `lead.emails[]` | `Lead.custom_email_ids[]` | **Only while that legacy child table still exists.** After the drop the Contact holds the full list and the two stock fields hold the projection. |
| `lead.phones[]` | `Lead.custom_mobile_numbers[]` | Same. |

A key that is **absent** is not the same as a key sent empty: an absent `emails` leaves what is
already stored alone, an empty list clears it.

### `contact.*` → `Contact`

The Contact ERPNext itself created in `Lead.before_insert` is **enriched**, never duplicated.
Each key falls back to the equivalent value on the Lead when the payload omits it.

| Payload key | ERPNext field | Falls back to |
| --- | --- | --- |
| `first_name` | `Contact.first_name` | `Lead.first_name`, then `Lead.lead_name`, then `Lead.name` |
| `middle_name` | `Contact.middle_name` | `Lead.middle_name` |
| `last_name` | `Contact.last_name` | `Lead.last_name` |
| `salutation` | `Contact.salutation` | `Lead.salutation` |
| `gender` | `Contact.gender` | `Lead.gender` |
| `designation` | `Contact.designation` | `Lead.job_title` |
| `company_name` | `Contact.company_name` | `Lead.company_name` |

The Contact is linked back with a `Dynamic Link` row (`link_doctype = "Lead"`).

### `address.*` → `Address`

| Payload key | ERPNext field | Notes |
| --- | --- | --- |
| `address_title` | `Address.address_title` | Truncated to 100 characters. |
| `address_type` | `Address.address_type` | Defaults to `Billing`, reported in `defaults_applied`. |
| `address_line1` | `Address.address_line1` | **Mandatory** when an address block is sent at all; otherwise `MANDATORY_MISSING`. |
| `address_line2` | `Address.address_line2` | Since 2026-10 central sends it from its own Frappe Lead field (`address_line_1` / `address_line_2` are separate there); an empty line 2 is omitted, so a stored one is kept. No protocol change. |
| `city` | `Address.city` | Defaults to `-`, reported in `defaults_applied`. |
| `state` | `Address.state` | Normalised; may be derived from the pincode by `derive_indian_state`. |
| `country` | `Address.country` | Defaults to `India`, reported in `defaults_applied`. |
| `pincode` | `Address.pincode` | Normalised by `normalize_pincode`. **Omitting the key keeps the stored value**; it does not clear it. |

The Address is linked back with a `Dynamic Link` row. A push never writes to an Address whose
title ends in `(Scanned)` unless that is the only address the Lead has — that row exists
precisely to hold a value the migration refused to overwrite.

### `attachments[]` → `File`

| Payload key | Lands on |
| --- | --- |
| `file_name` | `File.file_name` (defaults to `<vc_uuid>-<index>`) |
| `content_base64` | the file bytes; invalid base64 is `VALIDATION_FAILED`, not a retry |
| `is_private` | `File.is_private` (default `1`) |
| `kind: "front_card"` | `File.attached_to_field = custom_front_card_image`, while that field exists |
| `kind: "back_card"` | `File.attached_to_field = custom_back_card_image`, while that field exists |

Once the card Custom Fields have been dropped the File row is still attached to the Lead, with
no `attached_to_field` — which is exactly the shape the migration re-parented the old ones
into. Bytes already attached to this Lead are **not** re-attached, so an identical push adds no
File row.

---

## 2. The migration map: which standard field replaced each Custom Field

Earlier releases of this app put 10 Custom Fields + 4 layout fields on `Lead`, and 7 + 3 on
`Lead Source` / `UTM Source`. The target end state is **one**: `custom_address`.

### `Lead` — data fields

| Removed Custom Field | Replaced by | Performed by |
| --- | --- | --- |
| `custom_email_ids[]` (Table, `ERPNext Lead Email`) | `Contact.email_ids[]` | Patch A, `migrate_lead_emails` |
| `custom_mobile_numbers[]` (Table, `ERPNext Lead Mobile Number`) | `Contact.phone_nos[]`, deduped on `phone_match_key` | Patch A |
| either of the two, in a form ERPNext refuses to store | a tagged `CRM Note` on the Lead, written **as printed** | Patch A, logged `CONTACT_VALUE_UNSTORABLE` |
| `custom_business_id` (Data) | `Connector Lead Link.business_id`, with `vc_uuid` left NULL until the first push | Patch A |
| `custom_pincode` (Data) | `Address.pincode`, on the Address found via Dynamic Link | Patch A, `ensure_pincode_on_address` |
| `custom_scanned_text` (Long Text) | a tagged `CRM Note` on the Lead | Patch A, `write_scanned_note` |
| `custom_created_from_scan` (Check) | `Lead.utm_source` = `Business Card Scan`, **only when empty** | Patch A, `set_scan_source_if_empty` |
| `custom_front_card_image` (Attach Image) | a standard `File` attachment on the Lead | Patch A, re-parented, never re-uploaded |
| `custom_back_card_image` (Attach Image) | a standard `File` attachment on the Lead | Patch A |
| `custom_lead_activity_log[]` (Table, `ERPNext Lead Activity Log`) | **nothing.** There is no standard ERPNext counterpart. It survives only in the Patch B archive. | — |
| `custom_address` (Small Text) | **not removed.** The raw scanned address block has no standard home, so this one field stays. | — |

### `Lead` — layout-only fields

These carry no value. They are removed so the client is not left with four empty sections on
every Lead form for ever.

| Removed | Replaced by |
| --- | --- |
| `scanner_details_section` (Section Break) | nothing — layout only |
| `card_images_section` (Section Break) | nothing — layout only |
| `card_images_column_break` (Column Break) | nothing — layout only |
| `activity_log_section` (Section Break) | nothing — layout only |

### `Lead Source` / `UTM Source` — data fields

**None of these seven has a standard ERPNext replacement.** They were product-specific
additions. Patch B archives every value into `Connector Legacy Field Archive` on the client's own
site and then drops the fields; nothing is migrated onto a standard field, because there is no
standard field to migrate them onto.

| Removed Custom Field | Replaced by |
| --- | --- |
| `custom_business_id` (Data) | nothing — archive only |
| `custom_status` (Select: Active/Inactive) | nothing — archive only |
| `custom_is_default` (Check) | nothing — archive only |
| `custom_created_by_user` (Link → User) | nothing — archive only. ERPNext's own `owner` is the nearest equivalent and is not written by the migration. |
| `custom_text_messages[]` (Table, `Lead Source Text Message`) | nothing — archive only |
| `custom_whatsapp_messages[]` (Table, `Lead Source WhatsApp Message`) | nothing — archive only |
| `custom_email_messages[]` (Table, `Lead Source Email Message`) | nothing — archive only |

### `Lead Source` / `UTM Source` — layout-only fields

| Removed | Replaced by |
| --- | --- |
| `text_message_templates_section` | nothing — layout only |
| `whatsapp_message_templates_section` | nothing — layout only |
| `email_message_templates_section` | nothing — layout only |

### Property Setters

| Removed | Effect |
| --- | --- |
| `Lead-email_id-hidden`, `Lead-email_id-read_only` | The stock `Lead.email_id` field becomes visible and editable again. |
| `Lead-mobile_no-hidden`, `Lead-mobile_no-read_only` | The stock `Lead.mobile_no` field becomes visible and editable again. |

Removed by `patches/v1_0/restore_stock_lead_contact_fields.py`, and re-asserted by Patch B in
case a client re-ran an older `install.py` between the two.

---

## Undoing it

Every dropped value is archived first, one JSON row per record in `Connector Legacy Field Archive`,
on the client's own site. `sigzenlead_connector.legacy.restore.restore` re-creates the Custom Fields
and writes the values back. See the *Undo* section of `README.md`, including why
`records_restored: 0` immediately after the drop is the expected result and not a failure.
