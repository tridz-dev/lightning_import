// Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on('Lightning Field Mapping', {
	refresh: function(frm) {
		if (frm.doc.reference_doctype) {
			frm.trigger('set_field_options');
		}
	},

	reference_doctype: function(frm) {
		// Clear existing rows when changing doctype to avoid invalid mappings
		if (frm.doc.mappings && frm.doc.mappings.length > 0) {
			frm.clear_table('mappings');
			frm.refresh_field('mappings');
		}
		frm.trigger('set_field_options');
	},

	set_field_options: function(frm) {
		if (frm.doc.reference_doctype) {
			frappe.call({
				method: 'lightning_import.lightning_import.api.get_fields.get_doctype_fields',
				args: { doctype: frm.doc.reference_doctype },
				callback: function(r) {
					if (r.message && r.message.fields) {
						const options = [''].concat(r.message.fields.map(f => f.fieldname)).join('\n');
						
						// 1. Update base metadata
						let base_df = frappe.meta.get_docfield('Lightning Fields', 'field_name');
						if (base_df) {
							base_df.options = options;
						}
						
						// 2. Update form-specific metadata
						let form_df = frappe.meta.get_docfield('Lightning Fields', 'field_name', frm.doc.name);
						if (form_df) {
							form_df.options = options;
						}
						
						// 3. Update grid column properties directly
						if (frm.fields_dict.mappings && frm.fields_dict.mappings.grid) {
							frm.fields_dict.mappings.grid.update_docfield_property('field_name', 'options', options);
						}
						
						frm.refresh_field('mappings');
					}
				}
			});
		} else {
			// Clear options if no doctype selected
			let base_df = frappe.meta.get_docfield('Lightning Fields', 'field_name');
			if (base_df) base_df.options = "";
			
			let form_df = frappe.meta.get_docfield('Lightning Fields', 'field_name', frm.doc.name);
			if (form_df) form_df.options = "";
			
			if (frm.fields_dict.mappings && frm.fields_dict.mappings.grid) {
				frm.fields_dict.mappings.grid.update_docfield_property('field_name', 'options', "");
			}
			frm.refresh_field('mappings');
		}
	}
});

frappe.ui.form.on('Lightning Fields', {
	mappings_add: function(frm, cdt, cdn) {
		// Ensure options are set when a new row is added
		frm.trigger('set_field_options');
	}
});
