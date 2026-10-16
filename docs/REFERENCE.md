# AI SnapLead - Connector: technical reference

The full reference for administrators and integrators. For what the app is and how to set it
up, start with the [README](../README.md).

The client-side half of a business-card lead pipeline. It installs on a **client's own**
ERPNext v16 site and gives a central lead-capture site somewhere to push scanned leads and
their sources to, over one authenticated, versioned, atomic endpoint.

It is a marketplace-shaped app: it installs onto a site whose data belongs to somebody else,
and nearly everything below follows from that one fact.

* Protocol version: **`vc-connector/1`**
* Full field map: **[FIELD_MAP.md](../FIELD_MAP.md)**
* Release history and tested versions: **[CHANGELOG.md](../CHANGELOG.md)**

## Which way the traffic goes

**This app never contacts anyone.** It opens no outbound connection, registers no scheduled
task, and holds no URL that it dials. Verified by inspection of the whole package: there is no
import of `requests`, `urllib`, `httpx`, `socket` or `http.client`, no call to
`frappe.make_get_request` / `make_post_request`, no webhook and no `sendmail`; and `hooks.py`
declares no `scheduler_events`, `before_request`, `after_request` or `auth_hooks`.

All traffic is inbound. The central site is the client; your ERPNext is the server:

```
   central site  ── HTTPS POST ──▶  your ERPNext  ──▶  Lead / Contact / Address / File
                 ◀── JSON reply ──
```

Two consequences worth stating plainly to whoever approves the install:

* your firewall needs **no outbound rule** for this app, and keeping the site behind one
  changes nothing about how it works;
* the app cannot send your Leads anywhere of its own accord. The only way data leaves this
  site is in the reply to a request that has already authenticated with **this site's own**
  credentials — see `pull_changes` below, which a caller has to ask for, one page at a time.

The `Central Site` field in Settings is a **note to a human** recording which central instance
this site is paired with. Nothing reads it to make a request. See *Settings reference* for it
and for the one other field that is currently inert.

## Compatibility

| | |
| --- | --- |
| Frappe | **16** (verified on 16.25.0 and 16.26.3) |
| ERPNext | **16** (verified on 16.26.2 and 16.27.0) — required; installation is refused without it |
| Python | 3.10+ (`requires-python` in `pyproject.toml`) |
| Database | Verified on **MariaDB 10.11** and on **SQLite** |

ERPNext is a hard dependency, not an optional integration: this app writes `Lead`, `Contact`
and `Address`, and `install.py:check_erpnext_installed` throws if `erpnext` is not among the
installed apps.

The full suite was run on both databases, including a fresh install on a new MariaDB site,
which is what Frappe Cloud runs. `install.py:ensure_indexes` exists because SQLite does not
materialise a `search_index` during the `CREATE TABLE` of a fresh install; on MariaDB the model
sync has already built those indexes and the function leaves them alone. PostgreSQL has not
been exercised.

## What installing does not do to your data

This is the claim a client's administrator actually needs, so it is stated on its own:

> **Installing this app does not create, modify or delete a single existing `Lead`,
> `Contact` or `Address`. `bench migrate` does not either — with one exception, and only on
> a client that already carries this app's OLD Custom Fields: the legacy field migration
> described at the end of this document. It is registered in `patches.txt`, so `bench
> migrate` is what starts it, and it does write Leads, Contacts, Addresses and CRM Notes. On
> a site that never had those Custom Fields — every new client — it is a no-op and nothing is
> touched.**

