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
			with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
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
		
		with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
			reader = csv.DictReader(csvfile)
			data = []
			for row in reader:
				clean_row = {}
				for k, v in row.items():
					if k is not None:
						clean_row[k.strip()] = v.strip() if isinstance(v, str) else v
				data.append(clean_row)
			return data

	def get_mapped_data(self):
		"""Return list of mapped CSV rows using saved field mapping"""
		raw_rows = self.get_csv_data()
		
		# Load mapping (JSON string to dict)
		if not self.field_mapping:
			frappe.throw("Field mapping is not defined")

		mapping = json.loads(self.field_mapping)
		mapped_rows = []

		for row in raw_rows:
			mapped_row = {}
			for csv_field, doctype_field in mapping.items():
				if doctype_field:  # Only map if field is not empty (not restricted)
					mapped_row[doctype_field] = row.get(csv_field, None)
			mapped_rows.append(mapped_row)

		return mapped_rows

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
			"""Insert or update records in bulk using SQL, with a configurable update key."""
			success_count = 0
			failed_rows = []
			
			was_in_import = frappe.flags.in_import
			frappe.flags.in_import = True
			
			try:
				meta = frappe.get_meta(self.import_doctype)
				field_types = {f.fieldname: f.fieldtype for f in meta.fields}
				required_fields = [f.fieldname for f in meta.fields if f.reqd]
				
				# Identify existing records for 'Insert and Update' to allow validation to skip duplicates
				existing_docs_map = {}
				update_on_csv_col = self.update_on_field
				mapped_update_field = None
				
				if self.import_type in ("Insert and Update Records", "Update Existing Records"):
					if not update_on_csv_col:
						if self.import_type == "Insert and Update Records":
							raise ValueError("Validate On CSV Column not specified for 'Insert and Update' mode.")
					else:
						mapping = json.loads(self.field_mapping)
						mapped_update_field = mapping.get(update_on_csv_col)
						if not mapped_update_field:
							raise ValueError(f"The selected update column '{update_on_csv_col}' is not mapped to any DocType field.")
						
						keys_to_check = list(set([row.get(update_on_csv_col) for row in rows if row.get(update_on_csv_col)]))
						if keys_to_check:
							existing = frappe.get_all(
								self.import_doctype,
								filters={mapped_update_field: ['in', keys_to_check]},
								fields=['name', mapped_update_field]
							)
							existing_docs_map = {str(doc[mapped_update_field]): doc.name for doc in existing}

				# Step 1: Prepare all rows first (data conversion, validation)
				records_to_process = []
				for row in rows:
						try:
								converted_data = {}
								for field, value in row.items():
										if field in field_types:
												field_type = field_types[field]
												try:
														if value:
																if field_type == "Int": converted_data[field] = int(value)
																elif field_type == "Float": converted_data[field] = float(value)
																elif field_type == "Date": converted_data[field] = frappe.utils.getdate(value)
																elif field_type == "Datetime": converted_data[field] = frappe.utils.get_datetime(value)
																else: converted_data[field] = value
														else:
																converted_data[field] = None
												except (ValueError, TypeError):
														raise ValueError(f"Invalid value for field {field}: {value}")
										else:
												converted_data[field] = value
								
								missing_fields = [f for f in required_fields if not converted_data.get(f)]
								if missing_fields:
										raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

								if 'owner' not in converted_data: converted_data['owner'] = frappe.session.user
								if 'modified_by' not in converted_data: converted_data['modified_by'] = frappe.session.user
								if 'creation' not in converted_data: converted_data['creation'] = frappe.utils.now()
								if 'modified' not in converted_data: converted_data['modified'] = frappe.utils.now()
								
								# Assign name if found in existing_docs_map (for Insert and Update / Update Existing)
								if self.import_type in ("Insert and Update Records", "Update Existing Records") and mapped_update_field:
									key_val = row.get(update_on_csv_col)
									if key_val and str(key_val) in existing_docs_map:
										converted_data['name'] = existing_docs_map[str(key_val)]

								# For new records, generate a unique name (ID) before validation so hooks can use it
								if not converted_data.get('name') and self.import_type in ("Insert New Records", "Insert and Update Records"):
									converted_data['name'] = self.generate_docname(converted_data)

								self.validate_row_data(converted_data)
								
								records_to_process.append(converted_data)

						except Exception as e:
								failed_rows.append({'row': row, 'error': str(e)})

				if not records_to_process:
						return {'success_count': 0, 'failed_rows': failed_rows}

				# Step 2: Main logic fork based on the selected import type
				try:
						if self.import_type == "Insert and Update Records":
								to_insert = []
								to_update = []
								existing_names = set(existing_docs_map.values())
								for record in records_to_process:
										if record.get('name') in existing_names:
												to_update.append(record)
										else:
												to_insert.append(record)

								# Process updates in smaller chunks
								UPDATE_CHUNK_SIZE = 1000
								if to_update:
									for i in range(0, len(to_update), UPDATE_CHUNK_SIZE):
										chunk = to_update[i:i + UPDATE_CHUNK_SIZE]
										self._execute_bulk_update(chunk)
								
								if to_insert:
										self._execute_bulk_insert(to_insert)

								success_count = len(to_insert) + len(to_update)

						elif self.import_type == "Insert New Records":
								for record in records_to_process:
									if not record.get('name'):
										record['name'] = self.generate_docname(record)
								self._execute_bulk_insert(records_to_process)
								success_count = len(records_to_process)

						elif self.import_type == "Update Existing Records":
							# Process updates in smaller chunks
							UPDATE_CHUNK_SIZE = 1000
							for i in range(0, len(records_to_process), UPDATE_CHUNK_SIZE):
								chunk = records_to_process[i:i + UPDATE_CHUNK_SIZE]
								self._execute_bulk_update(chunk)
							success_count = len(records_to_process)
						
				except Exception as e:
						# Frappe will handle rollback in the calling function
						for record in records_to_process:
								failed_rows.append({'row': record, 'error': str(e)})
						success_count = 0

				return {'success_count': success_count, 'failed_rows': failed_rows}
			finally:
				frappe.flags.in_import = was_in_import

	def _execute_bulk_insert(self, records):
			"""Helper function to perform a bulk INSERT operation."""
			if not records: return
			
			meta = frappe.get_meta(self.import_doctype)
			all_fields = ['name', 'owner', 'modified_by', 'creation', 'modified'] + [f.fieldname for f in meta.fields]
			fields = sorted(list(set(k for r in records for k in r.keys() if k in all_fields)))
			
			values_list = []
			for record in records:
					row_values = [frappe.db.escape(cstr(record.get(f))) for f in fields]
					values_list.append(f"({', '.join(row_values)})")
			
			sql = f"""
					INSERT INTO `tab{self.import_doctype}` (`{'`, `'.join(fields)}`)
					VALUES {', '.join(values_list)}
			"""
			frappe.db.sql(sql)
			
			# Bulk share the records
			self._bulk_share_records(records)

	def _execute_bulk_update(self, records):
		"""Helper function to perform a single bulk UPDATE operation using CASE WHEN."""
		if not records:
			return

		meta = frappe.get_meta(self.import_doctype)
		# Exclude system fields from being updated directly, except 'modified' and 'modified_by'
		# 'name' is used for the WHERE clause, not for updating
		updatable_fields = [f.fieldname for f in meta.meta_fields if hasattr(meta, 'meta_fields')] if hasattr(meta, 'meta_fields') else [f.fieldname for f in meta.fields] + ['modified', 'modified_by']

		# Get a list of all fields present in at least one record to be updated
		fields_to_update = sorted(list(set(
				k for r in records for k in r.keys() if k in updatable_fields
		)))

		if not fields_to_update:
				return

		set_clauses = []
		for field in fields_to_update:
			# Build the CASE statement for each field
			case_statements = [
				f"WHEN `name` = {frappe.db.escape(record['name'])} THEN {frappe.db.escape(cstr(record.get(field)))}"
				for record in records if record.get('name') and record.get(field) is not None
			]
			
			if case_statements:
				set_clauses.append(f"`{field}` = CASE {' '.join(case_statements)} ELSE `{field}` END")

		if not set_clauses:
			return
		
		# Collect all the document names for the WHERE clause
		names_to_update = [frappe.db.escape(record['name']) for record in records if record.get('name')]
		unique_names = list(set(names_to_update))

		sql = f"""
			UPDATE `tab{self.import_doctype}`
			SET {', '.join(set_clauses)}
			WHERE `name` IN ({', '.join(unique_names)})
		"""
		frappe.db.sql(sql)
		
		# Bulk share the records
		self._bulk_share_records(records)

	def _bulk_share_records(self, records):
		"""Bulk insert DocShare records for the imported leads to grant access to lead_owners"""
		if not records or self.import_doctype != "CRM Lead":
			return
			
		docshares_to_insert = []
		lead_names = [r['name'] for r in records if r.get('name')]
		if not lead_names:
			return
			
		existing_shares = frappe.get_all(
			"DocShare",
			filters={
				"share_doctype": self.import_doctype,
				"share_name": ["in", lead_names]
			},
			fields=["share_name", "user"]
		)
		existing_shares_set = {(s.share_name, s.user) for s in existing_shares}
		
		for r in records:
			lead_owner = r.get('lead_owner')
			lead_name = r.get('name')
			# Only share if owner is specified and is different from the session user (who already has full access)
			if lead_owner and lead_name and lead_owner != frappe.session.user:
				if (lead_name, lead_owner) not in existing_shares_set:
					docshares_to_insert.append({
						"name": frappe.generate_hash(length=10),
						"doctype": "DocShare",
						"share_doctype": self.import_doctype,
						"share_name": lead_name,
						"user": lead_owner,
						"read": 1,
						"write": 1,
						"share": 0,
						"everyone": 0,
						"owner": frappe.session.user,
						"creation": frappe.utils.now(),
						"modified": frappe.utils.now(),
						"modified_by": frappe.session.user
					})
					existing_shares_set.add((lead_name, lead_owner))
					
		if docshares_to_insert:
			fields = list(docshares_to_insert[0].keys())
			values_list = []
			for ds in docshares_to_insert:
				row_vals = []
				for f in fields:
					val = ds.get(f)
					if val is None:
						row_vals.append("NULL")
					elif isinstance(val, (int, float)):
						row_vals.append(str(val))
					else:
						row_vals.append(frappe.db.escape(str(val)))
				values_list.append(f"({', '.join(row_vals)})")
				
			sql = f"INSERT INTO `tabDocShare` (`{', '.join(fields)}`) VALUES {', '.join(values_list)}"
			frappe.db.sql(sql)

	def generate_error_file(self, failed_rows):
		"""Generate a CSV file containing failed rows with error messages"""
		if not failed_rows:
			return None
			
		fd, path = tempfile.mkstemp(suffix='.csv')
		try:
			with os.fdopen(fd, 'w', newline='', encoding='utf-8') as csvfile:
				writer = csv.writer(csvfile)
				
				headers = list(failed_rows[0]['row'].keys())
				headers.extend(['Error Message', 'Row Number'])
				writer.writerow(headers)
				
				for idx, failed_row in enumerate(failed_rows, 1):
					row_data = list(failed_row['row'].values())
					row_data.extend([failed_row['error'], idx])
					writer.writerow(row_data)
			
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
			
			frappe.db.set_value("Lightning Upload", self.name, "error_file", file_doc.file_url)
			return file_doc.file_url
			
		finally:
			if os.path.exists(path):
				os.unlink(path)

	def validate_row_data(self, data):
		"""Validate row data before inserting"""
		if LightningUploadSettings.get_validate_from_hook():
			for method in frappe.get_hooks('lightning_import_validate_row'):
				frappe.call(method, data=data, doctype=self.import_doctype, import_type=self.import_type)

