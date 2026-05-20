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

# ==========================================
# FILE HELPERS
# ==========================================

def get_raw_sheet_rows(doc):
	"""Get raw CSV rows from the document's uploaded sheet"""
	if not doc.csv_file:
		frappe.throw(_("No CSV file attached"))
	
	try:
		file_doc = frappe.get_doc("File", {"file_url": doc.csv_file})
		file_path = file_doc.get_full_path()
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import File Access Error")
		frappe.throw(_("Error accessing file: {}".format(str(e))))

	try:
		with open(file_path, 'r', encoding='utf-8') as csvfile:
			reader = csv.DictReader(csvfile)
			return list(reader)
	except Exception as e:
		frappe.throw(_("Error reading CSV data: {}").format(str(e)))

def get_sheet_headers(doc):
	"""Get headers from CSV file for the doc"""
	if not doc.csv_file:
		frappe.throw(_("No CSV file attached"))
	
	try:
		file_doc = frappe.get_doc("File", {"file_url": doc.csv_file})
		file_path = file_doc.get_full_path()
		return get_csv_headers(file_path)
	except Exception as e:
		frappe.throw(_("Error reading CSV headers: {}").format(str(e)))

# ==========================================
# MAPPING HELPERS
# ==========================================

def normalize_column(value):
	"""Normalize column names for fuzzy mapping and aliases"""
	if not value:
		return ""
	return (
		str(value)
		.strip()
		.lower()
		.replace(" ", "")
		.replace("_", "")
		.replace("-", "")
	)

def build_alias_map_for_doctype(doctype):
	"""Build alias mapping dictionary for a specific target DocType"""
	alias_map = {}
	try:
		mapping_docs = frappe.get_all(
			"Lightning Field Mapping",
			filters={"reference_doctype": doctype},
			fields=["name"]
		)
		for mapping_doc in mapping_docs:
			mapping = frappe.get_doc("Lightning Field Mapping", mapping_doc.name)
			for row in mapping.mappings:
				if row.alternate_name and row.field_name:
					alias_map[normalize_column(row.alternate_name)] = row.field_name
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Error building alias map")
	return alias_map

def auto_map_headers_for_doctype(headers, doctype):
	"""Generate automatic field mapping from headers for a specific target DocType"""
	print("Headers:", headers)
	print("Target:", doctype)

	meta = frappe.get_meta(doctype)
	doctype_fields_meta = meta.get("fields", {"fieldtype": ["not in", ['Section Break', 'Column Break', 'Tab Break', 'Fold']]})
	required_fields = [f.fieldname for f in doctype_fields_meta if f.reqd]
	
	detailed_fields = get_detailed_doctype_fields(doctype)
	alias_map = build_alias_map_for_doctype(doctype)
	print("Alias Map:", alias_map)
	
	normalized_field_map = {}
	def normalize_legacy(s):
		if not s:
			return ""
		return s.lower().replace("_", " ").replace("-", " ")

	for f in detailed_fields:
		if f.get('fieldname'):
			normalized_field_map[normalize_legacy(f.get('fieldname'))] = f.get('fieldname')
		if f.get('label'):
			normalized_field_map[normalize_legacy(f.get('label'))] = f.get('fieldname')
	
	normalized_field_map['id'] = 'name'
	normalized_field_map['name'] = 'first_name'

	auto_mapping = {}
	for header in headers:
		# Step 1: Exact fieldname or label match (Legacy)
		header_norm_legacy = normalize_legacy(header)
		mapped_field = normalized_field_map.get(header_norm_legacy, "")

		# Step 2: Alias match
		header_norm_column = normalize_column(header)
		if not mapped_field:
			mapped_field = alias_map.get(header_norm_column)

		auto_mapping[header] = mapped_field or ""

	print("Generated Mapping:", auto_mapping)

	mapped_fields = [v for v in auto_mapping.values() if v]
	unmapped_required = [f for f in required_fields if f not in mapped_fields]

	return {
		"mapping": auto_mapping,
		"unmapped_required": unmapped_required
	}

# ==========================================
# ROW HELPERS
# ==========================================

def map_rows_for_doctype(raw_rows, mapping):
	"""Map CSV rows using the field mapping dictionary"""
	mapped_rows = []
	if not mapping:
		return mapped_rows
		
	if isinstance(mapping, str):
		mapping = json.loads(mapping)

	for idx, row in enumerate(raw_rows, start=1):
		mapped_row = {}
		for csv_field, doctype_field in mapping.items():
			if doctype_field:  # Only map if field is not empty
				mapped_row[doctype_field] = row.get(csv_field, None)
		# Inject meta-fields for precise row tracking
		mapped_row["__csv_row_number__"] = idx
		mapped_row["__original_row__"] = row
		mapped_rows.append(mapped_row)
	return mapped_rows

