app_name = "ai_snaplead_connector"
# The package is named after the app. It was `vc_connector` before its first public release and
# `sigzen_ai_scan_crm_connector` in release 1.0.0 (when the product was "Sigzen - AI Scan CRM").
# The API paths under both old names still answer (see override_whitelisted_methods below), and a
# site that installed an old package is renamed by the ordinary `bench migrate` (see
# upgrade/from_old_package.py and the stand-in packages vc_connector/ and
# sigzen_ai_scan_crm_connector/ next to this one).
#
# The Marketplace listing name and the Desk tile:
app_title = "AI SnapLead - Connector"
app_publisher = "Sigzen Solutions"
app_description = (
	"Receive business-card leads from AI SnapLead into ERPNext as standard Leads, Contacts and Addresses."
)
app_email = "support@sigzen.com"
app_license = "mit"

# Apps
# ------------------

# ERPNext is a HARD dependency, not a soft one: overrides/lead.py does
# `from erpnext.crm.doctype.lead.lead import Lead` at import time, and this app extends
# Lead, Contact and Address, all of which ERPNext owns. Without this, `bench install-app
# ai_snaplead_connector` on a Frappe-only site succeeds and then every request dies on an
# ImportError. Declaring it makes bench refuse the install with a readable message instead.
required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# Put the app on the /apps screen. Without this the app installs and works, but a client
# has no way to FIND it: its doctypes are only reachable by typing a URL. The route points
# at the app's own workspace rather than a custom page, so there is one place to configure.
# The logo is the AI SnapLead - Connector mark (see the "logo" key below).
#
# app_logo_url is deliberately NOT set. Frappe already takes this app's logo for /apps, the
# app switcher and the sidebar header from add_to_apps_screen. And navbar_settings.get_app_logo()
# picks the site's navbar logo from the LIST of every app's app_logo_url by count -- with exactly
# frappe + erpnext it shows ERPNext's; a third entry drops every client's navbar to the Frappe logo.
#
# has_permission is what stops the tile becoming a DEAD END. The route points at a
# workspace whose `roles` are System Manager only, and Frappe does NOT cross-check the two:
# without this hook every Desk user on the client sees a "VC Connector" tile, clicks it, and
# lands on a workspace they are not allowed to open. The client reads that as "the app does
# not work" / "the workspace was never created", when in fact both exist and they simply
# cannot see them.
app_home = "/desk/ai-snaplead-connector"

