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

async function ensure_doc_saved(frm) {
    if (frm.is_new() || frm.is_dirty() || frm.doc.__unsaved) {
        await frm.save();
    }
}

function setup_buttons(frm) {
    if (!frm.page) return;
    
    frm.page.clear_primary_action();
    frm.clear_custom_buttons();

    if (frm.doc.status === "Draft") {
        if (frm.is_new()) {
            // Before Drafting / Before Save: Show only Save button
            frm.page.set_primary_action(__('Save'), () => frm.save());
        } else {
            // After Drafting / After Save: Keep existing behavior
            if (frm.doc.single_import) {
                // Keep standard Save action as primary if form is dirty, otherwise set Start Import as primary
                if (frm.is_dirty() || frm.doc.__unsaved) {
                    frm.page.set_primary_action(__('Save'), () => frm.save());
                    frm.add_custom_button(__('Start Import'), async () => {
                        await ensure_doc_saved(frm);
                        start_import(frm);
                    });
                } else {
                    frm.page.set_primary_action(__('Start Import'), async () => {
                        await ensure_doc_saved(frm);
                        start_import(frm);
                    });
                }
                if (frm.doc.csv_file) {
                    frm.add_custom_button(__('Map Fields'), async () => {
                        open_field_mapping_dialog(frm);
                    });
                }
            } else if (frm.doc.multiple_import) {
                // Keep standard Save action as primary if form is dirty, otherwise set Start Multi Import as primary
                if (frm.is_dirty() || frm.doc.__unsaved) {
                    frm.page.set_primary_action(__('Save'), () => frm.save());
                    frm.add_custom_button(__('Start Multi Import'), async () => {
                        await ensure_doc_saved(frm);
                        start_multi_import(frm);
                    });
                } else {
                    frm.page.set_primary_action(__('Start Multi Import'), async () => {
                        await ensure_doc_saved(frm);
                        start_multi_import(frm);
                    });
                }
                if (frm.doc.csv_file) {
                    frm.add_custom_button(__('Map Fields'), async () => {
                        frappe.call({
                            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.auto_map_multi_import',
                            args: { docname: frm.doc.name },
                            callback: function (r) {
                                if (r.message && r.message.status === 'success') {
                                    frm.reload_doc().then(() => {
                                        open_combined_multi_mapping_dialog(frm);
                                    });
                                } else {
                                    frappe.msgprint(__('Error during auto-mapping. Please map fields manually.'));
                                }
                            }
                        });
                    });
                }
            }
        }
    }

    // Show Export Error Rows button only for Failed or Partial Success
    if (frm.doc.status === 'Failed' || frm.doc.status === 'Partial Success') {
        frm.add_custom_button(__('Export Error Rows'), () => {
            export_error_rows(frm);
        });
    }
}

// Centralized UI state handler that handles all visibilities, field options, and buttons
function update_import_mode_ui(frm) {
    if (!frm || !frm.doc) return;

    // 1. Toggle field visibilities and requirements
    if (frm.doc.single_import) {
        frm.set_df_property('import_doctype', 'reqd', 1);
        frm.set_df_property('import_doctype', 'hidden', 0);
        frm.set_df_property('import_type', 'reqd', 1);
        frm.set_df_property('import_type', 'hidden', 0);
        frm.set_df_property('update_on_field', 'hidden', frm.doc.import_type !== 'Insert and Update Records' ? 1 : 0);
        frm.set_df_property('duplicate_check_field', 'hidden', 0);
        frm.set_df_property('field_mapping', 'hidden', 0);
        
        frm.set_df_property('multi_import_targets', 'reqd', 0);
        frm.set_df_property('multi_import_targets', 'hidden', 1);
    } else if (frm.doc.multiple_import) {
        frm.set_df_property('import_doctype', 'reqd', 0);
        frm.set_df_property('import_doctype', 'hidden', 1);
        frm.set_df_property('import_type', 'reqd', 0);
        frm.set_df_property('import_type', 'hidden', 1);
        frm.set_df_property('update_on_field', 'hidden', 1);
        frm.set_df_property('duplicate_check_field', 'hidden', 1);
        frm.set_df_property('field_mapping', 'hidden', 1);
        
        frm.set_df_property('multi_import_targets', 'reqd', 1);
        frm.set_df_property('multi_import_targets', 'hidden', 0);
    }

    // 2. Populate CSV column dropdowns dynamically
    if (frm.doc.csv_file) {
        if (frm.doc.single_import) {
            if (frm.doc.import_type === 'Insert and Update Records') {
                frm.events.populate_update_on_field(frm);
            }
            frappe.db.get_single_value('Lightning Upload Settings', 'enable_file_duplicate_check').then(enabled => {
                if (enabled) {
                    frm.set_df_property('duplicate_check_field', 'hidden', 0);
                    frm.events.populate_duplicate_check_field(frm);
                } else {
                    frm.set_df_property('duplicate_check_field', 'hidden', 1);
                }
            });
        } else if (frm.doc.multiple_import) {
            frm.events.populate_multi_import_selects(frm);
        }
    }

    // 3. Stably debounce rendering of buttons to ensure they do not get cleared by framework/workflow
    if (frm._button_timeout) {
        clearTimeout(frm._button_timeout);
    }
    frm._button_timeout = setTimeout(() => {
        setup_buttons(frm);
    }, 150);
}

