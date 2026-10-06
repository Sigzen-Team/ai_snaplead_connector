# Changelog

All notable changes to `sigzen_ai_scan_crm_connector` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **wire protocol** is versioned separately from the app. The app version is what
`ping()` returns as `app_version`; the protocol version is `protocol`. A protocol bump is a
breaking change for every central site in the field and gets its own major release here.

| App version | Protocol | Frappe | ERPNext |
| --- | --- | --- | --- |
| 1.0.0 | `vc-connector/1` | 16 (verified on 16.25.0 and 16.26.3) | 16 (verified on 16.26.2 and 16.27.0) |

---

## [1.0.0] — 2026-09-30

First public release, prepared for the Frappe Marketplace.

### Name

* The app is titled **Sigzen - AI Scan CRM Connector**. The workspace, its sidebar, the desktop
  tile and the module carry the same name; the workspace route is
  `/desk/sigzen---ai-scan-crm-connector`.
* The package is `sigzen_ai_scan_crm_connector`. Development builds were called `vc_connector`;
  the API paths under that name (`vc_connector.api.v1.*`) still answer, so a central site and a
  client can upgrade on different days.

### Release hardening

* **Extends Lead instead of overriding it.** `override_doctype_class` replaced the Lead
  controller outright, so a second app overriding Lead on the same site would silently lose.
  The app now uses `extend_doctype_class`, which composes with other apps.
* **Public API methods are type-hinted**, which Frappe 16 uses to validate arguments.
* **Removed a setting that did nothing.** `VC Connector Settings.allow_insecure_http` was read
  by no code path. `Central Site` stays as a reference note and says so.
* **No duplicate indexes on MariaDB.** The install added a second index to columns the model
  sync had already indexed.
* **A fresh install is quiet on migrate.** On a new site the app tile is the workspace's
  desktop icon; the install no longer tries to create a second one on every `bench migrate`.
* **The test suite leaves nothing behind.** A run used to leave Contacts, and one Lead with its
  Address, on the site.
* `pyproject.toml` declares the supported Frappe and ERPNext versions
  (`[tool.bench.frappe-dependencies]`).
* Passes `ruff check` and the Frappe semgrep rules with no findings.

### Tested on

| Site | Database | Frappe / ERPNext | Result |
| --- | --- | --- | --- |
| new site, fresh `install-app` | **MariaDB 10.11** | 16.26.3 / 16.27.0 | 228 tests, 0 failures, run twice |
| existing site, upgraded from `vc_connector` | SQLite | 16.26.3 / 16.27.0 | 228 tests, 0 failures, run twice |
| six existing sites (one MariaDB, five SQLite) | both | 16 | `migrate` clean |

Record counts for `Contact`, `Lead`, `Address`, `VC Lead Link`, `VC Connector Log`, `User` and
`Custom Field` were identical before and after each run.

On a site with no legacy Custom Fields, which is every new install, the legacy-migration tests
skip themselves (56 of the 228). They were exercised on MariaDB and SQLite sites that carry
those fields. A lead was also delivered over HTTP on the fresh MariaDB site by an integration
user holding only the two recommended roles, through both the current API path and the
`vc_connector` one.

### No more "VC" in the Desk

* The app's DocTypes and role dropped the "VC" prefix: `Connector Settings`, `Connector Log`,
  `Connector Lead Link`, `Connector Source Link`, `Connector Legacy Field Archive` and the
  `Connector Integration` role (routes move with them, e.g. `/desk/connector-settings`). Where
  dropping "VC" alone would leave a name as generic as "Lead Link" -- DocType names are global
  across every app on a site -- they say "Connector".
* An existing site is renamed by `bench migrate` (`brand_rename.py`), before any patch or the
  model sync: tables, Single values, links, permissions and role holders move with frappe's own
  rename. Nothing to do by hand.
* Unchanged on purpose: field names such as `vc_uuid`, the `X-VC-Connector-Token` header and the
  `vc-connector/1` protocol string are the contract with the central site.
* A lead delivered with no name, company, email or phone is now called "Scanned Lead <id>"
  instead of "VC Lead <id>".

### Upgrading a pre-release install

