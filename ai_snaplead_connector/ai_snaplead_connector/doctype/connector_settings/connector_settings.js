// Copyright (c) 2026, support@sigzen.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Connector Settings", {
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
							"ai_snaplead_connector.ai_snaplead_connector.doctype.connector_settings.connector_settings.generate_connector_token",
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
