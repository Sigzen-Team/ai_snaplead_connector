app_name = "sigzen_ai_scan_crm_connector"
# The package is named after the app. Before its first public release it was `vc_connector`;
# the API paths under that name still answer (see override_whitelisted_methods below), and a site
# that installed the old package is renamed by the ordinary `bench migrate` (see
# upgrade/from_vc_connector.py and the stand-in package vc_connector/ next to this one).
#
# The Marketplace listing name and the Desk tile:
app_title = "Sigzen - AI Scan CRM Connector"
app_publisher = "Sigzen Solutions"
app_description = "Receive business-card leads from Sigzen - AI Scan CRM into ERPNext as standard Leads, Contacts and Addresses."
app_email = "support@sigzen.com"
app_license = "mit"

# Apps
# ------------------

# ERPNext is a HARD dependency, not a soft one: overrides/lead.py does
# `from erpnext.crm.doctype.lead.lead import Lead` at import time, and this app extends
# Lead, Contact and Address, all of which ERPNext owns. Without this, `bench install-app
# sigzen_ai_scan_crm_connector` on a Frappe-only site succeeds and then every request dies on an
# ImportError. Declaring it makes bench refuse the install with a readable message instead.
required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# Put the app on the /apps screen. Without this the app installs and works, but a client
# has no way to FIND it: its doctypes are only reachable by typing a URL. The route points
# at the app's own workspace rather than a custom page, so there is one place to configure.
# No logo is declared - Frappe falls back to a generic tile, and shipping a Sigzen mark on a
# client's own ERPNext would be wrong for an app published to the Marketplace.
#
# has_permission is what stops the tile becoming a DEAD END. The route points at a
# workspace whose `roles` are System Manager only, and Frappe does NOT cross-check the two:
# without this hook every Desk user on the client sees a "VC Connector" tile, clicks it, and
# lands on a workspace they are not allowed to open. The client reads that as "the app does
# not work" / "the workspace was never created", when in fact both exist and they simply
# cannot see them.
app_home = "/desk/sigzen---ai-scan-crm-connector"

