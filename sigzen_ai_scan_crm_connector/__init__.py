# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Stand-in for this app's release-1.0.0 package name. Not an app of its own.

Release 1.0.0 of the connector shipped as `sigzen_ai_scan_crm_connector`, when the product was
"Sigzen - AI Scan CRM". The package is `sigzenlead_connector` now. A site that installed 1.0.0
still lists `sigzen_ai_scan_crm_connector`, and frappe imports `sigzen_ai_scan_crm_connector.hooks`
for it before it does anything else. This package makes that import work, so `bench build` and
`bench migrate` run, and its only hook renames the site on the first migrate (see
sigzenlead_connector/upgrade/from_old_package.py). Once no site lists the old name, nothing loads
this package again.

The API paths under the old name keep answering through the real app's
override_whitelisted_methods, not through anything here.
"""
