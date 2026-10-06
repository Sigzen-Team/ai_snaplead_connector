# Upgrading a site that installed the app as `vc_connector`

Before its first release this app's package was called `vc_connector`. It is now
`sigzen_ai_scan_crm_connector`. **If you install the app from the Frappe Marketplace, or install
it for the first time, none of this applies to you.**

It applies only to a bench where `bench --site <site> list-apps` shows `vc_connector`. The
upgrade uses only standard bench commands; there is no script to run.

## Steps

Do this in a maintenance window. Deliveries from AI Scan CRM that arrive while the site is down
are retried by the central site; none are lost.

```bash
# 1. Back up every site that has the connector
bench --site <site> backup

# 2. Get the app under its new name (a new folder, apps/sigzen_ai_scan_crm_connector)
bench get-app <repository-url> --branch master

# 3. Migrate. A site that still lists vc_connector is renamed automatically, first thing.
bench --site all migrate

# 4. Remove the old folder from the bench (no site uses it any more; bench archives it)
bench remove-app vc_connector

# 5. Rebuild and restart
bench build --app sigzen_ai_scan_crm_connector
bench restart
```

Step 3 prints one line per renamed site:

```
<site>: renamed the installed app vc_connector -> sigzen_ai_scan_crm_connector (installed_apps=1, ...)
```

`bench --site <site> install-app sigzen_ai_scan_crm_connector` on such a site also works: it
renames the existing install instead of installing a second copy, and asks you to run the
migrate.

## If you already pulled the new code into `apps/vc_connector`

Then `bench build` failed with

```
TypeError: expected str, bytes or os.PathLike object, not NoneType   (frappe/build.py)
```

because git removed the old package's files but left untracked leftovers (`__pycache__`, built
assets), so `vc_connector` imported as an empty package. Current code ships a real
`vc_connector` package (see below), so this no longer happens. Follow the steps above from
step 2; step 4 archives that folder too.

If you run the migrate before step 2, it stops with:

```
<site> has the connector installed under its old name vc_connector, but
sigzen_ai_scan_crm_connector is not on this bench yet. Run: bench get-app ...
```

and changes nothing. Do step 2 and run it again.

## How it works

A site remembers its installed apps by package name, and `bench migrate` loads every one of
them before it does anything else. So the repository carries a small package named
`vc_connector` next to the real one. It has no features, no DocTypes and no API. Its only hook
is `before_migrate`, which renames the site's records to `sigzen_ai_scan_crm_connector` and lets
the same migrate carry on under the new name: pending patches run, patches that already ran do
not run again. Once no site lists `vc_connector`, nothing loads that package again.

Your data is not touched. Only the places that record the app's name change:

| Where | What |
| --- | --- |
| Installed apps list | `vc_connector` becomes `sigzen_ai_scan_crm_connector` |
| Installed Applications | the row shown in the Desk |
| Module Def | which package the module's code is loaded from |
| Patch Log | the app's patches are recorded under the new name, so none of them runs a second time |
| Desktop Icon, Workspace, Workspace Sidebar | the `app` they belong to, and the logo path |
| Saved desktop layouts | the copy of the tile they hold |
| `site_config.json` | the `installed_apps` key, if the site has one |

## What does not change

- **The pairing.** Business ID, connector token and the integration user's API key stay as
  they are. Nothing has to be entered again on either side.
- **The API.** The central site may keep calling `vc_connector.api.v1.*`; those paths still
  answer. A central site that already knows the new name tries it first and falls back.
- **Your records.** Leads, Contacts, Addresses, link rows and the log are untouched.

## Checking the result

```bash
bench --site <site> list-apps          # shows sigzen_ai_scan_crm_connector, not vc_connector
```

Then open **Connector Settings** and check **Last Request On** after the next scan.

## If something goes wrong

Restore the backup from step 1. The old package keeps working with the old database.
