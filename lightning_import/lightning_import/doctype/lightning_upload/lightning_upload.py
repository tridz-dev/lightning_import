# Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
import csv
import os
from frappe.model.document import Document
import hashlib
import json
from frappe.utils import cstr
from lightning_import.lightning_import.doctype.lightning_upload_settings.lightning_upload_settings import LightningUploadSettings
import tempfile
from frappe.utils.file_manager import save_file
import time
import random

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

	def get_csv_data(self):
		"""Get CSV data as list of dictionaries"""
		file_doc = frappe.get_doc("File", {"file_url": self.csv_file})
		file_path = file_doc.get_full_path()
		
		with open(file_path, 'r', encoding='utf-8') as csvfile:
			reader = csv.DictReader(csvfile)
			return list(reader)

	def generate_docname(self, row_data):
		"""Generate a unique docname based on row data"""
		# Get current timestamp in microseconds
		timestamp = int(time.time() * 1000000)
		# Generate a random 6-digit number
		random_suffix = random.randint(100000, 999999)
		# Combine timestamp and random number for absolute uniqueness
		unique_id = f"{timestamp}{random_suffix}"
		return f"{self.import_doctype}-{unique_id}"

	def insert_records(self, rows):
		"""Insert records in bulk using SQL"""
		success_count = 0
		failed_rows = []
		
		# Get field types from meta
		meta = frappe.get_meta(self.import_doctype)
		field_types = {f.fieldname: f.fieldtype for f in meta.fields}
		
		# Get required fields from meta
		required_fields = [f.fieldname for f in meta.fields if f.reqd]
		
		# Prepare data for bulk insert
		records = []
		for row in rows:
			try:
				# Convert values based on field type
				converted_data = {}
				for field, value in row.items():
					if field in field_types:
						field_type = field_types[field]
						try:
							if field_type == "Int":
								converted_data[field] = int(value) if value else None
							elif field_type == "Float":
								converted_data[field] = float(value) if value else None
							elif field_type == "Date":
								converted_data[field] = frappe.utils.getdate(value) if value else None
							elif field_type == "Datetime":
								converted_data[field] = frappe.utils.get_datetime(value) if value else None
							else:
								converted_data[field] = value
						except (ValueError, TypeError):
							raise ValueError(f"Invalid value for field {field}: {value}")
				
				# Check required fields
				missing_fields = [f for f in required_fields if f not in converted_data]
				if missing_fields:
					raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")
				
				# Generate docname
				docname = self.generate_docname(row)
				converted_data['name'] = docname
				
				# Add default fields if not present
				if 'owner' not in converted_data:
					converted_data['owner'] = frappe.session.user
				if 'modified_by' not in converted_data:
					converted_data['modified_by'] = frappe.session.user
				if 'creation' not in converted_data:
					converted_data['creation'] = frappe.utils.now()
				if 'modified' not in converted_data:
					converted_data['modified'] = frappe.utils.now()
				
				# Validate row data here
				try:
					self.validate_row_data(converted_data)
				except Exception as e:
					failed_rows.append({
						'row': row,
						'error': str(e)
					})
					continue
				
				records.append(converted_data)
				success_count += 1
				
			except Exception as e:
				failed_rows.append({
					'row': row,
					'error': str(e)
				})
		
		# Bulk insert if we have records
		if records:
			try:
				# Get all possible fields from meta
				all_fields = ['name', 'doctype', 'owner', 'modified_by', 'creation', 'modified']
				all_fields.extend([f.fieldname for f in meta.fields])
				
				# Filter fields that are actually present in our data
				fields = [f for f in all_fields if f in records[0]]
				
				# Convert records to SQL values
				values = []
				for record in records:
					row_values = []
					for field in fields:
						value = record.get(field)
						if value is None:
							row_values.append('NULL')
						elif isinstance(value, (int, float)):
							row_values.append(str(value))
						elif isinstance(value, (frappe.utils.datetime.datetime, frappe.utils.datetime.date)):
							row_values.append(frappe.db.escape(value.strftime('%Y-%m-%d %H:%M:%S')))
						else:
							row_values.append(frappe.db.escape(cstr(value)))
					values.append(f"({', '.join(row_values)})")
				
				# Execute bulk insert
				sql = f"""
					INSERT INTO `tab{self.import_doctype}` 
					(`{'`, `'.join(fields)}`)
					VALUES {', '.join(values)}
				"""
				frappe.db.sql(sql)
				frappe.db.commit()
				
			except Exception as e:
				frappe.db.rollback()
				# If bulk insert fails, mark all records as failed
				failed_rows.extend([{
					'row': record,
					'error': str(e)
				} for record in records])
				success_count = 0
		
		return {
			'success_count': success_count,
			'failed_rows': failed_rows
		}

	def generate_error_file(self, failed_rows):
		"""Generate a CSV file containing failed rows with error messages"""
		if not failed_rows:
			return None
			
		# Create a temporary file
		fd, path = tempfile.mkstemp(suffix='.csv')
		try:
			with os.fdopen(fd, 'w', newline='', encoding='utf-8') as csvfile:
				writer = csv.writer(csvfile)
				
				# Write headers
				headers = list(failed_rows[0]['row'].keys())
				headers.extend(['Error Message', 'Row Number'])
				writer.writerow(headers)
				
				# Write failed rows with error messages
				for idx, failed_row in enumerate(failed_rows, 1):
					row_data = list(failed_row['row'].values())
					row_data.extend([failed_row['error'], idx])
					writer.writerow(row_data)
			
			# Save the file
			with open(path, 'rb') as f:
				file_content = f.read()
				
			file_doc = save_file(
				fname=f"error_log_{self.name}.csv",
				content=file_content,
				dt="Lightning Upload",
				dn=self.name,
				folder="Home/Attachments",
				is_private=1
			)
			
			# Update the document with the file URL
			frappe.db.set_value("Lightning Upload", self.name, "error_file", file_doc.file_url)
			
			return file_doc.file_url
			
		finally:
			# Clean up the temporary file
			if os.path.exists(path):
				os.unlink(path)

	def validate_row_data(self, data):
		"""Validate row data before inserting"""
		# Only call custom validation hooks if validate_from_hook is enabled in settings
		if LightningUploadSettings.get_validate_from_hook():
			for method in frappe.get_hooks('lightning_import_validate_row'):
				frappe.call(method, data=data, doctype=self.import_doctype)

