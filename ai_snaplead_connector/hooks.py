# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
# Loaded only for a site that still lists the release-1.1 name `ai_snaplead_connector`.
# It carries no features: the first `bench migrate` renames the site to the real app and
# carries on under that name. See ai_snaplead_connector/__init__.py.

app_name = "ai_snaplead_connector"
app_title = "SigzenLead - Connector (old name, renamed on migrate)"
app_publisher = "Sigzen Technologies Pvt. Ltd."
app_description = "Renames a site that installed the connector as ai_snaplead_connector."
app_email = "support@sigzen.com"
app_license = "mit"

before_migrate = ["ai_snaplead_connector.upgrade.before_migrate"]
