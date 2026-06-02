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
import io

def build_readable_error_log(failed_rows, default_doctype=None):
	"""Convert failed rows into a human-readable text log."""
	lines = []
	for fr in failed_rows[:100]:
		row = fr.get('row') or {}
		row_num = row.get('__csv_row_number__', '?')
		error_msg = fr.get('error', '')
		target_doctype = fr.get('target_doctype') or default_doctype or 'Unknown'
		
		if isinstance(error_msg, str):
			error_msg_short = error_msg[:1024]
		else:
			error_msg_short = str(error_msg)[:1024]
			
		lines.append(f"Row {row_num} ({target_doctype}):\n{error_msg_short}")
		
	error_text = "\n\n".join(lines)
	if len(failed_rows) > 100:
		error_text += f"\n\n... and {len(failed_rows) - 100} more errors. Download the error CSV for full details."
		
	return error_text

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
			rows = list(reader)
			for idx, row in enumerate(rows, start=1):
				row["__csv_row_number__"] = idx
			return rows
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
				actual_csv_field = csv_field.split("::")[0] if "::" in csv_field else csv_field
				mapped_row[doctype_field] = row.get(actual_csv_field, None)
		# Inject meta-fields for precise row tracking
		mapped_row["__csv_row_number__"] = row.get("__csv_row_number__") or idx
		mapped_row["__original_row__"] = row
		mapped_rows.append(mapped_row)
	return mapped_rows