def get_doctype_fields(doctype):
	"""Get all field names from a DocType"""
	meta = frappe.get_meta(doctype)
	# Get regular fields
	fields = [field.fieldname for field in meta.fields if field.fieldtype not in ['Section Break', 'Column Break', 'Tab Break', 'Fold']]
	# Add system fields
	system_fields = ['name', 'owner', 'creation', 'modified', 'modified_by']
	return fields + system_fields

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
def process_import_queue(docname):
	"""Process the import in batches"""
	start_time = time.time()
	batch_timings = []
	
	try:
		# Get fresh copy of doc each time
		doc = frappe.get_doc("Lightning Upload", docname)
		
		# Update status using set_value and publish immediately
		frappe.db.set_value("Lightning Upload", docname, "status", "In Progress")
		frappe.db.commit()
		
		# Publish realtime event for status change
		progress_key = f"lightning_import_{docname}"
		initial_progress = {
			"status": "In Progress",
			"progress": 0,
			"title": "Starting import...",
			"progress_key": progress_key
		}
		frappe.cache().set_value(progress_key, initial_progress)
		frappe.publish_realtime(
			event='import_progress',
			message=initial_progress,
			user=frappe.session.user,
			after_commit=True
		)
		
		# Get CSV data with timing
		csv_start = time.time()
		csv_data = doc.get_csv_data()
		csv_time = round((time.time() - csv_start) * 1000, 2)
		total_rows = len(csv_data)
		
		# Update total records and publish immediately
		frappe.db.set_value("Lightning Upload", docname, "total_records", total_rows)
		frappe.db.commit()
		frappe.publish_realtime(
			event='import_progress',
			message={
				"status": "In Progress",
				"progress": 0,
				"title": f"Starting import of {total_rows} records...",
				"progress_key": progress_key,
				"total_records": total_rows
			},
			user=frappe.session.user,
			after_commit=True
		)
		
		# Get batch size from settings
		batch_size = LightningUploadSettings.get_batch_size()
		
		# Process in batches
		successful_records = 0
		failed_records = 0
		all_failed_rows = []
		
		for i in range(0, total_rows, batch_size):
			batch_start = time.time()
			batch = csv_data[i:i + batch_size]
			batch_num = (i // batch_size) + 1
			total_batches = (total_rows + batch_size - 1) // batch_size
			
			# Update progress
			progress = min(100, int((i / total_rows) * 100))
			
			# Update last processed row and publish immediately
			frappe.db.set_value("Lightning Upload", docname, "last_processed_row", i + len(batch))
			frappe.db.commit()
			
			# Get fresh doc for processing
			doc = frappe.get_doc("Lightning Upload", docname)
			
			# Time the record insertion
			insert_start = time.time()
			result = doc.insert_records(batch)
			insert_time = round((time.time() - insert_start) * 1000, 2)
			
			successful_records += result['success_count']
			failed_records += len(result['failed_rows'])
			all_failed_rows.extend(result['failed_rows'])
			
			# Calculate batch timing
			batch_time = round((time.time() - batch_start) * 1000, 2)
			batch_timings.append({
				'batch': batch_num,
				'total_batches': total_batches,
				'batch_size': len(batch),
				'total_time_ms': batch_time,
				'insert_time_ms': insert_time,
				'successful': result['success_count'],
				'failed': len(result['failed_rows'])
			})
			
			# Update successful and failed records counts and publish immediately
			frappe.db.set_value("Lightning Upload", docname, "successful_records", successful_records)
			frappe.db.set_value("Lightning Upload", docname, "failed_records", failed_records)
			frappe.db.commit()
			
			# Update progress in cache with timing info and publish immediately
			progress_data = {
				"status": "In Progress",
				"progress": progress,
				"title": f"Processing records... ({progress}%)",
				"progress_key": progress_key,
				"successful_records": successful_records,
				"failed_records": failed_records,
				"batch_info": {
					"current_batch": batch_num,
					"total_batches": total_batches,
					"batch_time_ms": batch_time,
					"insert_time_ms": insert_time
				}
			}
			frappe.cache().set_value(progress_key, progress_data)
			frappe.publish_realtime(
				event='import_progress',
				message=progress_data,
				user=frappe.session.user,
				after_commit=True
			)
		
		# Calculate time taken
		time_taken = time.time() - start_time
		time_str = f"{int(time_taken)}s" if time_taken < 60 else f"{time_taken/60:.1f}m"
		
		# Generate error file with timing if needed
		error_file_time = 0
		if all_failed_rows:
			error_start = time.time()
			doc = frappe.get_doc("Lightning Upload", docname)
			doc.error_log = json.dumps(all_failed_rows, indent=2)
			doc.save()
			# Generate error file
			doc.generate_error_file(all_failed_rows)
			error_file_time = round((time.time() - error_start) * 1000, 2)
		
		# Determine final status
		if failed_records == total_rows:
			final_status = "Failed"
		elif failed_records > 0:
			final_status = "Partial Success"
		else:
			final_status = "Completed"
		
		# Update final status and publish immediately
		frappe.db.set_value("Lightning Upload", docname, "status", final_status)
		frappe.db.set_value("Lightning Upload", docname, "import_time", time_str)
		frappe.db.set_value("Lightning Upload", docname, "timing_details", json.dumps({
			"total_time_seconds": round(time_taken, 2),
			"csv_load_time_ms": csv_time,
			"error_file_time_ms": error_file_time,
			"batch_timings": batch_timings,
			"average_batch_time_ms": round(sum(b['total_time_ms'] for b in batch_timings) / len(batch_timings), 2) if batch_timings else 0,
			"average_insert_time_ms": round(sum(b['insert_time_ms'] for b in batch_timings) / len(batch_timings), 2) if batch_timings else 0
		}, indent=2))
		frappe.db.commit()
		
		# Final progress update with detailed timing
		final_progress = {
			"status": final_status,
			"progress": 100,
			"title": f"Import {final_status.lower()}",
			"progress_key": progress_key,
			"time_taken": time_str,
			"total_records": total_rows,
			"successful_records": successful_records,
			"failed_records": failed_records,
			"timing_details": {
				"total_time_seconds": round(time_taken, 2),
				"csv_load_time_ms": csv_time,
				"error_file_time_ms": error_file_time,
				"average_batch_time_ms": round(sum(b['total_time_ms'] for b in batch_timings) / len(batch_timings), 2) if batch_timings else 0
			}
		}
		frappe.cache().set_value(progress_key, final_progress)
		frappe.publish_realtime(
			event='import_progress',
			message=final_progress,
			user=frappe.session.user,
			after_commit=True
		)
		
		return {
			"status": "success",
			"message": f"Import {final_status.lower()}. Successful: {successful_records}, Failed: {failed_records}, Time taken: {time_str}",
			"time_taken": time_str,
			"total_records": total_rows,
			"successful_records": successful_records,
			"failed_records": failed_records,
			"timing_details": final_progress["timing_details"]
		}
		
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Error")
		try:
			frappe.db.set_value("Lightning Upload", docname, "status", "Failed")
			frappe.db.set_value("Lightning Upload", docname, "error_log", str(e))
			frappe.db.commit()
			
			# Publish error status immediately
			progress_key = f"lightning_import_{docname}"
			error_progress = {
				"status": "Failed",
				"progress": 0,
				"title": "Import failed",
				"progress_key": progress_key,
				"error": str(e)
			}
			frappe.cache().set_value(progress_key, error_progress)
			frappe.publish_realtime(
				event='import_progress',
				message=error_progress,
				user=frappe.session.user,
				after_commit=True
			)
		except:
			pass
		return {
			"status": "error",
			"message": str(e)
		}

@frappe.whitelist()
def start_import(docname):
	"""API endpoint to start the import process"""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		
		# Validate document status
		if doc.status != "Draft":
			frappe.throw(_("Import can only be started from Draft status"))
		
		# Get file path and validate CSV
		file_doc = frappe.get_doc("File", {"file_url": doc.csv_file})
		file_path = file_doc.get_full_path()
		
		# Get CSV headers
		csv_headers = get_csv_headers(file_path)
		
		# Get DocType fields
		doctype_fields = get_doctype_fields(doc.import_doctype)
		
		# Validate headers
		matching_fields = []
		non_matching_fields = []
		
		for header in csv_headers:
			if header in doctype_fields:
				matching_fields.append(header)
			else:
				non_matching_fields.append(header)
		
		if not matching_fields:
			frappe.throw(_("No matching fields found between CSV headers and {0} DocType fields").format(doc.import_doctype))
		
		if non_matching_fields:
			error_msg = _("CSV headers do not match DocType fields. Please fix the following headers:\n\n")
			error_msg += _("Non-matching headers: {0}\n\n").format(", ".join(non_matching_fields))
			error_msg += _("Available DocType fields: {0}").format(", ".join(doctype_fields))
			frappe.throw(error_msg)
		
		# Additional validation for update type
		if doc.import_type == "Update Existing Records" and "name" not in csv_headers:
			frappe.throw(_("Column 'name' is required for updating existing records"))

		# Initialize progress in cache before enqueueing
		progress_key = f"lightning_import_{docname}"
		initial_progress = {
			"status": "Queued",
			"progress": 0,
			"title": "Import queued...",
			"progress_key": progress_key,
			"successful_records": 0,
			"failed_records": 0
		}
		frappe.cache().set_value(progress_key, initial_progress)
		
		# Update document status to Queued
		frappe.db.set_value("Lightning Upload", docname, "status", "Queued", update_modified=False)
		
		# Publish initial progress
		frappe.publish_realtime(
			event='import_progress',
			message=initial_progress,
			user=frappe.session.user,
			after_commit=True
		)
		
		# Enqueue the import process
		frappe.enqueue(
			"lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.process_import_queue",
			docname=docname,
			now=False,
			queue="long"
		)
		
		return {
			"status": "success",
			"message": _("Import process started successfully"),
			"matching_fields": matching_fields,
			"progress_key": progress_key  # Send progress key to client
		}
		
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Error")
		# If there's an error, try to set status back to Draft
		try:
			frappe.db.set_value("Lightning Upload", docname, "status", "Draft", update_modified=False)
			frappe.db.commit()
		except:
			pass
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

@frappe.whitelist()
def export_error_rows(docname):
	"""API endpoint to export error rows"""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		
		if doc.status not in ["Failed", "Partial Success"]:
			frappe.throw(_("Error file is only available for Failed or Partial Success imports"))
		
		if not doc.error_log:
			frappe.throw(_("No error log available"))
		
		# Parse error log
		failed_rows = json.loads(doc.error_log)
		
		# Generate error file
		file_url = doc.generate_error_file(failed_rows)
		
		if not file_url:
			frappe.throw(_("No error file could be generated"))
		
		return {
			"status": "success",
			"file_url": file_url
		}
		
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Error Export Error")
		return {
			"status": "error",
			"message": str(e)
		}
