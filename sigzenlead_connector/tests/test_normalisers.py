# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
"""The drift guard on the VENDORED normalisers.

sigzenlead_connector cannot import the central app (sigzenlead, formerly sigzen_lead): it is installed on a client's ERPNext where that app
does not exist. So the phone normaliser and the India state/pincode derivation are copied into
sigzenlead_connector/utils/. Copies drift, and a drifted normaliser is invisible -- both sides keep
working, they just stop agreeing about which two phone numbers are the same number, and the
central site starts creating a second Lead for a contact the client already has.

Every case here is read from tests/fixtures/normaliser_cases.json. That file is meant to be
SHARED: the central app needs the twin test that reads the same file. Until it has one, this test
pins only this side -- see the note in the run report.
"""

import json
import os
import unittest

from frappe.utils import validate_email_address

from sigzenlead_connector.legacy.migrate import storable_emails, unstorable_email_remainder
from sigzenlead_connector.utils.india_state import derive_indian_state, normalize_pincode
from sigzenlead_connector.utils.phone import (
	dedupe_emails,
	dedupe_phones,
	normalize_email,
	normalize_mobile_for_auth,
	normalize_mobile_number,
	phone_match_key,
	storable_phones,
	unstorable_remainder,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "normaliser_cases.json")


def load_cases():
	with open(FIXTURE, encoding="utf-8") as handle:
		return json.load(handle)