Only sites that installed a development build, under the package name `vc_connector`, need
this, and it takes no script: `bench get-app`, `bench --site all migrate`, `bench remove-app
vc_connector` (see [docs/UPGRADE_FROM_VC_CONNECTOR.md](docs/UPGRADE_FROM_VC_CONNECTOR.md)). The
repository ships a stand-in package named `vc_connector` with no features: it keeps the old
name importable, so `bench build` no longer fails with "expected str ... not NoneType", and the
migrate renames the site through it. In the same migrate a
`before_migrate` step (`sigzen_ai_scan_crm_connector/brand_rename.py`) moves the module from "Vc Connector" to
"Sigzen - AI Scan CRM Connector", repoints every DocType and link that named it, rebuilds the
cached module map and renames the workspace, before any patch or the model sync runs. It is
idempotent. Until the migrate has run, the app's DocTypes do not load on that site.

### Development history

The notes below were written on 2026-09-23, while the release was being prepared.

The theme of this release is that the app installs on
a site whose data belongs to somebody else, and everything below follows from that.

### Tested on

Verified on a Frappe **16.25.0** / ERPNext **16.26.2** site (`clienttest`), on **SQLite**:

* `bench --site clienttest install-app sigzen_ai_scan_crm_connector` and `bench --site clienttest migrate`
  complete without error;
* `bench --site clienttest run-tests --app sigzen_ai_scan_crm_connector` — **192 tests, 0 failures, 0 errors**
  (177 integration + 15 unit), of which 52 skip themselves on that site because the legacy
  Custom Fields have already been dropped there and there is nothing left for them to assert;
* a genuinely fresh client was built from nothing — `bench new-site` → `install-app erpnext`
  → `install-app sigzen_ai_scan_crm_connector` → `bench migrate` — and it ends with **one** Custom Field on
  `Lead` (`custom_address`), **none** on `UTM Source`, 0 Leads / 0 Contacts / 0 Addresses, and
  the `VC Connector Integration` role holding `read` / `write` / `create` on `VC Lead Link`
  and `VC Source Link`. The count is still one after a second `bench migrate`;
* an isolated snapshot → `bench migrate` → compare, with nothing else running in between,
  modified **0 of 41** existing Leads, **0 of 416** Contacts and **0 of 16** Addresses —
  compared both by `modified` timestamp and by field value (`email_id`, `mobile_no`,
  `lead_name`, `status`), and by document set. Repeated twice with the same result.
* `ping()` on that site returns `protocol: vc-connector/1`, `app_version: 1.0.0`,
  `frappe_version: 16.25.0`, `erpnext_version: 16.26.2`, `lead_source_field: utm_source`.
* the documented token recipe was executed against `_authenticate`: the generated token
  authenticates, a wrong token is `TOKEN_INVALID` / 401, and a missing header is
  `TOKEN_INVALID` / 401.

* the Lead-book freeze was reproduced and cleared on the review site itself (`advtest`,
  4 Leads: two sharing `02212345678`, two with no contact detail at all). Before the fix every
  one of the four was refused on save; after it all four save, a NEW Lead reusing the shared
  number is still refused, and the refusal names the Lead's own field and its Contact rather
  than a child table the site does not have. `after_install` on that site prints
  `02212345678 -> CRM-LEAD-2026-00003, CRM-LEAD-2026-00004`;
* every one of the fixes above was also run in reverse: the fix was temporarily backed out,
  the suite re-run, and the new test confirmed to fail — 1 for the request window, 1 for the
  scanned text, 2 for the emptied contact list, 2 for the address, 3 for the Lead-book freeze.

**Not tested:** MariaDB and PostgreSQL. See the database caveat in `README.md`.

### Added

* `README.md`, which was previously empty: what the app does, which way the traffic goes,
  install and configuration, the connector token, the full API reference, the error-code
  table, the protocol version, and an explicit statement that installing does not modify
  existing Leads.
* `FIELD_MAP.md` — the complete field map in two parts: the protocol map (what a central site
  sends → which ERPNext field it lands in) and the migration map (each removed Custom Field →
  the standard field that replaced it, including the ones that have no replacement).
* `MARKETPLACE.md` — the submission checklist of what a human still has to do.
* `CHANGELOG.md` — this file.
* The MIT licence text in `license.txt`, which was previously an empty file while `hooks.py`
  declared `app_license = "mit"`. `pyproject.toml` now declares the licence too, so the app is
  no longer legally ambiguous.
* Three whitelisted endpoints where there were none: `ping`, `upsert_lead_graph` and
  `pull_changes`, under `sigzen_ai_scan_crm_connector.api.v1.sync`.
