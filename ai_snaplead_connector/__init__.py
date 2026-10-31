# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Stand-in for this app's release-1.1 package name. Not an app of its own.

The connector's package was `ai_snaplead_connector` in releases 1.1.x and is `sigzenlead_connector`
now. A site that installed it then still lists `ai_snaplead_connector`, and frappe imports
`ai_snaplead_connector.hooks` for it before it does anything else. This package makes that import
work, so `bench build` and `bench migrate` run, and its only hook renames the site on the first
migrate (see sigzenlead_connector/upgrade/from_old_package.py). Once no site lists
`ai_snaplead_connector`, nothing loads this package again.
"""

__version__ = "0.0.1"