def prepare_records(import_doctype, rows, import_type=None):
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
					frappe.call(method, data=converted_data, doctype=import_doctype, import_type=import_type)
			
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
	records_to_process, failed_rows = prepare_records(import_doctype, valid_mapped_rows, import_type)
	
	if not records_to_process:
		return {"success_count": 0, "failed_rows": failed_rows}
		
	success_count = 0
	try:
		if import_type in ["Insert and Update Records", "Update Existing Records"]:
			mapping = json.loads(field_mapping) if isinstance(field_mapping, str) else field_mapping
			update_on_csv_col = import_config.get("update_on_field")
			
			if update_on_csv_col:
				mapped_update_field = (
					mapping.get(f"{update_on_csv_col}::{import_doctype}")
					or mapping.get(update_on_csv_col)
				)
				if not mapped_update_field:
					raise ValueError(f"The selected Validate On CSV Column '{update_on_csv_col}' is not mapped to any DocType field.")
	
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
						if import_type == "Insert and Update Records":
							timestamp = int(time.time() * 1000000)
							random_suffix = random.randint(100000, 999999)
							record['name'] = f"{import_doctype}-{timestamp}{random_suffix}"
							to_insert.append(record)
						else:
							failed_rows.append({'row': record, 'error': f"Matching record not found for {mapped_update_field} = {key_value}"})
			else:
				to_insert = []
				to_update = []
				for record in records_to_process:
					if record.get('name'):
						to_update.append(record)
					else:
						if import_type == "Insert and Update Records":
							timestamp = int(time.time() * 1000000)
							random_suffix = random.randint(100000, 999999)
							record['name'] = f"{import_doctype}-{timestamp}{random_suffix}"
							to_insert.append(record)
						else:
							failed_rows.append({'row': record, 'error': "Name ID not provided for update."})

			UPDATE_CHUNK_SIZE = 1000
			if to_update:
				for i in range(0, len(to_update), UPDATE_CHUNK_SIZE):
					chunk = to_update[i:i + UPDATE_CHUNK_SIZE]
					execute_bulk_update(import_doctype, chunk)
			
			if to_insert and import_type == "Insert and Update Records":
				execute_bulk_insert(import_doctype, to_insert)

			success_count = len(to_insert) + len(to_update)

		elif import_type == "Insert New Records":
			for record in records_to_process:
				timestamp = int(time.time() * 1000000)
				random_suffix = random.randint(100000, 999999)
				record['name'] = f"{import_doctype}-{timestamp}{random_suffix}"
			execute_bulk_insert(import_doctype, records_to_process)
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
		if not self.csv_file:
			frappe.throw(_("CSV File is required."))
			
		self.validate_csv_file()
		
		# Enforce one import mode selected
		if not self.single_import and not self.multiple_import:
			frappe.throw(_("Please select either Single Import or Multiple Import."))
			
		if self.single_import and self.multiple_import:
			frappe.throw(_("Both Single Import and Multiple Import cannot be selected together."))

		if self.single_import:
			if not self.import_doctype:
				frappe.throw(_("DocType is required for Single Import."))
			if not self.import_type:
				frappe.throw(_("Import Type is required for Single Import."))
			if self.import_type == "Insert and Update Records" and not self.update_on_field:
				frappe.throw(_("Validate On CSV Column is required for 'Insert and Update Records' import type."))
 
		elif self.multiple_import:
			enabled_targets = [t for t in self.multi_import_targets if t.enabled]
			if not enabled_targets:
				frappe.throw(_("At least one enabled target row must exist for Multiple Import."))
				
			for idx, target in enumerate(self.multi_import_targets, start=1):
				if not target.enabled:
					continue
				if not target.target_doctype:
					frappe.throw(_("Row #{0}: Target DocType is required.").format(idx))
				if not target.import_type:
					frappe.throw(_("Row #{0}: Import Type is required.").format(idx))
				if target.import_type == "Insert and Update Records" and not target.update_on_field:
					frappe.throw(_("Row #{0}: Validate On CSV Column is required for 'Insert and Update Records' import type.").format(idx))

			# Aggregate doctypes and import types to display cleanly in standard list view columns
			doctypes = sorted(list(set(t.target_doctype for t in enabled_targets if t.target_doctype)))
			import_types = sorted(list(set(t.import_type for t in enabled_targets if t.import_type)))
			
			joined_doctypes = ", ".join(doctypes)
			if len(joined_doctypes) > 140:
				joined_doctypes = joined_doctypes[:137] + "..."
			self.import_doctype = joined_doctypes
			
			joined_import_types = ", ".join(import_types)
			if len(joined_import_types) > 140:
				joined_import_types = joined_import_types[:137] + "..."
			self.import_type = joined_import_types

	def validate_mappings(self):
		"""Explicit mapping validation before starting the import process"""
		if self.single_import:
			if not self.field_mapping:
				frappe.throw(_("Please map fields before starting import"))
			meta = frappe.get_meta(self.import_doctype)
			required_fields = [f.fieldname for f in meta.fields if f.reqd]
			mapping = json.loads(self.field_mapping)
			mapped_fields = [v for v in mapping.values() if v]
			unmapped_required = [f for f in required_fields if f not in mapped_fields]
			if unmapped_required:
				frappe.throw(_("Please map all required fields for {0}: {1}").format(self.import_doctype, ", ".join(unmapped_required)))
			
			if self.import_type in ["Update Existing Records", "Insert and Update Records"]:
				has_name = 'name' in mapped_fields
				has_update_on = False
				if self.update_on_field:
					has_update_on = bool(mapping.get(self.update_on_field))
				if not (has_name or has_update_on):
					frappe.throw(_("For updating records, either the 'name' (ID) field or the configured 'Validate On CSV Column' ({0}) must be mapped.").format(self.update_on_field or ""))

		elif self.multiple_import:
			enabled_targets = [t for t in self.multi_import_targets if t.enabled]
			if not enabled_targets:
				frappe.throw(_("At least one enabled target row must exist for Multiple Import."))
			for idx, target in enumerate(self.multi_import_targets, start=1):
				if not target.enabled:
					continue
				if not target.field_mapping:
					frappe.throw(_("Row #{0}: Field mapping is not defined. Please map fields for {1}.").format(idx, target.target_doctype))
				meta = frappe.get_meta(target.target_doctype)
				required_fields = [f.fieldname for f in meta.fields if f.reqd]
				mapping = json.loads(target.field_mapping)
				mapped_fields = [v for v in mapping.values() if v]
				unmapped_required = [f for f in required_fields if f not in mapped_fields]
				if unmapped_required:
					frappe.throw(_("Row #{0}: Please map all required fields for {1}: {2}").format(idx, target.target_doctype, ", ".join(unmapped_required)))
				
				if target.import_type in ["Update Existing Records", "Insert and Update Records"]:
					has_name = 'name' in mapped_fields
					has_update_on = False
					if target.update_on_field:
						has_update_on = bool(
							mapping.get(f"{target.update_on_field}::{target.target_doctype}")
							or mapping.get(target.update_on_field)
						)
					if not (has_name or has_update_on):
						frappe.throw(_("Row #{0}: For updating records in {1}, either the 'name' (ID) field or the configured 'Validate On CSV Column' ({2}) must be mapped.").format(idx, target.target_doctype, target.update_on_field or ""))

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
		records_to_process, failed_rows = prepare_records(self.import_doctype, rows, self.import_type)
		
		if not records_to_process:
			return {'success_count': 0, 'failed_rows': failed_rows}
			
		success_count = 0
		try:
			if self.import_type in ["Insert and Update Records", "Update Existing Records"]:
				mapping = json.loads(self.field_mapping)
				update_on_csv_col = self.update_on_field
				
				if update_on_csv_col:
					mapped_update_field = mapping.get(update_on_csv_col)
					if not mapped_update_field:
						raise ValueError(f"The selected Validate On CSV Column '{update_on_csv_col}' is not mapped to any DocType field.")
	
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
							if self.import_type == "Insert and Update Records":
								record['name'] = self.generate_docname(record)
								to_insert.append(record)
							else:
								failed_rows.append({'row': record, 'error': f"Matching record not found for {mapped_update_field} = {key_value}"})
				else:
					to_insert = []
					to_update = []
					for record in records_to_process:
						if record.get('name'):
							to_update.append(record)
						else:
							if self.import_type == "Insert and Update Records":
								record['name'] = self.generate_docname(record)
								to_insert.append(record)
							else:
								failed_rows.append({'row': record, 'error': "Name ID not provided for update."})

				UPDATE_CHUNK_SIZE = 1000
				if to_update:
					for i in range(0, len(to_update), UPDATE_CHUNK_SIZE):
						chunk = to_update[i:i + UPDATE_CHUNK_SIZE]
						execute_bulk_update(self.import_doctype, chunk)
				
				if to_insert and self.import_type == "Insert and Update Records":
					execute_bulk_insert(self.import_doctype, to_insert)

				success_count = len(to_insert) + len(to_update)

			elif self.import_type == "Insert New Records":
				for record in records_to_process:
					record['name'] = self.generate_docname(record)
				execute_bulk_insert(self.import_doctype, records_to_process)
				success_count = len(records_to_process)
				
		except Exception as e:
			for record in records_to_process:
				failed_rows.append({'row': record, 'error': str(e)})
			success_count = 0

		return {'success_count': success_count, 'failed_rows': failed_rows}

	def generate_error_file(self, failed_rows):
		"""Generate CSV files containing failed rows with error messages, chunked to avoid size limits."""
		if not failed_rows:
			return None

		# Define maximum rows per chunk (adjustable)
		CHUNK_SIZE = 4000
		file_urls = []

		# Determine base headers from first failed row (excluding internal metadata fields)
		example_row = failed_rows[0]["row"]
		base_headers = [k for k in example_row.keys() if not k.startswith("__")]
		headers = base_headers + ["Error Message", "Row Number"]

		# Process each chunk
		for idx, start in enumerate(range(0, len(failed_rows), CHUNK_SIZE), start=1):
			chunk = failed_rows[start:start + CHUNK_SIZE]
			fd, path = tempfile.mkstemp(suffix='.csv')
			try:
				with os.fdopen(fd, 'w', newline='', encoding='utf-8') as csvfile:
					writer = csv.writer(csvfile)
					writer.writerow(headers)
					for failed in chunk:
						row_dict = failed["row"]
						row_data = [row_dict.get(h) for h in base_headers]
						row_num = row_dict.get('__csv_row_number__', '')
						row_data.extend([failed["error"], row_num])
						writer.writerow(row_data)

				with open(path, 'rb') as f:
					file_content = f.read()

				file_doc = save_file(
					fname=f"error_log_{self.name}_part_{idx}.csv",
					content=file_content,
					dt="Lightning Upload",
					dn=self.name,
					folder="Home/Attachments",
					is_private=1,
				)
				file_urls.append(file_doc.file_url)
			finally:
				if os.path.exists(path):
					os.unlink(path)

		# Save reference to the first generated file in the primary error_file field
		if file_urls:
			frappe.db.set_value("Lightning Upload", self.name, "error_file", file_urls[0])
			error_log_text = build_readable_error_log(failed_rows, self.import_doctype)
			self.error_log = error_log_text
			frappe.db.set_value("Lightning Upload", self.name, "error_log", error_log_text)
			return file_urls[0]
		return None

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

		error_file_time = 0
		if all_failed_rows:
			error_start = time.time()
			doc.generate_error_file(all_failed_rows)
			error_file_time = round((time.time() - error_start) * 1000, 2)

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
				"import_time": time_str,
				"timing_details": json.dumps({
					"total_time_seconds": round(time_taken, 2),
					"csv_load_time_ms": csv_time,
					"error_file_time_ms": error_file_time,
					"batch_timings": batch_timings,
					"average_batch_time_ms": round(sum(b['total_time_ms'] for b in batch_timings) / len(batch_timings), 2) if batch_timings else 0,
					"average_insert_time_ms": round(sum(b['insert_time_ms'] for b in batch_timings) / len(batch_timings), 2) if batch_timings else 0
				}, indent=2)
			}
		)
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
			frappe.db.set_value(
				"Lightning Upload",
				docname,
				{
					"status": "Failed",
					"error_log": str(e)
				}
			)
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
	"""
	API endpoint to start the single import process.
	"""
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
		
		frappe.db.set_value("Lightning Upload", docname, "status", "Queued", update_modified=False)
		frappe.db.commit()
		
		frappe.publish_realtime(
			event='import_progress',
			message=initial_progress,
			user=frappe.session.user,
			after_commit=True
		)
		
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
def get_multi_import_progress(progress_key):
	"""Get the current progress of a multiple import"""
	try:
		progress = frappe.cache().get_value(progress_key)
		return progress or {"status": "Not Found", "progress": 0, "title": "Multiple Import not found"}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Multiple Import Progress Error")
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
def save_multi_field_mapping(docname, target_row_name, mapping):
	"""
	API endpoint to save the field mapping JSON to a specific target DocType row in multi_import_targets.
	"""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		for target in doc.multi_import_targets:
			if target.name == target_row_name:
				frappe.db.set_value("Lightning Multi Import Target", target_row_name, "field_mapping", mapping)
				return {"status": "success"}
		return {"status": "error", "message": f"Target row {target_row_name} not found"}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Save Multi Field Mapping Error")
		return {"status": "error", "message": str(e)}

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