def prepare_records(import_doctype, rows):
	"""Prepare, type-convert, and validate mapped rows for a DocType"""
	meta = frappe.get_meta(import_doctype)
	field_types = {f.fieldname: f.fieldtype for f in meta.fields}
	required_fields = [f.fieldname for f in meta.fields if f.reqd]
	
	records_to_process = []
	failed_rows = []
	
	for row in rows:
		try:
			converted_data = {}
			for field, value in row.items():
				# Bypass metadata fields from strict validation
				if field.startswith("__"):
					converted_data[field] = value
					continue
					
				if field in field_types:
					field_type = field_types[field]
					try:
						if value is not None and str(value).strip() != "":
							if field_type == "Int": converted_data[field] = int(float(value))
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
			
			# Validate row data via hooks
			if LightningUploadSettings.get_validate_from_hook():
				for method in frappe.get_hooks('lightning_import_validate_row'):
					frappe.call(method, data=converted_data, doctype=import_doctype, import_type=None)
			
			records_to_process.append(converted_data)

		except Exception as e:
			failed_rows.append({'row': row, 'error': str(e)})
			
	return records_to_process, failed_rows

# ==========================================
# DATABASE HELPERS
# ==========================================

def execute_bulk_insert(import_doctype, records):
	"""Perform bulk INSERT operation on the database"""
	if not records:
		return
	
	meta = frappe.get_meta(import_doctype)
	all_fields = ['name', 'owner', 'modified_by', 'creation', 'modified'] + [f.fieldname for f in meta.fields]
	
	# Exclude helper metadata fields beginning with '__'
	fields = sorted(list(set(k for r in records for k in r.keys() if k in all_fields and not k.startswith("__"))))
	
	values_list = []
	for record in records:
		row_values = [frappe.db.escape(cstr(record.get(f))) for f in fields]
		values_list.append(f"({', '.join(row_values)})")
	
	sql = f"""
		INSERT INTO `tab{import_doctype}` (`{ '`, `'.join(fields) }`)
		VALUES {', '.join(values_list)}
	"""
	frappe.db.sql(sql)

def execute_bulk_update(import_doctype, records):
	"""Perform bulk UPDATE operation using CASE WHEN statements"""
	if not records:
		return

	meta = frappe.get_meta(import_doctype)
	updatable_fields = [f.fieldname for f in meta.fields] + ['modified', 'modified_by']

	fields_to_update = sorted(list(set(
		k for r in records for k in r.keys() if k in updatable_fields and not k.startswith("__")
	)))

	if not fields_to_update:
		return

	set_clauses = []
	for field in fields_to_update:
		case_statements = [
			f"WHEN `name` = {frappe.db.escape(record['name'])} THEN {frappe.db.escape(cstr(record.get(field)))}"
			for record in records if record.get('name') and record.get(field) is not None
		]
		
		if case_statements:
			set_clauses.append(f"`{field}` = CASE {' '.join(case_statements)} ELSE `{field}` END")

	if not set_clauses:
		return
	
	names_to_update = [frappe.db.escape(record['name']) for record in records if record.get('name')]
	unique_names = list(set(names_to_update))

	sql = f"""
		UPDATE `tab{import_doctype}`
		SET {', '.join(set_clauses)}
		WHERE `name` IN ({', '.join(unique_names)})
	"""
	frappe.db.sql(sql)

# ==========================================
# MAIN REUSABLE IMPORT ENGINE
# ==========================================

