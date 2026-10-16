# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
# Loaded only for a site that still lists the release-1.0.0 name `sigzen_ai_scan_crm_connector`.
# It carries no features: the first `bench migrate` renames the site to the real app and
# carries on under that name. See sigzen_ai_scan_crm_connector/__init__.py.

app_name = "sigzen_ai_scan_crm_connector"
app_title = "AI SnapLead - Connector (old name, renamed on migrate)"
app_publisher = "Sigzen Solutions"
app_description = "Renames a site that installed the connector as sigzen_ai_scan_crm_connector."
app_email = "support@sigzen.com"
app_license = "mit"

before_migrate = ["sigzen_ai_scan_crm_connector.upgrade.before_migrate"]
