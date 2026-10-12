"""eightfold PCSX search mode, offline against a fake board.

Microsoft, PayPal and Qualcomm refuse /api/apply/v2/jobs with 403 "Not
authorized for PCSX" but serve /api/pcsx/search to a session that has
loaded /careers. The fake below enforces that cookie, serves 10 rows a page
whatever is asked, and can swap rows across page boundaries between
requests the way the live board reorders.
"""
from __future__ import annotations

import httpx
import pytest

from jobscan import adapters, registry
from jobscan.adapters import PCSX_PAGE, PCSX_STEP, eightfold
from jobscan.runner import is_us_location

HOST = "apply.careers.microsoft.com"
ROW = {"company": "Microsoft", "ats": "eightfold", "host": HOST, "site": "pcsx",
       "query": "microsoft.com", "token": ""}


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(adapters, "PCSX_PAUSE", 0)
    monkeypatch.setattr(adapters, "PCSX_BACKOFF", 0)


def _pos(n: int, **over) -> dict:
    # Shape copied from a live Microsoft row on 2026-10-12.
    p = {"id": 1970393557000000 + n, "displayJobId": str(200000000 + n),
         "name": f"Software Engineer {n}",
         "locations": ["United States, Washington, Redmond"],
         "standardizedLocations": ["Redmond, WA, US"],
         "postedTs": 1791799068 - n, "creationTs": 1791796982 - n,
         "department": "Software Engineering", "atsJobId": str(200000000 + n),
         "positionUrl": f"/careers/job/{1970393557000000 + n}"}
    p.update(over)
    return p


