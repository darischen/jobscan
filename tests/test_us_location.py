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
