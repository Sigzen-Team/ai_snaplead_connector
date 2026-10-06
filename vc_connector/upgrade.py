# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt


def before_migrate():
	# frappe.get_attr() only calls a hook that lives in an INSTALLED app, and until this runs the
	# site lists `vc_connector`, not the real app. So the hook is here and imports the real code.
	from sigzen_ai_scan_crm_connector.upgrade.from_vc_connector import upgrade_during_migrate

	upgrade_during_migrate()
