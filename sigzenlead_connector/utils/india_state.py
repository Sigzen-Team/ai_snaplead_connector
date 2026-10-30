# Copyright (c) 2026, support@sigzen.com and contributors
# For license information, please see license.txt
#
# VENDORED COPY -- DO NOT IMPORT sigzenlead FROM THIS APP.
#
# Source of truth: sigzenlead/sigzenlead/connector/india_state.py (copied verbatim below,
# only this header added). sigzenlead_connector is installed on a CLIENT's ERPNext, where sigzenlead
# does not exist and never will; an import of it is the coupling this release removes.
#
# The copy is the price of that independence, and copies drift. The guard against drift is
# sigzenlead_connector/tests/fixtures/normaliser_cases.json: a shared fixture asserting input ->
# output for both normalisers. sigzenlead must grow the twin test reading the SAME fixture
# file, so a change to either side that is not made to the other fails a test in that repo.
# Until that twin test exists, this file is only pinned on the sigzenlead_connector side.
"""Derive an Indian state for an ERPNext Address (ERPNEXT_SYNC_SPEC 6.3). Pure functions, no I/O.

india_compliance refuses an Indian Address without a state. Scanned cards rarely carry the
state as its own field, but the address text, the city or the PIN code usually decide it.
When the clues disagree or are ambiguous the answer is None: a wrong state on a GST address is
worse than no Address at all.
"""

import re

CANONICAL_STATES = (
	"Andaman and Nicobar Islands",
	"Andhra Pradesh",
	"Arunachal Pradesh",
	"Assam",
	"Bihar",
	"Chandigarh",
	"Chhattisgarh",
	"Dadra and Nagar Haveli and Daman and Diu",
	"Delhi",
	"Goa",
	"Gujarat",
	"Haryana",
	"Himachal Pradesh",
	"Jammu and Kashmir",
	"Jharkhand",
	"Karnataka",
	"Kerala",
	"Ladakh",
	"Lakshadweep Islands",
	"Madhya Pradesh",
	"Maharashtra",
	"Manipur",
	"Meghalaya",
	"Mizoram",
	"Nagaland",
	"Odisha",
	"Puducherry",
	"Punjab",
	"Rajasthan",
	"Sikkim",
	"Tamil Nadu",
	"Telangana",
	"Tripura",
	"Uttar Pradesh",
	"Uttarakhand",
	"West Bengal",
)

STATE_ALIASES = {
	"orissa": "Odisha",
	"pondicherry": "Puducherry",
	"new delhi": "Delhi",
	"nct of delhi": "Delhi",
	"uttaranchal": "Uttarakhand",
	"j&k": "Jammu and Kashmir",
	"jammu & kashmir": "Jammu and Kashmir",
	"daman": "Dadra and Nagar Haveli and Daman and Diu",
	"diu": "Dadra and Nagar Haveli and Daman and Diu",
	"silvassa": "Dadra and Nagar Haveli and Daman and Diu",
	"lakshadweep": "Lakshadweep Islands",
	"andaman and nicobar": "Andaman and Nicobar Islands",
}

CITY_ALIASES = {
	"mumbai": "Maharashtra",
	"pune": "Maharashtra",
	"nagpur": "Maharashtra",
	"thane": "Maharashtra",
	"ahmedabad": "Gujarat",
	"surat": "Gujarat",
	"vadodara": "Gujarat",
	"rajkot": "Gujarat",
	"gandhinagar": "Gujarat",
	"bengaluru": "Karnataka",
	"bangalore": "Karnataka",
	"mysuru": "Karnataka",
	"chennai": "Tamil Nadu",
	"coimbatore": "Tamil Nadu",
	"madurai": "Tamil Nadu",
	"kolkata": "West Bengal",
	"hyderabad": "Telangana",
	"secunderabad": "Telangana",
	"jaipur": "Rajasthan",
	"lucknow": "Uttar Pradesh",
	"noida": "Uttar Pradesh",
	"kanpur": "Uttar Pradesh",
	"gurugram": "Haryana",
	"gurgaon": "Haryana",
	"kochi": "Kerala",
	"thiruvananthapuram": "Kerala",
	"bhopal": "Madhya Pradesh",
	"indore": "Madhya Pradesh",
	"chandigarh": "Chandigarh",
}

AMBIGUOUS = "__ambiguous__"


