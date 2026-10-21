// SPDX-License-Identifier: AGPL-3.0-or-later
// The sources whose owner approves the connection through OAuth 2 (core.source.OAuthSource).
const OAUTH_SOURCES = ["Etsy", "QuickBooks Online", "Xero", "Zoho Books", "Zoho Inventory", "Acumatica", "Agendrix", "HighLevel", "HubSpot", "Salesforce", "Pipedrive", "Zoho CRM"];

frappe.ui.form.on("Importer Connection", {
	refresh(frm) {
		if (frm.is_new()) return;
		const run = (full, dry_run, label) =>
			frm.add_custom_button(
				label,
				() =>
					frappe.call({
						method: "idempotent_importer.api.sync_now",
						args: { connection: frm.doc.name, full, dry_run },
						freeze: true,
						freeze_message: __("Starting the run…"),
						callback: (r) => {
							frappe.show_alert({
								message: r.message.started
									? __("Run started; it continues in the background.")
									: __("A run of this connection is already in progress."),
								indicator: "green",
							});
							frm.reload_doc();
						},
					}),
				__("Sync")
			);
		if (OAUTH_SOURCES.includes(frm.doc.source)) {
			frm.add_custom_button(__("Connect to {0}", [frm.doc.source]), () =>
				frappe.call({
					method: "idempotent_importer.api.connect",
					args: { connection: frm.doc.name },
					callback: (r) => {
						window.location.href = r.message;
					},
				})
			);
		}
		run(0, 0, __("Sync Now"));
		run(1, 0, __("Full Run"));
		run(1, 1, __("Dry Run"));
		if (frm.doc.credential_hold) {
			frm.dashboard.set_headline_alert(
				__("The source rejected the credentials; scheduled runs are held until this connection is saved or a run is started by hand."),
				"red"
			);
		}
	},
});
