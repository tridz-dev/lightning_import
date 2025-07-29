// Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
// For license information, please see license.txt

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
            // Add Map Fields button if CSV file is attached
            if (frm.doc.csv_file) {
                frm.add_custom_button(__('Map Fields'), () => {
                    open_field_mapping_dialog(frm);
                });
            }
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
    // Helper function to make the final call to start the import
    const call_start_import_py = (mapping_json = null) => {
        if (!frm.progress_bar) {
            frm.progress_bar = $(`
                <div class="progress-bar-container" style="margin: 20px 0;">
                    <div class="progress" style="height: 20px; margin-bottom: 10px;">
                        <div class="progress-bar" role="progressbar" style="width: 0%;"></div>
                    </div>
                    <div class="progress-status text-muted">Starting import...</div>
                </div>
            `).insertAfter(frm.page.main);
        }

        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.start_import',
            args: {
                docname: frm.doc.name,
                mapping: mapping_json
            },
            callback: function(r) {
                if (r.message && r.message.status === 'success') {
                    frappe.show_alert({
                        message: r.message.message,
                        indicator: 'green'
                    });
                } else {
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
    };

    // Main logic starts here
    if (frm.doc.field_mapping) {
        // If a mapping is already saved, proceed directly.
        call_start_import_py();
    } else {
        // If no mapping exists, perform auto-mapping and validation.
        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.auto_map_and_validate',
            args: { docname: frm.doc.name },
            callback: function(r) {
                if (!r.message) {
                    frappe.msgprint(__('Error during auto-mapping. Please map fields manually.'));
                    return;
                }
                const data = r.message;
                if (data.unmapped_required.length > 0) {
                    // If required fields are missing, ask the user what to do.
                    frappe.confirm(
                        __('The following required fields could not be auto-mapped: <br><b>{0}</b>. <br><br>Rows without these fields will fail to import. Do you want to continue anyway?', [data.unmapped_required.join(', ')]),
                        () => {
                            // User chose to "Continue Anyway"
                            call_start_import_py(JSON.stringify(data.mapping));
                        },
                        () => {
                            // User chose to "Cancel and Map Fields"
                            open_field_mapping_dialog(frm);
                        },
                        __('Missing Required Fields'),
                        __('Continue Anyway'),
                        __('Cancel and Map Fields')
                    );
                } else {
                    // If all required fields were auto-mapped, start the import.
                    frappe.show_alert({
                        message: __('All required fields were auto-mapped. Starting import...'),
                        indicator: 'green'
                    });
                    call_start_import_py(JSON.stringify(data.mapping));
                }
            }
        });
    }
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