def _prefix_map():
	table = {}

	def put(state, *prefixes):
		for prefix in prefixes:
			table[str(prefix)] = state

	put("Delhi", 11)
	put("Haryana", 12, 13)
	put("Punjab", 14, 15)
	put("Himachal Pradesh", 17)
	put("Jammu and Kashmir", 18, 19)
	put("Uttar Pradesh", *range(20, 29))
	put("Rajasthan", *range(30, 35))
	put("Gujarat", *range(36, 40))
	put("Maharashtra", *range(40, 45))
	put("Madhya Pradesh", *range(45, 49))
	put("Chhattisgarh", 49)
	put("Telangana", 50)
	put("Andhra Pradesh", 51, 52, 53)
	put("Karnataka", *range(56, 60))
	put("Tamil Nadu", *range(60, 65))
	put("Kerala", 67, 68, 69)
	put("West Bengal", *range(70, 75))
	put("Odisha", 75, 76, 77)
	put("Assam", 78)
	put("Bihar", 80, 84, 85)
	put("Jharkhand", 82, 83)
	return table


PIN_PREFIX_2 = _prefix_map()

PIN_PREFIX_3 = {
	"160": AMBIGUOUS,
	"194": "Ladakh",
	"244": AMBIGUOUS,
	"246": "Uttarakhand",
	"247": AMBIGUOUS,
	"248": "Uttarakhand",
	"249": "Uttarakhand",
	"262": AMBIGUOUS,
	"263": "Uttarakhand",
	"362": AMBIGUOUS,
	"396": AMBIGUOUS,
	"403": "Goa",
	"533": AMBIGUOUS,
	"605": AMBIGUOUS,
	"609": AMBIGUOUS,
	"673": AMBIGUOUS,
	"682": AMBIGUOUS,
	"737": "Sikkim",
	"744": "Andaman and Nicobar Islands",
	"790": "Arunachal Pradesh",
	"791": "Arunachal Pradesh",
	"792": "Arunachal Pradesh",
	"793": "Meghalaya",
	"794": "Meghalaya",
	"795": "Manipur",
	"796": "Mizoram",
	"797": "Nagaland",
	"798": "Nagaland",
	"799": "Tripura",
	"811": "Bihar",
	"812": "Bihar",
	"813": "Bihar",
	"814": "Jharkhand",
	"815": "Jharkhand",
	"816": "Jharkhand",
}

_CANONICAL_BY_LOWER = {name.lower(): name for name in CANONICAL_STATES}
_PIN_IN_TEXT = re.compile(r"(?<!\d)([1-9]\d{2})\s?(\d{3})(?!\d)")


def canonical_state(value):
	"""Canonical state for an exact name or alias (case-insensitive), else None."""
	key = re.sub(r"\s+", " ", (value or "").strip()).lower()
	if not key:
		return None
	return _CANONICAL_BY_LOWER.get(key) or STATE_ALIASES.get(key)


def _text_terms():
	terms = {}
	terms.update({name.lower(): name for name in CANONICAL_STATES})
	terms.update(STATE_ALIASES)
	terms.update(CITY_ALIASES)
	# Longest first, so "west bengal" is tried before a shorter term could claim part of it.
	return sorted(terms.items(), key=lambda item: -len(item[0]))


_TEXT_TERMS = _text_terms()


def state_from_text(text):
	"""The single state named by the text (names, aliases, big cities), else None."""
	haystack = f" {(text or '').lower()} "
	if not haystack.strip():
		return None
	found = set()
	for term, state in _TEXT_TERMS:
		pattern = r"(?<![a-z0-9&])" + re.escape(term) + r"(?![a-z0-9&])"
		if re.search(pattern, haystack):
			found.add(state)
			# Blank the match so "andhra pradesh" cannot also count as "pradesh"-like shorter hits.
			haystack = re.sub(pattern, " ", haystack)
	if len(found) == 1:
		return found.pop()
	return None


def normalize_pincode(pincode, address_text=""):
	"""Six-digit PIN from the pincode field, else the LAST PIN-shaped number in the address."""
	digits = re.sub(r"\D", "", str(pincode or ""))
	if len(digits) == 6 and digits[0] != "0":
		return digits
	matches = _PIN_IN_TEXT.findall(address_text or "")
	if matches:
		first, rest = matches[-1]
		return f"{first}{rest}"
	return None


def state_from_pincode(pin):
	if not pin or len(pin) != 6:
		return None
	three = PIN_PREFIX_3.get(pin[:3])
	if three:
		return None if three == AMBIGUOUS else three
	two = pin[:2]
	if two == "16" or two.startswith("9"):
		return None
	return PIN_PREFIX_2.get(two)


def derive_indian_state(address_text, city=None, state=None, pincode=None):
	"""(state_or_None, source) where source is given | text | pincode | None."""
	given = canonical_state(state)
	if given:
		return given, "given"

	from_text = state_from_text(" ".join(part for part in (address_text or "", city or "") if part))
	pin = normalize_pincode(pincode, address_text)
	from_pin = state_from_pincode(pin)

	if from_text and from_pin and from_text != from_pin:
		return None, None
	if from_text:
		return from_text, "text"
	if from_pin:
		return from_pin, "pincode"
	return None, None