def get_detailed_doctype_fields(doctype):
	"""Internal helper to get field details including labels."""
	meta = frappe.get_meta(doctype)
	fields = meta.get("fields", {"fieldtype": ["not in", ['Section Break', 'Column Break', 'Tab Break', 'Fold']]})
	
	detailed_fields = [
		{'fieldname': f.fieldname, 'label': f.label} for f in fields
	] + [
		{'fieldname': 'name', 'label': 'ID'},
		{'fieldname': 'owner', 'label': 'Owner'},
		{'fieldname': 'creation', 'label': 'Created On'},
		{'fieldname': 'modified', 'label': 'Last Modified'},
		{'fieldname': 'modified_by', 'label': 'Modified By'},
	]
	return detailed_fields


def get_doctype_fields(doctype):
	"""Get all field names from a DocType"""
	meta = frappe.get_meta(doctype)
	fields = [field.fieldname for field in meta.fields if field.fieldtype not in ['Section Break', 'Column Break', 'Tab Break', 'Fold']]
	system_fields = ['name', 'owner', 'creation', 'modified', 'modified_by']
	return fields + system_fields

def get_csv_headers(file_path):
	"""Get headers from CSV file"""
	try:
		with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
			reader = csv.reader(csvfile)
			headers = next(reader, None)
			if not headers:
				frappe.throw("CSV file is empty")
			return [header.strip() for header in headers if header]
	except Exception as e:
		frappe.throw(f"Error reading CSV headers: {str(e)}")
		