// --- Field Mapping Dialog ---
function open_field_mapping_dialog(frm) {
    frappe.call({
        method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_csv_headers_for_upload',
        args: { docname: frm.doc.name },
        callback: function(csvRes) {
            if (!csvRes.message || csvRes.message.status !== 'success') {
                frappe.show_alert({ message: csvRes.message ? csvRes.message.message : __('Failed to fetch CSV headers'), indicator: 'red' });
                return;
            }

            const csvHeaders = csvRes.message.headers;
            frappe.call({
                method: 'lightning_import.lightning_import.api.get_fields.get_doctype_fields',
                args: { doctype: frm.doc.import_doctype },
                callback: function (dtRes) {
                    if (!dtRes.message || !dtRes.message.fields) {
                        frappe.show_alert({ message: __('Failed to fetch DocType fields'), indicator: 'red' });
                        return;
                    }
            
                    const fieldOptions = dtRes.message.fields || [];
                    
                    const normalize = str => (typeof str === 'string' ? str.toLowerCase().replace(/[\s_]+/g, '') : '');
            
                    const normalizedFieldMap = {};
                    fieldOptions.forEach(f => {
                        if (f.fieldname) {
                            normalizedFieldMap[normalize(f.fieldname)] = f.fieldname;
                            if (f.label) {
                                normalizedFieldMap[normalize(f.label)] = f.fieldname;
                            }
                        }
                    });
                    normalizedFieldMap['id'] = 'name';
                    normalizedFieldMap['name'] = 'first_name'
            
                    frappe.model.with_doctype(frm.doc.import_doctype, () => {
                        const meta = frappe.get_meta(frm.doc.import_doctype);
                        const requiredFields = meta.fields.filter(f => f.reqd).map(f => f.fieldname);
            
                        let existingMapping = {};
                        try {
                            if (frm.doc.field_mapping) {
                                existingMapping = JSON.parse(frm.doc.field_mapping);
                            }
                        } catch (e) { 
                            console.log('Error parsing existing mapping:', e);
                        }
            
                        const mapping = {};
                        csvHeaders.forEach(header => {
                            const normalizedHeader = normalize(header);
                            if (existingMapping[header]) {
                                mapping[header] = existingMapping[header];
                            } else if (normalizedFieldMap[normalizedHeader]) {
                                mapping[header] = normalizedFieldMap[normalizedHeader];
                            } else {
                                mapping[header] = '';
                            }
                        });
            
                        let tableHtml = `<div style="margin-bottom:16px"><b>Map columns from <span style='color:#007bff'>${frappe.utils.escape_html(frm.doc.csv_file.split('/').pop())}</span> to fields in <span style='color:#007bff'>${frappe.utils.escape_html(frm.doc.import_doctype)}</span></b></div>`;
                        tableHtml += `<table class="table table-bordered" style="width:100%;background:#fff"><thead><tr><th style='width:50%'>CSV Column</th><th style='width:50%'>DocType Field</th></tr></thead><tbody>`;
            
                        csvHeaders.forEach(header => {
                            tableHtml += `<tr><td><input type='text' class='form-control' value='${frappe.utils.escape_html(header)}' readonly tabindex='-1'></td>`;
                            tableHtml += `<td><select class='form-control field-mapping-select' data-header="${frappe.utils.escape_html(header)}">`;
                            tableHtml += `<option value=''>Don't Import</option>`;
                            fieldOptions.forEach(field => {
                                const label = field.label || field.fieldname;
                                const displayText = `${label} (${field.fieldname})`;
                                const selected = mapping[header] === field.fieldname ? 'selected' : '';
                                const escapedDisplay = frappe.utils.escape_html(displayText);
                                const escapedField = frappe.utils.escape_html(field.fieldname);
                                tableHtml += `<option value="${escapedField}" ${selected}>${escapedDisplay}</option>`;
                            });
                            tableHtml += `</select></td></tr>`;
                        });
            
                        tableHtml += `</tbody></table>`;
            
                        const d = new frappe.ui.Dialog({
                            title: __('Map Columns'),
                            fields: [
                                { fieldtype: 'HTML', fieldname: 'mapping_table', options: tableHtml }
                            ],
                            primary_action_label: __('Save Mapping'),
                            primary_action() {
                                const values = {};
                                d.$wrapper.find('.field-mapping-select').each(function () {
                                    const header = $(this).data('header');
                                    const value = $(this).val();

                                    values[header] = value;
                                });
            
                                const mappedFields = Object.values(values).filter(Boolean);
                                const unmappedRequired = requiredFields.filter(f => !mappedFields.includes(f));
                                if (unmappedRequired.length) {
                                    frappe.msgprint(__('Please map all required fields: {0}', [unmappedRequired.join(', ')]));
                                    return;
                                }
            
                                const duplicates = mappedFields.filter((item, idx) => mappedFields.indexOf(item) !== idx);
                                if (duplicates.length) {
                                    frappe.msgprint(__('Duplicate mapping for: {0}', [duplicates.join(', ')]));
                                    return;
                                }
            
                                frappe.call({
                                    method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.save_field_mapping',
                                    args: {
                                        docname: frm.doc.name,
                                        mapping: JSON.stringify(values)
                                    },
                                    callback: function (res) {
                                        if (res.message && res.message.status === 'success') {
                                            d.hide();
                                            frm.reload_doc(); 
                                            frappe.show_alert({ message: __('Field mapping saved.'), indicator: 'green' });
                                        } else {
                                            frappe.show_alert({ message: res.message?.message || __('Failed to save mapping'), indicator: 'red' });
                                        }
                                    }
                                });
                            }
                        });
            
                        d.show();
                    });
                }
            });
        }
    });
}