class TestVendoredNormalisers(unittest.TestCase):
	"""A plain unittest.TestCase: these functions are pure and need no site."""

	@classmethod
	def setUpClass(cls):
		cls.cases = load_cases()

	def test_phone_stored_and_match_forms(self):
		for case in self.cases["phones"]:
			with self.subTest(phone=case["input"]):
				self.assertEqual(normalize_mobile_for_auth(case["input"]), case["stored"])
				self.assertEqual(normalize_mobile_number(case["input"]), case["key"])

	def test_phone_lists_collapse_respellings_of_one_number(self):
		for case in self.cases["phone_lists"]:
			with self.subTest(phones=case["input"]):
				self.assertEqual(dedupe_phones(case["input"]), case["deduped"])

	def test_country_code_survives_the_stored_form(self):
		"""The match key is India-shaped; the STORED value must not be, or every
		international number silently loses its country code on the way in."""
		self.assertEqual(dedupe_phones(["+1 415 555 0123"]), ["+14155550123"])

	def test_a_country_code_is_part_of_the_match_key(self):
		"""sigzenlead_connector's own key, not a vendored one: the migration and the verification gate
		both use it, and the vendored key made "+1 212 555 0100" and "+44 20 2125 550100" the
		same number -- so one of them was never written and the gate could not see the gap."""
		for case in self.cases["phone_match_keys"]:
			with self.subTest(a=case["a"], b=case["b"]):
				same = phone_match_key(case["a"]) == phone_match_key(case["b"])
				self.assertEqual(same, case["same"], (phone_match_key(case["a"]), phone_match_key(case["b"])))

	def test_a_card_spelling_yields_storable_numbers_and_a_remainder(self):
		"""Frappe refuses these three spellings outright. Splitting them is what stops one
		annotated number aborting a whole Lead's migration."""
		for case in self.cases["storable_phones"]:
			with self.subTest(phone=case["input"]):
				self.assertEqual(storable_phones(case["input"]), case["stored"])
				self.assertEqual(unstorable_remainder(case["input"]), case["remainder"])

	def test_a_number_too_long_to_store_is_never_replaced_by_an_annotation(self):
		"""The migration may lose a number. It may not INVENT one.

		"+91 (080) 2555-1234-5678 ext 9" holds one 22-character run, which frappe's
		20-character pattern refuses, and one two-character run, "ext 9". The unstorable run
		was filtered out first and the leftover then won a max() fallback, so "9" was written
		to the client's Contact as their phone number -- a value that was never on the card,
		with the verification gate asking this same function and agreeing.
		"""
		self.assertEqual(storable_phones("+91 (080) 2555-1234-5678 ext 9"), [])
		self.assertEqual(
			unstorable_remainder("+91 (080) 2555-1234-5678 ext 9"),
			"+91 (080) 2555-1234-5678 ext 9",
		)

		# A bracketed trunk code is not a second number: this must not be salvaged either.
		self.assertEqual(storable_phones("+91 (080) 2555-1234-5678"), [])

		# ...while a value whose every run is short really is one short number.
		self.assertEqual(storable_phones("22"), ["22"])

	def test_two_numbers_written_as_one_run_are_both_recovered(self):
		""" "7011477004(7011477005)" is 22 characters, so it was refused whole and BOTH real
		numbers reached only a note. Splitting at a bracket that opens a full number keeps
		them, and the remainder is then empty because nothing was left behind."""
		self.assertEqual(storable_phones("7011477004(7011477005)"), ["7011477004", "7011477005"])
		self.assertEqual(unstorable_remainder("7011477004(7011477005)"), "")

	def test_a_legacy_email_field_can_hold_two_addresses(self):
		"""frappe.utils.validate_email_address says yes to "a@b.com, c@d.com" -- it returns
		the whole string -- and Contact Email stores it without complaint. That is how a
		migration wrote ONE row holding two addresses, neither of them findable by a lookup
		for itself, while the gate asked the same question and passed."""
		from frappe.utils import validate_email_address

		# The property that made the bug: frappe itself is not a usable answer here.
		self.assertTrue(validate_email_address("ravi@a.com, ravi.k@b.com"))
		self.assertTrue(validate_email_address("Ravi Kumar <ravi2@a.com>"))

		for case in self.cases["storable_emails"]:
			with self.subTest(email=case["input"]):
				self.assertEqual(storable_emails(case["input"]), case["stored"])
				self.assertEqual(unstorable_email_remainder(case["input"]), case["remainder"])

	def test_every_stored_address_is_findable_on_its_own(self):
		"""The point of splitting: each stored form must be one whole address and nothing
		else, or a later lookup for it matches no row."""
		for case in self.cases["storable_emails"]:
			for stored in storable_emails(case["input"]):
				with self.subTest(email=case["input"], stored=stored):
					self.assertNotIn(",", stored)
					self.assertNotIn(" ", stored)
					self.assertNotIn("<", stored)
					self.assertEqual(validate_email_address(stored), stored)

	def test_every_storable_form_is_one_frappe_accepts(self):
		"""The contract with frappe.utils.PHONE_NUMBER_PATTERN, checked against frappe itself."""
		from frappe.utils import PHONE_NUMBER_PATTERN

		for case in self.cases["storable_phones"] + self.cases["phones"]:
			for stored in storable_phones(case["input"]):
				with self.subTest(phone=case["input"], stored=stored):
					self.assertTrue(PHONE_NUMBER_PATTERN.match(stored), stored)

	def test_emails(self):
		for case in self.cases["emails"]:
			with self.subTest(email=case["input"]):
				self.assertEqual(normalize_email(case["input"]), case["normalized"])

		for case in self.cases["email_lists"]:
			with self.subTest(emails=case["input"]):
				self.assertEqual(dedupe_emails(case["input"]), case["deduped"])

	def test_pincode(self):
		for case in self.cases["pincodes"]:
			with self.subTest(pincode=case["pincode"], text=case["address_text"]):
				self.assertEqual(normalize_pincode(case["pincode"], case["address_text"]), case["expected"])

	def test_state_derivation(self):
		for case in self.cases["states"]:
			with self.subTest(text=case["address_text"], pin=case["pincode"]):
				state, source = derive_indian_state(
					case["address_text"],
					city=case["city"],
					state=case["state"],
					pincode=case["pincode"],
				)
				self.assertEqual(state, case["expected_state"])
				self.assertEqual(source, case["expected_source"])

	def test_ambiguous_clues_refuse_to_guess(self):
		"""A wrong state on a GST address is worse than no address: two cities in two
		states in one text must derive nothing at all."""
		state, source = derive_indian_state("Mumbai office, near Bengaluru road")
		self.assertIsNone(state)
		self.assertIsNone(source)

	def test_this_app_does_not_import_the_central_app(self):
		"""The whole point of vendoring. A stray import would work in this bench -- where
		both apps happen to be on the path -- and fail on every client site."""
		import pathlib
		import re

		# The central app's package, under its current and its former names. The boundary matters:
		# `sigzenlead_connector` is this app and starts with the same letters.
		central = re.compile(r"^(import|from)\s+(sigzenlead|ai_snaplead|sigzen_lead)(\.|\s|$)")
		app_root = pathlib.Path(__file__).resolve().parents[1]
		offenders = []
		for path in app_root.rglob("*.py"):
			if "__pycache__" in path.parts:
				continue
			text = path.read_text(encoding="utf-8", errors="replace")
			for line in text.splitlines():
				stripped = line.strip()
				if central.match(stripped):
					offenders.append(f"{path}: {stripped}")

		self.assertEqual(offenders, [], "sigzenlead_connector must never import the central app")
