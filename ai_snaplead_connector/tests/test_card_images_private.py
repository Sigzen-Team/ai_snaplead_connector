# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""patches.v1_0.make_card_images_private: pushed card images become private, nothing else does.

Files are written to disk and the patch commits per batch, so every fixture is committed and
removed in tearDown (file names start with ``vccard-``).
"""

import io
import json
import os
import random

import frappe
from frappe.tests import IntegrationTestCase
from PIL import Image

from ai_snaplead_connector.patches.v1_0 import make_card_images_private as patch_module

PREFIX = "vccard-"


def _png():
	"""Random pixels: no two fixtures share a content hash (frappe dedupes on it)."""
	image = Image.new("RGB", (32, 32))
	image.putdata([tuple(random.randrange(256) for _ in range(3)) for _ in range(32 * 32)])
	buffer = io.BytesIO()
	image.save(buffer, format="PNG")
	return buffer.getvalue()


def _path(file_url):
	folder = ("private", "files") if file_url.startswith("/private/") else ("public", "files")
	return frappe.get_site_path(*folder, file_url.split("/")[-1])


class TestMakeCardImagesPrivate(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.run_id = frappe.generate_hash(length=6)
		self.leads = []
		self.files = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, force=1, ignore_permissions=True)
		for name in self.leads:
			if frappe.db.exists("Lead", name):
				frappe.delete_doc("Lead", name, force=1, ignore_permissions=True)
		frappe.db.commit()
		for folder in (("public", "files"), ("private", "files")):
			base = frappe.get_site_path(*folder)
			for file_name in os.listdir(base):
				if file_name.startswith(PREFIX) and not frappe.db.exists(
					"File", {"file_url": ("like", f"%/{file_name}")}
				):
					os.remove(os.path.join(base, file_name))

	def _lead(self):
		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": f"Card {self.run_id}",
				"company_name": "VC Card Test Co",
				# The connector requires a contact detail on every Lead (overrides/lead.py).
				"email_id": f"card.{self.run_id}.{frappe.generate_hash(length=6)}@example.com",
			}
		).insert(ignore_permissions=True)
		self.leads.append(lead.name)
		return lead

	def _file(self, lead_name, field=None, is_private=0, file_name=None, content=None):
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name or f"{PREFIX}{frappe.generate_hash(length=8)}.png",
				"content": content or _png(),
				"is_private": is_private,
				"attached_to_doctype": "Lead",
				"attached_to_name": lead_name,
				"attached_to_field": field,
			}
		).insert(ignore_permissions=True)
		self.files.append(doc.name)
		return doc

	def test_a_pushed_card_image_becomes_private(self):
		lead = self._lead()
		card = self._file(lead.name, field="custom_front_card_image")
		old_url = card.file_url
		self.assertTrue(old_url.startswith("/files/"))
		modified = frappe.db.get_value("Lead", lead.name, "modified")
		frappe.db.commit()

		summary = patch_module.execute(names=[card.name])
		self.assertEqual((summary["moved"], summary["failed"]), (1, 0), summary)
		card.reload()
		self.assertEqual(card.is_private, 1)
		self.assertTrue(card.file_url.startswith("/private/files/"))
		self.assertFalse(os.path.exists(_path(old_url)))
		self.assertTrue(os.path.exists(_path(card.file_url)))
		self.assertEqual(frappe.db.get_value("Lead", lead.name, "modified"), modified)

		again = patch_module.execute(names=[card.name])
		self.assertEqual((again["moved"], again["repointed"], again["failed"]), (0, 0, 0), again)

	def test_a_clients_own_attachment_is_left_alone(self):
		lead = self._lead()
		own = self._file(lead.name)
		frappe.db.commit()

		summary = patch_module.execute(names=[own.name])
		self.assertEqual(summary["moved"], 0, summary)
		self.assertEqual(frappe.db.get_value("File", own.name, ["is_private", "file_url"]), (0, own.file_url))

	def test_a_name_clash_moves_the_card_to_a_free_name(self):
		lead = self._lead()
		other_lead = self._lead()
		card = self._file(lead.name, field="custom_back_card_image")
		file_name = card.file_url.split("/")[-1]
		other_content = _png()
		other = self._file(other_lead.name, is_private=1, file_name=file_name, content=other_content)
		self.assertEqual(other.file_url, f"/private/files/{file_name}")
		frappe.db.commit()

		summary = patch_module.execute(names=[card.name])
		self.assertEqual((summary["moved"], summary["failed"]), (1, 0), summary)
		card.reload()
		self.assertTrue(card.file_url.startswith("/private/files/"))
		self.assertNotEqual(card.file_url, other.file_url)
		with open(_path(other.file_url), "rb") as handle:
			self.assertEqual(handle.read(), other_content)

	def test_lost_bytes_never_adopt_another_records_file(self):
		lead = self._lead()
		other_lead = self._lead()
		card = self._file(lead.name, field="custom_front_card_image")
		old_url = card.file_url
		os.remove(_path(old_url))
		other = self._file(other_lead.name, is_private=1, file_name=old_url.split("/")[-1])
		frappe.db.commit()

		summary = patch_module.execute(names=[card.name])
		self.assertEqual((summary["repointed"], summary["failed"]), (0, 1), summary)
		self.assertEqual(frappe.db.get_value("File", card.name, ["is_private", "file_url"]), (0, old_url))
		self.assertTrue(os.path.exists(_path(other.file_url)))

	def test_card_urls_are_read_from_the_legacy_archive(self):
		legacy = json.dumps({"fields": {"custom_front_card_image": "/files/a.png", "other": "/files/b.png"}})
		notes = json.dumps({"custom_back_card_image": {"outcome": "migrated", "file_url": "/files/c.png"}})
		self.assertEqual(patch_module._card_urls(legacy, notes, "not json", None), {"/files/a.png", "/files/c.png"})

	def test_an_archived_card_url_is_moved_and_the_archive_repointed(self):
		if not frappe.db.table_exists(patch_module.ARCHIVE):
			self.skipTest("Connector Legacy Field Archive is not installed on this site")
		lead = self._lead()
		card = self._file(lead.name)  # the legacy migration cleared attached_to_field
		archive = frappe.get_doc(
			{
				"doctype": patch_module.ARCHIVE,
				"reference_doctype": "Lead",
				"reference_name": lead.name,
				"legacy_values": json.dumps({"fields": {"custom_front_card_image": card.file_url}}),
			}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: (frappe.db.delete(patch_module.ARCHIVE, {"name": archive.name}), frappe.db.commit()))
		frappe.db.commit()

		summary = patch_module.execute(names=[card.name])
		self.assertEqual(summary["moved"], 1, summary)
		card.reload()
		self.assertTrue(card.file_url.startswith("/private/files/"))
		stored = json.loads(frappe.db.get_value(patch_module.ARCHIVE, archive.name, "legacy_values"))
		self.assertEqual(stored["fields"]["custom_front_card_image"], card.file_url)
