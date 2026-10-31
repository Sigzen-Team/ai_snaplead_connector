# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Desk URLs from before the workspace was renamed keep working (bookmarks, links in emails).

The workspace's name is its route. It was "AI SnapLead Connector" (/desk/ai-snaplead-connector),
before that "AI SnapLead - Connector" (/desk/ai-snaplead---connector, frappe's slug only turns
spaces into hyphens) and before that "Sigzen - AI Scan CRM Connector"; it is "SigzenLead Connector"
now (/desk/sigzenlead-connector).

website_redirects cannot do this: frappe's path resolver serves every /desk/* path as the Desk
page BEFORE it looks at redirects, so the single-page app would open on a workspace that no
longer exists. This runs as a before_request hook instead, and costs a prefix check on every
other request.
"""

import frappe
from werkzeug.exceptions import HTTPException
from werkzeug.utils import redirect

#: Old desk route -> new one.
RENAMED_DESK_ROUTES = {
	"/desk/ai-snaplead-connector": "/desk/sigzenlead-connector",
	"/desk/ai-snaplead---connector": "/desk/sigzenlead-connector",
	"/desk/sigzen---ai-scan-crm-connector": "/desk/sigzenlead-connector",
	"/desk/sigzen-scan-to-crm-erpnext-connector": "/desk/sigzenlead-connector",
	"/desk/vc-connector": "/desk/sigzenlead-connector",
}


def redirect_renamed_desk_routes():
	"""``before_request``: 301 an old desk route (and anything below it) to its new name."""
	request = getattr(frappe.local, "request", None)
	if not request or request.method not in ("GET", "HEAD"):
		return
	path = (request.path or "").rstrip("/")
	if not path.startswith("/desk/"):
		return
	target = renamed_desk_route(path)
	if not target:
		return
	query = request.query_string.decode() if request.query_string else ""
	raise HTTPException(response=redirect(f"{target}?{query}" if query else target, code=301))


def renamed_desk_route(path):
	"""The current route for an old one, or None when `path` was never renamed."""
	path = (path or "").rstrip("/")
	for old, new in RENAMED_DESK_ROUTES.items():
		if path == old or path.startswith(old + "/"):
			return new + path[len(old) :]
	return None
