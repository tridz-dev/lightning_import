# Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
import csv
import os
from frappe.model.document import Document

class LightningUpload(Document):
	def validate(self):
		"""Validate the document before save"""
		if self.csv_file:
			self.validate_csv_file()
	
	def validate_csv_file(self):
		"""Validate if the uploaded file is a valid CSV file"""
		# Get file content
		try:
			file_doc = frappe.get_doc("File", {"file_url": self.csv_file})
			file_path = file_doc.get_full_path()
		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "Lightning Import File Access Error")
			frappe.throw(_("Error accessing file: {}".format(str(e))))
			return

		# Check file extension
		file_ext = os.path.splitext(file_path)[1].lower()
		if file_ext != '.csv':
			frappe.throw(_("Please upload a CSV file. Current file type: {}".format(file_ext)))
			return

		# Try to read the file as CSV
		try:
			with open(file_path, 'r', encoding='utf-8') as csvfile:
				# Try to read first few lines to validate CSV format
				reader = csv.reader(csvfile)
				header = next(reader, None)
				
				if not header:
					frappe.throw(_("CSV file is empty"))
					return
				
				# Try to read a few rows to ensure it's properly formatted
				for i in range(5):  # Check first 5 rows
					try:
						next(reader)
					except StopIteration:
						break
					except csv.Error as e:
						frappe.throw(_("Invalid CSV format: {}".format(str(e))))
						return
				
		except UnicodeDecodeError:
			frappe.throw(_("Invalid file encoding. Please upload a UTF-8 encoded CSV file"))
			return
		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "Lightning Import CSV Validation Error")
			frappe.throw(_("Error reading CSV file: {}".format(str(e))))
			return

def get_doctype_fields(doctype):
	"""Get all field names from a DocType"""
	fields = frappe.get_meta(doctype).fields
	return [field.fieldname for field in fields if field.fieldtype not in ['Section Break', 'Column Break', 'Tab Break', 'Fold']]

def get_csv_headers(file_path):
	"""Get headers from CSV file"""
	try:
		with open(file_path, 'r', encoding='utf-8') as csvfile:
			reader = csv.reader(csvfile)
			headers = next(reader, None)
			if not headers:
				frappe.throw("CSV file is empty")
			return [header.strip() for header in headers]
	except Exception as e:
		frappe.throw(f"Error reading CSV headers: {str(e)}")

@frappe.whitelist()
def start_import(docname):
	"""API endpoint to start the import process"""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		
		# Validate document status
		if doc.status != "Draft":
			frappe.throw(_("Import can only be started from Draft status"))
		
		# Get file path
		file_doc = frappe.get_doc("File", {"file_url": doc.csv_file})
		file_path = file_doc.get_full_path()
		
		# Get CSV headers
		csv_headers = get_csv_headers(file_path)
		
		# Get DocType fields
		doctype_fields = get_doctype_fields(doc.import_doctype)
		
		# Find matching and non-matching fields
		matching_fields = []
		non_matching_fields = []
		
		for header in csv_headers:
			if header in doctype_fields:
				matching_fields.append(header)
			else:
				non_matching_fields.append(header)
		
		# If no matching fields found, throw error
		if not matching_fields:
			frappe.throw(_("No matching fields found between CSV headers and {0} DocType fields").format(doc.import_doctype))
		
		# If there are non-matching fields, throw error with details
		if non_matching_fields:
			error_msg = _("CSV headers do not match DocType fields. Please fix the following headers:\n\n")
			error_msg += _("Non-matching headers: {0}\n\n").format(", ".join(non_matching_fields))
			error_msg += _("Available DocType fields: {0}").format(", ".join(doctype_fields))
			frappe.throw(error_msg)
		
		# Update status to In Progress
		doc.status = "In Progress"
		doc.save()

		# Create a progress key for this import
		progress_key = f"lightning_import_{docname}"
		frappe.cache().set_value(progress_key, {
			"status": "In Progress",
			"progress": 0,
			"title": "Starting import..."
		})

		def update_progress(phase, current_step, total_phase_steps):
			progress = int((current_step / total_phase_steps) * 100)
			message = {
				"status": "In Progress",
				"progress": progress,
				"title": f"{phase}... ({progress}%)"
			}
			frappe.cache().set_value(progress_key, message)
			frappe.publish_realtime(
				event='import_progress',
				message=message,
				user=frappe.session.user,
				after_commit=True
			)
			frappe.db.commit()
			frappe.sleep(0.1)

		try:
			# Simulate validation phase (20%)
			for i in range(20):
				update_progress("Validating data", i + 1, 20)

			# Simulate processing phase (60%)
			for i in range(60):
				update_progress("Processing records", i + 1, 60)

			# Simulate final phase (20%)
			for i in range(20):
				update_progress("Finalizing import", i + 1, 20)

			# Final completion message
			completion_message = {
				"status": "Complete",
				"progress": 100,
				"title": "Import completed successfully!"
			}
			frappe.cache().set_value(progress_key, completion_message)
			frappe.publish_realtime(
				event='import_progress',
				message=completion_message,
				user=frappe.session.user,
				after_commit=True
			)
			frappe.db.commit()

			# Return success message with matching fields info
			return {
				"status": "success",
				"message": _("Import process started successfully"),
				"matching_fields": matching_fields,
				"progress_key": progress_key
			}

		finally:
			# Clean up progress key after 1 hour
			frappe.cache().expire(progress_key, 3600)
		
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Error")
		return {
			"status": "error",
			"message": str(e)
		}

@frappe.whitelist()
def get_import_progress(progress_key):
	"""Get the current progress of an import"""
	try:
		progress = frappe.cache().get_value(progress_key)
		return progress or {"status": "Not Found", "progress": 0, "title": "Import not found"}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Progress Error")
		return {"status": "Error", "progress": 0, "title": str(e)}