add_to_apps_screen = [
	{
		"name": "ai_snaplead_connector",
		# Required, not decorative: create_desktop_icons_from_installed_apps() reads
		# app_details[0]["logo"] with no default, and that pass runs before workspace icons
		# are created -- so a missing key leaves the CLIENT's whole site with no desktop
		# icons, failing silently inside a print()-only except block.
		#
		# The AI SnapLead family mark (scan frame around a database: scanned cards landing in
		# this ERPNext). images/vc-connector-logo.svg, the path 1.1.0 and earlier shipped, holds
		# the same mark for icons that still point there; install.repair_desk() repoints them.
		"logo": "/assets/ai_snaplead_connector/images/ai-snaplead-connector-logo.svg",
		"title": "AI SnapLead - Connector",
		# "/desk/...", not "/app/...". This Frappe renamed the desk mount: /app 301s to
		# /desk, and frappe.apps.is_desk_apps() matches every app's route against
		# ^/desk(/.*)?$ -- one /app route makes that False for the WHOLE site, dropping
		# get_default_path() to "/apps" and flipping boot.is_desk_apps to 0 for every user
		# on the client's ERPNext, not just for this app.
		"route": "/desk/ai-snaplead-connector",
		"has_permission": "ai_snaplead_connector.install.has_app_permission",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ai_snaplead_connector/css/ai_snaplead_connector.css"
# app_include_js = "/assets/ai_snaplead_connector/js/ai_snaplead_connector.js"

# include js, css files in header of web template
# web_include_css = "/assets/ai_snaplead_connector/css/ai_snaplead_connector.css"
# web_include_js = "/assets/ai_snaplead_connector/js/ai_snaplead_connector.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ai_snaplead_connector/public/scss/website"

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
# app_include_icons = "ai_snaplead_connector/public/icons.svg"

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
# 	"methods": "ai_snaplead_connector.utils.jinja_methods",
# 	"filters": "ai_snaplead_connector.utils.jinja_filters"
# }

# Installation
# ------------

# Rebuilds a stale module map so the install syncs this app's DocTypes, and on a site that has
# this app under its old package name renames that install instead of installing a second copy.
before_install = "ai_snaplead_connector.install.before_install"
after_install = "ai_snaplead_connector.install.after_install"
# The module and the workspace were renamed. This has to run before every patch and before
# the model sync, or each DocType of this app fails to load -- see brand_rename.py.
# check_frappe_version first: on Frappe < 16 extend_doctype_class does not exist, so refuse
# the migrate with a clear message rather than leave Lead without this app's extension.
before_migrate = [
	"ai_snaplead_connector.install.check_frappe_version",
	"ai_snaplead_connector.brand_rename.execute",
]
after_migrate = "ai_snaplead_connector.install.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "ai_snaplead_connector.uninstall.before_uninstall"
# after_uninstall = "ai_snaplead_connector.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ai_snaplead_connector.utils.before_app_install"
# after_app_install = "ai_snaplead_connector.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ai_snaplead_connector.utils.before_app_uninstall"
# after_app_uninstall = "ai_snaplead_connector.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ai_snaplead_connector.notifications.get_notification_config"

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
	"Lead": ["ai_snaplead_connector.overrides.lead.CustomLead"],
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
# 		"ai_snaplead_connector.tasks.all"
# 	],
# 	"daily": [
# 		"ai_snaplead_connector.tasks.daily"
# 	],
# 	"hourly": [
# 		"ai_snaplead_connector.tasks.hourly"
# 	],
# 	"weekly": [
# 		"ai_snaplead_connector.tasks.weekly"
# 	],
# 	"monthly": [
# 		"ai_snaplead_connector.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "ai_snaplead_connector.install.before_tests"

# Overriding Methods
# ------------------------------
#
# The API was served under two earlier package names: `vc_connector` (development builds) and
# `sigzen_ai_scan_crm_connector` (release 1.0.0). A central site built before a rename still calls
# those paths, and a client must not lose deliveries because the two sides upgraded on different
# days. frappe resolves this map BEFORE importing the path, so the old spellings keep working
# although those packages hold no code any more (only the stand-ins that rename a site).
override_whitelisted_methods = {
	# sigzen_ai_scan_crm_connector
	"sigzen_ai_scan_crm_connector.api.v1.sync.ping": "ai_snaplead_connector.api.v1.sync.ping",
	"sigzen_ai_scan_crm_connector.api.v1.sync.upsert_lead_graph": "ai_snaplead_connector.api.v1.sync.upsert_lead_graph",
	"sigzen_ai_scan_crm_connector.api.v1.sync.pull_changes": "ai_snaplead_connector.api.v1.sync.pull_changes",
	"sigzen_ai_scan_crm_connector.api.v1.legacy_sync.ping_remote_sync": "ai_snaplead_connector.api.v1.legacy_sync.ping_remote_sync",
	"sigzen_ai_scan_crm_connector.api.v1.legacy_sync.upsert_document": "ai_snaplead_connector.api.v1.legacy_sync.upsert_document",
	"sigzen_ai_scan_crm_connector.api.v1.legacy_sync.issue_connector_token": "ai_snaplead_connector.api.v1.legacy_sync.issue_connector_token",
	# vc_connector
	"vc_connector.api.v1.sync.ping": "ai_snaplead_connector.api.v1.sync.ping",
	"vc_connector.api.v1.sync.upsert_lead_graph": "ai_snaplead_connector.api.v1.sync.upsert_lead_graph",
	"vc_connector.api.v1.sync.pull_changes": "ai_snaplead_connector.api.v1.sync.pull_changes",
	"vc_connector.api.v1.legacy_sync.ping_remote_sync": "ai_snaplead_connector.api.v1.legacy_sync.ping_remote_sync",
	"vc_connector.api.v1.legacy_sync.upsert_document": "ai_snaplead_connector.api.v1.legacy_sync.upsert_document",
	"vc_connector.api.v1.legacy_sync.issue_connector_token": "ai_snaplead_connector.api.v1.legacy_sync.issue_connector_token",
	# Connector Settings buttons: a desk page loaded before the upgrade still calls the old path.
	"sigzen_ai_scan_crm_connector.sigzen___ai_scan_crm_connector.doctype.connector_settings.connector_settings.generate_connector_token": (
		"ai_snaplead_connector.ai_snaplead_connector.doctype.connector_settings.connector_settings.generate_connector_token"
	),
	"sigzen_ai_scan_crm_connector.sigzen___ai_scan_crm_connector.doctype.connector_settings.connector_settings.repair_business_id": (
		"ai_snaplead_connector.ai_snaplead_connector.doctype.connector_settings.connector_settings.repair_business_id"
	),
}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ai_snaplead_connector.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# The workspace's route lost its "---" (/desk/ai-snaplead---connector -> /desk/ai-snaplead-connector).
# Not website_redirects: frappe serves /desk/* before it reads those. See utils/renamed_routes.py.
before_request = ["ai_snaplead_connector.utils.renamed_routes.redirect_renamed_desk_routes"]
# after_request = ["ai_snaplead_connector.utils.after_request"]

# Job Events
# ----------
# before_job = ["ai_snaplead_connector.utils.before_job"]
# after_job = ["ai_snaplead_connector.utils.after_job"]

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
# 	"ai_snaplead_connector.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# Log Settings clears Connector Log rows older than this (days). One row per push adds up.
default_log_clearing_doctypes = {
	"Connector Log": 90,
}
