# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Stand-in for this app's development-build package name. Not an app of its own.

The connector's package was `vc_connector` before its first release. A site that installed it
then still lists `vc_connector`, and frappe imports `vc_connector.hooks` for it before it does
anything else. This package makes that import work, so `bench build` and `bench migrate` run, and
its only hook renames the site on the first migrate (see
sigzen_ai_scan_crm_connector/upgrade/from_vc_connector.py). Once no site lists `vc_connector`,
nothing loads this package again.
"""