That exception is stated inside the claim rather than four paragraphs below it, because the
blockquote is the sentence an administrator is told to read on its own. What the migration
does, what it archives and how to undo it is in
[The legacy Custom Field migration](#the-legacy-custom-field-migration); it is resumable,
archived and reversible, and it loses nothing.

`after_install` and `after_migrate` call the same function, `install.setup_connector_fields`,
and everything it does is additive: it checks ERPNext is present, attaches this app's Custom
Fields to `Lead` (and, before the legacy migration has run, to `Lead Source` / `UTM Source`),
asserts a few secondary indexes, and clears the cache. It reads no Lead and saves no Lead.

Two behaviours that **were** in earlier releases were removed and must never come back:

* `hide_legacy_lead_contact_fields()` wrote Property Setters setting `hidden = 1` and
  `read_only = 1` on the stock `Lead.email_id` and `Lead.mobile_no` fields. Those fields are
  ERPNext's, not this app's; hiding them breaks stock list views, reports, contact lookups
  and email workflows on a site whose owner never asked for it;
* `migrate_existing_lead_contacts()` loaded and re-saved **every** `Lead` on the client with
  `ignore_validate`, moved `email_id` / `mobile_no` into child tables and then nulled the
  stock columns — on install *and* on every single `bench migrate`.

`patches/v1_0/restore_stock_lead_contact_fields.py` removes the Property Setters the old code
wrote, so a client who already has them gets the stock fields back on the next `bench migrate`.
Two tests pin the absence of both behaviours: `test_schema.test_install_does_not_hide_stock_lead_contact_fields`
and `test_schema.test_install_module_has_no_bulk_record_rewrite`.

Contact details are now adopted **per document, at save time**, only for documents that are
being written anyway, by `utils.lead_contacts.adopt_legacy_parent_contact_fields`; and
`sync_legacy_parent_contact_fields` mirrors the first child row back onto the stock fields so
they stay populated and usable.

The one thing that *does* rewrite existing Leads is the opt-in legacy Custom Field migration,
which is a separate, resumable, archived and reversible operation described at the end of this
document. It runs as a patch on a site that has this app's old Custom Fields on it; on a site
that never had them it is a no-op.

## Install

```bash
bench get-app ai_snaplead_connector <repository-url>
bench --site <site> install-app ai_snaplead_connector
bench --site <site> migrate
```

ERPNext must already be installed; `install.py:check_erpnext_installed` refuses otherwise.

A fresh install leaves **one** Custom Field on `Lead` — `custom_address` — and none at all on
`UTM Source`. Nothing else is added to a DocType ERPNext owns, no patch has to be run by hand,
and `Connector Settings.legacy_migration_message` says so in as many words. (Earlier
versions of this app did not manage that: `install-app` writes a `Patch Log` row for every
patch in `patches.txt` before it calls `after_install`, so the two legacy patches were
recorded as executed without having run, `legacy_fields_dropped` was never set, and a brand
new client got the full historical set — fourteen Custom Fields and four empty sections on
`Lead`, ten more on `UTM Source` — recreated on install and on every later migrate.
`install.settle_install_time_migration_state` is what decides this now; see
[The legacy Custom Field migration](#the-legacy-custom-field-migration).)

#### Two things to check before the first push

1. **An `Address Template`.** ERPNext refuses to save *any* `Address` on a site that has none,
   and a site created without the setup wizard — every developer site, every Frappe Cloud
   restore, and some real clients — has none. Any push carrying an `address` block is then
   refused with `SETUP_REQUIRED` (409, **retryable**) and nothing is written, so no record is
   lost; but every such push keeps failing until somebody creates one at
   *Setup > Printing and Branding > Address Template*. `ping()` reports the gap as
   `default_address_template: false`, so the central site can see it before it pushes ten
   thousand records. This app deliberately does not create one: a marketplace app does not
   invent print formats on a stranger's site.
2. **`Connector Settings`**, below — `business_id`, `enabled` and `token_hash`. Until
   `business_id` is set the site has no tenancy at all.

### Settings reference

`Connector Settings` is a Single DocType, reachable from the Desk by that name. The
connector refuses every push until the first three fields are set.

| Field | Required | What it does |
| --- | --- | --- |
| `business_id` | **yes** | The business this ERP site speaks for. Checked against every payload at the door; a payload naming another business is refused with `BUSINESS_MISMATCH`. **Write-once** — re-pointing a configured site at a different business would silently re-tenant every row already mapped through `Connector Lead Link`, so the field refuses to change once set. |
| `enabled` | **yes** | The off switch. While it is unticked every push is refused with `CONNECTOR_DISABLED` and nothing is written. |
| `token_hash` | **yes** | SHA-256 of the shared connector token. The token itself is never stored. See *The connector token*. |
| `reject_business_mismatch` | recommended | On (the normal case) this site holds exactly one business. Off means you are deliberately keeping several businesses in one Lead table, which changes how duplicate protection is scoped — see *Duplicate protection*. |
| `central_site` | no | A note for your own reference: which central site this ERP is paired with. Nothing reads it; pairing is decided by `business_id` and the connector token. |
| `last_request_on` | read-only | Stamped after each successful push. The quickest way to answer "is the central site actually reaching us?". |

The `Legacy Field Migration` fields below those are entirely read-only progress reporting for
the one-time Custom Field migration.

To set them from the shell:

```bash
bench --site <site> console
```

```python
import frappe
s = frappe.get_single("Connector Settings")
s.business_id = "ACME-001"        # write-once: choose carefully
s.enabled = 1
s.token_hash = "<the sha256 from the next section>"
s.reject_business_mismatch = 1
s.save()
frappe.db.commit()
```

### The connector token

Authentication is **two factors**, and they prove different things:

1. an ordinary ERPNext **API key / secret** for an integration user on this site. That
   identifies *a user*. It says nothing about being the central site — API keys are issued
   for integrations of every kind and are frequently reused;
2. the **connector token**, a secret shared with exactly one central instance at onboarding,
   sent in the `X-VC-Connector-Token` header. That identifies *the central site*.

Presenting both says "a user of this site, acting as our central instance". Either one alone
is not enough to write a whole lead graph with permissions bypassed.

Only the SHA-256 of the token is stored, and the comparison is constant-time: a plain `==` on
a secret leaks its prefix through timing to anyone who can reach the endpoint at all.

**To obtain one**, generate it wherever the two sides agree to, then give the *token* to the
central site and the *hash* to the client. One way, using only what ships with the bench:

```bash
python3 -c "import secrets, hashlib; t = secrets.token_urlsafe(32); print('token  ->', t); print('sha256 ->', hashlib.sha256(t.encode()).hexdigest())"
```

Put the `sha256` line into `Connector Settings.token_hash` as shown above, and give the
`token` line to the central site to send as `X-VC-Connector-Token`. Rotating the token is the
same procedure; there is no grace period, so the two sides switch together.

`ping()` reports `connector_token_configured` as a boolean, so the central site can tell an
unconfigured client from a wrong token without either side printing a secret.

### Required roles

The endpoint writes as the authenticated integration user, with `ignore_permissions` only
after checking that same user really holds the rights. It needs `create` and `write` on
`Lead`, `Contact` and `Address`, and `create` on `Connector Lead Link`. A user without them gets a
`REMOTE_PERMISSION` 403 naming the missing right and the DocType. **The connector will not
fall back to writing as Administrator.**

Give the integration user **two** roles:

| Role | Grants | Who ships it |
| --- | --- | --- |
| whatever sales role the client already uses — `Sales User` in stock ERPNext | `create` / `write` on `Lead`, `Contact`, `Address` | ERPNext |
| **`Connector Integration`** | `read` / `write` / `create` on `Connector Lead Link` and `Connector Source Link` | **this app**, created by `install.ensure_integration_role` on install and re-asserted on every `bench migrate` |

`System Manager` also works, because it holds everything — but handing a remote party a full
System Manager on a client's ERPNext is a large thing to ask, and it used to be the only
option that actually worked. `connector_lead_link.json` ships exactly one DocPerm row (System
Manager), `Sales User` has no permission entry on it at all, and "write access" would not
have granted `create` in any case; the combination earlier versions of this README
recommended did not exist. The shipped role is what replaces it.

`ai_snaplead_connector/tests/test_install.py::TestTheIntegrationRoleExists` pins the role against the
endpoint's own `REQUIRED_PERMISSIONS` list, so the two cannot drift apart.

### Upgrading an existing client

`patches/v1_0/adopt_file_backed_doctypes` hands the six child DocTypes — which earlier releases
created imperatively with `custom = 1` — over to this app so they become file-backed and
versioned. A DocType with `custom = 1` is one a System Manager may add fields to from the Desk
with no developer mode, and adoption makes model sync re-import the shipped JSON over the row,
which deletes every DocField the client added. The patch therefore re-creates each non-shipped
field as a **Custom Field** first: a Custom Field lives in its own table, survives the re-import
and keeps the column visible to forms, list views, reports and the REST API. Nothing is renamed
and no data is moved, so the client sees the same field with the same values afterwards.

Fill `Connector Settings` in as part of the upgrade. Until `business_id` is set the site has no
tenancy: foreign stamps cannot be rejected, and the duplicate guard falls back to searching every
Lead rather than a business.

## The API

Three endpoints, and there is no fourth way in.

| Method | Path | Verb |
| --- | --- | --- |
| `ping` | `/api/method/ai_snaplead_connector.api.v1.sync.ping` | GET, POST |
| `upsert_lead_graph` | `/api/method/ai_snaplead_connector.api.v1.sync.upsert_lead_graph` | POST |
| `pull_changes` | `/api/method/ai_snaplead_connector.api.v1.sync.pull_changes` | GET, POST |

The same three (and the legacy_sync fallbacks) also answer under the app's earlier package
names, `sigzen_ai_scan_crm_connector.api.v1.*` (release 1.0.0) and `vc_connector.api.v1.*`
(development builds), through `override_whitelisted_methods` in hooks.py. A central site built
before a rename keeps working; a current central asks for `ai_snaplead_connector` first.

All three require the API key/secret. `upsert_lead_graph` and `pull_changes` additionally
require the `X-VC-Connector-Token` header; `ping` does not, so a central site can discover an
unconfigured client.

### `ping` — capabilities, detected rather than assumed

```json
{
  "ok": true,
  "protocol": "vc-connector/1",
  "protocols_supported": ["vc-connector/1"],
  "app_version": "1.0.0",
  "business_id": "ACME-001",
  "lead_source_field": "utm_source",
  "india_compliance": false,
  "enabled": true,
  "connector_token_configured": true,
  "default_address_template": true,
  "erpnext_version": "16.26.2",
  "frappe_version": "16.25.0"
}
```

Every one of those is *detected*. The central site used to guess — it assumed
`india_compliance` was installed because the client was Indian, and it assumed `Lead.source`
because that is what ERPNext v14 called it. Both assumptions are wrong on some client, and
each produced a push that failed on every record with a message about a field the central
site had never heard of. `default_address_template` is there because Frappe refuses to save
**any** Address on a site with no default Address Template, which is a client-side setup gap
the central site cannot otherwise see, and which this app deliberately does not paper over by
creating a print format on a stranger's site.

### `upsert_lead_graph` — the one write endpoint

Creates or updates one lead graph — `Lead`, its `Contact`, its `Address`, its file attachments
and the `Connector Lead Link` row — **atomically**. One endpoint, one savepoint, one identity key. On
any failure at any point the whole graph is rolled back and the bytes of any file written are
removed from disk, so "partially synced" is not a state this app can be in.

Post the payload as JSON, either as the body or as a `payload` field:

```bash
curl -sS -X POST "https://client.example.com/api/method/ai_snaplead_connector.api.v1.sync.upsert_lead_graph" \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "X-VC-Connector-Token: <the token>" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol": "vc-connector/1",
    "request_id": "2f1c...-0001",
    "business_id": "ACME-001",
    "vc_uuid": "9d2b7c1e-...",
    "lead": {
      "first_name": "Ravi", "last_name": "Kumar",
      "company_name": "Acme Pumps", "job_title": "Purchase Manager",
      "emails": ["ravi@acme.example", "sales@acme.example"],
      "phones": ["+91 98765 43210", "080-25551234"],
      "source": "Business Card Scan"
    },
    "address": {"address_line1": "12 MG Road", "city": "Bengaluru", "pincode": "560001"},
    "attachments": [{"kind": "front_card", "file_name": "card.png", "content_base64": "iVBOR..."}]
  }'
```

The validation order is fixed, and **every step runs before any write**:

1. authentication — the two factors above;
2. the connector is switched on;
3. the payload's business is this site's business — the tenancy check, made once, at the door;
4. the protocol version is one this client speaks;
5. the payload's shape. **Unknown keys are rejected, not dropped** — a dropped key is the
   worst possible outcome, because the central site believes it sent a field, the client never
   stores it, and nothing anywhere says so;
6. normalisation of phones, emails, pincode and state.

A success looks like this. `defaults_applied` and `warnings` are the two fields a central site
should actually read, not just log: they are how it learns what the client filled in on its
behalf and what it declined to overwrite.

```json
{
  "ok": true,
  "protocol": "vc-connector/1",
  "request_id": "2f1c...-0001",
  "vc_uuid": "9d2b7c1e-...",
  "outcome": "created",
  "lead": "CRM-LEAD-2026-00042",
  "contact": "Ravi Kumar-Acme Pumps",
  "address": "Acme Pumps-Billing",
  "attachments": ["/private/files/card.png"],
  "defaults_applied": {"address.country": "India"},
  "warnings": [],
  "payload_hash": "…",
  "replayed": false,
  "unchanged": false,
  "synced_on": "2026-09-23 10:41:39.542388",
  "link": "VCL-00042"
}
```

`outcome` is one of `created`, `updated` or `adopted`. `replayed` is true when the same
`request_id` arrived twice; `unchanged` is true when the same content arrived under a new
`request_id` and the stored graph is still intact. Both are successes and both write nothing.

#### What `request_id` actually guarantees

`request_id` is idempotent for every request **inside the record's window** — the last 500
requests applied to that record — and not only for the most recent one. That is the whole
reason it exists: an at-least-once transport reorders and redelivers as a matter of course.

It is a window and not a promise about all of history, and the endpoint says so rather than
pretending otherwise. Past the window the answer carries a warning (see the third bullet).

* **the latest request, re-delivered** — the stored answer is returned verbatim, with
  `replayed: true`;
* **an EARLIER request, re-delivered** — the record has moved on, and the payload attached to
  an old `request_id` is by definition older than what the client now holds. The endpoint
  answers with the record's **current** state and `replayed: true`, `unchanged: true`,
  `superseded: true`, `superseded_by: "<the request that won>"` and a warning naming both. It
  writes nothing. Before this, the link row kept one `last_request_id`, so a re-delivery of an
  earlier request missed the replay lookup, failed the payload-hash comparison as well (the
  hash on the row is the *later* payload's), fell through to the write, and **silently
  re-applied the stale payload over the newer one** — answering `ok: true`,
  `outcome: "updated"`, `replayed: false`. `Connector Lead Link.seen_request_ids` keeps the ids
  already applied — the last `REQUEST_HISTORY_LIMIT` of them, 500 — and any of them is
  answered without writing;
* **a re-delivery from BEFORE the window** — at 50 remembered ids the window was silent about
  its own edge: the fifty-first push pushed the first id out of the list, and a re-delivery of
  that request then missed the replay lookup and reverted the record, reported as a clean
  `outcome: "updated"`. The window is now 500, and the row also counts the ids it has
  **forgotten**, so the endpoint can tell "this id is new" apart from "this id is one I can no
  longer rule out". The second case is still written — refusing it would refuse every genuinely
  new push on a record with more than 500 behind it — but the answer carries a warning saying
  exactly that: *request_id X could not be checked against this record's whole history …  If
  this is a re-delivery of one of them, this write has just applied a payload older than the
  stored one.* A revert is never again reported as a clean update;
* **the same `request_id` for a DIFFERENT `vc_uuid`** — a `CONFLICT` naming both records, not
  a silently discarded record.

`seen_request_ids` holds `{"ids": [...], "dropped": N}`. Rows written before this release hold
a bare JSON array and are read as "nothing forgotten", which is what they believed.

### `pull_changes` — read only

Leads this connector owns that changed since `since`, or after `cursor`. The cursor is
`(Lead.modified, Lead.name)`, not an offset — rows keep changing while a pull is paging
through them, and an OFFSET page silently skips a record every time an earlier one is
re-saved. It is opaque to the caller by design. `limit` defaults to 50 and is capped at 200.

```json
{"ok": true, "protocol": "vc-connector/1", "business_id": "ACME-001",
 "count": 50, "records": [...], "cursor": "2026-09-23 10:41:39.542388|CRM-LEAD-2026-00042",
 "has_more": true}
```

## Error codes

Every failure — including an unexpected one — leaves the endpoint as
`{"ok": false, "error": {...}}` with one of these codes, this HTTP status, and never a
traceback. `error.field` names the offending field where one can be identified, and
`error.request_id` echoes the payload's.

| Code | HTTP | Retryable | Means |
| --- | --- | --- | --- |
| `UNAUTHENTICATED` | 401 | no | No authenticated ERPNext user. Send an API key/secret for the integration user. This is the only failure that writes **no log row** — an unauthenticated caller must not be a free write amplifier for anyone who can reach the URL. |
| `TOKEN_INVALID` | 401 | no | The `X-VC-Connector-Token` header is missing, does not match, or the client has no `token_hash` configured at all. |
| `BUSINESS_MISMATCH` | 403 | no | The payload is for a different business than this site is configured for. |
| `REMOTE_PERMISSION` | 403 | no | The integration user lacks a required role. The message names the missing right and the DocType. |
| `CONNECTOR_DISABLED` | 409 | no | `enabled` is off, or `business_id` is not set, or `Connector Settings` does not exist yet (run `bench migrate`). |
| `CONFLICT` | 409 | no | A duplicate entry or a unique-index violation — for example a `request_id` already spent on a different `vc_uuid`. Re-sending the same bytes will not fix it. |
| `CONCURRENT_MODIFICATION` | 409 | **yes** | Somebody saved this Lead in the Desk while the push was being written, so nothing was saved. A race, not a verdict: **re-send the same bytes**. |
| `SETUP_REQUIRED` | 409 | **yes** | Something a human has to switch on at this client. Today that means one thing: the site has no `Address Template`, so ERPNext refuses to save any `Address`. The payload is fine and will be accepted once the client is set up — **keep the record and re-send it**. `ping()` reports the same gap as `default_address_template: false`. |
| `VALIDATION_FAILED` | 422 | no | The payload's shape or content is wrong: an unknown key, a wrong type, invalid base64, a bad cursor, or an ERPNext validation error. |
| `MANDATORY_MISSING` | 422 | no | A mandatory ERPNext field could not be filled — for example an `address` block with no `address_line1`. `error.field` names it. |
| `PROTOCOL_UNSUPPORTED` | 426 | no | The `protocol` value is not one this client speaks. Compare against `protocols_supported` from `ping`. |
| `INTERNAL` | 500 | **yes** | An unexpected failure. The traceback is written to this site's Error Log, not returned. |

**Three codes are retryable — `INTERNAL`, `CONCURRENT_MODIFICATION` and `SETUP_REQUIRED`**
— and the rest deliberately are not. Everything else is a decision about *this payload* or
*this configuration* and will fail identically forever, so a client that retries it only
generates load — an earlier pusher retried 422s in a loop. `error.retryable` carries this as a
boolean so a central site does not have to hard-code the list.

The two that were added are the two where *not* retrying loses a record for no reason:

* a `TimestampMismatchError` used to be folded into `CONFLICT`, whose contract says never to
  retry. It means a salesperson pressed Ctrl+S on the Lead while the push was in flight —
  precisely the transient race that re-sending the same bytes fixes — so it now has its own
  code, `CONCURRENT_MODIFICATION`, and is retryable. The status stays 409: still a conflict,
  just a passing one;
* a missing `Address Template` used to surface as a bare ERPNext `ValidationError` and was
  typed `VALIDATION_FAILED`, a 422 marked permanent. A site created without the setup wizard
  has no Address Template at all, so a conforming central site marked **every** card-scan
  carrying an address permanently failed over a client-side gap a human fixes in a minute.
  It is refused before the first write now, as `SETUP_REQUIRED`.

Every request that gets past authentication also writes one `Connector Log` row —
`request_id`, `vc_uuid`, outcome, error code, message, duration — on the client's own site.
Logging can never be the reason a push fails, or turn a 422 into a 500.

## What it owns

All schema is file-backed under `ai_snaplead_connector/ai_snaplead_connector/doctype/` and is applied by
`bench migrate`'s model sync. Nothing is created imperatively any more.

| DocType | Kind | Purpose |
| --- | --- | --- |
| `Connector Settings` | Single | ERP-side tenancy and transport config: `business_id` (write-once), `central_site`, `token_hash`, `enabled`, `reject_business_mismatch`, `last_request_on`. |
| `Connector Lead Link` | Document | Maps a central `vc_uuid` to a local `Lead`. `vc_uuid` carries a UNIQUE index; `lead` is indexed. Also holds `last_request_id`, `payload_hash`, `last_response`, `last_synced_on`, `migrated_fields`, `client_held_contacts`. |
| `Connector Source Link` | Document | The same shape for `UTM Source` (called `Lead Source` before ERPNext v16). |
| `Connector Log` | Document | One row per inbound request: `request_id`, `vc_uuid`, `outcome`, `error_code`, `message`, `duration_ms`. |
| `ERPNext Lead Email` | Child | Multi-email table on `Lead`. |
| `ERPNext Lead Mobile Number` | Child | Multi-mobile table on `Lead`. |
| `ERPNext Lead Activity Log` | Child | Activity rows on `Lead`. |
| `Lead Source Text Message` | Child | SMS templates on the source DocType. |
| `Lead Source WhatsApp Message` | Child | WhatsApp templates on the source DocType. |
| `Lead Source Email Message` | Child | Email templates on the source DocType. |

### Why a link table and not a Custom Field

Central identity (`vc_uuid`) lives in `Connector Lead Link`, not in a `custom_vc_uuid` Custom Field on
`Lead`, for two reasons:

1. A Custom Field cannot portably carry a UNIQUE index. Without one, a retried push races
   itself and creates two Leads for a single central record. A file-backed DocType declares
   `"unique": 1` and the database enforces it.
2. It keeps our identity out of a DocType ERPNext owns. If the app is uninstalled, or ERPNext
   reshapes `Lead`, the mapping is still ours and still intact.

## What a push may and may not overwrite

The central record is the authority for what it sends. It is not the authority for what it does
not send, and three rules follow from that — all three are pinned by tests in
`ai_snaplead_connector/tests/test_sync_api.py`:

* **a field the payload omits is not a field the payload cleared.** This covers the WHOLE
  address block — `address_line2`, `city`, `state`, `country` and `pincode`. An address block
  arriving without one of them keeps what is stored and names the kept fields in `warnings`; a
  value the payload *does* send still wins. The rule used to be implemented for `pincode` alone,
  and the other three were destroyed silently: `address_line2` was written from the payload
  unconditionally, `city` fell back to the declared default `-` and overwrote a real stored city
  (reported as a *default applied*, not as the overwrite it was), and `state` was blanked to
  `""` — on a non-India country without even a warning. So a push that narrowed a Lead's street
  address erased the rest of its address, and the response warned about the pincode it had KEPT
  while saying nothing about the three fields it had just erased. A brand new Address still gets
  the declared defaults; there is nothing stored to keep.
* **`lead.emails: []` clears BOTH halves of the record.** An empty list is an instruction, and
  it now empties the `Contact Email` rows *and* clears `Lead.email_id`, with a warning naming
  the address that went. Guarding the Lead half on truthiness left the Lead advertising an
  address the central record had just deleted — permanently, to every stock list view, report
  and email workflow, which read the Lead — while the duplicate guard went on reserving it. The
  same applied to `phones` / `mobile_no`. Leaving the `emails` key out entirely is still not a
  clear: both halves are left exactly as they were.
* **a corrected `scanned_text` replaces the one already stored.** After Patch B the value has
  no home but a tagged `CRM Note`, and the writer was tag-idempotent: it found its own note,
  returned "already" and wrote nothing, and the caller discarded that answer. Every corrected
  or re-OCR'd scan after the first was accepted with a 200, warned about nothing and stored
  nowhere. The live push now rewrites the note when the text has changed (the migration itself
  is still write-once — it is copying a field that is about to be dropped).
* **a push never writes to the `(Scanned)` Address.** Patch A creates that second Address only
  when the Lead already had one with a different pincode, precisely so the human-entered value is
  not overwritten. An incoming address is written to the client's own address — the oldest linked
  Address that is not a scanned one — and the scanned row is chosen only when it is the only one
  the Lead has.
* **contact details the client held before adoption survive every later push.** Adopting a
  pre-protocol Lead merges the emails and phone numbers the central record has never seen into
  the payload, and the merged set is recorded on `Connector Lead Link.client_held_contacts` so the next
  ordinary push — which replaces `email_ids` / `phone_nos` wholesale — does not delete them
  again. Two things take a value out of that set: the client deleting it (it is not put back) and
  the central record starting to send it (it is the central record's from then on, and the
  central record may later remove it).

An attachment already on the Lead is also not re-attached: an identical push adds no File row, so
the card image does not accumulate one copy per push.

## Tenancy

`Connector Settings.business_id` is the authority for "which business is this ERP site?".
It is write-once: re-pointing a configured site at another business would silently re-tenant
every row already mapped through `Connector Lead Link`.

`Lead.custom_business_id` is kept as a denormalised per-row stamp (existing client sites hold
real data in that column, and the in-business duplicate guard filters on it), but
`ai_snaplead_connector.tenancy.get_business_id()` resolves Settings first. With
`reject_business_mismatch` on, a Lead stamped for a different business is refused instead of
being mixed into this tenant's table. The same check applies to `Connector Lead Link` and
`Connector Source Link`: they hold the connector's identity map, so a row stamped for a business this
site is not configured for is a cross-tenant write, not a stray field value.

`Connector Lead Link` enforces the mapping in both directions. The UNIQUE index on `vc_uuid` stops one
central record claiming two Leads; a validate-level check stops two central records claiming one
Lead. The second is deliberately not a UNIQUE index — adding one would make `bench migrate`
abort on any existing site that already holds such a pair, and there is no data-migration phase
here to clean that up.

## Duplicate protection

Stock ERPNext refuses a second Lead that reuses an email address anywhere on the site. That rule
stays ON for a normal configured site: with `reject_business_mismatch` on, the site holds exactly
one business, so "anywhere on the site" already means "within this business". It is handed over
to `validate_duplicate_lead_contacts` only when an admin switches `reject_business_mismatch` off,
i.e. deliberately allows several businesses in one table.

`find_duplicate_lead_by_contacts` searches **three** places, and needs all three. It matches
emails case-insensitively, because rows written before this release were stored with their
original casing and an exact-match lookup never sees those on SQLite or PostgreSQL.

1. **the connector's child tables**, while the site still has them (before Patch B);
2. **the stock `Lead.email_id` / `Lead.mobile_no` columns.** Every Lead that predates the
   install carries its contacts there and has no child rows at all — the bulk backfill that
   used to create them was removed;
3. **the Lead's linked `Contact`** (`Contact Email` / `Contact Phone`, reached through
   `Dynamic Link`). After Patch B this is the ONLY home of a Lead's second and third email
   address and phone number. Reading the first two alone left the guard comparing primary
   against primary: the same business card, re-pushed under a new `vc_uuid` with its
   addresses in a different order, produced a second Lead for the same person, with one
   address sitting on two Leads and two Contacts. The `Contact` is reached the way stock
   ERPNext attaches one, so this covers Leads this app never wrote — including the Contact
   `Lead.before_insert` creates by itself.

A push knows more than the Lead does. The Lead itself has room for one email address and one
phone number; the rest of a scanned card goes on the `Contact`, which does not exist yet when
`Lead.validate` runs. So `upsert_lead_graph` declares the whole set in
`flags.vc_contact_values`, and the guard checks all of it rather than row 1 alone.

### What the guard will not do to a client's existing data

`Lead.validate` runs on **every Desk save**, not only on connector pushes, and this guard covers
phone numbers — which stock ERPNext never deduplicated. Installing the app therefore changes a
rule that a client's existing Lead book was never written under, and two limits keep that from
being a wall:

* **only the contact details a save INTRODUCES are checked.** Two pre-existing Leads that
  legitimately share a number — a shared office landline, a switchboard, a receptionist's
  mobile — each named the other, so after the install neither could be saved again: every save
  was refused over a value that save had not touched, and the only way out was to edit data
  that was correct. Refusing the save does not un-share the number; it only stops the row being
  edited. A value the Lead already holds (on the Lead, on its child tables or on its linked
  Contact, compared by phone match key so a respelling counts) is left alone; a value the save
  adds is refused exactly as before, and so is every value on a brand new Lead.
* **the refusal names a place that exists on this site.** The message used to say *remove it
  from its Email IDs table* / *its Mobile Numbers table*. Those are the legacy Custom Field
  child tables, and they are exactly what Patch B drops — so on any migrated or freshly
  installed client the refusal instructed the user to edit something their Desk does not have.
  After the drop it names the Lead's own field and the Contact linked to it.

A related rule has the same shape. This app requires a Lead to carry at least one contact
detail; stock ERPNext is happy with a Lead that has a name and nothing else, and every client's
book holds some. Enforcing it on every save made those rows permanently unsaveable — a spelling
could not be corrected on them. A row that never had a contact detail is now left alone; a row
that has one still may not have its last one deleted, because that is a change the save is
making.

### The pre-flight report

Nothing used to warn a client that the rule was about to change, so the first they heard of it
was a refused save. Before installing, run:

```bash
bench --site <site> execute ai_snaplead_connector.install.report_duplicate_contacts_preflight
```

It needs nothing installed on the site, names every email address and phone number already
shared by more than one Lead (with the spellings actually stored, not the internal match key)
and the Leads holding them, and returns the full report as data. `after_install` runs it too, so
the same list is printed by `bench install-app`.

**Reading a candidate's business.** A candidate Lead's stamp is read by
`_candidate_business_stamp`, from `Lead.custom_business_id` while that Custom Field exists and
from the Lead's `Connector Lead Link` row afterwards — which is where the migration deliberately put
it so it would survive the drop. Asking `frappe.db.get_value` for a dropped fieldname does not
raise: it hands the **fieldname back as a literal**, so a migrated site read every candidate
as being stamped for a business called `custom_business_id`, matched none of them against the
scope, and threw them all away. A configured client came out of the migration with the
duplicate guard switched off completely — for primary contacts as much as secondary ones.

### Which Leads count as candidates

The check is scoped by `tenancy.get_duplicate_scope_business_ids`, which returns the **union** of
this site's own business and the business the incoming row is stamped for — not just the site's.
Scoping to the site alone left a site that deliberately holds several businesses
(`reject_business_mismatch` off) with no protection at all for any business but its own: every
Lead of a foreign business was discarded as a candidate, and stock ERPNext's guard is handed over
in exactly that mode, so an identical retried push simply created a second Lead. The scope is a
union rather than a swap so a foreign-stamped push still cannot reuse a contact this site's own
business already holds.

Unknown never means "different business":

* a candidate Lead with no `custom_business_id` belongs to whichever business this site speaks
  for, and if the site has none configured its business is simply unknown;
* an **empty scope** — no business in Settings and none on the incoming row — excludes nothing,
  so the guard searches every Lead.

That last case is not exotic. It is the default state of an upgraded client the moment
`bench migrate` finishes: `Connector Settings` is a brand new Single and nothing prompts anyone
to fill it in. The guard used to return immediately there, which — together with the stock
`email_id` / `mobile_no` columns the removed bulk rewrite had nulled — left the client's entire
pre-existing Lead book with no duplicate protection from anything at all.

## Stock fields and the child tables

`Lead.email_id` / `Lead.mobile_no` are the single-value projection of child row 1, reconciled in
`before_insert` (so ERPNext's auto-created Contact is built from real values rather than an empty
field) and again in `validate`.

Writing a new value into a stock field promotes it to row 1 **by insertion**, never by
overwriting row 1 by position: the document shape a Desk user produces by editing the projection
is indistinguishable from the one a machine pusher sends when it sanitizes its payload down to
stock ERPNext fields, and for that caller overwriting silently destroyed whichever contact
happened to sit in row 1. An obsolete value stays in the table as a secondary row and can be
deleted in the grid.

There is one exception. If the Lead's **only** contact of that kind is the one the stock field was
projecting, the field and the table describe the same single contact, so changing the field is a
correction of it and the superseded value is dropped. Keeping it is not harmless: every stored
child row is a live claim in the duplicate guard, so a mistyped number left behind permanently
refuses the Lead that actually owns it, with a message naming a Lead whose visible number is
something else. A Lead that holds several contacts keeps them all — promoting a new primary must
not delete one of the others.

Emptying a contact grid is honoured. The Desk deletes the last row by sending the child table
empty and leaving the stock field alone, which the adopt step used to undo by re-appending the
stock value "because the table is empty" — so the last row of either grid could not be deleted at
all. Telling that apart from a Lead that predates this app (also an empty table, also a populated
stock field, but there the value must be adopted, not discarded) needs the database, so
`_has_stored_child_rows` asks it.

## Adopting the child DocTypes on an older client

`ai_snaplead_connector/patches/v1_0/adopt_file_backed_doctypes.py` runs in `pre_model_sync` and flips
`tabDocType.custom` to `0` (and clears `migration_hash`) for the six child DocTypes that used to
be created imperatively with `custom = 1`. Model sync then imports the shipped JSON over them
in place — `import_file` deletes the old row with `for_reload`, so the tables and their rows
survive. On a fresh site the patch is a no-op.

## The legacy Custom Field migration

Two patches move this app off its own Custom Fields and onto the standard ERPNext ones. They are
registered in `patches.txt` and run on `bench migrate`. The target end state is **one** Custom
Field on Lead — `custom_address`, the raw scanned address block, which has no standard home.

**None of this applies to a new client.** A fresh install has no legacy Custom Fields and no
legacy values, so there is nothing to migrate and nothing to drop, and
`install.settle_install_time_migration_state` records that end state directly:
`legacy_migration_status` = `Complete`, `legacy_fields_dropped` = 1, and a
`legacy_migration_message` saying so. `create_custom_fields` then creates `custom_address`
alone, on the install and on every later `bench migrate`.

That function exists because of an ordering inside frappe that quietly broke the promise
above. `frappe.installer.install_app` calls `set_all_patches_as_completed(app)` — one
`Patch Log` row per patch in `patches.txt` — and only *then* calls `after_install`. Patch A
and Patch B are therefore marked executed on **every** install without either of them having
run, and `bench migrate` will never call them again. Left alone, nothing ever set
`legacy_fields_dropped`, so `create_custom_fields` took the "not migrated yet" branch for ever
and recreated the full historical set — fourteen Custom Fields and four empty sections on
`Lead`, ten more on `UTM Source` — on a site that had never held one legacy value. A stranger
installing this on their production ERPNext got twenty-four Custom Fields and four new empty
sections on their Lead and UTM Source forms, and the end state this section promises was
unreachable without running two `bench` commands nothing told them about.

`settle_install_time_migration_state` decides between three cases at install time:

| What the site holds | What it does |
| --- | --- |
| no legacy Custom Fields — a fresh install | records the end state the two patches would have reached, which here is "done, nothing to do" |
| legacy Custom Fields present — a re-install over an older version of this app, the only way an install can meet them | **deletes** the `Patch Log` rows install just wrote for the two legacy patches, so the next `bench migrate` runs the migration properly |
| `legacy_fields_dropped` already set — a site that has finished | nothing at all. That flag *is* the end state, and re-deriving it from what is on the DocType would undo a deliberate `legacy.restore` |

`ai_snaplead_connector/tests/test_install.py` covers all three, and
`TestTheLegacyPatchNamesAreReal` fails the build if a patch is renamed without updating
`install.LEGACY_PATCHES`.

| Legacy field | Lands on |
| --- | --- |
| `custom_email_ids[]` | `Contact.email_ids[]` (Contact found or created via Dynamic Link) |
| `custom_mobile_numbers[]` | `Contact.phone_nos[]`, deduped on the match key (see below) |
| either of those, in a form ERPNext refuses to store | a tagged `CRM Note` on the Lead, written as printed |
| `custom_pincode` | `Address.pincode` (Address found or created via Dynamic Link) |
| `custom_scanned_text` | a tagged `CRM Note` on the Lead |
| `custom_front_card_image` / `custom_back_card_image` | standard File attachments, re-parented |
| `custom_created_from_scan` | `utm_source` = "Business Card Scan", only when it is empty |
| `custom_business_id` | `Connector Lead Link.business_id`, with `vc_uuid` left NULL until the first push |
| `custom_lead_activity_log[]` | **nothing.** It has no standard ERPNext counterpart, so it is not migrated anywhere: it survives only in the Patch B archive. That is said here rather than hidden behind a mapping that does not exist. |

The seven Custom Fields on `Lead Source` / `UTM Source` have no standard replacement either.
They are archived and dropped, not migrated. **[FIELD_MAP.md](../FIELD_MAP.md) carries the
complete table**, field by field, including the layout-only fields and the Property Setters.

**Patch A — `migrate_legacy_lead_fields`** copies and drops nothing. `execute()` enqueues and
returns, so a client with 200k Leads does not watch `bench migrate` sit silent; the cursor lives
in `Connector Settings` so an interrupted run resumes instead of restarting. Every write is
idempotent and each migrated Lead gets a mark row, so a re-run is a no-op. A site with no
background workers will see the job queued and never run — the supported answer is:

	bench --site <site> execute ai_snaplead_connector.legacy.migrate.run_now

A pincode that disagrees with an Address already on the Lead is **never overwritten**: both are
kept and a `Connector Log` row records the conflict. Losing nothing beats winning.

Four things about a client's real card data are worth knowing before you run it:

* **a phone number written the way a card writes it does not abort the Lead.** Frappe validates
  every `Contact Phone` row against `PHONE_NUMBER_PATTERN`, which refuses `9876543210 (O)`,
  `080-25551234 Ext 22` and two numbers in one field. Patch A pulls the numbers out, stores them,
  and writes the value **as printed** to a tagged `CRM Note` so the annotation survives the drop
  — logged as `CONTACT_VALUE_UNSTORABLE`. The verification gate checks for that note as well as
  for the number, so nothing is skipped quietly. The same holds for an address ERPNext will not
  accept as an email. What it will **not** do is invent one: when the real number is too long for
  frappe's 20 characters, the whole value goes to the note and *nothing* reaches the Contact,
  because promoting the leftover fragment of an annotation (`ext 9` → the phone number `9`) puts
  a value on the client's Contact that was never on the card;
* **two numbers are the same number only if their country codes agree.** The match key keeps a
  country code that is not India's, so `+1 212 555 0100` and `+44 20 2125 550100` are two phone
  numbers, not one. Indian re-spellings (`+91 98765 43210`, `09876543210`, `9876543210`) still
  collapse to one row;
* **one legacy field can hold two email addresses, and that is not visible from frappe.**
  `frappe.utils.validate_email_address` is truthy for `sales@acme.com, ravi@acme.com` — it hands
  back the whole string — and for `Ravi Kumar <ravi@acme.com>`, and a `Contact Email` row stores
  either without complaint. So Patch A splits the value: every address becomes its own row, in a
  form a lookup for that address matches exactly, and whatever is left over (a display name, a
  scan that is not an address) goes to the tagged note as printed. Checking only "is this a
  valid email?" is how a Lead ended up holding one row that neither address could be found in,
  with the gate agreeing that it had been migrated;
* **two Leads scanned from the same card keep one attachment each.** Frappe dedupes uploads by
  content hash, so byte-identical card images are two File rows sharing one `file_url`. Patch A
  re-parents the row belonging to *this* Lead; where the only row belongs to another record it
  writes a second row pointing at the same bytes (no re-upload, no disk cost) and logs
  `CARD_FILE_SHARED`.

If a migrated Lead is **deleted** before Patch B runs, its mark row is closed out as `Orphaned`
with nothing to archive, and is listed in the drop report under `archived.orphans`. It does not
block the drop, and `restore` passes over it.

**Patch B — `drop_legacy_lead_fields`** refuses unless Patch A reports Complete *and* the
verification queries return zero. On a non-zero result it logs, re-enqueues Patch A and returns
having dropped nothing. Only then does it archive every legacy value into **VC Legacy Field
Archive** — one JSON row per record, on the client's own site — and only after that does it drop
the 9 + 4 fields on Lead, the 7 + 3 on Lead Source / UTM Source, and the Property Setters that
hid stock `Lead.email_id` and `Lead.mobile_no`. To run it by hand:

	bench --site <site> execute ai_snaplead_connector.legacy.drop.run

### Undo

	bench --site <site> execute ai_snaplead_connector.legacy.restore.restore
	bench --site <site> execute ai_snaplead_connector.legacy.restore.restore --kwargs "{'dry_run': True}"

This re-creates the Custom Fields and writes every archived value back. It is additive: a value
already present is not overwritten unless you pass `force=True`, and the Contacts, Addresses,
notes and attachments Patch A created are left alone, because they are correct.

**Expect `records_restored: 0` if you run it straight after Patch B, and do not read that as a
failure.** Deleting a Custom Field in Frappe removes the field definition, not the column, so
every legacy value is still sitting in `tabLead` — there is simply nothing missing to write. The
values only actually go away when somebody runs `bench trim-tables`. The report's `note` says
which of the two zeroes you are looking at.

## Tests

```bash
bench --site <site> run-tests --app ai_snaplead_connector
```

The suite is in `ai_snaplead_connector/tests/`:

| File | What it pins |
| --- | --- |
| `test_sync_api.py` | the whole API surface — atomicity, the door, the schema, idempotency (the re-delivery of an *earlier* request, and what the endpoint says when a re-delivery falls outside the window), the missing `Address Template`, a concurrent Desk save, the duplicate guard reaching a Lead's `Contact` *and* leaving a client's pre-existing shared numbers alone, an address keeping every field the payload did not send, a corrected `scanned_text`, and `lead.emails: []` clearing both halves of the record |
| `test_install.py` | what `install-app` and `bench migrate` leave on a stranger's site: the three cases of `settle_install_time_migration_state`, the one-Custom-Field end state, and the shipped `Connector Integration` role |
| `test_legacy_migration.py` | Patch A and Patch B, end to end, with their archive and undo |
| `test_lead_contacts.py` | the child tables and the stock-field projection |
| `test_tenancy.py` | business scoping |
| `test_patches.py` | the DocType adoption patch |
| `test_schema.py` | the shape of the app itself: nothing custom, nothing hidden, no bulk rewrite |
| `test_normalisers.py` | the vendored phone/email/pincode/state helpers, with a fixture, and the guard that fails the build if anything imports `ai_snaplead` |

Note that the legacy-migration group (`test_legacy_migration.py`) and `test_lead_contacts.py`
skip themselves on a site where the legacy Custom Fields have already been dropped — there is
nothing left for them to assert about. A run on a site that still has them exercises about
fifty more tests than a run on a migrated one, so a clean full-coverage run has to happen
**before** Patch B on that site.

Two things to know before reading a run as evidence:

* on a migrated site the legacy-migration tests skip themselves, so a green run there is not a
  green run of the migration;
* the suite leaves nothing behind. Record counts for `Contact`, `Lead`, `Address`,
  `Connector Lead Link`, `Connector Log`, `User` and `Custom Field` are identical before and after
  a run, on both databases, and a second consecutive run passes.

## Support and licence

* Field map: [FIELD_MAP.md](../FIELD_MAP.md)
* Release history and tested versions: [CHANGELOG.md](../CHANGELOG.md)
* Licence: MIT — see [license.txt](../license.txt).
