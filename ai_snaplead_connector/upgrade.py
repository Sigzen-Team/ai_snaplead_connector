# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt


def before_migrate():
	# frappe.get_attr() only calls a hook that lives in an INSTALLED app, and until this runs the
	# site lists `ai_snaplead_connector`, not the real app. So the hook is here and imports the real
	# code.
	from sigzenlead_connector.upgrade.from_old_package import upgrade_during_migrate

	upgrade_during_migrate("ai_snaplead_connector")
