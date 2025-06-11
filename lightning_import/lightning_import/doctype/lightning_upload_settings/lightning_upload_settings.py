# Copyright (c) 2025, Tridz Technologies Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class LightningUploadSettings(Document):
	def validate(self):
		"""Validate settings before save"""
		if not self.batch_size:
			self.batch_size = 1000  # Default batch size
		else:
			try:
				batch_size = int(self.batch_size)
				if batch_size <= 0:
					frappe.throw(_("Batch size must be a positive number"))
				self.batch_size = batch_size
			except ValueError:
				frappe.throw(_("Batch size must be a valid number"))

	@staticmethod
	def get_batch_size():
		"""Get the configured batch size"""
		settings = frappe.get_single("Lightning Upload Settings")
		return int(settings.batch_size or 1000)

	@staticmethod
	def get_validate_from_hook():
		"""Get whether validation from hooks is enabled"""
		settings = frappe.get_single("Lightning Upload Settings")
		return bool(settings.validate_from_hook)