class Board:
    """A PCSX tenant: /careers sets a cookie, search wants it back."""

    def __init__(self, positions, count=None, swap=False, throttle_at=None):
        self.positions = positions
        self.count = count
        self.swap = swap                  # reorder across page edges on odd requests
        self.throttle_at = set(throttle_at or ())
        self.searches: list[httpx.Request] = []
        self.sessions = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/careers":
            self.sessions += 1
            return httpx.Response(200, text="<html></html>",
                                  headers={"Set-Cookie": f"_vs=s{self.sessions}; Path=/"})
        assert request.url.path == "/api/pcsx/search"
        self.searches.append(request)
        if "_vs=" not in request.headers.get("cookie", ""):
            return httpx.Response(429, json={"message": "Too Many Requests"})
        if len(self.searches) in self.throttle_at:
            self.throttle_at.discard(len(self.searches))
            return httpx.Response(429, json={"message": "Too Many Requests"})
        start = int(request.url.params["start"])
        rows = list(self.positions)
        if self.swap and len(self.searches) % 2 == 0:
            # Swap every pair that straddles a multiple of 10. A step-10
            # sweep then sees row 9 twice and row 10 never.
            for edge in range(PCSX_PAGE, len(rows), PCSX_PAGE):
                rows[edge - 1], rows[edge] = rows[edge], rows[edge - 1]
        count = self.count(len(self.searches)) if callable(self.count) else self.count
        return httpx.Response(200, json={
            "status": 200, "error": {"message": "", "body": ""},
            "data": {"positions": rows[start:start + PCSX_PAGE],
                     "count": len(self.positions) if count is None else count,
                     "sortBy": "timestamp"}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def test_session_cookie_is_established_before_searching():
    board = Board([_pos(i) for i in range(7)])
    async with board.client() as c:
        jobs = await eightfold(c, "Microsoft", ROW)
    assert board.sessions == 1
    assert len(jobs) == 7
    assert all("_vs=s1" in r.headers["cookie"] for r in board.searches)
    assert board.searches[0].url.params["domain"] == "microsoft.com"
    assert board.searches[0].headers["referer"] == f"https://{HOST}/careers"


async def test_pages_with_overlap_to_the_reported_count():
    board = Board([_pos(i) for i in range(53)])
    async with board.client() as c:
        jobs = await eightfold(c, "Microsoft", ROW)
    starts = [int(r.url.params["start"]) for r in board.searches]
    assert starts == list(range(0, 53, PCSX_STEP))
    assert len(jobs) == 53
    assert len({j.raw_id for j in jobs}) == 53


async def test_overlap_recovers_rows_a_reordering_board_would_skip():
    positions = [_pos(i) for i in range(40)]
    board = Board(positions, swap=True)
    async with board.client() as c:
        jobs = await eightfold(c, "Microsoft", ROW)
    assert {j.raw_id for j in jobs} == {str(p["id"]) for p in positions}
    assert len(jobs) == 40          # duplicates served by the overlap are dropped


async def test_drifting_count_does_not_cut_the_sweep_short():
    # Count reads 30 on the first page, then dips to 25 as a posting closes.
    board = Board([_pos(i) for i in range(30)], count=lambda n: 30 if n == 1 else 25)
    async with board.client() as c:
        jobs = await eightfold(c, "Microsoft", ROW)
    assert len(jobs) == 30


async def test_429_mid_sweep_reestablishes_the_session_and_retries():
    board = Board([_pos(i) for i in range(25)], throttle_at=[3])
    async with board.client() as c:
        jobs = await eightfold(c, "Microsoft", ROW)
    assert board.sessions == 2
    assert len(jobs) == 25


async def test_persistent_429_raises():
    board = Board([_pos(i) for i in range(25)],
                  throttle_at=range(1, adapters.PCSX_RETRIES + 1))
    async with board.client() as c:
        with pytest.raises(httpx.HTTPStatusError):
            await eightfold(c, "Microsoft", ROW)


async def test_error_body_without_data_raises():
    def handler(request):
        if request.url.path == "/careers":
            return httpx.Response(200, text="")
        return httpx.Response(200, json={"status": 400,
                                         "error": {"message": "bad domain"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ValueError, match="bad domain"):
            await eightfold(c, "Microsoft", ROW)


async def test_pcsx_needs_a_domain():
    async with Board([]).client() as c:
        with pytest.raises(ValueError, match="domain"):
            await eightfold(c, "Microsoft", {**ROW, "query": ""})


async def test_maps_id_title_url_location_date_department():
    p = _pos(1, locations=["United States, Washington, Redmond",
                           "United States, California, Mountain View"])
    board = Board([p, _pos(2, postedTs=None), _pos(3, postedTs=None, creationTs=None)])
    async with board.client() as c:
        jobs = await eightfold(c, "Microsoft", ROW)
    j = jobs[0]
    assert j.raw_id == str(p["id"])
    assert j.title == "Software Engineer 1"
    assert j.url == f"https://{HOST}/careers/job/{p['id']}"
    assert j.location == ("United States, Washington, Redmond; "
                          "United States, California, Mountain View")
    assert j.ats == "eightfold"
    assert j.department == "Software Engineering"
    assert (j.posted_at, j.posted_source) == ("2026-10-12T09:57:47+00:00", "postedTs")
    assert jobs[1].posted_source == "creationTs"
    assert (jobs[2].posted_at, jobs[2].posted_source) == (None, "")


@pytest.mark.parametrize("loc, us", [
    # Real `locations` strings from the three tenants on 2026-10-12.
    ("United States, Washington, Redmond", True),
    ("San Jose, California, United States of America", True),
    ("San Diego, California, United States of America", True),
    ("Utah, United States of America (Virtual)", True),
    ("India, Karnataka, Bangalore", False),
    ("India, Telangana, Hyderabad", False),
    ("Hyderabad, Telangāna, India", False),
    ("Ireland, Dublin, Dublin", False),
    ("Kuwait, Multiple Locations, Multiple Locations", False),
    ("Beijing, China", False),
    ("London, County London, England, United Kingdom", False),
    # Mexican state named after California: 71 Qualcomm rows read as US
    # until runner masked it. A real US option alongside still passes.
    ("Tijuana, Baja California, Mexico", False),
    ("Tijuana, Baja California, Mexico; Mexico City, Distrito Federal, Mexico", False),
    ("Tijuana, Baja California, Mexico; San Diego, California, United States of America", True),
])
def test_tenant_location_strings_classify_correctly(loc, us):
    assert is_us_location(loc) is us


def test_standardized_locations_would_have_collided():
    # Why the adapter reads `locations`: the standardized form of an Indian
    # posting ends in IN, which reads as Indiana.
    assert is_us_location("Hyderabad, TS, IN") is True


async def test_rows_without_site_still_use_the_apply_v2_path():
    # Netflix's row: no site column. Same endpoint, params and id as before.
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"count": 1, "positions": [{
            "id": 790298011234, "name": "Engineer", "location": "Los Gatos, California",
            "t_create": 1791799068, "canonicalPositionUrl": "https://x/careers/job/1",
            "department": "Eng"}]})
    row = {"company": "Netflix", "ats": "eightfold", "host": "explore.jobs.netflix.net",
           "site": "", "query": "", "token": ""}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        jobs = await eightfold(c, "Netflix", row)
    assert [r.url.path for r in seen] == ["/api/apply/v2/jobs"]
    assert dict(seen[0].url.params) == {"start": "0", "num": "10", "sort_by": "relevance"}
    assert (jobs[0].raw_id, jobs[0].posted_source) == ("790298011234", "t_create")


def test_registry_rejects_unknown_eightfold_site(tmp_path):
    cols = ",".join(registry.COLUMNS)
    f = tmp_path / "c.csv"
    f.write_text(f"{cols}\n"
                 "A,A,eightfold,,,pcsx,,a.example,,a.com,\n"
                 "B,B,eightfold,,,bogus,,b.example,,b.com,\n"
                 "C,C,eightfold,,,pcsx,,c.example,,,\n", encoding="utf-8")
    with pytest.raises(registry.RegistryError) as e:
        registry.load(f)
    msg = str(e.value)
    assert "B: eightfold site must be blank or 'pcsx'" in msg
    assert "C: eightfold pcsx needs the tenant domain in query" in msg
    assert " A:" not in msg
