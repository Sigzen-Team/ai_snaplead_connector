# SigzenLead - Connector

Receive the leads your sales team scans with **SigzenLead** straight into your own ERPNext,
as standard **Lead**, **Contact** and **Address** records.

SigzenLead is a mobile app for scanning business cards at expos, events and meetings. This
connector is the ERPNext side of it. Install it on your ERPNext site, pair it once, and every
lead your team captures arrives in your normal Lead list, tagged with the source it came from
and with the card photos attached.

| | |
| --- | --- |
| App name | `sigzenlead_connector` |
| Version | 1.1.1 |
| Works with | Frappe 16, ERPNext 16 |
| Licence | MIT |
| Publisher | Sigzen Technologies Pvt. Ltd. |

## What is SigzenLead, and why a connector?

**SigzenLead** ([aisnaplead.com](https://aisnaplead.com)) is a lead-capture service run by
Sigzen Technologies. It has two parts:

- **The mobile app** (Android and iOS). Your sales team photographs a business card, and AI
  reads the name, company, job title, phones, emails, website and address from it. The lead
  is tagged with the expo, event or campaign where the card was collected.
- **The SigzenLead website** ([aisnaplead.com](https://aisnaplead.com)). The business owner
  manages the team, the plan, the lead sources and the connection to ERPNext. Every scanned
  lead is kept here too.

SigzenLead is a hosted service. It is **not** installed on your ERPNext site, and it works
on its own: a team can scan and manage leads in SigzenLead without any ERPNext at all.

**This app is the bridge to your ERPNext.** Most businesses don't want their leads stuck in
one more tool. They want them where sales already works: in the ERPNext Lead list, next to
their quotations, customers and follow-ups. The connector makes that happen:

```
 Sales team        SigzenLead (hosted)           Your ERPNext + this connector
┌──────────┐      ┌──────────────────────┐      ┌──────────────────────────────┐
│ scans a  │ ───▶ │ AI reads the card,   │ ───▶ │ Lead + Contact + Address     │
│ card     │      │ stores the lead      │      │ + card photos, in your lists │
└──────────┘      └──────────────────────┘      └──────────────────────────────┘
```

Without the connector, your ERPNext has no safe way to accept these leads. With it, every
scanned card becomes a normal ERPNext Lead within minutes, with nothing typed by hand.

| You want to… | You need |
| --- | --- |
| Scan cards and keep the leads in SigzenLead only | SigzenLead (app + account). No connector. |
| Get those leads into your ERPNext automatically | SigzenLead **and** this connector on your ERPNext site |
| Use this connector without SigzenLead | Not possible. It only accepts leads sent by SigzenLead. |

To get started with SigzenLead, sign up at [aisnaplead.com](https://aisnaplead.com) and
install the SigzenLead app on your team's phones. Then install this connector and follow
[Setup](#setup) below.

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
SigzenLead  ── HTTPS POST ──▶  your ERPNext  ──▶  Lead / Contact / Address / File
             ◀── JSON reply ──
```

**The connector only receives.** It never opens an outbound connection, adds no scheduled job,
sends no email and has no webhook. Your firewall needs no outbound rule for it, and it cannot
send your data anywhere on its own.

**Installing it does not change your existing data.** No existing Lead, Contact or Address is
created, modified or deleted by installing the app or by `bench migrate`.

Every delivery must present two things: the API key of an ERPNext user you create for the
integration, and a connector token that identifies your SigzenLead account. Either one alone
is refused. Disable that user, or untick **Enabled** in the settings, and deliveries stop at
once.

## Requirements

- Frappe Framework **version 16**
- ERPNext **version 16** (required; the app refuses to install without it)
- An **SigzenLead** business account on a plan that includes the ERPNext connection
- An **Address Template** on the site (*Setup > Printing and Branding > Address Template*).
  ERPNext cannot save any Address without one, and a site created without the setup wizard
  has none.

Tested on MariaDB 10.11, which Frappe Cloud uses, and on SQLite.

## Installation

### Frappe Cloud

1. Open your site in the Frappe Cloud dashboard.
2. Go to **Apps**, choose **Install App** and select **SigzenLead - Connector**.
3. Wait for the install to finish. No further deploy step is needed.

### Self-hosted bench

```bash
bench get-app sigzenlead_connector http://192.168.1.51/Sigzen-Team/ai_snaplead_connector.git --branch master
bench --site <your-site> install-app sigzenlead_connector
bench --site <your-site> migrate
```

After installing you will find an **SigzenLead - Connector** tile on the desk. It opens
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
want SigzenLead to finish steps 3 and 4 for you (see below).

### 2. Fill in the settings

Open **Connector Settings** from the workspace.

- **Business ID**: your SigzenLead business ID. It is filled in automatically when you
  connect from SigzenLead (step 4) with a System Manager user; otherwise set it here and
  check it before saving. Only a System Manager can change it later; the form shows how many
  linked leads will move before it saves. If this ERPNext is already linked to a different
  business, connecting refuses with `BUSINESS_MISMATCH` instead of changing it.
- **Enabled**: tick it.

### 3. Generate the connector token

Still in **Connector Settings**, click **Generate Connector Token**. The token is shown
once. Copy it. Only a hash of it is stored on your site.

### 4. Connect from SigzenLead

The business owner or manager signs in to SigzenLead, either on the website
(**Dashboard > Connectors**) or in the mobile app (**Profile > ERPNext Settings**), enters your
site address, the integration user and its password, then saves and clicks **Test Connection**.

If the integration user is a System Manager, SigzenLead creates the API key and the connector
token, and fills in the Business ID, by itself during this step. Otherwise, generate an API key for the user
(*User > API Access > Generate Keys*) and give the key, the secret and the token from step 3
to your SigzenLead administrator.

### 5. Scan a card

Scan a business card in the app. Within a few minutes the lead appears in your ERPNext Lead
list, and **Last Request On** in the settings shows when SigzenLead last reached your site.

## What the app adds to your site

- A workspace, **SigzenLead - Connector** (`/desk/sigzenlead-connector`), visible to System Managers
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
| `TOKEN_INVALID` | The token in SigzenLead does not match. Generate a new one and enter it again. |
| `BUSINESS_MISMATCH` | The Business ID in the settings is not the one the leads are sent for. |
| `REMOTE_PERMISSION` | The integration user is missing one of the two roles in step 1. |
| `CONNECTOR_DISABLED` | Tick **Enabled** in the settings. |

The full list of error codes is in the [technical reference](docs/REFERENCE.md#error-codes).

## Uninstalling

```bash
bench --site <your-site> uninstall-app sigzenlead_connector
```

The Leads, Contacts and Addresses the connector created are ordinary ERPNext records and stay
on your site. The connector's own records (settings, link rows and log) are removed.

## Documentation

- [Technical reference](docs/REFERENCE.md): the API, error codes, tenancy, duplicate
  protection and the upgrade path for older installs
- [Field map](FIELD_MAP.md): which incoming field goes to which ERPNext field
- [Changelog](CHANGELOG.md): release history and tested versions
- [Upgrading from an older package name](docs/UPGRADE_FROM_OLD_PACKAGE.md): only for sites that
  installed `vc_connector`, `sigzen_ai_scan_crm_connector` (release 1.0.0) or `ai_snaplead_connector` (1.1.x)

## Development

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app sigzenlead_connector
```

The suite has 275 tests and leaves no records behind. Run it on a test site, never on a site
that is paired with a live SigzenLead account: it rewrites the connector settings while it
runs.

Code is checked with [ruff](https://docs.astral.sh/ruff/) and the
[Frappe semgrep rules](https://github.com/frappe/semgrep-rules).

## Support

Email **support@sigzen.com** with your site address and the relevant row from
**Connector Log**.

## Licence

MIT. See [license.txt](license.txt).