frappe.ui.form.on('Lightning Upload', {
    refresh: function (frm) {
        frappe.progress_state.current_form = frm;

        // Perform centralized UI updates
        update_import_mode_ui(frm);

        // Set up progress tracking if import is in progress
        if (frm.doc.status === 'Queued' || frm.doc.status === 'In Progress') {
            setup_progress_tracking(frm);
        }
    },

    onload: function (frm) {
        frappe.progress_state.current_form = frm;

        if (frm.doc.status === 'Queued' || frm.doc.status === 'In Progress') {
            setup_progress_tracking(frm);
        }

        update_import_mode_ui(frm);
    },

    csv_file: function (frm) {
        update_import_mode_ui(frm);
    },

    import_type: function (frm) {
        update_import_mode_ui(frm);
    },

    single_import: function(frm) {
        if (frm.doc.single_import) {
            frm.set_value('multiple_import', 0);
            update_import_mode_ui(frm);
        } else if (!frm.doc.multiple_import) {
            frm.set_value('single_import', 1);
            frappe.msgprint(__('Please select either Single Import or Multiple Import.'));
        }
    },

    multiple_import: function(frm) {
        if (frm.doc.multiple_import) {
            frm.set_value('single_import', 0);
            update_import_mode_ui(frm);
        } else if (!frm.doc.single_import) {
            frm.set_value('multiple_import', 1);
            frappe.msgprint(__('Please select either Single Import or Multiple Import.'));
        }
    },

    populate_duplicate_check_field: function (frm) {
        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_csv_headers_for_upload',
            args: { file_url: frm.doc.csv_file },
            callback: function (r) {
                if (r.message && r.message.status === 'success') {
                    const headers = r.message.headers;
                    const options = [''].concat(headers);
                    frm.set_df_property('duplicate_check_field', 'options', options);
                    frm.refresh_field('duplicate_check_field');
                }
            }
        });
    },

    populate_update_on_field: function (frm) {
        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_csv_headers_for_upload',
            args: { file_url: frm.doc.csv_file },
            callback: function (r) {
                if (r.message && r.message.status === 'success') {
                    const headers = r.message.headers;
                    const options = [''].concat(headers);
                    frm.set_df_property('update_on_field', 'options', options);
                    frm.refresh_field('update_on_field');
                }
            }
        });
    },

    populate_multi_import_selects: function (frm) {
        if (!frm.doc.csv_file) return;

        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_csv_headers_for_upload',
            args: { file_url: frm.doc.csv_file },
            callback: function (r) {
                if (r.message && r.message.status === 'success') {
                    const headers = r.message.headers;
                    const options = [''].concat(headers);
                    
                    if (frm.fields_dict['multi_import_targets'] && frm.fields_dict['multi_import_targets'].grid) {
                        const grid = frm.fields_dict['multi_import_targets'].grid;
                        
                        // Set on grid columns metadata
                        grid.docfields.forEach(df => {
                            if (df.fieldname === 'update_on_field' || df.fieldname === 'duplicate_check_field') {
                                df.options = options;
                            }
                        });

                        // Set on global form docfields metadata
                        const update_df = frappe.meta.get_docfield("Lightning Multi Import Target", "update_on_field", frm.docname);
                        if (update_df) update_df.options = options;

                        const dup_df = frappe.meta.get_docfield("Lightning Multi Import Target", "duplicate_check_field", frm.docname);
                        if (dup_df) dup_df.options = options;
                        
                        grid.refresh();
                    }
                }
            }
        });
    }
});

