// Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
// For license information, please see license.txt

// frappe.ui.form.on("Lightning Upload", {
// 	refresh(frm) {

// 	},
// });

frappe.ui.form.on('Lightning Upload', {
    refresh(frm) {
      if (!frm.is_new()) {
        frm.page.set_primary_action(__('Start Import'), () => {
          // Your custom action
          frappe.call({
            method: 'your_app.api.start_import',
            args: {
              docname: frm.doc.name
            },
            callback: function(r) {
              if (!r.exc) {
                frappe.msgprint(__('Import started successfully'));
              }
            }
          });
        });
      }
    }
  });
  
