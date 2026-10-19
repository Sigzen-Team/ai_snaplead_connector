# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
# Loaded only for a site that still lists the development-build name `vc_connector`.
# It carries no features: the first `bench migrate` renames the site to the real app and
# carries on under that name. See vc_connector/__init__.py.

app_name = "vc_connector"
app_title = "AI SnapLead - Connector (old name, renamed on migrate)"
app_publisher = "Sigzen Solutions"
app_description = "Renames a site that installed the connector as vc_connector."
app_email = "support@sigzen.com"
app_license = "mit"

before_migrate = ["vc_connector.upgrade.before_migrate"]
