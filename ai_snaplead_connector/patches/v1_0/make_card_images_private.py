# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""Make the scanned-card images already on this site private files (DPDP Rule 6).

A card photo is personal data. The central site's v1 push uploaded every card through
``upload_file`` with ``is_private = 0``, so each one sits in ``public/files`` and is served at
``/files/<name>`` to anyone who has the URL. The central site now uploads with
``is_private = 1`` and this app's sync endpoint defaults to private, but that covers new
uploads only: re-pushing a Lead does not help (frappe dedupes on ``is_private``, so a private
copy is added and the public one stays attached).

WHICH FILES

Only rows this app or the central push can be shown to own, never a client's own attachment:

* File rows attached to a Lead with ``attached_to_field`` ``custom_front_card_image`` /
  ``custom_back_card_image`` (the v1 upload claimed the field);
* File rows whose URL a Lead's ``custom_*_card_image`` column still holds (before the legacy
  drop);
* File rows whose URL the Connector Legacy Field Archive recorded for those fields (after the
  legacy migration cleared ``attached_to_field``).

HOW

The bytes move to ``private/files`` (under a free name when one of the same name is already
there -- it may be another record's file) and every File row, Lead card column and archive
entry naming the old URL is re-pointed. ``File.save(is_private=1)`` is not used: it writes the
Lead with ``set_value`` (bumping ``modified``) and throws ``FileExistsError`` on a name clash,
leaving the card public. Lead columns are re-pointed by primary key with no ``modified`` change.

Re-runnable: only ``is_private = 0`` rows are selected; each file runs in a savepoint and a
failure is logged and left public. A generic ERPNext that receives cards WITHOUT this app
installed cannot be reached from here; its administrator has to make those files private.
"""

import json
import os
import shutil

import frappe
from frappe.core.doctype.file.utils import get_content_hash

LEAD = "Lead"
CARD_FIELDS = ("custom_front_card_image", "custom_back_card_image")
ARCHIVE = "Connector Legacy Field Archive"
PUBLIC_PREFIX = "/files/"
PRIVATE_PREFIX = "/private/files/"
BATCH_SIZE = 200


def execute(names=None):
	"""names scopes the run to specific File rows (tests); the migration runs site-wide."""
	if not frappe.db.exists("DocType", LEAD):
		return {}

	summary = {"moved": 0, "repointed": 0, "failed": 0}
	failures = []
	lead_refs = _lead_refs()
	archive_refs = _archive_refs()
	work = _work_list(lead_refs, archive_refs, names)

	for start in range(0, len(work), BATCH_SIZE):
		for name in work[start : start + BATCH_SIZE]:
			frappe.db.savepoint("connector_card_private")
			try:
				outcome = _make_private(name, lead_refs, archive_refs)
			except Exception as exc:
				frappe.db.rollback(save_point="connector_card_private")
				summary["failed"] += 1
				failures.append(f"{name}: {exc!r}")
				continue
			if outcome:
				summary[outcome] += 1
		frappe.db.commit()  # nosemgrep - one batch of a resumable file migration

	if failures:
		frappe.log_error(
			title="make_card_images_private: card images left public",
			message="\n".join(failures[:500]),
		)
		frappe.db.commit()  # nosemgrep - keep the report even if the caller rolls back
	return summary


def _lead_refs():
	"""{public URL: [(lead, column)]} from the card columns, while they still exist."""
	refs = {}
	for field in CARD_FIELDS:
		if not frappe.db.has_column(LEAD, field):
			continue
		for (
			lead_name,
			url,
		) in frappe.db.sql(  # nosemgrep - table/column names come from this module's fixed lists, never from input; values are bound parameters
			f"SELECT name, `{field}` FROM `tabLead` WHERE `{field}` LIKE %(prefix)s",
			{"prefix": f"{PUBLIC_PREFIX}%"},
		):
			refs.setdefault(url, []).append((lead_name, field))
	return refs


def _archive_refs():
	"""{public URL: [archive row]} for the card URLs the legacy archive recorded."""
	refs = {}
	if not frappe.db.table_exists(ARCHIVE):
		return refs
	for row in frappe.get_all(
		ARCHIVE,
		filters={"reference_doctype": LEAD},
		fields=["name", "legacy_values", "migration_notes"],
	):
		for url in _card_urls(row.legacy_values, row.migration_notes):
			refs.setdefault(url, []).append(row.name)
	return refs


def _card_urls(*documents):
	urls = set()
	for document in documents:
		try:
			payload = json.loads(document or "{}")
		except (TypeError, ValueError):
			continue
		if not isinstance(payload, dict):
			continue
		# legacy_values keeps them under "fields"; migration_notes as {field: {"file_url": ...}}.
		sources = [payload, payload.get("fields") if isinstance(payload.get("fields"), dict) else {}]
		for source in sources:
			for field in CARD_FIELDS:
				value = source.get(field)
				if isinstance(value, dict):
					value = value.get("file_url")
				if isinstance(value, str) and value.startswith(PUBLIC_PREFIX):
					urls.add(value)
	return urls


def _placeholders(values, items):
	keys = []
	for index, item in enumerate(items):
		values[f"i{index}"] = item
		keys.append(f"%(i{index})s")
	return ", ".join(keys)


def _public_rows(condition, extra=None, items=None):
	values = {"prefix": f"{PUBLIC_PREFIX}%", **(extra or {})}
	if items is not None:
		condition = condition.format(items=_placeholders(values, items))
	return frappe.db.sql(  # nosemgrep - table/column names come from this module's fixed lists, never from input; values are bound parameters
		f"""
		SELECT name, file_url, attached_to_doctype, attached_to_field FROM `tabFile`
		WHERE is_private = 0 AND is_folder = 0 AND file_url LIKE %(prefix)s AND {condition}
		""",
		values,
		as_dict=True,
	)


def _chunks(items, size=200):
	items = list(items)
	for start in range(0, len(items), size):
		yield items[start : start + size]


def _work_list(lead_refs, archive_refs, names=None):
	rows = []
	if names is not None:
		for chunk in _chunks(names):
			rows += _public_rows("name IN ({items})", items=chunk)
	else:
		rows += _public_rows("attached_to_doctype = %(lead)s", {"lead": LEAD})
		for chunk in _chunks(set(lead_refs) | set(archive_refs)):
			rows += _public_rows("file_url IN ({items})", items=chunk)

	def is_card(row):
		if row.file_url in lead_refs or row.file_url in archive_refs:
			return True
		return row.attached_to_doctype == LEAD and row.attached_to_field in CARD_FIELDS

	return sorted({row.name for row in rows if is_card(row)})


def _make_private(name, lead_refs, archive_refs):
	row = frappe.db.get_value("File", name, ["file_url", "is_private", "content_hash"], as_dict=True)
	if not row or row.is_private or not (row.file_url or "").startswith(PUBLIC_PREFIX):
		return None

	old_url = row.file_url
	file_name = old_url[len(PUBLIC_PREFIX) :]
	if not file_name or "/" in file_name:
		raise ValueError(f"{old_url} is not a file in public/files")
	public_path = frappe.get_site_path("public", "files", file_name)

	moved = None
	if os.path.isfile(public_path):
		target = _free_private_name(file_name, row.content_hash)
		target_path = frappe.get_site_path("private", "files", target)
		shutil.move(public_path, target_path)
		moved = (target_path, public_path)
		outcome = "moved"
	else:
		target = _unfinished_move(file_name, row.content_hash)
		if not target:
			raise FileNotFoundError(f"{old_url} is not on disk")
		outcome = "repointed"

	new_url = f"{PRIVATE_PREFIX}{target}"
	try:
		frappe.db.sql(
			"UPDATE `tabFile` SET file_url = %(new)s, is_private = 1 WHERE file_url = %(old)s",
			{"new": new_url, "old": old_url},
		)
		for lead_name, field in lead_refs.get(old_url, ()):
			frappe.db.sql(  # nosemgrep - table/column names come from this module's fixed lists, never from input; values are bound parameters
				f"UPDATE `tabLead` SET `{field}` = %(new)s WHERE name = %(lead)s AND `{field}` = %(old)s",
				{"new": new_url, "lead": lead_name, "old": old_url},
			)
		# The archive is the legacy restore's source: it must not restore a dead public URL.
		for archive_name in archive_refs.get(old_url, ()):
			values = frappe.db.get_value(
				ARCHIVE, archive_name, ["legacy_values", "migration_notes"], as_dict=True
			)
			updates = {
				key: value.replace(json.dumps(old_url), json.dumps(new_url))
				for key, value in values.items()
				if value and json.dumps(old_url) in value
			}
			if updates:
				frappe.db.set_value(ARCHIVE, archive_name, updates, update_modified=False)
	except Exception:
		if moved:
			shutil.move(*moved)
		raise
	return outcome


def _private_name_taken(file_name):
	return os.path.exists(frappe.get_site_path("private", "files", file_name)) or frappe.db.exists(
		"File", {"file_url": f"{PRIVATE_PREFIX}{file_name}"}
	)


def _hash_name(file_name, content_hash):
	stem, ext = os.path.splitext(file_name)
	return f"{stem}{content_hash[-6:]}{ext}" if content_hash else None


def _free_private_name(file_name, content_hash):
	stem, ext = os.path.splitext(file_name)
	candidates = [file_name, _hash_name(file_name, content_hash)]
	candidates += [f"{stem}{frappe.generate_hash(length=6)}{ext}" for _ in range(5)]
	for candidate in candidates:
		if candidate and not _private_name_taken(candidate):
			return candidate
	raise FileExistsError(f"no free private name for {file_name}")


def _unfinished_move(file_name, content_hash):
	"""A private file an interrupted run moved these bytes to -- only if the bytes match and
	no File row holding other bytes owns its URL (a same-named file may be another record's)."""
	if not content_hash:
		return None
	for candidate in (file_name, _hash_name(file_name, content_hash)):
		path = frappe.get_site_path("private", "files", candidate)
		if not os.path.isfile(path):
			continue
		# Safe to open: path = the site's private/files folder + a name generated here from a File row, not from request input
		with open(path, "rb") as handle:  # nosemgrep
			if get_content_hash(handle.read()) != content_hash:
				continue
		other_owner = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabFile` WHERE file_url = %(url)s AND IFNULL(content_hash, '') != %(hash)s",
			{"url": f"{PRIVATE_PREFIX}{candidate}", "hash": content_hash},
		)[0][0]
		if not other_owner:
			return candidate
	return None
