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
    current_title: '',
    current_form: null
};

// Debug socket connection
frappe.realtime.on('socket_connected', () => {
    console.log('[Lightning Import] Socket connected');
});

frappe.realtime.on('socket_disconnected', () => {
    console.log('[Lightning Import] Socket disconnected');
});

frappe.ui.form.on('Lightning Upload', {
    refresh: function(frm) {
        // Store reference to current form
        frappe.progress_state.current_form = frm;
        
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
        // Store reference to current form
        frappe.progress_state.current_form = frm;
        
        // Set up progress tracking when form loads if import is in progress
        if (frm.doc.status === 'Queued' || frm.doc.status === 'In Progress') {
            setup_progress_tracking(frm);
        }
    }
});

// Global event handler for import progress
frappe.realtime.on('import_progress', function(data) {
    console.log('[Lightning Import] Received import_progress event:', data);
    
    const frm = frappe.progress_state.current_form;
    if (!frm) {
        console.log('[Lightning Import] No form found in progress_state');
        return;
    }

    // If we have a progress key in the event, verify it matches
    if (data.progress_key) {
        const formProgressKey = `lightning_import_${frm.doc.name}`;
        console.log('[Lightning Import] Progress key check:', {
            received: data.progress_key,
            expected: formProgressKey,
            matches: data.progress_key === formProgressKey
        });
        if (data.progress_key !== formProgressKey) {
            console.log('[Lightning Import] Progress key mismatch, ignoring event');
            return;
        }
    }

    console.log('[Lightning Import] Updating progress with data:', data);
    update_progress(frm, data);
});

function setup_progress_tracking(frm) {
    console.log('[Lightning Import] Setting up progress tracking for form:', frm.doc.name);
    
    // Clear any existing progress bar
    if (frm.progress_bar) {
        console.log('[Lightning Import] Removing existing progress bar');
        frm.progress_bar.remove();
    }

    // Create progress bar container
    console.log('[Lightning Import] Creating new progress bar');
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

function update_progress(frm, data) {
    console.log('[Lightning Import] update_progress called with:', {
        form: frm.doc.name,
        data: data,
        hasProgressBar: !!frm.progress_bar
    });
    
    if (!data || !frm.progress_bar) {
        console.log('[Lightning Import] Missing data or progress bar, skipping update');
        return;
    }
    
    // Update progress bar
    console.log('[Lightning Import] Updating progress bar to:', data.progress + '%');
    frm.progress_bar.find('.progress-bar')
        .css('width', `${data.progress}%`)
        .attr('aria-valuenow', data.progress);
    frm.progress_bar.find('.progress-status').html(data.title);

    // Update status in form without marking as dirty
    if (data.status) {
        console.log('[Lightning Import] Updating status to:', data.status);
        frm.doc.status = data.status;
        frm.refresh_field('status');
        // pill is updating only after update of workflow_state and header
        frm.refresh_field('workflow_state');
        frm.refresh_header();
    }

    // Update other fields if available
    if (data.successful_records !== undefined) {
        console.log('[Lightning Import] Updating successful_records to:', data.successful_records);
        frm.doc.successful_records = data.successful_records;
        frm.refresh_field('successful_records');
    }
    if (data.failed_records !== undefined) {
        console.log('[Lightning Import] Updating failed_records to:', data.failed_records);
        frm.doc.failed_records = data.failed_records;
        frm.refresh_field('failed_records');
    }
    if (data.import_time) {
        console.log('[Lightning Import] Updating import_time to:', data.import_time);
        frm.doc.import_time = data.import_time;
        frm.refresh_field('import_time');
    }
    if (data.total_records !== undefined) {
        console.log('[Lightning Import] Updating total_records to:', data.total_records);
        frm.doc.total_records = data.total_records;
        frm.refresh_field('total_records');
    }

    // Handle completion
    if (data.status === 'Completed' || data.status === 'Failed' || data.status === 'Partial Success') {
        console.log('[Lightning Import] Import completed with status:', data.status);
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
            if (data.error) {
                message += `: ${data.error}`;
            }
        }

        console.log('[Lightning Import] Showing completion alert:', message);
        frappe.show_alert({
            message: message,
            indicator: data.status === 'Completed' ? 'green' : (data.status === 'Partial Success' ? 'orange' : 'red'),
            timeout: 10
        });

        // Remove progress bar
        if (frm.progress_bar) {
            console.log('[Lightning Import] Removing progress bar after completion');
            frm.progress_bar.remove();
            frm.progress_bar = null;
        }
        
        // Update buttons based on status
        if (data.status === 'Failed' || data.status === 'Partial Success') {
            // Check if the Export Error Rows button already exists
            const hasExportButton = frm.page.custom_buttons && 
                frm.page.custom_buttons.some(btn => btn.label === __('Export Error Rows'));
            
            if (!hasExportButton) {
                frm.add_custom_button(__('Export Error Rows'), () => {
                    export_error_rows(frm);
                });
            }
        }
        
        // Refresh primary action button
        if (data.status === 'Draft') {
            frm.page.set_primary_action(__('Start Import'), () => {
                start_import(frm);
            });
        } else {
            frm.page.clear_primary_action();
        }

        // Force a full form refresh after completion
        setTimeout(() => {
            frm.reload_doc();
        }, 1000);
    }
}

function start_import(frm) {
    console.log('[Lightning Import] Starting import for form:', frm.doc.name);
    
    // Add progress bar before starting import
    if (!frm.progress_bar) {
        console.log('[Lightning Import] Creating initial progress bar');
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
            console.log('[Lightning Import] Start import callback:', r);
            if (r.message && r.message.status === 'success') {
                console.log('[Lightning Import] Import started successfully');
                frappe.show_alert({
                    message: r.message.message,
                    indicator: 'green'
                });
            } else {
                console.log('[Lightning Import] Failed to start import:', r.message);
                // Hide progress bar on error
                if (frm.progress_bar) {
                    console.log('[Lightning Import] Removing progress bar due to error');
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