// Grid row trigger mapping
frappe.ui.form.on('Lightning Multi Import Target', {
    multi_import_targets_add: function(frm, cdt, cdn) {
        if (frm.doc.csv_file) {
            frm.events.populate_multi_import_selects(frm);
        }
    }
});

// Global event handler for import progress
frappe.realtime.on('import_progress', function (data) {
    console.log('[Lightning Import] Received import_progress event:', data);

    const frm = frappe.progress_state.current_form;
    if (!frm) return;

    if (data.progress_key) {
        const formProgressKey = `lightning_import_${frm.doc.name}`;
        if (data.progress_key !== formProgressKey) return;
    }

    update_progress(frm, data);
});

function setup_progress_tracking(frm) {
    if (frm.progress_bar) {
        frm.progress_bar.remove();
    }

    frm.progress_bar = $(`
        <div class="progress-bar-container" style="margin: 20px 0;">
            <div class="progress" style="height: 20px; margin-bottom: 10px;">
                <div class="progress-bar progress-bar-striped progress-bar-animated" role="progressbar" style="width: 0%;" 
                    aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
                </div>
            </div>
            <div class="progress-status text-muted" style="font-weight: 500;"></div>
        </div>
    `).insertAfter(frm.page.main);
}

function update_progress(frm, data) {
    if (!data || !frm.progress_bar) return;

    frm.progress_bar.find('.progress-bar')
        .css('width', `${data.progress}%`)
        .attr('aria-valuenow', data.progress);
    
    let titleHtml = data.title;
    if (data.multiple_import && data.current_target_doctype) {
        titleHtml = `<b>Overall Progress: ${data.progress}%</b><br>` +
                    `<span style="font-size:13px; color:#555;">Importing Target DocType: <b>${data.current_target_doctype}</b> (${data.current_target_index}/${data.total_targets})</span><br>` +
                    `<span style="font-size:12px; color:#888;">Processed: ${data.target_successful_records} Succeeded, ${data.target_failed_records} Failed (Total: ${data.target_total_records})</span>`;
    }
    frm.progress_bar.find('.progress-status').html(titleHtml);

    if (data.status) {
        frm.doc.status = data.status;
        frm.refresh_field('status');
        frm.refresh_field('workflow_state');
        frm.refresh_header();
    }

    if (data.successful_records !== undefined) {
        frm.doc.successful_records = data.successful_records;
        frm.refresh_field('successful_records');
    }
    if (data.failed_records !== undefined) {
        frm.doc.failed_records = data.failed_records;
        frm.refresh_field('failed_records');
    }
    if (data.import_time) {
        frm.doc.import_time = data.import_time;
        frm.refresh_field('import_time');
    }
    if (data.total_records !== undefined) {
        frm.doc.total_records = data.total_records;
        frm.refresh_field('total_records');
    }

    // Refresh child table rows state in real-time
    if (data.multiple_import && frm.fields_dict['multi_import_targets']) {
        frm.reload_doc();
    }

    if (data.status === 'Completed' || data.status === 'Failed' || data.status === 'Partial Success') {
        let message = '';
        if (data.status === 'Completed') {
            message = __(`Successfully imported ${data.successful_records} records`);
            if (data.time_taken) message += __(`, time taken: ${data.time_taken}`);
        } else if (data.status === 'Partial Success') {
            message = __(`Import partially completed. ${data.successful_records} records imported, ${data.failed_records} failed`);
            if (data.time_taken) message += __(`, time taken: ${data.time_taken}`);
        } else {
            message = __('Import failed');
            if (data.error) message += `: ${data.error}`;
        }

        frappe.show_alert({
            message: message,
            indicator: data.status === 'Completed' ? 'green' : (data.status === 'Partial Success' ? 'orange' : 'red'),
            timeout: 10
        });

        if (frm.progress_bar) {
            frm.progress_bar.remove();
            frm.progress_bar = null;
        }

        setTimeout(() => {
            frm.reload_doc();
        }, 1000);
    }
}

