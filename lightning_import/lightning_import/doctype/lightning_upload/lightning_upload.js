// Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
// For license information, please see license.txt

// frappe.ui.form.on("Lightning Upload", {
// 	refresh(frm) {

// 	},
// });

// Store progress state globally
frappe.progress_state = {
    is_importing: false,
    progress_bar: null,
    current_progress: 0,
    current_title: ''
};

frappe.ui.form.on('Lightning Upload', {
    refresh(frm) {
        // Show Start Import button only when status is Draft and document is saved
        if (!frm.is_new() && frm.doc.status === "Draft") {
            frm.page.set_primary_action(__('Start Import'), () => {
                // Add progress bar container if it doesn't exist
                if (!frm.progress_bar) {
                    frm.progress_bar = $(`
                        <div class="progress-bar-container" style="margin: 20px 0; display: none;">
                            <div class="progress" style="height: 20px; margin-bottom: 10px;">
                                <div class="progress-bar" role="progressbar" style="width: 0%;" 
                                    aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
                                </div>
                            </div>
                            <div class="progress-status text-muted"></div>
                        </div>
                    `).insertAfter(frm.page.main);
                }

                // Show progress bara
                frm.progress_bar.show();
                frm.progress_bar.find('.progress-bar').css('width', '0%').attr('aria-valuenow', 0);
                frm.progress_bar.find('.progress-status').html('Starting import...');

                frappe.call({
                    method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.start_import',
                    args: {
                        docname: frm.doc.name
                    },
                    callback: function(r) {
                        if (!r.exc && r.message && r.message.progress_key) {
                            frappe.msgprint(__('Import started successfully'));
                            // Start polling for progress
                            frm.progress_key = r.message.progress_key;
                            frm.poll_progress();
                        } else {
                            frm.progress_bar.hide();
                        }
                    }
                });
            });
        } else if (frm.doc.status === "In Progress") {
            // If document is in progress, try to restore progress
            frappe.call({
                method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_import_progress',
                args: {
                    progress_key: `lightning_import_${frm.doc.name}`
                },
                callback: function(r) {
                    if (!r.exc && r.message && r.message.status === "In Progress") {
                        // Add and show progress bar
                        if (!frm.progress_bar) {
                            frm.progress_bar = $(`
                                <div class="progress-bar-container" style="margin: 20px 0;">
                                    <div class="progress" style="height: 20px; margin-bottom: 10px;">
                                        <div class="progress-bar" role="progressbar" style="width: 0%;" 
                                            aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
                                        </div>
                                    </div>
                                    <div class="progress-status text-muted"></div>
                                </div>
                            `).insertAfter(frm.page.main);
                        }
                        frm.progress_bar.show();
                        frm.progress_key = `lightning_import_${frm.doc.name}`;
                        frm.poll_progress();
                    }
                }
            });
        }

        // Add polling method to form
        frm.poll_progress = function() {
            if (!frm.progress_key) return;

            frappe.call({
                method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_import_progress',
                args: {
                    progress_key: frm.progress_key
                },
                callback: function(r) {
                    if (!r.exc && r.message) {
                        const progress = r.message;
                        if (frm.progress_bar) {
                            frm.progress_bar.find('.progress-bar')
                                .css('width', `${progress.progress}%`)
                                .attr('aria-valuenow', progress.progress);
                            frm.progress_bar.find('.progress-status').html(progress.title);

                            if (progress.status === "Complete") {
                                setTimeout(() => {
                                    frm.progress_bar.hide();
                                    frm.reload_doc();
                                }, 2000);
                            } else if (progress.status === "In Progress") {
                                // Continue polling
                                setTimeout(() => frm.poll_progress(), 1000);
                            }
                        }
                    }
                }
            });
        };

        if (frm.doc.status === 'Failed' || frm.doc.status === 'Partial Success') {
            frm.add_custom_button(__('Export Error Rows'), function() {
                frappe.call({
                    method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.export_error_rows',
                    args: {
                        docname: frm.doc.name
                    },
                    callback: function(r) {
                        if (r.message && r.message.status === 'success') {
                            window.open(r.message.file_url, '_blank');
                        } else {
                            frappe.msgprint({
                                title: __('Error'),
                                message: r.message.message || __('Error exporting failed rows'),
                                indicator: 'red'
                            });
                        }
                    }
                });
            });
        }
    }
});

frappe.realtime.on('import_progress', function (data) {
    const frm = frappe.get_form('Lightning Upload');
    if (frm && frm.progress_bar && data) {
        frm.progress_bar.find('.progress-bar')
            .css('width', `${data.progress}%`)
            .attr('aria-valuenow', data.progress);
        frm.progress_bar.find('.progress-status').html(data.title);

        if (data.status === "Complete") {
            setTimeout(() => {
                frm.progress_bar.hide();
                frm.reload_doc();
            }, 2000);
        }
    }
});
