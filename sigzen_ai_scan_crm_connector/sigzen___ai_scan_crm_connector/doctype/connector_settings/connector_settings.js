// Copyright (c) 2026, support@sigzen.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Connector Settings", {
	onload(frm) {
		frm._saved_business_id = frm.doc.business_id;
	},

	after_save(frm) {
		frm._saved_business_id = frm.doc.business_id;
	},

	before_save(frm) {
		// Changing the Business ID re-stamps every row that carries the old one (see
		// validate_business_id_change). Say how many, and let the user back out, before it does.
		const previous = (frm._saved_business_id || "").trim();
		const next = (frm.doc.business_id || "").trim();
		if (!previous || !next || previous === next) return;

		return new Promise((resolve) => {
			frappe.call({
				method:
					"sigzen_ai_scan_crm_connector.sigzen___ai_scan_crm_connector.doctype.connector_settings.connector_settings.preview_business_id_change",
				args: { business_id: previous },
				callback: (r) => {
					const c = r.message || {};
					frappe.confirm(
						__(
							"Change Business ID from {0} to {1}? {2} lead link(s), {3} source link(s) and {4} lead(s) still on {0} will move to {1}. On the central site, the ERPNext Connector of business {1} must point to this site.",
							[previous, next, c.lead_links || 0, c.source_links || 0, c.leads || 0]
						),
						() => resolve(),
						() => {
							frappe.validated = false;
							resolve();
						}
					);
				},
				error: () => {
					frappe.validated = false;
					resolve();
				},
			});
		});
	},

	refresh(frm) {
		// Only the hash of the token is stored, so a token cannot be read back. Without a
		// button here the only way to set one up was to hash a random string by hand and
		// paste both halves in the right places -- which surfaced as TOKEN_INVALID on the
		// first push if you got it wrong.
		frm.add_custom_button(__("Generate Connector Token"), () => {
			frappe.confirm(
				__(
					"This replaces any existing token. The central site will stop pushing until you paste the new one into its ERPNext Connector. Continue?"
				),
				() => {
					frappe.call({
						method:
							"sigzen_ai_scan_crm_connector.sigzen___ai_scan_crm_connector.doctype.connector_settings.connector_settings.generate_connector_token",
						freeze: true,
						freeze_message: __("Generating..."),
						callback: (r) => {
							if (!r.message || !r.message.token) return;
							frm.reload_doc();
							frappe.msgprint({
								title: __("Connector Token"),
								indicator: "orange",
								message: `
									<p>${__("Copy this now. It is shown once and cannot be recovered.")}</p>
									<pre style="user-select:all;padding:10px;background:var(--fg-color);border:1px solid var(--border-color);border-radius:6px;white-space:pre-wrap;word-break:break-all">${frappe.utils.escape_html(
										r.message.token
									)}</pre>
									<p>${__("Paste it into the central site: ERPNext Connector &rarr; Connector Token.")}</p>
								`,
							});
						},
					});
				}
			);
		});
	},
});