// Start Single Import
function start_import(frm) {
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
            callback: function (r) {
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

    const check_and_proceed = (mapping_json = null) => {
        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.check_file_duplicates',
            args: {
                docname: frm.doc.name,
                mapping: mapping_json
            },
            freeze: true,
            freeze_message: __('Checking for duplicates in file...'),
            callback: function (r) {
                if (!r.message || r.message.status === 'error') {
                    call_start_import_py(mapping_json);
                    return;
                }

                const result = r.message;
                if (!result.has_duplicates) {
                    call_start_import_py(mapping_json);
                    return;
                }

                let html = `
                    <div style="margin-bottom:12px;">
                        <span style="font-size:15px;font-weight:600;color:#e2622a;">
                            &#9888; ${result.total_duplicate_rows} row(s) contain duplicate values across ${result.duplicates.length} column(s).
                        </span>
                        <div style="color:#666;margin-top:4px;font-size:12px;">
                            Total rows in file: <b>${result.total_rows}</b>
                        </div>
                    </div>
                `;

                result.duplicates.forEach(col => {
                    html += `
                        <div style="margin-bottom:14px;border:1px solid #f0c080;border-radius:6px;padding:10px 14px;background:#fffbf0;">
                            <div style="font-weight:600;color:#b36b00;margin-bottom:6px;">
                                Column: <span style="color:#333">${frappe.utils.escape_html(col.csv_column)}</span>
                                <span style="font-size:11px;color:#888;margin-left:8px;">(maps to: ${frappe.utils.escape_html(col.field)})</span>
                            </div>
                            <table class="table table-condensed table-bordered" style="font-size:12px;margin-bottom:0;background:#fff;">
                                <thead><tr>
                                    <th>Duplicate Value</th>
                                    <th>Occurrences</th>
                                    <th>Row Numbers</th>
                                </tr></thead>
                                <tbody>
                    `;
                    col.duplicate_values.forEach(entry => {
                        const rowDisplay = entry.rows.length > 20
                            ? entry.rows.slice(0, 20).join(', ') + `... (+${entry.rows.length - 20} more)`
                            : entry.rows.join(', ');
                        html += `
                            <tr>
                                <td><b>${frappe.utils.escape_html(String(entry.value))}</b></td>
                                <td style="text-align:center">${entry.count}</td>
                                <td style="color:#555">${rowDisplay}</td>
                            </tr>
                        `;
                    });
                    html += `</tbody></table></div>`;
                });

                html += `<div style="margin-top:10px;color:#555;font-size:12px;">
                    You can still continue with the import. Duplicate rows will be processed normally — no rows are skipped automatically.
                </div>`;

                const d = new frappe.ui.Dialog({
                    title: __('Duplicate Values Detected in File'),
                    fields: [{ fieldtype: 'HTML', fieldname: 'dup_summary', options: html }],
                    primary_action_label: __('Continue Import'),
                    primary_action() {
                        d.hide();
                        call_start_import_py(mapping_json);
                    },
                    secondary_action_label: __('Cancel'),
                    secondary_action() {
                        d.hide();
                    }
                });
                d.show();
                d.$wrapper.find('.modal-dialog').css('max-width', '750px');
            }
        });
    };

    if (frm.doc.field_mapping) {
        check_and_proceed();
    } else {
        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.auto_map_and_validate',
            args: { docname: frm.doc.name },
            callback: function (r) {
                if (!r.message) {
                    frappe.msgprint(__('Error during auto-mapping. Please map fields manually.'));
                    return;
                }
                const data = r.message;
                const mapping_json = JSON.stringify(data.mapping);

                const proceed_with_dup_check = () => {
                    check_and_proceed(mapping_json);
                };

                if (data.unmapped_required.length > 0) {
                    frappe.confirm(
                        __('The following required fields could not be auto-mapped: <br><b>{0}</b>. <br><br>Rows without these fields will fail to import. Do you want to continue anyway?', [data.unmapped_required.join(', ')]),
                        () => {
                            proceed_with_dup_check();
                        },
                        () => {
                            open_field_mapping_dialog(frm);
                        },
                        __('Missing Required Fields'),
                        __('Continue Anyway'),
                        __('Cancel and Map Fields')
                    );
                } else {
                    frappe.show_alert({
                        message: __('All required fields were auto-mapped. Checking for duplicates...'),
                        indicator: 'green'
                    });
                    proceed_with_dup_check();
                }
            }
        });
    }
}