* File-backed DocType JSON for all eleven owned DocTypes, so they are versioned and applied by
  `bench migrate`'s model sync.
* `patches.txt` and four patches under `patches/v1_0/`.
* A test suite under `sigzen_ai_scan_crm_connector/tests/`, where there was no tests directory at all.
* `install.ensure_indexes`, because SQLite does not materialise a `search_index` during the
  `CREATE TABLE` a fresh install performs, so those indexes would otherwise never exist on the
  client target.

### Changed

* The six child DocTypes are no longer created imperatively with `custom = 1` on every
  migrate. `patches/v1_0/adopt_file_backed_doctypes.py` hands the existing rows over to the
  app; a client-added field on one of them is re-created as a Custom Field first so the
  re-import does not delete it.
* Pushes no longer go through the generic REST resource API one document per HTTP request.
  One endpoint, one savepoint, one identity key: after a call the graph is either wholly there
  or wholly absent. "Partially synced" is no longer a representable state.
* The tenancy check moved from a filter that every query had to remember to a single check at
  the door.
* `business_id` is now write-once. Re-pointing a configured site at a different business would
  silently re-tenant every row already mapped through `VC Lead Link`.

### Fixed

Each of these was reproduced on a site before and after the change, not reasoned about.

* **Installing this app froze a client's existing Lead book.** `Lead.validate` runs on every
  Desk save, not only on connector pushes, and the duplicate guard covers phone numbers —
  which stock ERPNext never deduplicated. Two pre-existing Leads that legitimately shared a
  number (a shared office landline, a switchboard, a receptionist's mobile) each named the
  other, so neither could be saved again; on the review site that was 2 of its 4 Leads, and
  the other 2 were unsaveable as well because they carried no contact detail at all and this
  app requires one. **100% of that site's Lead book could not be edited after installing.**
  Three changes: only the contact details a save INTRODUCES are checked (a value the Lead
  already holds, compared by phone match key, is left alone); a Lead that never had a contact
  detail is no longer required to grow one (a Lead that has one still may not have its last
  one deleted); and the refusal names a place that exists — it used to say *remove it from
  its Mobile Numbers table*, which is a legacy Custom Field child table that Patch B drops, so
  on every migrated or freshly installed client it instructed the user to edit something their
  Desk does not have. Nothing warned a client that the rule was about to change, so
  `install.report_duplicate_contacts_preflight` now reports every already-shared email address
  and phone number, runs from `after_install`, and can be run **before** installing:
  `bench --site <site> execute sigzen_ai_scan_crm_connector.install.report_duplicate_contacts_preflight`.
* **A re-delivery from outside the idempotency window silently reverted the record.** The
  window was 50 request ids and was silent about its own edge: the fifty-first push pushed the
  first id out of the list, and a re-delivery of that request then missed the replay lookup,
  failed the payload-hash comparison (the stored hash is the newer payload's), fell through to
  the write and re-applied the stale payload — answering `ok: true`, `outcome: "updated"`,
  `replayed: false`, `warnings: []`. Fifty pushes of work reverted and reported as a clean
  success. The window is now 500, and `seen_request_ids` also counts the ids it has
  **forgotten**, so a request that cannot be ruled out is still written (refusing it would
  refuse every genuinely new push) but is reported with a warning saying exactly what could not
  be checked. A revert is never again reported as a clean update.
* **Every corrected `scanned_text` after the first was dropped into nothing.** On a
  post-Patch-B client — which is every client, since a fresh install starts at
  `legacy_fields_dropped = 1` — the value has no home but a tagged `CRM Note`, and
  `write_scanned_note` is tag-idempotent: it found its own note, returned `"already"` and
  wrote nothing, and `_settle_deferred_fields` discarded that answer. A re-OCR'd or hand-
  corrected scan was answered `ok: true`, `outcome: "updated"`, `warnings: []` and stored
  nowhere on the client. The live push now rewrites the note when the text has changed; the
  migration itself stays write-once, because it is copying a field that is about to be dropped.
* **A narrower address push destroyed the rest of the address.** *A field the payload omits is
  not a field the payload cleared* was implemented for `pincode` alone. `address_line2` was
  written from the payload unconditionally; `city` fell back to the declared default `-` and
  overwrote a real stored city, reported as a *default applied* rather than as the overwrite it
  was; `state` was blanked to `""`, and on a non-India country nothing even warned. A push
  carrying only `address_line1` therefore left the row reading `address_line2: ""`, `city: "-"`,
  `state: ""` — and the response warned about the pincode it had KEPT while saying nothing
  about the three fields it had just erased. The rule now covers the whole block, and the kept
  fields are named in `warnings`.
* **`lead.emails: []` left the Lead and its Contact permanently disagreeing.** The empty list
  wiped every `Contact Email` row (that half tested `is not None`) but left `Lead.email_id`
  standing (that half tested truthiness), so the Lead went on advertising an address the
  central record had deleted — to every stock ERPNext list view, report and email workflow,
  which read the Lead — and the duplicate guard went on reserving it. No warning was emitted.
  The same asymmetry applied to `phones` / `mobile_no`. Both halves now follow the payload, and
  clearing one is reported in `warnings`. An omitted `emails` key is still not a clear.

* **A fresh install left the entire legacy Custom Field set on a stranger's ERPNext, for
  ever.** `frappe.installer.install_app` writes a `Patch Log` row for every patch in
  `patches.txt` *before* it calls `after_install`, so the two legacy patches were recorded as
  executed without having run and `bench migrate` would never call them again. Nothing then
  set `legacy_fields_dropped`, so `create_custom_fields` recreated the full historical set —
  14 Custom Fields and 4 empty sections on `Lead`, 10 more on `UTM Source` — on the install
  and on every later migrate, on a site holding no legacy data at all, and the documented end
  state was unreachable without two `bench` commands nothing mentioned.
  `install.settle_install_time_migration_state` now decides at install time between a fresh
  site (record the end state; one Custom Field), a re-install over legacy fields (delete the
  `Patch Log` rows install just wrote, so the migration really runs on the next migrate) and a
  site that has already finished (touch nothing). Covered by `tests/test_install.py`.
* **`request_id` was idempotent only for the most recent request.** The link row kept one
  `last_request_id`, so a re-delivery of any *earlier* request missed the replay lookup, failed
  the payload-hash comparison too, fell through to the write and **silently re-applied the
  stale payload over the newer one** — answering `ok: true`, `outcome: "updated"`,
  `replayed: false`. At-least-once transports, which are the only reason `request_id` exists,
  reorder and redeliver routinely. `VC Lead Link.seen_request_ids` now keeps a bounded window
  of the ids already applied; a re-delivery of any of them returns the record's current state
  with `superseded: true` and writes nothing.
* **The duplicate guard was switched off completely on a migrated site.** Two causes, and the
  second was the one that mattered: (a) after the drop, a Lead's second and third email
  address and phone number live only on its `Contact`, which the guard did not read, so it
  compared primary against primary — `find_duplicate_lead_by_contacts` now searches
  `Contact Email` / `Contact Phone` through `Dynamic Link` as well, and `upsert_lead_graph`
  declares the whole incoming set in `flags.vc_contact_values` because the `Contact` does not
  exist yet at `Lead.validate` time; (b) the candidate's business was read with
  `frappe.db.get_value("Lead", name, "custom_business_id")`, and that field is one the drop
  removes. `get_value` does not fail on a fieldname a DocType no longer has — **it hands the
  fieldname back as a literal** — so every candidate was read as stamped for a business called
  `custom_business_id`, matched no scope, and was discarded. On a configured client that left
  *no* duplicate protection of any kind after the migration. `_candidate_business_stamp` now
  reads the Custom Field only while it exists and falls back to `VC Lead Link.business_id`,
  which is where the migration deliberately put the stamp so it would survive the drop.
* **A missing `Address Template` looked like a bad payload.** ERPNext refuses to save any
  `Address` on a site without one, and a site created without the setup wizard has none. The
  failure surfaced as a bare `ValidationError`, typed `VALIDATION_FAILED` — a 422 the contract
  marks permanent — so a conforming central site marked every card-scan carrying an address
  **permanently failed**. It is now refused before the first write with `SETUP_REQUIRED`
  (409, retryable), naming the setup step. `ping()` already reported the gap as
  `default_address_template: false` and now something acts on it.
* **A concurrent Desk save permanently dropped the record.** `TimestampMismatchError` was
  folded into `CONFLICT`, whose contract says re-sending the same bytes will not help — but it
  means a salesperson pressed Ctrl+S while the push was in flight, which is exactly the race a
  retry fixes. It has its own code now, `CONCURRENT_MODIFICATION` (409, retryable).
* **The README recommended a permission combination that did not exist.** "Sales User plus
  write access" could never satisfy the endpoint: `vc_lead_link.json` ships one DocPerm row
  (System Manager), `Sales User` has none on it, and `write` is not `create`. In practice the
  only integration user that worked was a full System Manager. `install.ensure_integration_role`
  now ships a **`VC Connector Integration`** role granting `read` / `write` / `create` on
  `VC Lead Link` and `VC Source Link`, and the `REMOTE_PERMISSION` message names it.
* **The "installing changes nothing" blockquote was false for the one population that runs
  `bench migrate` with legacy fields present.** The exception — the opt-in legacy migration,
  which does write Leads, Contacts, Addresses and CRM Notes — is now stated inside the
  blockquote instead of four paragraphs below it.

### Removed — behaviour that must not come back

* **`hide_legacy_lead_contact_fields()`.** It wrote Property Setters setting `hidden = 1` and
  `read_only = 1` on the stock `Lead.email_id` and `Lead.mobile_no` fields. Those fields are
  ERPNext's; hiding them breaks stock list views, reports, contact lookups and email workflows
  on a site whose owner never asked for it.
  `patches/v1_0/restore_stock_lead_contact_fields.py` removes them from clients who already
  have them.
* **`migrate_existing_lead_contacts()`.** It loaded and re-saved **every** Lead on the client
  with `ignore_validate` and nulled the two stock columns — on install *and* on every single
  `bench migrate`. Contact details are now adopted per document, at save time, only for
  documents being written anyway.

Both removals are pinned by tests: `test_schema.test_install_does_not_hide_stock_lead_contact_fields`
and `test_schema.test_install_module_has_no_bulk_record_rewrite`.

### Removed — coupling to the central product

* The client no longer needs the central app installed. The previous transport logged in with
  a username and password and probed the client for `sigzen_lead.connector.remote_erp_sync`,
  i.e. it required the central app to be installed **on the client**. Nothing in this app
  imports it; the two normalisers the two sides share are vendored under `sigzen_ai_scan_crm_connector/utils/`
  with a fixture (`tests/fixtures/normaliser_cases.json`) pinning them against drift, and
  `test_normalisers.test_this_app_does_not_import_sigzen_lead` fails the build if an import
  ever reappears.
* `gitlab-ci/semgrep.yml` no longer pulls a vendor-private container image, and no longer
  targets a vendor-specific self-hosted runner tag.
* `gitlab-ci/tag.yml` no longer hardcodes a private GitLab host's IP address, a vendor
  username in a credentialed git remote URL, a vendor group name, or a vendor bot email. They
  are replaced by the CI variables GitLab provides.
* `pyproject.toml` and `hooks.py` no longer carry `"sigzen_ai_scan_crm_connector"` as the app description.

### Known gaps

Recorded here rather than left to be discovered:

* `VC Connector Settings.central_site` and `VC Connector Settings.allow_insecure_http` are
  **inert**: no code path reads either field in this release. They are documented as inert in
  `README.md`. Either wire them up or remove them before a second release.
* The three source-template child DocTypes (`Lead Source Text Message`,
  `Lead Source WhatsApp Message`, `Lead Source Email Message`) are still shipped, but the
  migration drops the Custom Fields on `Lead Source` / `UTM Source` that were their only
  parents. On a fully migrated site they are unreachable from any form.
* `custom_lead_activity_log` and the seven `Lead Source` / `UTM Source` Custom Fields have no
  standard ERPNext counterpart. They are archived into `VC Legacy Field Archive` and dropped;
  nothing migrates them onto a standard field, because there is nothing to migrate them onto.
* No MariaDB or PostgreSQL verification. See `README.md`.
* The test suite leaks documents. A full `run-tests` run on `clienttest` left 5–7 `Contact`
  rows behind (`Dup Two-42`, `Foreign Retry-36`, `Lower Case Push-41`, `Multi Biz B-37`,
  `Retry Of Legacy-40` and similar). The Leads they were created for are rolled back but the
  Contacts are not, so a site accumulates them on every run. This is a defect in the tests,
  not in the app — no `Lead` or `Address` leaks — but it should be fixed before the tests are
  offered as evidence to a reviewer.