add_to_apps_screen = [
	{
		"name": "sigzen_ai_scan_crm_connector",
		# Required, not decorative: create_desktop_icons_from_installed_apps() reads
		# app_details[0]["logo"] with no default, and that pass runs before workspace icons
		# are created -- so a missing key leaves the CLIENT's whole site with no desktop
		# icons, failing silently inside a print()-only except block.
		#
		# The mark is deliberately unbranded. This app is installed on a client's own
		# ERPNext and published to the Marketplace; a Sigzen logo on their desk would be
		# wrong, so it is a neutral link glyph that says what the app does and nothing
		# about who wrote it.
		"logo": "/assets/sigzen_ai_scan_crm_connector/images/vc-connector-logo.svg",
		"title": "Sigzen - AI Scan CRM Connector",
		# "/desk/...", not "/app/...". This Frappe renamed the desk mount: /app 301s to
		# /desk, and frappe.apps.is_desk_apps() matches every app's route against
		# ^/desk(/.*)?$ -- one /app route makes that False for the WHOLE site, dropping
		# get_default_path() to "/apps" and flipping boot.is_desk_apps to 0 for every user
		# on the client's ERPNext, not just for this app.
		"route": "/desk/sigzen---ai-scan-crm-connector",
		"has_permission": "sigzen_ai_scan_crm_connector.install.has_app_permission",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/sigzen_ai_scan_crm_connector/css/sigzen_ai_scan_crm_connector.css"
# app_include_js = "/assets/sigzen_ai_scan_crm_connector/js/sigzen_ai_scan_crm_connector.js"

# include js, css files in header of web template
# web_include_css = "/assets/sigzen_ai_scan_crm_connector/css/sigzen_ai_scan_crm_connector.css"
# web_include_js = "/assets/sigzen_ai_scan_crm_connector/js/sigzen_ai_scan_crm_connector.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "sigzen_ai_scan_crm_connector/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "sigzen_ai_scan_crm_connector/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "sigzen_ai_scan_crm_connector.utils.jinja_methods",
# 	"filters": "sigzen_ai_scan_crm_connector.utils.jinja_filters"
# }

# Installation
# ------------

# On a site that has this app under its old package name, rename that install instead of
# installing a second copy (upgrade/from_vc_connector.py).
before_install = "sigzen_ai_scan_crm_connector.upgrade.from_vc_connector.before_install"
after_install = "sigzen_ai_scan_crm_connector.install.after_install"
# The module and the workspace were renamed. This has to run before every patch and before
# the model sync, or each DocType of this app fails to load -- see brand_rename.py.
before_migrate = ["sigzen_ai_scan_crm_connector.brand_rename.execute"]
after_migrate = "sigzen_ai_scan_crm_connector.install.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "sigzen_ai_scan_crm_connector.uninstall.before_uninstall"
# after_uninstall = "sigzen_ai_scan_crm_connector.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "sigzen_ai_scan_crm_connector.utils.before_app_install"
# after_app_install = "sigzen_ai_scan_crm_connector.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "sigzen_ai_scan_crm_connector.utils.before_app_uninstall"
# after_app_uninstall = "sigzen_ai_scan_crm_connector.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "sigzen_ai_scan_crm_connector.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# EXTEND, not override. `override_doctype_class` replaces the Lead controller outright, and
# only one app on a site can win that: a client who also runs another app that overrides Lead
# would silently lose one of the two. `extend_doctype_class` mixes this class in front of
# whatever controller the site already resolves for Lead, so both keep working.

extend_doctype_class = {
	"Lead": ["sigzen_ai_scan_crm_connector.overrides.lead.CustomLead"],
}

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"sigzen_ai_scan_crm_connector.tasks.all"
# 	],
# 	"daily": [
# 		"sigzen_ai_scan_crm_connector.tasks.daily"
# 	],
# 	"hourly": [
# 		"sigzen_ai_scan_crm_connector.tasks.hourly"
# 	],
# 	"weekly": [
# 		"sigzen_ai_scan_crm_connector.tasks.weekly"
# 	],
# 	"monthly": [
# 		"sigzen_ai_scan_crm_connector.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "sigzen_ai_scan_crm_connector.install.before_tests"

# Overriding Methods
# ------------------------------
#
# The API was first served under the package name `vc_connector`. A central site built before
# the rename still calls those paths, and a client must not lose deliveries because the two
# sides upgraded on different days. frappe resolves this map BEFORE importing the path, so
# the old spelling keeps working although no such package exists any more.
override_whitelisted_methods = {
	"vc_connector.api.v1.sync.ping": "sigzen_ai_scan_crm_connector.api.v1.sync.ping",
	"vc_connector.api.v1.sync.upsert_lead_graph": "sigzen_ai_scan_crm_connector.api.v1.sync.upsert_lead_graph",
	"vc_connector.api.v1.sync.pull_changes": "sigzen_ai_scan_crm_connector.api.v1.sync.pull_changes",
	"vc_connector.api.v1.legacy_sync.ping_remote_sync": "sigzen_ai_scan_crm_connector.api.v1.legacy_sync.ping_remote_sync",
	"vc_connector.api.v1.legacy_sync.upsert_document": "sigzen_ai_scan_crm_connector.api.v1.legacy_sync.upsert_document",
	"vc_connector.api.v1.legacy_sync.issue_connector_token": "sigzen_ai_scan_crm_connector.api.v1.legacy_sync.issue_connector_token",
}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "sigzen_ai_scan_crm_connector.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["sigzen_ai_scan_crm_connector.utils.before_request"]
# after_request = ["sigzen_ai_scan_crm_connector.utils.after_request"]

# Job Events
# ----------
# before_job = ["sigzen_ai_scan_crm_connector.utils.before_job"]
# after_job = ["sigzen_ai_scan_crm_connector.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"sigzen_ai_scan_crm_connector.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }
