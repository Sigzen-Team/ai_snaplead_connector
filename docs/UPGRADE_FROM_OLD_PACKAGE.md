# Upgrading a site that installed the app under an older package name

This app's Python package has carried three names:

| Package | When |
| --- | --- |
| `vc_connector` | development builds, before the first release |
| `sigzen_ai_scan_crm_connector` | release 1.0.0, when the product was "Sigzen - AI Scan CRM" |
| `ai_snaplead_connector` | now: the product is **AI SnapLead** and the app is **AI SnapLead - Connector** |

**If you install the app from the Frappe Marketplace, or install it for the first time, none of
this applies to you.**

It applies only to a bench where `bench --site <site> list-apps` shows `vc_connector` or
`sigzen_ai_scan_crm_connector`. The upgrade uses only standard bench commands; there is no
script to run.

## Steps

Do this in a maintenance window. Deliveries from AI SnapLead that arrive while the site is down
are retried by the central site; none are lost.

```bash
# 1. Back up every site that has the connector
bench --site <site> backup

# 2. Get the app under its new name. bench clones the repository, then names the folder
#    after the package: apps/ai_snaplead_connector. On a bench that has release 1.0.0 the
#    repository's name is already taken by the old folder, apps/sigzen_ai_scan_crm_connector,
#    so bench asks "A directory for the application ... already exists. Do you want to
#    continue and overwrite it?". Answer yes: bench moves the old folder to archived/apps/
#    (it is not deleted) and clones the new release in its place.
bench get-app <repository-url> --branch master

# 3. Migrate. A site that still lists an old name is renamed automatically, first thing.
bench --site all migrate

# 4. Only if the bench still has a folder under an old name (apps/vc_connector, or
#    apps/sigzen_ai_scan_crm_connector if you answered no in step 2): archive it.
#    No site uses it any more.
bench remove-app vc_connector

# 5. Rebuild and restart
bench build --app ai_snaplead_connector
bench restart
```

Step 3 prints one line per renamed site:

```
<site>: renamed the installed app sigzen_ai_scan_crm_connector -> ai_snaplead_connector (installed_apps=1, ...)
```

`bench --site <site> install-app ai_snaplead_connector` on such a site also works: it renames
the existing install instead of installing a second copy, and asks you to run the migrate.

## If you pulled the new code into the old folder

`git pull` inside `apps/sigzen_ai_scan_crm_connector` (or `apps/vc_connector`) brings the new
package into a folder whose name no longer matches it. bench lists apps by FOLDER name, so the
bench keeps loading the old name, which is now only a stand-in, and the migrate stops with:

```
<site> has the connector installed under its old name sigzen_ai_scan_crm_connector, but
ai_snaplead_connector is not on this bench yet. Run: bench get-app ...
```

It changes nothing. Do step 2 (answer yes: it archives the old folder) and run the migrate again.

## How it works

A site remembers its installed apps by package name, and `bench migrate` loads every one of
them before it does anything else. So the repository carries a small package for each old name
(`vc_connector/`, `sigzen_ai_scan_crm_connector/`) next to the real one. They have no features,
no DocTypes and no API. The only hook of each is `before_migrate`, which renames the site's
records to `ai_snaplead_connector` and lets the same migrate carry on under the new name:
pending patches run, patches that already ran do not run again. Once no site lists an old name,
nothing loads its stand-in again.

Your data is not touched. Only the places that record the app's name change:

| Where | What |
| --- | --- |
| Installed apps list | the old name becomes `ai_snaplead_connector` |
| Installed Applications | the row shown in the Desk |
| Module Def | the module is now "AI SnapLead Connector", loaded from the new package |
| Patch Log | the app's patches are recorded under the new name, so none of them runs a second time |
| Scheduled Job Type | any job method under the old package |
| Desktop Icon, Workspace, Workspace Sidebar, Sidebar Item Group | the `app` they belong to |
| Workspace, sidebar and desk tile | the tile, sidebar and heading read "AI SnapLead - Connector"; the workspace is "AI SnapLead Connector" (route `/desk/ai-snaplead-connector`; the old routes redirect) |
| `/assets/<old name>/...` URLs | logos, saved desktop layouts, files, print formats, letter heads, web pages, settings |
| User / System Settings default app | if it named the old package |
| `site_config.json` | the `installed_apps` key, if the site has one |

## What does not change

- **The pairing.** Business ID, connector token and the integration user's API key stay as
  they are. Nothing has to be entered again on either side.
- **The API.** The central site may keep calling `sigzen_ai_scan_crm_connector.api.v1.*` or
  `vc_connector.api.v1.*`; those paths still answer. A central site that already knows the new
  name tries `ai_snaplead_connector.api.v1.*` first and falls back.
- **The wire protocol.** `vc-connector/1`, the `X-VC-Connector-Token` header and the field names.
- **Your records.** Leads, Contacts, Addresses, link rows and the log are untouched. DocType
  names (Connector Settings, Connector Log, ...) and the Connector Integration role stay.

## Checking the result

```bash
bench --site <site> list-apps          # shows ai_snaplead_connector, no old name
```

Then open **Connector Settings** and check **Last Request On** after the next scan.

## If something goes wrong

Restore the backup from step 1. The old release keeps working with the old database.