def import_rows_for_doctype(import_config, raw_rows):
	"""
	Main reusable import engine.
	Imports raw_rows into the target doctype according to import_config.
	"""
	import_doctype = import_config.get("import_doctype")
	import_type = import_config.get("import_type")
	field_mapping = import_config.get("field_mapping")
	update_on_field = import_config.get("update_on_field")
	
	# 1. Map rows
	mapped_rows = map_rows_for_doctype(raw_rows, field_mapping)
	
	# 2. Filter out rows with no mapped data
	valid_mapped_rows = []
	for r in mapped_rows:
		if any(val is not None and str(val).strip() != "" for k, val in r.items() if not k.startswith("__")):
			valid_mapped_rows.append(r)
			
	if not valid_mapped_rows:
		return {"success_count": 0, "failed_rows": []}
		
	# 3. Prepare records
	records_to_process, failed_rows = prepare_records(import_doctype, valid_mapped_rows)
	
	if not records_to_process:
		return {"success_count": 0, "failed_rows": failed_rows}
		
	success_count = 0
	try:
		if import_type == "Insert and Update Records":
			mapping = json.loads(field_mapping) if isinstance(field_mapping, str) else field_mapping
			update_on_csv_col = update_on_field
			if not update_on_csv_col:
				raise ValueError("Validate On CSV Column not specified for 'Insert and Update' mode.")
			
			mapped_update_field = mapping.get(update_on_csv_col)
			if not mapped_update_field:
				raise ValueError(f"The selected update column '{update_on_csv_col}' is not mapped to any DocType field.")

			keys_to_check = list(set([rec.get(mapped_update_field) for rec in records_to_process if rec.get(mapped_update_field)]))
			
			existing_docs_map = {}
			if keys_to_check:
				existing = frappe.get_all(
					import_doctype,
					filters={mapped_update_field: ['in', keys_to_check]},
					fields=['name', mapped_update_field]
				)
				existing_docs_map = {doc[mapped_update_field]: doc.name for doc in existing}

			to_insert = []
			to_update = []
			for record in records_to_process:
				key_value = record.get(mapped_update_field)
				if key_value in existing_docs_map:
					record['name'] = existing_docs_map[key_value]
					to_update.append(record)
				else:
					# Generate unique docname
					timestamp = int(time.time() * 1000000)
					random_suffix = random.randint(100000, 999999)
					record['name'] = f"{import_doctype}-{timestamp}{random_suffix}"
					to_insert.append(record)

			UPDATE_CHUNK_SIZE = 1000
			if to_update:
				for i in range(0, len(to_update), UPDATE_CHUNK_SIZE):
					chunk = to_update[i:i + UPDATE_CHUNK_SIZE]
					execute_bulk_update(import_doctype, chunk)
			
			if to_insert:
				execute_bulk_insert(import_doctype, to_insert)

			success_count = len(to_insert) + len(to_update)

		elif import_type == "Insert New Records":
			for record in records_to_process:
				timestamp = int(time.time() * 1000000)
				random_suffix = random.randint(100000, 999999)
				record['name'] = f"{import_doctype}-{timestamp}{random_suffix}"
			execute_bulk_insert(import_doctype, records_to_process)
			success_count = len(records_to_process)

		elif import_type == "Update Existing Records":
			UPDATE_CHUNK_SIZE = 1000
			for i in range(0, len(records_to_process), UPDATE_CHUNK_SIZE):
				chunk = records_to_process[i:i + UPDATE_CHUNK_SIZE]
				execute_bulk_update(import_doctype, chunk)
			success_count = len(records_to_process)
			
	except Exception as e:
		for record in records_to_process:
			failed_rows.append({'row': record, 'error': str(e)})
		success_count = 0

	return {'success_count': success_count, 'failed_rows': failed_rows}

# ==========================================
# DOCTYPE CONTROLLER
# ==========================================

