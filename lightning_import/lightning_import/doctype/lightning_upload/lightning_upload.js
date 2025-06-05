// Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
// For license information, please see license.txt

// frappe.ui.form.on("Lightning Upload", {
// 	refresh(frm) {

// 	},
// });

frappe.ui.form.on('Lightning Upload', {
    refresh(frm) {
        // Show Start Import button only when status is Draft and document is saved
        if (!frm.is_new() && frm.doc.status === "Draft") {
            frm.page.set_primary_action(__('Start Import'), () => {
                frappe.call({
                    method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.start_import',
                    args: {
                        docname: frm.doc.name
                    },
                    callback: function(r) {
                        if (!r.exc) {
                            frappe.msgprint(__('Import started successfully'));
                            frm.reload_doc();
                        }
                    }
                });
            });
        } else {
            // Remove the primary action button if conditions are not met
            frm.page.clear_primary_action();
        }
    }
});
  
