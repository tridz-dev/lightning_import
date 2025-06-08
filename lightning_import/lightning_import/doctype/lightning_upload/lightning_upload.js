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
    refresh: function(frm) {
        // Show Start Import button only when status is Draft and document is saved
        if (!frm.is_new() && frm.doc.status === "Draft") {
            frm.page.set_primary_action(__('Start Import'), () => {
                start_import(frm);
            });
        } else {
            // Remove the primary action button if not in Draft status
            frm.page.clear_primary_action();
        }

        // Show Export Error Rows button only for Failed or Partial Success
        if (frm.doc.status === 'Failed' || frm.doc.status === 'Partial Success') {
            frm.add_custom_button(__('Export Error Rows'), () => {
                export_error_rows(frm);
            });
        }

        // Set up progress tracking if import is in progress
        if (frm.doc.status === 'Queued' || frm.doc.status === 'In Progress') {
            setup_progress_tracking(frm);
        }
    },

    onload: function(frm) {
        // Set up progress tracking when form loads if import is in progress
        if (frm.doc.status === 'Queued' || frm.doc.status === 'In Progress') {
            setup_progress_tracking(frm);
        }
    }
});

// Global event handler for import progress
frappe.realtime.on('import_progress', function(data) {
    const frm = frappe.get_form('Lightning Upload');
    if (!frm || !data) return;

    const progress_key = `lightning_import_${frm.doc.name}`;
    if (data.progress_key !== progress_key) return;

    update_progress(frm, data);
});

function setup_progress_tracking(frm) {
    // Clear any existing progress bar
    if (frm.progress_bar) {
        frm.progress_bar.remove();
    }

    // Create progress bar container
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

    // Set up polling for progress updates
    const progress_key = `lightning_import_${frm.doc.name}`;
    frm.progress_key = progress_key;
    
    // Start polling
    poll_progress(frm);
}

function poll_progress(frm) {
    if (!frm.progress_key) return;

    frappe.call({
        method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_import_progress',
        args: { progress_key: frm.progress_key },
        callback: function(r) {
            if (r.message) {
                update_progress(frm, r.message);
                
                // Continue polling if still in progress
                if (r.message.status === 'In Progress' || r.message.status === 'Queued') {
                    setTimeout(() => poll_progress(frm), 1000);
                }
            }
        }
    });
}

function update_progress(frm, data) {
    if (!data || !frm.progress_bar) return;
    
    // Update progress bar
    frm.progress_bar.find('.progress-bar')
        .css('width', `${data.progress}%`)
        .attr('aria-valuenow', data.progress);
    frm.progress_bar.find('.progress-status').html(data.title);

    // Update status in form
    if (data.status) {
        frm.set_value('status', data.status);
        frm.refresh_field('status');
    }

    // Update other fields if available
    if (data.successful_records !== undefined) {
        frm.set_value('successful_records', data.successful_records);
        frm.refresh_field('successful_records');
    }
    if (data.failed_records !== undefined) {
        frm.set_value('failed_records', data.failed_records);
        frm.refresh_field('failed_records');
    }
    if (data.import_time) {
        frm.set_value('import_time', data.import_time);
        frm.refresh_field('import_time');
    }

    // Handle completion
    if (data.status === 'Completed' || data.status === 'Failed' || data.status === 'Partial Success') {
        let message = '';
        if (data.status === 'Completed') {
            message = __(`Successfully imported ${data.successful_records} records`);
            if (data.time_taken) {
                message += __(`, time taken: ${data.time_taken}`);
            }
        } else if (data.status === 'Partial Success') {
            message = __(`Import partially completed. ${data.successful_records} records imported, ${data.failed_records} failed`);
            if (data.time_taken) {
                message += __(`, time taken: ${data.time_taken}`);
            }
        } else {
            message = __('Import failed');
        }

        frappe.show_alert({
            message: message,
            indicator: data.status === 'Completed' ? 'green' : (data.status === 'Partial Success' ? 'orange' : 'red'),
            timeout: 10
        });

        // Remove progress bar and refresh form
        if (frm.progress_bar) {
            frm.progress_bar.remove();
            frm.progress_bar = null;
        }
        
        // Refresh the form to update buttons
        frm.reload_doc();
    }
}

function start_import(frm) {
    // Add progress bar before starting import
    if (!frm.progress_bar) {
        frm.progress_bar = $(`
            <div class="progress-bar-container" style="margin: 20px 0;">
                <div class="progress" style="height: 20px; margin-bottom: 10px;">
                    <div class="progress-bar" role="progressbar" style="width: 0%;" 
                        aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
                    </div>
                </div>
                <div class="progress-status text-muted">Starting import...</div>
            </div>
        `).insertAfter(frm.page.main);
    }

    frappe.call({
        method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.start_import',
        args: {
            docname: frm.doc.name
        },
        callback: function(r) {
            if (r.message && r.message.status === 'success') {
                frappe.show_alert({
                    message: r.message.message,
                    indicator: 'green'
                });
                // Set up progress tracking immediately
                setup_progress_tracking(frm);
            } else {
                // Hide progress bar on error
                if (frm.progress_bar) {
                    frm.progress_bar.remove();
                    frm.progress_bar = null;
                }
                frappe.show_alert({
                    message: r.message.message || __('Failed to start import'),
                    indicator: 'red'
                });
            }
        }
    });
}

function export_error_rows(frm) {
    frappe.call({
        method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.export_error_rows',
        args: {
            docname: frm.doc.name
        },
        callback: function(r) {
            if (r.message && r.message.status === 'success') {
                window.open(r.message.file_url, '_blank');
            } else {
                frappe.show_alert({
                    message: r.message.message || __('Failed to export error rows'),
                    indicator: 'red'
                });
            }
        }
    });
}