class LightningUpload(Document):
	def validate(self):
		"""Validate the document before save"""
		if self.csv_file:
			self.validate_csv_file()
	
	def validate_mappings(self):
		"""Explicit mapping validation before starting the import process"""
		if not self.field_mapping:
			frappe.throw(_("Please map fields before starting import"))
		meta = frappe.get_meta(self.import_doctype)
		required_fields = [f.fieldname for f in meta.fields if f.reqd]
		mapping = json.loads(self.field_mapping)
		mapped_fields = [v for v in mapping.values() if v]
		unmapped_required = [f for f in required_fields if f not in mapped_fields]
		if unmapped_required:
			frappe.throw(_("Please map all required fields for {0}: {1}").format(self.import_doctype, ", ".join(unmapped_required)))
		if self.import_type == "Update Existing Records" and 'name' not in mapped_fields:
			frappe.throw(_("For 'Update Existing Records', the target field 'name' (ID) must be mapped."))

	def validate_csv_file(self):
		"""Validate if the uploaded file is a valid CSV file"""
		try:
			file_doc = frappe.get_doc("File", {"file_url": self.csv_file})
			file_path = file_doc.get_full_path()
		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "Lightning Import File Access Error")
			frappe.throw(_("Error accessing file: {}".format(str(e))))
			return

		file_ext = os.path.splitext(file_path)[1].lower()
		if file_ext != '.csv':
			frappe.throw(_("Please upload a CSV file. Current file type: {}".format(file_ext)))
			return

		try:
			with open(file_path, 'r', encoding='utf-8') as csvfile:
				reader = csv.reader(csvfile)
				header = next(reader, None)
				
				if not header:
					frappe.throw(_("CSV file is empty"))
					return
				
				for i in range(5):
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
		return get_raw_sheet_rows(self)

	def get_mapped_data(self):
		"""Return list of mapped CSV rows using saved field mapping"""
		raw_rows = self.get_csv_data()
		return map_rows_for_doctype(raw_rows, self.field_mapping)

	def generate_docname(self, row_data):
		"""Generate a unique docname based on row data"""
		timestamp = int(time.time() * 1000000)
		random_suffix = random.randint(100000, 999999)
		return f"{self.import_doctype}-{timestamp}{random_suffix}"

	def insert_records(self, rows):
		"""Insert or update records in bulk using SQL (for backward compatibility)"""
		records_to_process, failed_rows = prepare_records(self.import_doctype, rows)
		
		if not records_to_process:
			return {'success_count': 0, 'failed_rows': failed_rows}
			
		success_count = 0
		try:
			if self.import_type == "Insert and Update Records":
				mapping = json.loads(self.field_mapping)
				update_on_csv_col = self.update_on_field
				if not update_on_csv_col:
					raise ValueError("Validate On CSV Column not specified for 'Insert and Update' mode.")
				
				mapped_update_field = mapping.get(update_on_csv_col)
				if not mapped_update_field:
					raise ValueError(f"The selected update column '{update_on_csv_col}' is not mapped to any DocType field.")

				keys_to_check = list(set([rec.get(mapped_update_field) for rec in records_to_process if rec.get(mapped_update_field)]))
				
				existing_docs_map = {}
				if keys_to_check:
					existing = frappe.get_all(
						self.import_doctype,
						filters={mapped_update_field: ['in', keys_to_check]},
						fields=['name', mapped_update_field]
					)
					existing_docs_map = {doc[mapped_update_field]: doc.name for doc in existing}

				to_insert = []
				to_update = []
				for record in records_to_process:
					key_value = record.get(mapped_update_field)
					if key_value in existing_docs_map:
						record['name'] = existing_docs_map[key_value]
						to_update.append(record)
					else:
						record['name'] = self.generate_docname(record)
						to_insert.append(record)

				UPDATE_CHUNK_SIZE = 1000
				if to_update:
					for i in range(0, len(to_update), UPDATE_CHUNK_SIZE):
						chunk = to_update[i:i + UPDATE_CHUNK_SIZE]
						execute_bulk_update(self.import_doctype, chunk)
				
				if to_insert:
					execute_bulk_insert(self.import_doctype, to_insert)

				success_count = len(to_insert) + len(to_update)

			elif self.import_type == "Insert New Records":
				for record in records_to_process:
					record['name'] = self.generate_docname(record)
				execute_bulk_insert(self.import_doctype, records_to_process)
				success_count = len(records_to_process)

			elif self.import_type == "Update Existing Records":
				UPDATE_CHUNK_SIZE = 1000
				for i in range(0, len(records_to_process), UPDATE_CHUNK_SIZE):
					chunk = records_to_process[i:i + UPDATE_CHUNK_SIZE]
					execute_bulk_update(self.import_doctype, chunk)
				success_count = len(records_to_process)
				
		except Exception as e:
			for record in records_to_process:
				failed_rows.append({'row': record, 'error': str(e)})
			success_count = 0

		return {'success_count': success_count, 'failed_rows': failed_rows}

	def generate_error_file(self, failed_rows):
		"""Generate a CSV file containing failed rows with error messages"""
		if not failed_rows:
			return None
			
		fd, path = tempfile.mkstemp(suffix='.csv')
		try:
			with os.fdopen(fd, 'w', newline='', encoding='utf-8') as csvfile:
				writer = csv.writer(csvfile)
				
				# Get original columns if available
				row_example = failed_rows[0]['row']
				headers = [k for k in row_example.keys() if not k.startswith("__")]
				headers.extend(['Error Message', 'Row Number'])
				writer.writerow(headers)
				
				for failed_row in failed_rows:
					r_dict = failed_row['row']
					row_data = [r_dict.get(h) for h in headers if not h.startswith("__")]
					
					row_num = r_dict.get('__csv_row_number__', '')
					row_data.extend([failed_row['error'], row_num])
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

