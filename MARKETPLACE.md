# Marketplace submission checklist

Where this app stands against what the Frappe Marketplace asks for. Review guidance changes;
check the current [Frappe Marketplace publishing docs](https://frappecloud.com/docs/marketplace)
before relying on any item here.

Last reviewed: 2026-10-19, for release 1.1.0.

## Ready

- [x] **Package name matches the app.** The package is `ai_snaplead_connector`
      (development builds were `vc_connector`, release 1.0.0 was `sigzen_ai_scan_crm_connector`;
      `docs/UPGRADE_FROM_OLD_PACKAGE.md` covers the
      few sites that installed one, and the old API paths still answer).
- [x] **Packaging.** `pyproject.toml` declares the name, licence, description and the supported
      Frappe and ERPNext versions (`[tool.bench.frappe-dependencies]`). `required_apps` lists
      ERPNext. `__version__` is `1.1.0`.
- [x] **Licence.** MIT, full text in `license.txt`.
- [x] **README** written for a site administrator: what it does, requirements, install on
      Frappe Cloud and on a bench, setup, troubleshooting, uninstall, support.
- [x] **Documentation.** `docs/REFERENCE.md` (API, error codes, tenancy, duplicate protection),
      `FIELD_MAP.md`, `CHANGELOG.md`.
- [x] **Identity on the desk.** App title, workspace, sidebar, tile and module are all
      "AI SnapLead - Connector". The logo (`public/images/ai-snaplead-connector-logo.svg`)
      is shipped and declared in `add_to_apps_screen`; listing PNGs (512/256/128) and a
      1200x630 banner are exported from the same SVG.
- [x] **Installs cleanly on MariaDB**, the database Frappe Cloud runs: fresh `install-app` on a
      new site, then `migrate`, then the full suite twice.
- [x] **Tests.** 275 tests, 0 failures on MariaDB and SQLite. The suite leaves no records
      behind and passes when run twice in a row.
- [x] **Static checks.** `ruff check` and `ruff format` clean (ruff 0.14.10, as pinned in
      `.pre-commit-config.yaml`). Frappe semgrep rules: 0 findings. The manual
      commits that remain are one-time migration stages and single-use token issuance, each
      annotated with its reason.
- [x] **Plays well with other apps.** Lead is extended (`extend_doctype_class`), not
      overridden. No standard field is hidden, renamed or made read-only.
- [x] **No outbound traffic.** No network calls, no scheduler events, no webhooks, no email.
- [x] **No switch that does nothing.** The inert `allow_insecure_http` setting is removed.
- [x] **Existing data is safe.** Installing and migrating do not touch existing Leads, Contacts
      or Addresses. The one exception, the legacy field migration, only runs on a site that
      carries this app's old Custom Fields; it is archived, resumable and reversible.
- [x] **Least privilege.** The app ships the `Connector Integration` role, so the
      integration user does not have to be a System Manager.

## Decisions taken

- The three `Lead Source * Message` child DocTypes stay in the app. No current form uses them,
  but the legacy migration's archive and restore read them on sites upgraded from a
  development build, and removing a DocType a client may still hold rows for is the riskier
  choice.
- The legacy migration stays an automatic `bench migrate` step. It is a no-op on every new
  install, which is every Marketplace install.

## A person still has to do

### 1. Put the repository where Frappe Cloud can reach it

The source of record is an internal GitLab host. Frappe Cloud installs an app by cloning it, so
the repository has to be on a host it can reach (GitHub is what the Marketplace expects).

- [ ] Mirror or move the repository to a public or Frappe-Cloud-connected GitHub repository
- [ ] Update the three URLs in `[project.urls]` in `pyproject.toml` and the `bench get-app`
      line in `README.md` (today they point at the internal repository
      `Sigzen-Team/ai_snaplead_connector`)
- [ ] Review `gitlab-ci/`: the pipelines reference an internal image and host, which is right
      for the internal GitLab and should not be copied to a public repository as they are

### 2. Confirm the publisher identity

- [x] `app_publisher` in `hooks.py`, the author in `pyproject.toml` and `license.txt` say
      "Sigzen Technologies Pvt. Ltd.", the same legal name as the live privacy and terms pages.
- [ ] `support@sigzen.com` becomes the public support contact. Confirm somebody reads it.

### 3. Listing assets

- [ ] Screenshots: the workspace, Connector Settings, a received Lead with its Contact and
      card photos, Connector Log
- [ ] Short and long listing descriptions (drafts are in the launch kit)
- [ ] Category, website, documentation, privacy policy and terms URLs

### 4. Release

- [ ] Add the app in the Frappe Cloud dashboard, choose the `master` branch for version 16 and
      confirm it builds
- [ ] Install it on a Frappe Cloud test site and pair it once with AI SnapLead end to end
- [ ] Submit for review and answer the reviewer's questions
