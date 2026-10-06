# Sigzen - AI Scan CRM Connector

Receive the leads your sales team scans with **AI Scan CRM** straight into your own ERPNext,
as standard **Lead**, **Contact** and **Address** records.

AI Scan CRM is a mobile app for scanning business cards at expos, events and meetings. This
connector is the ERPNext side of it. Install it on your ERPNext site, pair it once, and every
lead your team captures arrives in your normal Lead list, tagged with the source it came from
and with the card photos attached.

| | |
| --- | --- |
| App name | `sigzen_ai_scan_crm_connector` |
| Version | 1.0.0 |
| Works with | Frappe 16, ERPNext 16 |
| Licence | MIT |
| Publisher | Sigzen Solutions |

## Features

- **Standard records, nothing to learn.** Leads land in the stock ERPNext Lead list with
  their Contact and Address. Names, company, job title, phones, emails, website and address
  are mapped to standard fields.
- **All the phones and emails on a card.** A card with three numbers keeps all three.
- **Card photos attached.** The front and back images are saved on the Lead as private files.
- **Lead source included.** The expo, event or campaign a lead was captured at is filed as its
  source (`UTM Source` on ERPNext 16).
- **Never half a lead.** Each lead is written in a single transaction: the Lead, its Contact,
  its Address and its files are all saved, or none of them are.
- **No duplicates from retries.** A lead that is sent twice is recognised and updated, not
  created again.
- **A log you can read.** *Connector Log* shows every delivery and every refusal, with the
  reason in plain words.
- **Your edits are respected.** A push does not overwrite contact details your own staff added
  to a Lead in ERPNext.

## How it works

```
AI Scan CRM  ── HTTPS POST ──▶  your ERPNext  ──▶  Lead / Contact / Address / File
             ◀── JSON reply ──
```

**The connector only receives.** It never opens an outbound connection, adds no scheduled job,
sends no email and has no webhook. Your firewall needs no outbound rule for it, and it cannot
send your data anywhere on its own.

**Installing it does not change your existing data.** No existing Lead, Contact or Address is
created, modified or deleted by installing the app or by `bench migrate`.

Every delivery must present two things: the API key of an ERPNext user you create for the
integration, and a connector token that identifies your AI Scan CRM account. Either one alone
is refused. Disable that user, or untick **Enabled** in the settings, and deliveries stop at
once.

## Requirements

- Frappe Framework **version 16**
- ERPNext **version 16** (required; the app refuses to install without it)
- An **AI Scan CRM** business account on a plan that includes the ERPNext connection
- An **Address Template** on the site (*Setup > Printing and Branding > Address Template*).
  ERPNext cannot save any Address without one, and a site created without the setup wizard
  has none.

Tested on MariaDB 10.11, which Frappe Cloud uses, and on SQLite.

## Installation

### Frappe Cloud

1. Open your site in the Frappe Cloud dashboard.
2. Go to **Apps**, choose **Install App** and select **Sigzen - AI Scan CRM Connector**.
3. Wait for the install to finish. No further deploy step is needed.

### Self-hosted bench

```bash
bench get-app sigzen_ai_scan_crm_connector <repository-url> --branch master
bench --site <your-site> install-app sigzen_ai_scan_crm_connector
bench --site <your-site> migrate
```

After installing you will find a **Sigzen - AI Scan CRM Connector** tile on the desk. It opens
a workspace with the settings, the received leads and the log.

## Setup

Setup takes a few minutes and is done once.

### 1. Create the integration user

In ERPNext, create a user for the connection (for example `scan.integration@yourcompany.com`)
and give it these two roles:

| Role | Why |
| --- | --- |
| **Sales User** (or the sales role you already use) | create and update Lead, Contact and Address |
| **Connector Integration** (added by this app) | create and update the connector's own link records |

Do not use your own login, and avoid making the integration user a System Manager unless you
want AI Scan CRM to finish steps 3 and 4 for you (see below).

### 2. Fill in the settings

Open **Connector Settings** from the workspace.

- **Business ID**: your AI Scan CRM business ID. It can be set only once, so check it before
  saving.
- **Enabled**: tick it.

### 3. Generate the connector token

Still in **Connector Settings**, click **Generate Connector Token**. The token is shown
once. Copy it. Only a hash of it is stored on your site.

### 4. Connect from AI Scan CRM

In the AI Scan CRM mobile app, the business manager opens **Profile > ERPNext Settings** and
enters your site address, the integration user and its password, then saves and tests the
connection.

If the integration user is a System Manager, AI Scan CRM creates the API key and the connector
token by itself during this step. Otherwise, generate an API key for the user
(*User > API Access > Generate Keys*) and give the key, the secret and the token from step 3
to your AI Scan CRM administrator.

### 5. Scan a card

Scan a business card in the app. Within a few minutes the lead appears in your ERPNext Lead
list, and **Last Request On** in the settings shows when AI Scan CRM last reached your site.

## What the app adds to your site

- A workspace, **Sigzen - AI Scan CRM Connector**, visible to System Managers
- **Connector Settings**: pairing and lead routing
- **Connector Lead Link** and **Connector Source Link**: which incoming record maps to which Lead or source
- **Connector Log**: one row per delivery
- The role **Connector Integration**
- One field on Lead, `custom_address`, holding the address text as printed on the card

No standard ERPNext field is hidden, renamed or made read-only.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Leads do not arrive | Open **Connector Log**; the newest row says why the delivery was refused. |
| `SETUP_REQUIRED` | Create an Address Template (see Requirements). Nothing was lost; the lead is retried. |
| `TOKEN_INVALID` | The token in AI Scan CRM does not match. Generate a new one and enter it again. |
| `BUSINESS_MISMATCH` | The Business ID in the settings is not the one the leads are sent for. |
| `REMOTE_PERMISSION` | The integration user is missing one of the two roles in step 1. |
| `CONNECTOR_DISABLED` | Tick **Enabled** in the settings. |

The full list of error codes is in the [technical reference](docs/REFERENCE.md#error-codes).

## Uninstalling

```bash
bench --site <your-site> uninstall-app sigzen_ai_scan_crm_connector
```

The Leads, Contacts and Addresses the connector created are ordinary ERPNext records and stay
on your site. The connector's own records (settings, link rows and log) are removed.

## Documentation

- [Technical reference](docs/REFERENCE.md): the API, error codes, tenancy, duplicate
  protection and the upgrade path for older installs
- [Field map](FIELD_MAP.md): which incoming field goes to which ERPNext field
- [Changelog](CHANGELOG.md): release history and tested versions
- [Upgrading from `vc_connector`](docs/UPGRADE_FROM_VC_CONNECTOR.md): only for sites that
  installed a development build under the old package name

## Development

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app sigzen_ai_scan_crm_connector
```

The suite has 228 tests and leaves no records behind. Run it on a test site, never on a site
that is paired with a live AI Scan CRM account: it rewrites the connector settings while it
runs.

Code is checked with [ruff](https://docs.astral.sh/ruff/) and the
[Frappe semgrep rules](https://github.com/frappe/semgrep-rules).

## Support

Email **support@sigzen.com** with your site address and the relevant row from
**Connector Log**.

## Licence

MIT. See [license.txt](license.txt).