// Start Multiple Import
function start_multi_import(frm) {
    const call_start_multi_import_py = () => {
        setup_progress_tracking(frm);

        frappe.call({
            method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.start_multi_import',
            args: {
                docname: frm.doc.name
            },
            callback: function (r) {
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
                        message: r.message.message || __('Failed to start Multiple Import'),
                        indicator: 'red'
                    });
                }
            }
        });
    };

    // Check duplicates across all targets
    frappe.call({
        method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.check_multi_file_duplicates',
        args: {
            docname: frm.doc.name
        },
        freeze: true,
        freeze_message: __('Checking for duplicates across target DocTypes...'),
        callback: function (r) {
            if (!r.message || r.message.status === 'error') {
                call_start_multi_import_py();
                return;
            }

            const result = r.message;
            if (!result.has_duplicates) {
                call_start_multi_import_py();
                return;
            }

            // Build duplicate warning layout
            let html = `
                <div style="margin-bottom:12px;">
                    <span style="font-size:15px;font-weight:600;color:#e2622a;">
                        &#9888; Duplicate values detected in the CSV file for one or more targets.
                    </span>
                </div>
            `;

            result.targets.forEach(target => {
                html += `
                    <div style="margin-bottom:14px;border:1px solid #f0c080;border-radius:6px;padding:10px 14px;background:#fffbf0;">
                        <div style="font-weight:600;color:#b36b00;margin-bottom:6px;">
                            DocType: <span style="color:#007bff">${frappe.utils.escape_html(target.target_doctype)}</span> | 
                            Column: <span style="color:#333">${frappe.utils.escape_html(target.csv_column)}</span>
                            <span style="font-size:11px;color:#888;margin-left:8px;">(maps to: ${frappe.utils.escape_html(target.field)})</span>
                        </div>
                        <table class="table table-condensed table-bordered" style="font-size:12px;margin-bottom:0;background:#fff;">
                            <thead><tr>
                                <th>Duplicate Value</th>
                                <th>Occurrences</th>
                                <th>Row Numbers</th>
                            </tr></thead>
                            <tbody>
                `;
                target.duplicate_values.forEach(entry => {
                    const rowDisplay = entry.rows.length > 20
                        ? entry.rows.slice(0, 20).join(', ') + `... (+${entry.rows.length - 20} more)`
                        : entry.rows.join(', ');
                    html += `
                        <tr>
                            <td><b>${frappe.utils.escape_html(String(entry.value))}</b></td>
                            <td style="text-align:center">${entry.count}</td>
                            <td style="color:#555">${rowDisplay}</td>
                        </tr>
                    `;
                });
                html += `</tbody></table></div>`;
            });

            html += `<div style="margin-top:10px;color:#555;font-size:12px;">
                You can still continue with the import. Duplicate rows will be processed normally — no rows are skipped automatically.
            </div>`;

            const d = new frappe.ui.Dialog({
                title: __('Duplicate Values Detected'),
                fields: [{ fieldtype: 'HTML', fieldname: 'dup_summary', options: html }],
                primary_action_label: __('Continue Import'),
                primary_action() {
                    d.hide();
                    call_start_multi_import_py();
                },
                secondary_action_label: __('Cancel'),
                secondary_action() {
                    d.hide();
                }
            });
            d.show();
            d.$wrapper.find('.modal-dialog').css('max-width', '750px');
        }
    });
}

