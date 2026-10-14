"""The US filter on locations no adapter's own tests cover. Offline."""
from __future__ import annotations

import pytest

from jobscan.runner import is_us_location


@pytest.mark.parametrize("loc", [
    "福岡市, 福岡県",        # Fukuoka: Tesla, 2026-10-13
    "港区, 東京都",
    "台南市",
    "서울특별시",
    "Москва",
    "Remote - 東京都",
])
def test_a_place_written_in_another_script_is_still_a_place(loc: str):
    """The no-place test looked for A-Z only, so a location with no Latin
    letters read as a non-answer like "Remote" and passed: 201 of Tesla's
    Japanese and Taiwanese postings."""
    assert is_us_location(loc) is False


@pytest.mark.parametrize("loc", ["", "Remote", "Fully Remote", "Location Negotiable",
                                 "Multiple Locations", "Palo Alto, California",
                                 "Akron, Ohio"])
def test_non_answers_and_us_places_still_pass(loc: str):
    assert is_us_location(loc) is True


@pytest.mark.parametrize("loc", [
    "Chennai, TN, India",          # American Express: TN read as Tennessee
    "Bogotá D.C., Colombia",       # Uber: D.C. read as Washington
    "Dublin OR London",            # Stripe: OR read as Oregon
    "Milan OR Madrid ",
])
def test_a_foreign_location_with_a_state_lookalike_is_not_us(loc: str):
    assert is_us_location(loc) is False


@pytest.mark.parametrize("loc", [
    # US places whose names contain a country, which is why a country
    # anywhere in the string cannot simply win.
    "Lake Wales, FL 2240",
    "Panama City, FL, us",
    "Lebanon, TN 1692",
    "West Jordan, UT",
    "LA CANADA FLINTRIDGE, CA",
    "Albany, OR (Millersburg) 3057",
    "Portland, OR Metro",
    # A multi-location req a US applicant can take.
    "Dublin, Ireland; Austin, TX",
    "US, Canada",                              # Stripe: US on its own counts
    "Chennai, TN, India; Nashville, TN, United States",
])
def test_us_places_that_look_foreign_still_pass(loc: str):
    assert is_us_location(loc) is True
