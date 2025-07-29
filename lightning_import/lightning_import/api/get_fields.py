import frappe
from frappe import _

@frappe.whitelist()
def get_doctype_fields(doctype, docname=None):
    """
    API endpoint to get all field names (and optionally values) from a DocType.
    If docname is provided, also returns the values for that document.
    """
    try:
        meta = frappe.get_meta(doctype)
        # Exclude layout fields
        fields = [field for field in meta.fields if field.fieldtype not in ['Section Break', 'Column Break', 'Tab Break', 'Fold']]
        
        # Add system fields with proper labels
        system_fields = [
            {'fieldname': 'name', 'label': 'ID'},
            {'fieldname': 'owner', 'label': 'Owner'},
            {'fieldname': 'creation', 'label': 'Created On'},
            {'fieldname': 'modified', 'label': 'Last Modified'},
            {'fieldname': 'modified_by', 'label': 'Modified By'},
        ]
        
        # Build field list with labels
        all_fields = [
            {
                'fieldname': field.fieldname, 
                'label': field.label or field.fieldname,
                'fieldtype': field.fieldtype,
                'reqd': field.reqd if hasattr(field, 'reqd') else 0
            } for field in fields
        ] + system_fields

        result = {"fields": all_fields}

        if docname:
            try:
                doc = frappe.get_doc(doctype, docname)
                result["values"] = {field['fieldname']: doc.get(field['fieldname']) for field in all_fields}
            except Exception as e:
                frappe.throw(_("Error fetching document: {0}").format(str(e)))

        return result
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Get DocType Fields Error")
        frappe.throw(_("Error getting DocType fields: {0}").format(str(e)))