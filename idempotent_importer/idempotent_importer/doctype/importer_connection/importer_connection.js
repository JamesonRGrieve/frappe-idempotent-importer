// SPDX-License-Identifier: AGPL-3.0-or-later
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
		if (frm.doc.source === "Etsy") {
			frm.add_custom_button(__("Connect to Etsy"), () =>
				frappe.call({
					method: "idempotent_importer.api.etsy_connect",
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