# ==========================================
# PUBLIC API UTILITIES
# ==========================================

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
		with open(file_path, 'r', encoding='utf-8') as csvfile:
			reader = csv.reader(csvfile)
			headers = next(reader, None)
			if not headers:
				frappe.throw("CSV file is empty")
			return [header.strip() for header in headers]
	except Exception as e:
		frappe.throw(f"Error reading CSV headers: {str(e)}")

# ==========================================
# WHITELISTED ENDPOINTS
# ==========================================

@frappe.whitelist()
def process_import_queue(docname):
	"""Process the single import in batches"""
	start_time = time.time()
	batch_timings = []

	try:
		doc = frappe.get_doc("Lightning Upload", docname)

		frappe.db.set_value("Lightning Upload", docname, "status", "In Progress")
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

			frappe.db.set_value(
				"Lightning Upload",
				docname,
				{
					"successful_records": successful_records,
					"failed_records": failed_records,
					"last_processed_row": i + len(batch)
				},
				update_modified=False
			)
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

		if all_failed_rows:
			doc.error_log = json.dumps([{"error": f['error'], "row": f['row']} for f in all_failed_rows], indent=2)
			frappe.db.set_value("Lightning Upload", docname, "error_log", doc.error_log)
			doc.generate_error_file(all_failed_rows)

		if failed_records == total_rows:
			final_status = "Failed"
		elif failed_records > 0:
			final_status = "Partial Success"
		else:
			final_status = "Completed"

		frappe.db.set_value(
			"Lightning Upload",
			docname,
			{
				"status": final_status,
				"import_time": time_str
			}
		)
		frappe.db.commit()

		final_progress = {
			"status": final_status,
			"progress": 100,
			"title": f"Import {final_status.lower()}",
			"progress_key": progress_key,
			"time_taken": time_str,
			"successful_records": successful_records,
			"failed_records": failed_records,
			"total_records": total_rows
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
			"message": f"Import completed. Successful: {successful_records}, Failed: {failed_records}. Time taken: {time_str}"
		}

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Processing Error")
		try:
			frappe.db.set_value("Lightning Upload", docname, "status", "Failed")
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
	headers = get_sheet_headers(doc)
	return auto_map_headers_for_doctype(headers, doc.import_doctype)

@frappe.whitelist()
def start_import(docname, mapping=None):
	"""API endpoint to start the import process in the background"""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		
		if doc.status != "Draft":
			frappe.throw(_("Import can only be started from Draft status"))
		
		if mapping:
			frappe.db.set_value("Lightning Upload", docname, "field_mapping", mapping)
			doc.field_mapping = mapping
		
		doc.validate_mappings()

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
		
		frappe.enqueue(
			"lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.process_import_queue",
			docname=docname,
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
		frappe.log_error(frappe.get_traceback(), "Lightning Import Start Error")
		try:
			frappe.db.set_value("Lightning Upload", docname, "status", "Draft")
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
		
		return {
			"status": "success",
			"file_url": doc.error_file
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
	"""Pre-import duplicate check for single import."""
	try:
		settings = frappe.get_single("Lightning Upload Settings")
		if not settings.get("enable_file_duplicate_check"):
			return {"status": "success", "has_duplicates": False, "duplicates": [], "total_duplicate_rows": 0}

		doc = frappe.get_doc("Lightning Upload", docname)

		if not doc.get("duplicate_check_field"):
			return {"status": "success", "has_duplicates": False, "duplicates": [], "total_duplicate_rows": 0}

		csv_col_to_check = doc.duplicate_check_field

		if mapping:
			frappe.db.set_value("Lightning Upload", docname, "field_mapping", mapping)
			doc.field_mapping = mapping

		raw_rows = doc.get_csv_data()
		total_rows = len(raw_rows)

		if not raw_rows:
			return {"status": "success", "has_duplicates": False, "duplicates": [], "total_duplicate_rows": 0}

		duplicates = []
		duplicate_row_numbers = set()

		value_to_rows = {}
		for idx, row in enumerate(raw_rows, start=1):
			val = str(row.get(csv_col_to_check, "") or "").strip()
			if val == "":
				continue
			value_to_rows.setdefault(val, []).append(idx)

		dup_entries = [
			{"value": val, "rows": idxs, "count": len(idxs)}
			for val, idxs in value_to_rows.items()
			if len(idxs) > 1
		]

		if dup_entries:
			for entry in dup_entries:
				duplicate_row_numbers.update(entry["rows"])

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