# ==========================================
# NEW MULTIPLE IMPORT ENDPOINTS & HELPERS
# ==========================================

def generate_multi_error_file(doc, all_failed_rows):
	"""Generate a single combined CSV error file for multiple target DocTypes"""
	if not all_failed_rows:
		return None

	# Configuration
	CHUNK_SIZE = 500  # aggressive chunking to keep each file small
	MAX_SAFE_SIZE = 10 * 1024 * 1024 - 1024  # 10MB minus 1KB margin
	file_urls = []

	def chunk_list(lst, size):
		for i in range(0, len(lst), size):
			yield lst[i:i + size]

	def generate_csv_content_for_chunk(chunk_rows):
		# Keep rows lightweight: include only non-meta row fields and short error message
		output = io.StringIO()
		writer = csv.writer(output)
		# Determine headers from first row
		first = chunk_rows[0]
		row_fields = [k for k in (first.get('row') or {}).keys() if not k.startswith('__')]
		headers = ['Target DocType', 'CSV Row Number', 'Error Message'] + row_fields
		writer.writerow(headers)
		for fr in chunk_rows:
			target_doctype = fr.get('target_doctype', '')
			row = fr.get('row') or {}
			row_num = row.get('__csv_row_number__', '')
			error_msg = fr.get('error', '')
			# Keep error message short
			if isinstance(error_msg, str):
				error_msg_short = error_msg[:1024]
			else:
				error_msg_short = str(error_msg)[:1024]
			row_values = [row.get(f) for f in row_fields]
			writer.writerow([target_doctype, row_num, error_msg_short] + row_values)
		return output.getvalue()

	# First-level chunking
	for idx, base_chunk in enumerate(chunk_list(all_failed_rows, CHUNK_SIZE), start=1):
		# Generate CSV content for this chunk
		csv_content = generate_csv_content_for_chunk(base_chunk)
		print(len(csv_content))
		content_bytes = csv_content.encode('utf-8')

		# If content still too large, split the base_chunk further
		if len(content_bytes) > MAX_SAFE_SIZE:
			# Split into halves until each piece is under MAX_SAFE_SIZE
			sub_chunks = list(chunk_list(base_chunk, max(1, CHUNK_SIZE // 2)))
			# Process sub-chunks individually
			for sidx, sub in enumerate(sub_chunks, start=1):
				sub_csv = generate_csv_content_for_chunk(sub)
				print(len(sub_csv))
				sub_bytes = sub_csv.encode('utf-8')
				if len(sub_bytes) > MAX_SAFE_SIZE:
					# As a last resort, split per-row to guarantee safety
					for ridx, single in enumerate(chunk_list(sub, 1), start=1):
						single_csv = generate_csv_content_for_chunk(single)
						print(len(single_csv))
						single_bytes = single_csv.encode('utf-8')
						if len(single_bytes) > MAX_SAFE_SIZE:
							# If a single row exceeds MAX_SAFE_SIZE (very unlikely), truncate row fields
							single_csv = single_csv[:MAX_SAFE_SIZE]
							single_bytes = single_csv.encode('utf-8', errors='ignore')
						file_doc = save_file(
							fname=f"multi_error_log_{doc.name}_part_{idx}_{sidx}_{ridx}.csv",
							content=single_bytes,
							dt="Lightning Upload",
							dn=doc.name,
							folder="Home/Attachments",
							is_private=1
						)
						file_urls.append(file_doc.file_url)
					else:
						file_doc = save_file(
							fname=f"multi_error_log_{doc.name}_part_{idx}_{sidx}_{ridx}.csv",
							content=single_bytes,
							dt="Lightning Upload",
							dn=doc.name,
							folder="Home/Attachments",
							is_private=1
						)
						file_urls.append(file_doc.file_url)
				else:
					file_doc = save_file(
						fname=f"multi_error_log_{doc.name}_part_{idx}_{sidx}.csv",
						content=sub_bytes,
						dt="Lightning Upload",
						dn=doc.name,
						folder="Home/Attachments",
						is_private=1
					)
					file_urls.append(file_doc.file_url)
		else:
			# Content is safe to save
			file_doc = save_file(
				fname=f"multi_error_log_{doc.name}_part_{idx}.csv",
				content=content_bytes,
				dt="Lightning Upload",
				dn=doc.name,
				folder="Home/Attachments",
				is_private=1
			)
			file_urls.append(file_doc.file_url)

	# Save reference to the first generated file in the primary error_file field
	if file_urls:
		frappe.db.set_value("Lightning Upload", doc.name, "error_file", file_urls[0])
		error_log_text = build_readable_error_log(all_failed_rows)
		doc.error_log = error_log_text
		frappe.db.set_value("Lightning Upload", doc.name, "error_log", error_log_text)
		return file_urls[0]
	return None

@frappe.whitelist()
def check_multi_file_duplicates(docname):
	"""Check duplicates for all enabled target DocTypes in a multiple import"""
	try:
		settings = frappe.get_single("Lightning Upload Settings")
		if not settings.get("enable_file_duplicate_check"):
			return {"status": "success", "has_duplicates": False, "targets": []}

		doc = frappe.get_doc("Lightning Upload", docname)
		if not doc.multiple_import:
			return {"status": "success", "has_duplicates": False, "targets": []}

		raw_rows = get_raw_sheet_rows(doc)
		if not raw_rows:
			return {"status": "success", "has_duplicates": False, "targets": []}

		has_duplicates = False
		targets_with_duplicates = []

		for target in doc.multi_import_targets:
			if not target.enabled:
				continue
			if not target.duplicate_check_field:
				continue

			csv_col_to_check = target.duplicate_check_field
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
				has_duplicates = True
				doctype_field = csv_col_to_check
				if target.field_mapping:
					mapping = json.loads(target.field_mapping)
					combined_key = f"{csv_col_to_check}::{target.target_doctype}"
					doctype_field = (
						mapping.get(combined_key)
						or mapping.get(csv_col_to_check)
						or csv_col_to_check
					)

				targets_with_duplicates.append({
					"target_doctype": target.target_doctype,
					"csv_column": csv_col_to_check,
					"field": doctype_field,
					"duplicate_values": dup_entries,
					"count": len(dup_entries)
				})

		return {
			"status": "success",
			"has_duplicates": has_duplicates,
			"targets": targets_with_duplicates
		}

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Multi Duplicate Check Error")
		return {"status": "error", "message": str(e)}

@frappe.whitelist()
def auto_map_multi_import(docname):
	"""Generate field mapping for every enabled target DocType in multi_import_targets"""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		if not doc.multiple_import:
			frappe.throw(_("Multiple Import is not enabled for this document."))

		if not doc.csv_file:
			frappe.throw(_("No CSV file attached."))

		headers = get_sheet_headers(doc)

		for target in doc.multi_import_targets:
			if not target.enabled:
				continue
			if not target.target_doctype:
				continue

			mapping_res = auto_map_headers_for_doctype(headers, target.target_doctype)
			# Rekey mapping to combined header::doctype format for unique identity
			combined_mapping = {
				f"{header}::{target.target_doctype}": field
				for header, field in mapping_res["mapping"].items()
			}
			if not target.field_mapping:
				target.field_mapping = json.dumps(combined_mapping)

		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)
		return {"status": "success", "message": _("Auto mapping completed for all enabled targets.")}

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Import Auto Map Multi Error")
		return {"status": "error", "message": str(e)}

@frappe.whitelist()
def start_multi_import(docname):
	"""API endpoint to start the multiple import process."""
	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		
		if doc.status != "Draft":
			frappe.throw(_("Import can only be started from Draft status"))
		
		if not doc.multiple_import:
			frappe.throw(_("Multiple Import must be enabled."))

		doc.validate_mappings()

		progress_key = f"lightning_import_{docname}"
		initial_progress = {
			"status": "Queued",
			"progress": 0,
			"title": "Import queued...",
			"progress_key": progress_key,
			"multiple_import": True,
			"successful_records": 0,
			"failed_records": 0
		}
		frappe.cache().set_value(progress_key, initial_progress)
		
		frappe.db.set_value("Lightning Upload", docname, "status", "Queued", update_modified=False)
		frappe.db.commit()
		
		frappe.publish_realtime(
			event='import_progress',
			message=initial_progress,
			user=frappe.session.user,
			after_commit=True
		)
		
		frappe.enqueue(
			"lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.process_multi_import_queue",
			docname=docname,
			now=False,
			queue="long",
			timeout=3600
		)
		
		return {
			"status": "success",
			"message": _("Multiple Import process started successfully"),
			"progress_key": progress_key
		}
		
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Multi Import Error")
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
def process_multi_import_queue(docname):
	"""Background job to execute multiple target DocType imports"""
	start_time = time.time()

	try:
		doc = frappe.get_doc("Lightning Upload", docname)
		if not doc.multiple_import:
			frappe.throw(_("Multiple Import is not enabled for this document."))

		frappe.db.set_value("Lightning Upload", docname, "status", "In Progress")
		frappe.db.commit()

		progress_key = f"lightning_import_{docname}"
		initial_progress = {
			"status": "In Progress",
			"progress": 0,
			"title": "Starting multiple import...",
			"progress_key": progress_key,
			"multiple_import": True
		}
		frappe.cache().set_value(progress_key, initial_progress)
		frappe.publish_realtime(
			event='import_progress',
			message=initial_progress,
			user=frappe.session.user,
			after_commit=True
		)

		raw_rows = get_raw_sheet_rows(doc)
		total_raw_rows = len(raw_rows)

		enabled_targets = [t for t in doc.multi_import_targets if t.enabled]
		enabled_targets.sort(key=lambda x: (x.execution_order if x.execution_order is not None and x.execution_order != "" else float('inf'), x.idx))
		
		total_targets = len(enabled_targets)
		if total_targets == 0:
			frappe.throw(_("No enabled targets to import."))

		# Initialize target statuses in database
		for target in enabled_targets:
			frappe.db.set_value("Lightning Multi Import Target", target.name, {
				"status": "Queued",
				"total_records": 0,
				"successful_records": 0,
				"failed_records": 0,
				"last_processed_row": 0
			}, update_modified=False)
		frappe.db.commit()

		all_failed_rows = []
		target_statuses = []
		batch_size = LightningUploadSettings.get_batch_size()

		overall_successful_records = 0
		overall_failed_records = 0

		for t_idx, target in enumerate(enabled_targets):
			target_doctype = target.target_doctype
			import_type = target.import_type
			field_mapping = target.field_mapping
			update_on_field = target.update_on_field

			frappe.db.set_value("Lightning Multi Import Target", target.name, "status", "In Progress", update_modified=False)
			frappe.db.commit()

			mapped_rows = map_rows_for_doctype(raw_rows, field_mapping)
			
			valid_indices_and_rows = []
			for idx, r in enumerate(mapped_rows, start=1):
				if any(val is not None and str(val).strip() != "" for k, val in r.items() if not k.startswith("__")):
					valid_indices_and_rows.append((idx, raw_rows[idx-1], r))

			total_target_rows = len(valid_indices_and_rows)
			
			frappe.db.set_value("Lightning Multi Import Target", target.name, "total_records", total_target_rows, update_modified=False)
			frappe.db.commit()

			successful_records = 0
			failed_records = 0

			if total_target_rows == 0:
				frappe.db.set_value("Lightning Multi Import Target", target.name, "status", "Completed", update_modified=False)
				frappe.db.commit()
				target_statuses.append("Completed")
				continue

			for i in range(0, total_target_rows, batch_size):
				batch_slice = valid_indices_and_rows[i:i + batch_size]
				batch_raw_rows = [item[1] for item in batch_slice]
				
				import_config = {
					"import_doctype": target_doctype,
					"import_type": import_type,
					"field_mapping": field_mapping,
					"update_on_field": update_on_field,
					"duplicate_check_field": target.duplicate_check_field
				}
				
				result = import_rows_for_doctype(import_config, batch_raw_rows)
				
				successful_records += result['success_count']
				failed_records += len(result['failed_rows'])
				
				for failed_row in result['failed_rows']:
					failed_row['target_doctype'] = target_doctype
					all_failed_rows.append(failed_row)

				frappe.db.set_value(
					"Lightning Multi Import Target",
					target.name,
					{
						"successful_records": successful_records,
						"failed_records": failed_records,
						"last_processed_row": min(total_target_rows, i + batch_size)
					},
					update_modified=False
				)
				frappe.db.commit()

				overall_progress = min(100, int(((t_idx + (min(total_target_rows, i + batch_size) / total_target_rows)) / total_targets) * 100))

				progress_data = {
					"status": "In Progress",
					"progress": overall_progress,
					"title": f"Importing {target_doctype}... ({overall_progress}%)",
					"progress_key": progress_key,
					"multiple_import": True,
					"current_target_doctype": target_doctype,
					"total_targets": total_targets,
					"current_target_index": t_idx + 1,
					"target_status": "In Progress",
					"target_successful_records": successful_records,
					"target_failed_records": failed_records,
					"target_total_records": total_target_rows
				}
				frappe.cache().set_value(progress_key, progress_data)
				frappe.publish_realtime(
					event='import_progress',
					message=progress_data,
					user=frappe.session.user,
					after_commit=True
				)

			if failed_records == total_target_rows:
				target_final_status = "Failed"
			elif failed_records > 0:
				target_final_status = "Partial Success"
			else:
				target_final_status = "Completed"

			frappe.db.set_value("Lightning Multi Import Target", target.name, "status", target_final_status, update_modified=False)
			frappe.db.commit()
			target_statuses.append(target_final_status)

			overall_successful_records += successful_records
			overall_failed_records += failed_records

		time_taken = time.time() - start_time
		time_str = f"{int(time_taken)}s" if time_taken < 60 else f"{time_taken/60:.1f}m"

		if all_failed_rows:
			generate_multi_error_file(doc, all_failed_rows)

		if overall_successful_records == 0 and overall_failed_records > 0:
			final_status = "Failed"
		elif all(status == "Completed" for status in target_statuses):
			final_status = "Completed"
		elif all(status == "Failed" for status in target_statuses):
			final_status = "Failed"
		else:
			final_status = "Partial Success"

		frappe.db.set_value(
			"Lightning Upload",
			docname,
			{
				"status": final_status,
				"import_time": time_str,
				"successful_records": overall_successful_records,
				"failed_records": overall_failed_records,
				"total_records": total_raw_rows
			}
		)
		frappe.db.commit()

		final_progress = {
			"status": final_status,
			"progress": 100,
			"title": f"Multiple Import {final_status.lower()}",
			"progress_key": progress_key,
			"multiple_import": True,
			"time_taken": time_str,
			"successful_records": overall_successful_records,
			"failed_records": overall_failed_records,
			"total_records": total_raw_rows
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
			"message": f"Multiple Import {final_status.lower()}. Successful: {overall_successful_records}, Failed: {overall_failed_records}, Time taken: {time_str}"
		}

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Lightning Multiple Import Error")
		try:
			frappe.db.set_value(
				"Lightning Upload",
				docname,
				{
					"status": "Failed",
					"error_log": str(e)
				}
			)
			frappe.db.commit()
			progress_key = f"lightning_import_{docname}"
			error_progress = {
				"status": "Failed",
				"progress": 0,
				"title": "Multiple Import failed",
				"progress_key": progress_key,
				"multiple_import": True,
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
def get_auto_mapping_for_doctype(docname, doctype):
	"""Get automatic field mapping for a specific target DocType"""
	print("Docname:", docname)
	print("Target DocType:", doctype)
	try:
		if not docname or docname.startswith("new-lightning-upload-"):
			frappe.throw(_("Document name must be a saved document in the database."))
			
		if not frappe.db.exists("Lightning Upload", docname):
			frappe.throw(_("Lightning Upload {0} not found").format(docname))

		doc = frappe.get_doc("Lightning Upload", docname)
		headers = get_sheet_headers(doc)
		print("Headers:", headers)

		res = auto_map_headers_for_doctype(headers, doctype)
		print("Generated Mapping:", res.get("mapping", {}))
		return res
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Error getting auto mapping")
		return {"mapping": {}, "unmapped_required": []}