@frappe.whitelist()
def process_import_queue(docname):
	"""Process the import in batches"""
	start_time = time.time()
	batch_timings = []

	try:
		# --- Get the doc once outside the loop ---
		doc = frappe.get_doc("Lightning Upload", docname)

		# Update initial status and total records
		doc.status = "In Progress"
		doc.save(ignore_permissions=True, ignore_version=True)
		frappe.db.commit()

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
		csv_start = time.time()
		csv_data = doc.get_mapped_data()
		csv_time = round((time.time() - csv_start) * 1000, 2)
		total_rows = len(csv_data)
		# Update total records on the doc object
		doc.total_records = total_rows
		doc.save(ignore_permissions=True, ignore_version=True)
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

		batch_size = LightningUploadSettings.get_batch_size()
		successful_records = 0
		failed_records = 0
		all_failed_rows = []

		for i in range(0, total_rows, batch_size):
			batch_start = time.time()
			batch = csv_data[i:i + batch_size]
			batch_num = (i // batch_size) + 1
			total_batches = (total_rows + batch_size - 1) // batch_size

			progress = min(100, int((i / total_rows) * 100))

			insert_start = time.time()
			# The doc.insert_records(batch) call implicitly uses the updated doc object
			result = doc.insert_records(batch)
			insert_time = round((time.time() - insert_start) * 1000, 2)

			successful_records += result['success_count']
			failed_records += len(result['failed_rows'])
			all_failed_rows.extend(result['failed_rows'])

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

			# --- Update doc properties in memory and save once per batch ---
			doc.successful_records = successful_records
			doc.failed_records = failed_records
			doc.last_processed_row = i + len(batch)
			doc.save(ignore_permissions=True, ignore_version=True)
			frappe.db.commit()

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

		time_taken = time.time() - start_time
		time_str = f"{int(time_taken)}s" if time_taken < 60 else f"{time_taken/60:.1f}m"

		error_file_time = 0
		if all_failed_rows:
			error_start = time.time()
			doc.error_log = json.dumps(all_failed_rows, indent=2)
			doc.save(ignore_permissions=True, ignore_version=True)
			doc.generate_error_file(all_failed_rows)
			error_file_time = round((time.time() - error_start) * 1000, 2)

		if failed_records == total_rows:
			final_status = "Failed"
		elif failed_records > 0:
			final_status = "Partial Success"
		else:
			final_status = "Completed"

		# --- Final updates on the doc object before the final save ---
		doc.status = final_status
		doc.import_time = time_str
		doc.timing_details = json.dumps({
			"total_time_seconds": round(time_taken, 2),
			"csv_load_time_ms": csv_time,
			"error_file_time_ms": error_file_time,
			"batch_timings": batch_timings,
			"average_batch_time_ms": round(sum(b['total_time_ms'] for b in batch_timings) / len(batch_timings), 2) if batch_timings else 0,
			"average_insert_time_ms": round(sum(b['insert_time_ms'] for b in batch_timings) / len(batch_timings), 2) if batch_timings else 0
		}, indent=2)
		doc.save(ignore_permissions=True, ignore_version=True)
		frappe.db.commit()

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
			doc = frappe.get_doc("Lightning Upload", docname) # Re-fetch in case of an error to ensure we have the latest state
			doc.status = "Failed"
			doc.error_log = str(e)
			doc.save(ignore_permissions=True, ignore_version=True)
			frappe.db.commit()
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
def auto_map_and_validate(docname):
	"""
	Performs an automatic mapping of CSV headers to DocType fields
	and validates if all required fields are mapped.
	"""
	doc = frappe.get_doc("Lightning Upload", docname)
	
	file_doc = frappe.get_doc("File", {"file_url": doc.csv_file})
	file_path = file_doc.get_full_path()
	csv_headers = get_csv_headers(file_path)

	meta = frappe.get_meta(doc.import_doctype)
	doctype_fields_meta = meta.get("fields", {"fieldtype": ["not in", ['Section Break', 'Column Break', 'Tab Break', 'Fold']]})
	required_fields = [f.fieldname for f in doctype_fields_meta if f.reqd]
	
	detailed_fields = get_detailed_doctype_fields(doc.import_doctype)

	normalized_field_map = {}
	def normalize(s):
		return s.lower().replace("_", " ").replace("-", " ")

	for f in detailed_fields:
		if f.get('fieldname'):
			normalized_field_map[normalize(f.get('fieldname'))] = f.get('fieldname')
		if f.get('label'):
			normalized_field_map[normalize(f.get('label'))] = f.get('fieldname')
	
	normalized_field_map['id'] = 'name'
	normalized_field_map['name'] = 'first_name'

	auto_mapping = {}
	for header in csv_headers:
		normalized_header = normalize(header)
		auto_mapping[header] = normalized_field_map.get(normalized_header, "")

	mapped_fields = [v for v in auto_mapping.values() if v]
	unmapped_required = [f for f in required_fields if f not in mapped_fields]

	return {
		"mapping": auto_mapping,
		"unmapped_required": unmapped_required
	}

@frappe.whitelist()
def start_import(docname, mapping=None):
	"""
	API endpoint to start the import process.
	If a mapping is provided, it saves it before starting.
	"""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		
		if doc.status != "Draft":
			frappe.throw(_("Import can only be started from Draft status"))
		
		if mapping:
			frappe.db.set_value("Lightning Upload", docname, "field_mapping", mapping)
			doc.field_mapping = mapping
		
		if not doc.field_mapping:
			frappe.throw(_("Please map fields before starting import"))

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
		
		frappe.db.set_value("Lightning Upload", docname, "status", "Queued", update_modified=False)
		
		frappe.publish_realtime(
			event='import_progress',
			message=initial_progress,
			user=frappe.session.user,
			after_commit=True
		)
		
		frappe.enqueue(
			"lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.process_import_queue",
			docname=docname,
			# now=frappe.conf.developer_mode,
			now=False,
			queue="long",
			timeout=3600
		)
		
		return {
			"status": "success",
			"message": _("Import process started successfully"),
			"progress_key": progress_key
		}
		
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Error")
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
		
		failed_rows = json.loads(doc.error_log)
		
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

@frappe.whitelist()
def get_csv_headers_for_upload(docname=None, file_url=None):
	"""Return the CSV headers for a given Lightning Upload docname or file_url"""
	try:
		if not file_url and docname:
			# Fallback to fetching via docname if file_url not provided
			if frappe.db.exists("Lightning Upload", docname):
				doc = frappe.get_doc("Lightning Upload", docname)
				file_url = doc.csv_file
			else:
				return {"status": "error", "message": f"Lightning Upload {docname} not found"}
		
		if not file_url:
			return {"status": "error", "message": "No CSV file attached"}

		file_doc = frappe.get_doc("File", {"file_url": file_url})
		file_path = file_doc.get_full_path()
		headers = get_csv_headers(file_path)
		return {"status": "success", "headers": headers}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Get CSV Headers Error")
		return {"status": "error", "message": str(e)}

@frappe.whitelist()
def save_field_mapping(docname, mapping):
	"""Save the field mapping JSON to the Lightning Upload doc"""
	try:
		frappe.db.set_value("Lightning Upload", docname, "field_mapping", mapping)
		return {"status": "success"}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Save Field Mapping Error")
		raise

@frappe.whitelist()
def check_file_duplicates(docname, mapping=None):
	"""
	Pre-import duplicate check.

	Checks if 'enable_file_duplicate_check' is on in Settings.
	If so, it scans the specific CSV column selected in 'duplicate_check_field'
	on the Lightning Upload document.
	"""
	try:
		# Check if feature is enabled in Settings
		settings = frappe.get_single("Lightning Upload Settings")
		if not settings.get("enable_file_duplicate_check"):
			return {"status": "success", "has_duplicates": False, "duplicates": [], "total_duplicate_rows": 0}

		doc = frappe.get_doc("Lightning Upload", docname)

		# If no column was selected, skip check
		if not doc.get("duplicate_check_field"):
			return {"status": "success", "has_duplicates": False, "duplicates": [], "total_duplicate_rows": 0}

		csv_col_to_check = doc.duplicate_check_field

		# Save mapping if provided
		if mapping:
			frappe.db.set_value("Lightning Upload", docname, "field_mapping", mapping)
			doc.field_mapping = mapping

		raw_rows = doc.get_csv_data()
		total_rows = len(raw_rows)

		if not raw_rows:
			return {"status": "success", "has_duplicates": False, "duplicates": [], "total_duplicate_rows": 0}

		duplicates = []
		duplicate_row_numbers = set()

		# Group 1-based row numbers by cell value
		value_to_rows = {}
		for idx, row in enumerate(raw_rows, start=1):
			val = str(row.get(csv_col_to_check, "") or "").strip()
			if val == "":
				continue  # skip blank cells
			value_to_rows.setdefault(val, []).append(idx)

		dup_entries = [
			{"value": val, "rows": idxs, "count": len(idxs)}
			for val, idxs in value_to_rows.items()
			if len(idxs) > 1
		]

		if dup_entries:
			for entry in dup_entries:
				duplicate_row_numbers.update(entry["rows"])

			# Try to find the doctype field this maps to (for display)
			doctype_field = csv_col_to_check
			if doc.field_mapping:
				field_mapping = json.loads(doc.field_mapping)
				doctype_field = field_mapping.get(csv_col_to_check) or csv_col_to_check

			duplicates.append({
				"field": doctype_field,
				"csv_column": csv_col_to_check,
				"duplicate_values": dup_entries,
				"count": len(dup_entries)
			})

		return {
			"status": "success",
			"has_duplicates": len(duplicates) > 0,
			"duplicates": duplicates,
			"total_duplicate_rows": len(duplicate_row_numbers),
			"total_rows": total_rows
		}

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Duplicate Check Error")
		return {"status": "error", "message": str(e)}