function export_error_rows(frm) {
    frappe.call({
        method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.export_error_rows',
        args: {
            docname: frm.doc.name
        },
        callback: function (r) {
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

// Single Field Mapping Dialog
function open_field_mapping_dialog(frm) {
    frappe.call({
        method: 'lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_csv_headers_for_upload',
        args: { file_url: frm.doc.csv_file },
        callback: function (csvRes) {
            if (!csvRes.message || csvRes.message.status !== 'success') {
                frappe.show_alert({ message: csvRes.message ? csvRes.message.message : __('Failed to fetch CSV headers'), indicator: 'red' });
                return;
            }

            const csvHeaders = csvRes.message.headers;
            
            frappe.model.with_doctype(frm.doc.import_doctype, async () => {
                const meta = frappe.get_meta(frm.doc.import_doctype);
                const requiredFields = meta.fields.filter(f => f.reqd).map(f => f.fieldname);
                const fields = meta.fields.filter(f => !['Section Break', 'Column Break', 'Tab Break', 'Fold'].includes(f.fieldtype)).map(f => ({
                    fieldname: f.fieldname,
                    label: f.label || f.fieldname,
                    reqd: f.reqd
                }));
                const system_fields = [
                    { fieldname: 'name', label: 'ID' },
                    { fieldname: 'owner', label: 'Owner' },
                    { fieldname: 'creation', label: 'Created On' },
                    { fieldname: 'modified', label: 'Last Modified' },
                    { fieldname: 'modified_by', label: 'Modified By' }
                ];
                const fieldOptions = fields.concat(system_fields);

                let existingMapping = {};
                try {
                    if (frm.doc.field_mapping) {
                        existingMapping = JSON.parse(frm.doc.field_mapping);
                    }
                } catch (e) {
                    console.log('Error parsing existing mapping:', e);
                }

                const auto_mapping_res = await frappe.xcall('lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.auto_map_and_validate', { docname: frm.doc.name });
                const backend_mapping = auto_mapping_res ? auto_mapping_res.mapping : {};

                const mapping = {};
                csvHeaders.forEach(header => {
                    if (existingMapping[header]) {
                        mapping[header] = existingMapping[header];
                    } else {
                        mapping[header] = backend_mapping[header] || '';
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
                            frappe.show_alert({
                                message: __('Note: Missing required fields: {0}', [unmappedRequired.join(', ')]),
                                indicator: 'orange'
                            });
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
                d.$wrapper.find('.modal-dialog').css('max-width', '750px');
            });
        }
    });
}

// Combined Multi-Field Mapping Dialog
async function open_combined_multi_mapping_dialog(frm) {
    const enabled_targets = frm.doc.multi_import_targets.filter(t => t.enabled && t.target_doctype);
    if (!enabled_targets.length) {
        frappe.msgprint(__('Please add, enable, and select at least one Target DocType first.'));
        return;
    }
    if (!frm.doc.csv_file) {
        frappe.msgprint(__('Please attach a CSV file first.'));
        return;
    }

    let csvRes;
    try {
        // 1. Fetch CSV headers safely
        csvRes = await frappe.xcall('lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_csv_headers_for_upload', {
            file_url: frm.doc.csv_file
        });
    } catch (err) {
        console.error('Error fetching CSV headers:', err);
        frappe.msgprint(__('Could not retrieve CSV headers. Please ensure the attached file is valid and the document is saved.'));
        return;
    }

    if (!csvRes || csvRes.status !== 'success') {
        frappe.show_alert({ message: csvRes ? csvRes.message : __('Failed to fetch CSV headers'), indicator: 'red' });
        return;
    }

    const csvHeaders = csvRes.headers;

    // 2. Fetch metadata for all target DocTypes and auto-mappings safely
    const doctypes_metadata = {};
    for (const target of enabled_targets) {
        try {
            // Load metadata
            await new Promise((resolve, reject) => {
                frappe.model.with_doctype(target.target_doctype, resolve, reject);
            });

            const meta = frappe.get_meta(target.target_doctype);
            if (!meta) {
                throw new Error(`Meta not found for ${target.target_doctype}`);
            }

            const requiredFields = meta.fields.filter(f => f.reqd).map(f => f.fieldname);
            const fields = meta.fields.filter(f => !['Section Break', 'Column Break', 'Tab Break', 'Fold'].includes(f.fieldtype)).map(f => ({
                fieldname: f.fieldname,
                label: f.label || f.fieldname,
                reqd: f.reqd
            }));
            const system_fields = [
                { fieldname: 'name', label: 'ID' },
                { fieldname: 'owner', label: 'Owner' },
                { fieldname: 'creation', label: 'Created On' },
                { fieldname: 'modified', label: 'Last Modified' },
                { fieldname: 'modified_by', label: 'Modified By' }
            ];
            const fieldOptions = fields.concat(system_fields);

            // Fetch backend auto-mapping safely
            let auto_mapping_res;
            try {
                auto_mapping_res = await frappe.xcall('lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.get_auto_mapping_for_doctype', {
                    docname: frm.doc.name,
                    doctype: target.target_doctype
                });
            } catch (err) {
                console.error(`Error getting auto mapping for ${target.target_doctype}:`, err);
                auto_mapping_res = { mapping: {} };
            }
            
            const backend_mapping = auto_mapping_res ? auto_mapping_res.mapping : {};

            let existingMapping = {};
            try {
                if (target.field_mapping) {
                    existingMapping = JSON.parse(target.field_mapping);
                }
            } catch (e) {
                console.log('Error parsing existing mapping:', e);
            }

            doctypes_metadata[target.target_doctype] = {
                fields: fieldOptions,
                required: requiredFields,
                backend_mapping: backend_mapping,
                existingMapping: existingMapping,
                targetName: target.name
            };
        } catch (targetErr) {
            console.error(`Error preparing target DocType ${target.target_doctype}:`, targetErr);
            frappe.msgprint(__('Failed to prepare metadata for Target DocType "{0}". Please check its configuration.', [target.target_doctype]));
            return;
        }
    }

    // 3. Build combined HTML table with C * T rows to allow multi-doctype mapping
    let tableHtml = `<div style="margin-bottom:16px"><b>Combined Map Columns from <span style='color:#007bff'>${frappe.utils.escape_html(frm.doc.csv_file.split('/').pop())}</span> to targets</b></div>`;
    tableHtml += `<div style="max-height: 450px; overflow-y: auto;"><table class="table table-bordered" style="width:100%;background:#fff;margin-bottom:0;">`;
    tableHtml += `<thead><tr><th style='width:35%'>CSV Column</th><th style='width:30%'>DocType</th><th style='width:35%'>DocType Field</th></tr></thead><tbody>`;

    csvHeaders.forEach(header => {
        Object.keys(doctypes_metadata).forEach(doctype => {
            const meta = doctypes_metadata[doctype];

            // Determine initial Field for this specific target
            let initialField = '';
            if (meta.existingMapping[header]) {
                initialField = meta.existingMapping[header];
            } else if (meta.backend_mapping[header]) {
                initialField = meta.backend_mapping[header];
            }

            // Only generate mapping row if a matching alias exists or an existing mapping is defined
            if (!initialField) {
                return;
            }

            tableHtml += `<tr class="mapping-dialog-row" data-header="${frappe.utils.escape_html(header)}">`;
            tableHtml += `<td><input type='text' class='form-control' value='${frappe.utils.escape_html(header)}' readonly tabindex='-1'></td>`;
            
            // DocType Dropdown Selector
            tableHtml += `<td><select class='form-control combined-doctype-select' data-header="${frappe.utils.escape_html(header)}" style="width:100%;">`;
            tableHtml += `<option value=''>Don't Import</option>`;
            Object.keys(doctypes_metadata).forEach(dt => {
                const selected = dt === doctype ? 'selected' : '';
                tableHtml += `<option value="${frappe.utils.escape_html(dt)}" ${selected}>${frappe.utils.escape_html(dt)}</option>`;
            });
            tableHtml += `</select></td>`;

            // Target Field Dropdown Selector
            tableHtml += `<td><select class='form-control combined-field-select' data-header="${frappe.utils.escape_html(header)}" style="width:100%;">`;
            tableHtml += `<option value=''>Don't Import</option>`;
            if (doctype && doctypes_metadata[doctype]) {
                const options = doctypes_metadata[doctype].fields;
                options.forEach(field => {
                    const label = field.label || field.fieldname;
                    const displayText = `${label} (${field.fieldname})`;
                    const selected = initialField === field.fieldname ? 'selected' : '';
                    tableHtml += `<option value="${frappe.utils.escape_html(field.fieldname)}" ${selected}>${frappe.utils.escape_html(displayText)}</option>`;
                });
            }
            tableHtml += `</select></td></tr>`;
        });
    });

    tableHtml += `</tbody></table></div>`;

    const d = new frappe.ui.Dialog({
        title: __('Combined Field Mapping'),
        fields: [
            { fieldtype: 'HTML', fieldname: 'mapping_table', options: tableHtml }
        ],
        primary_action_label: __('Save Mapping'),
        primary_action() {
            // Collect mapped values for each target row
            const targetMappings = {};
            enabled_targets.forEach(t => {
                targetMappings[t.name] = {};
            });

            // Gather inputs from table
            let hasHeaderDoctypeDuplicate = false;
            d.$wrapper.find('.mapping-dialog-row').each(function () {
                const header = $(this).data('header');
                const docType = $(this).find('.combined-doctype-select').val();
                const field = $(this).find('.combined-field-select').val();
                
                if (docType && field) {
                    const meta = doctypes_metadata[docType];
                    if (meta && targetMappings[meta.targetName]) {
                        if (targetMappings[meta.targetName][header]) {
                            frappe.msgprint(__('Invalid Mapping: CSV Header "{0}" is mapped to multiple fields in DocType "{1}".', [header, docType]));
                            hasHeaderDoctypeDuplicate = true;
                            return false; // break jquery each loop
                        }
                        targetMappings[meta.targetName][header] = field;
                    }
                }
            });

            if (hasHeaderDoctypeDuplicate) return;

            // Save values back to the child rows and check duplicates inside each target independently
            let hasDuplicates = false;
            for (const doctype of Object.keys(doctypes_metadata)) {
                const meta = doctypes_metadata[doctype];
                const targetName = meta.targetName;
                const mapping = targetMappings[targetName];
                const mappedFields = Object.values(mapping).filter(Boolean);

                // Duplicate check within a single doctype mappings
                const duplicates = mappedFields.filter((item, idx) => mappedFields.indexOf(item) !== idx);
                if (duplicates.length) {
                    frappe.msgprint(__('Duplicate mapping for {0} fields: {1}', [doctype, duplicates.join(', ')]));
                    hasDuplicates = true;
                    break;
                }

                // Warn for missing required fields
                const unmappedRequired = meta.required.filter(f => !mappedFields.includes(f));
                if (unmappedRequired.length) {
                    frappe.show_alert({
                        message: __('Note: {0} is missing required fields: {1}', [doctype, unmappedRequired.join(', ')]),
                        indicator: 'orange'
                    });
                }
            }

            if (hasDuplicates) return;

            // Commit mappings to the child rows in the form doc
            enabled_targets.forEach(t => {
                const mappingString = JSON.stringify(targetMappings[t.name]);
                frappe.model.set_value(t.doctype, t.name, 'field_mapping', mappingString);
            });

            // Save document to database
            frm.save().then(() => {
                d.hide();
                frm.reload_doc();
                frappe.show_alert({ message: __('All field mappings successfully saved in database.'), indicator: 'green' });
            });
        }
    });

    // Dynamic field list switching on DocType change
    d.$wrapper.on('change', '.combined-doctype-select', function() {
        const header = $(this).data('header');
        const selectedDocType = $(this).val();
        const row = $(this).closest('tr');
        const fieldSelect = row.find('.combined-field-select');

        // Clear existing options
        fieldSelect.empty();
        fieldSelect.append($('<option>', { value: '', text: __("Don't Import") }));

        if (selectedDocType && doctypes_metadata[selectedDocType]) {
            const fieldOptions = doctypes_metadata[selectedDocType].fields;
            fieldOptions.forEach(field => {
                const label = field.label || field.fieldname;
                const displayText = `${label} (${field.fieldname})`;
                fieldSelect.append($('<option>', {
                    value: field.fieldname,
                    text: displayText
                }));
            });
        }
    });

    d.show();
    d.$wrapper.find('.modal-dialog').css('max-width', '800px');
}