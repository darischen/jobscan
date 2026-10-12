"""discover.py marker detection for the board families added since July.

Offline. Pages come from an httpx.MockTransport; confirmation is either the
real adapter talking to that same fake site, or `discover.confirm` replaced
by a table of which candidate rows "have jobs". The second form is what lets
a test say exactly which rows were tried and in what order.

The page snippets copy the markers measured live on 2026-10-12: tbcdn on
jobs.intuit.com, jibecdn and /careers-home on careers.amd.com, rmkcdn on
careers.sap.com, avacdn plus avature.portal metas on jobs.ea.com (which never
says avature.net), entity-escaped "domain" on the eightfold tenants, and
Phenom's search-results page carrying each job's Workday applyUrl.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from jobscan.adapters import TIER_A
from tools import discover

CONFIG = ("ats", "token", "tenant", "site", "wd", "host", "query")

RADANCY_PAGE = """<html><head>
<link rel="stylesheet" href="https://tbcdn.talentbrew.com/company/27595/css/site.css">
</head><body><a href="/search-jobs">Search jobs</a></body></html>"""

ICIMS_HOME_PAGE = """<html><head>
<link rel="stylesheet" href="https://app.jibecdn.com/prod/personalization/1/main.css">
<script>window._jibe = {models: {}};</script></head>
<body><a href="/careers-home/jobs">Jobs</a>
<a href="https://careers-acme.icims.com/jobs/login">Sign in</a></body></html>"""

SF_PAGE = """<html><head>
<link rel="stylesheet" href="//rmkcdn.successfactors.com/84a30c28/e6639b75.css">
</head><body class="successfactors-page"><a href="/search/">Search</a>
<script src="https://rmk-map.jobs2web.com/map/x.js"></script></body></html>"""

AVATURE_PAGE = """<html><head>
<meta name="avature.portal.id" content="4"/>
<meta name="avature.portal.urlPath" content="careers"/>
<meta name="avature.portal.lang" content="en_US"/>
<link href="https://templates-static-assets.avacdn.net/cssLibrary/24.2/core.min.css">
</head><body>
<a href="https://jobs.acme.example/en_US/careers/SearchJobs">Explore</a></body></html>"""

EIGHTFOLD_PAGE = """<html><head>
<link rel="stylesheet" href="https://static.vscdn.net/images/careers/x.css"></head>
<body><code id="smartApplyData">{&#34;domain&#34;: &#34;acme.com&#34;,
&#34;customDomain&#34;: &#34;careers.acme.example&#34;,
&#34;pcsx-hero-image-height&#34;: &#34;100px&#34;, &#34;domain&#34;: &#34;acme.com&#34;,
&#34;branding&#34;: {&#34;domain&#34;: &#34;careers.acme.com&#34;}}</code>
<script src="https://acme.eightfold.ai/static/app.js"></script></body></html>"""

PHENOM_HOME = """<html><head>
<link href="https://cdn.phenompeople.com/CareerConnectResources/ACMEGLOBAL/x.css">
</head><body><a href="search-results">Search all jobs</a>
<a href="https://careers.acme.example/global/en/blog">Blog</a></body></html>"""

PHENOM_RESULTS = (
    '<html><head><link href="https://cdn.phenompeople.com/CareerConnectResources/x.css">'
    '</head><body><script>phApp.ddo = {"eagerLoadRefineSearch":{"data":{"jobs":['
    '{"title":"Software Engineer","applyUrl":"https:\\/\\/acme.wd5.myworkdayjobs.com'
    '\\/Acme_Careers\\/job\\/San-Jose\\/Software-Engineer_2023799\\/apply"}]}}};'
    '</script></body></html>')


def _site(pages: dict[str, Any], seen: list[str] | None = None) -> httpx.AsyncClient:
    """A fake web. `pages` maps a URL (no query) to a body, a Response, or a
    redirect target written as "-> https://...". Anything else is a 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None)).rstrip("/")
        if seen is not None:
            seen.append(url)
        body = pages.get(url)
        if body is None:
            return httpx.Response(404, text="not found")
        if isinstance(body, httpx.Response):
            return body
        if body.startswith("-> "):
            return httpx.Response(302, headers={"Location": body[3:]})
        return httpx.Response(200, text=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             follow_redirects=True)


def _confirms(monkeypatch, rules: dict[tuple, int]) -> list[tuple]:
    """Replace discover.confirm. `rules` maps a candidate's config tuple
    (ats, token, tenant, site, wd, host, query) to its job count; every other
    candidate fails, as a board that does not exist would."""
    tried: list[tuple] = []

    async def fake(client, ats, row):
        key = (ats,) + tuple(row.get(k, "") or "" for k in CONFIG[1:])
        tried.append(key)
        return rules.get(key, -1)

    monkeypatch.setattr(discover, "confirm", fake)
    return tried


def _key(ats="", token="", tenant="", site="", wd="", host="", query="") -> tuple:
    return (ats, token, tenant, site, wd, host, query)


def _config(row: dict[str, Any]) -> tuple:
    return tuple(row.get(k, "") or "" for k in CONFIG)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(discover, "FOLLOW_DELAY", 0)
    monkeypatch.setattr(discover, "FOLLOW_BACKOFF", 0)


# ------------------------------------------------------- markers, pure part
def test_radancy_markers_nominate_the_page_host():
    cands = discover.site_candidates(RADANCY_PAGE, "https://jobs.acme.example/")
    assert cands[0] == ("radancy", {"host": "jobs.acme.example"})
    assert cands.count(("radancy", {"host": "jobs.acme.example"})) == 1


def test_search_jobs_path_alone_is_a_weak_radancy_candidate_tried_last():
    page = ICIMS_HOME_PAGE + '<a href="/search-jobs">Search</a>'
    cands = discover.site_candidates(page, "https://careers.acme.example/careers-home")
    assert [ats for ats, _ in cands] == ["icims", "radancy"]


def test_icims_careers_home_markers_nominate_the_page_host():
    cands = discover.site_candidates(
        ICIMS_HOME_PAGE, "https://careers.acme.example/careers-home/jobs")
    assert cands == [("icims", {"host": "careers.acme.example"})]


def test_classic_icims_host_is_never_a_candidate():
    page = '<a href="/careers-home">x</a> <script>window._jibe = 1</script>'
    assert discover.site_candidates(page, "https://careers-acme.icims.com/jobs") == []
    link_only = '<a href="https://careers-acme.icims.com/jobs/search">Jobs</a>'
    assert discover.site_candidates(link_only, "https://www.acme.example/careers") == []


def test_successfactors_markers_try_classic_then_unify():
    cands = discover.site_candidates(SF_PAGE, "https://careers.acme.example")
    assert cands == [
        ("successfactors", {"host": "careers.acme.example"}),
        ("successfactors", {"host": "careers.acme.example", "site": "unify"}),
    ]


def test_avature_is_seen_without_avature_net_and_reads_the_portal_path():
    assert "avature.net" not in AVATURE_PAGE
    cands = discover.site_candidates(AVATURE_PAGE, "https://jobs.acme.example/en_US/careers")
    assert cands == [("avature", {"host": "jobs.acme.example", "site": "en_US/careers"})]


def test_avature_portal_path_from_metas_alone():
    page = ('<meta name="avature.portal.urlPath" content="careers"/>'
            '<meta name="avature.portal.lang" content="en_US"/>')
    assert discover.avature_sites(page, "https://jobs.acme.example/")[0] == "en_US/careers"


def test_kula_account_comes_from_the_url():
    cands = discover.site_candidates(
        "https://careers.kula.ai/acmebio\n<html>"
        '<script src="https://careers.kula.ai/_next/static/x.js"></script></html>',
        "https://careers.kula.ai/acmebio")
    assert cands == [("kula", {"token": "acmebio", "host": "careers.kula.ai"})]


def test_eightfold_tries_plain_then_pcsx_with_the_pages_domain():
    cands = discover.site_candidates(EIGHTFOLD_PAGE, "https://careers.acme.example/careers")
    host = "careers.acme.example"
    assert cands[0] == ("eightfold", {"host": host})
    # "domain" twice beats the one nested value; customDomain is another key.
    assert cands[1] == ("eightfold", {"host": host, "site": "pcsx", "query": "acme.com"})
    assert all(c[1].get("query") != "careers.acme.example" for c in cands)


def test_eightfold_domain_guess_for_a_hosted_tenant():
    assert discover.eightfold_domains("", "acme.eightfold.ai") == ["acme.com"]
    assert discover.eightfold_domains("", "apply.careers.acme.com") == ["acme.com"]


def test_follow_links_resolves_relative_links_against_the_page_as_a_directory():
    links = discover.follow_links(PHENOM_HOME, "https://careers.acme.example/global/en")
    assert links == ["https://careers.acme.example/global/en/search-results"]


def test_follow_links_is_bounded_and_skips_assets():
    page = "".join(f'<a href="/jobs/{i}/x">j</a><a href="/openings/{i}">o</a>'
                   f'<link href="/search-results/{i}.css">' for i in range(9))
    links = discover.follow_links(page, "https://careers.acme.example/")
    assert len(links) == discover.FOLLOW_MAX
    assert not any(u.endswith(".css") for u in links)


# -------------------------------------------- resolve, candidate row checks
async def test_radancy_row(monkeypatch):
    tried = _confirms(monkeypatch, {_key("radancy", host="jobs.acme.example"): 567})
    async with _site({"https://jobs.acme.example": RADANCY_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "ACME", "https://jobs.acme.example")
    assert _config(row) == _key("radancy", host="jobs.acme.example")
    assert row["notes"] == "confirmed 567 jobs"
    assert tried == [_key("radancy", host="jobs.acme.example")]


async def test_taleo_labelled_radancy_site_is_radancy(monkeypatch):
    """UnitedHealth: Taleo behind it, TalentBrew is the only public listing."""
    page = RADANCY_PAGE + '<a href="https://acme.taleo.net/careersection/apply">Apply</a>'
    _confirms(monkeypatch, {_key("radancy", host="careers.acme.example"): 5547})
    async with _site({"https://careers.acme.example": "-> https://careers.acme.example/search-jobs",
                      "https://careers.acme.example/search-jobs": page}) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example")
    assert _config(row) == _key("radancy", host="careers.acme.example")
    assert "taleo front end over radancy" in row["notes"]


async def test_icims_careers_home_row_uses_the_host_after_redirects(monkeypatch):
    _confirms(monkeypatch, {_key("icims", host="careers.acme.example"): 1256})
    async with _site({
        "https://acme.example/careers": "-> https://careers.acme.example/careers-home/jobs",
        "https://careers.acme.example/careers-home/jobs": ICIMS_HOME_PAGE,
    }) as c:
        row = await discover.resolve(c, "Acme", "", "https://acme.example/careers")
    assert _config(row) == _key("icims", host="careers.acme.example")


async def test_classic_icims_portal_is_not_promoted(monkeypatch):
    """The 405 AWS WAF captcha page: no candidate, no tier A name on the row."""
    tried = _confirms(monkeypatch, {})
    captcha = httpx.Response(405, text="<html><script>AwsWafIntegration</script></html>")
    async with _site({"https://careers-acme.icims.com/jobs/search": captcha}) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers-acme.icims.com/jobs/search")
    assert not any(t[0] == "icims" for t in tried)
    assert row["ats"] == "custom" and row["ats"] not in TIER_A
    assert row["notes"].startswith("tier B") and "icims" in row["notes"]


async def test_page_that_only_links_to_a_classic_icims_portal_is_not_promoted(monkeypatch):
    tried = _confirms(monkeypatch, {})
    page = '<html><a href="https://careers-acme.icims.com/jobs/intro">Apply</a></html>'
    async with _site({"https://www.acme.example/careers": page}) as c:
        row = await discover.resolve(c, "Acme", "", "https://www.acme.example/careers")
    assert tried == []
    assert row["ats"] == "custom"
    assert row["host"] == "www.acme.example"


async def test_successfactors_classic_row(monkeypatch):
    tried = _confirms(monkeypatch, {_key("successfactors", host="careers.acme.example"): 788})
    async with _site({"https://careers.acme.example": SF_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example")
    assert _config(row) == _key("successfactors", host="careers.acme.example")
    assert len(tried) == 1      # unify is not probed once classic confirms


async def test_successfactors_unify_row(monkeypatch):
    unify = _key("successfactors", site="unify", host="careers.acme.example")
    tried = _confirms(monkeypatch, {unify: 105})
    async with _site({"https://careers.acme.example": SF_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example")
    assert _config(row) == unify
    assert tried == [_key("successfactors", host="careers.acme.example"), unify]


async def test_unconfirmed_successfactors_markers_do_not_keep_a_tier_a_name(monkeypatch):
    _confirms(monkeypatch, {})
    async with _site({"https://careers.acme.example": SF_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example")
    assert row["ats"] == "custom"
    assert "successfactors markers" in row["notes"]


async def test_avature_row(monkeypatch):
    want = _key("avature", site="en_US/careers", host="jobs.acme.example")
    _confirms(monkeypatch, {want: 362})
    async with _site({"https://jobs.acme.example": "-> https://jobs.acme.example/en_US/careers",
                      "https://jobs.acme.example/en_US/careers": AVATURE_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", "https://jobs.acme.example")
    assert _config(row) == want


async def test_kula_row(monkeypatch):
    want = _key("kula", token="acmebio", host="careers.kula.ai")
    _confirms(monkeypatch, {want: 34})
    async with _site({"https://careers.kula.ai/acmebio": "<html>Acme Bio</html>"}) as c:
        row = await discover.resolve(c, "Acme Bio", "", "https://careers.kula.ai/acmebio")
    assert _config(row) == want


async def test_eightfold_pcsx_row(monkeypatch):
    host = "acme.eightfold.ai"
    want = _key("eightfold", site="pcsx", host=host, query="acme.com")
    tried = _confirms(monkeypatch, {want: 312})
    async with _site({f"https://{host}/careers": EIGHTFOLD_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", f"https://{host}/careers")
    assert _config(row) == want
    # The old v2 API is asked first and refuses; only then the pcsx row.
    assert tried == [_key("eightfold", host=host), want]


async def test_eightfold_plain_row_when_the_v2_api_answers(monkeypatch):
    host = "explore.jobs.acme.example"
    tried = _confirms(monkeypatch, {_key("eightfold", host=host): 476})
    async with _site({f"https://{host}/careers": EIGHTFOLD_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", f"https://{host}/careers")
    assert _config(row) == _key("eightfold", host=host)
    assert len(tried) == 1


async def test_small_site_hosted_board_is_flagged_verify(monkeypatch):
    _confirms(monkeypatch, {_key("radancy", host="jobs.acme.example"): 2})
    async with _site({"https://jobs.acme.example": RADANCY_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", "https://jobs.acme.example")
    assert row["ats"] == "radancy"
    assert row["notes"].startswith("VERIFY: only 2 jobs")


# ------------------------------------------------------------- front ends
async def test_phenom_front_end_over_workday_prefers_workday(monkeypatch):
    """Cisco: nothing on the home page, the listing's applyUrl names Workday."""
    want = _key("workday", tenant="acme", site="Acme_Careers", wd="wd5")
    tried = _confirms(monkeypatch, {want: 1336})
    seen: list[str] = []
    async with _site({
        "https://careers.acme.example": "-> https://careers.acme.example/global/en",
        "https://careers.acme.example/global/en": PHENOM_HOME,
        "https://careers.acme.example/global/en/search-results": PHENOM_RESULTS,
    }, seen) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example")
    assert _config(row) == want
    assert tried == [want]
    assert "phenom front end over workday" in row["notes"]
    assert "found via https://careers.acme.example/global/en/search-results" in row["notes"]
    assert seen[-1] == "https://careers.acme.example/global/en/search-results"


async def test_phenom_site_stays_tier_b_when_nothing_confirms(monkeypatch):
    tried = _confirms(monkeypatch, {})
    async with _site({
        "https://careers.acme.example/global/en": PHENOM_HOME,
        "https://careers.acme.example/global/en/search-results": PHENOM_RESULTS,
    }) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example/global/en")
    assert tried == [_key("workday", tenant="acme", site="Acme_Careers", wd="wd5")]
    assert (row["ats"], row["host"], row["notes"]) == (
        "phenom", "careers.acme.example", "tier B")


async def test_followed_page_that_is_throttled_is_retried_once_then_dropped(monkeypatch):
    tried = _confirms(monkeypatch, {})
    seen: list[str] = []
    async with _site({
        "https://careers.acme.example/global/en": PHENOM_HOME,
        "https://careers.acme.example/global/en/search-results": httpx.Response(429),
    }, seen) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example/global/en")
    assert seen.count("https://careers.acme.example/global/en/search-results") == 2
    assert tried == [] and row["ats"] == "phenom"


async def test_shared_cluster_workday_link_sets_host(monkeypatch):
    """jobs.fidelity.com style: apply links go to wd1.myworkdaysite.com."""
    want = _key("workday", tenant="acme", site="AcmeCareers", wd="wd1",
                host="wd1.myworkdaysite.com")
    _confirms(monkeypatch, {want: 795})
    page = ('<a href="https://wd1.myworkdaysite.com/recruiting/acme/AcmeCareers'
            '/job/Boston/Engineer_1">Apply</a>')
    async with _site({"https://jobs.acme.example": page}) as c:
        row = await discover.resolve(c, "Acme", "", "https://jobs.acme.example")
    assert _config(row) == want


async def test_hosted_board_linked_from_a_corporate_page_is_followed(monkeypatch):
    """morganstanley.com links out to morganstanley.eightfold.ai/careers."""
    want = _key("eightfold", site="pcsx", host="acme.eightfold.ai", query="acme.com")
    _confirms(monkeypatch, {want: 1285})
    corp = '<html><a href="https://acme.eightfold.ai/careers">Search jobs</a></html>'
    async with _site({"https://www.acme.com/careers/search": corp,
                      "https://acme.eightfold.ai/careers": EIGHTFOLD_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", "https://www.acme.com/careers/search")
    assert _config(row) == want
    assert row["careers_url"] == "https://www.acme.com/careers/search"


# ------------------------------------------------ unchanged token families
async def test_greenhouse_embed_still_confirms_first_and_dead_embeds_are_skipped(monkeypatch):
    tried = _confirms(monkeypatch, {_key("greenhouse", token="acme"): 40})
    page = ('<a href="https://jobs.lever.co/oldacme">old</a>'
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>'
            + RADANCY_PAGE)
    async with _site({"https://acme.example/careers": page}) as c:
        row = await discover.resolve(c, "Acme", "", "https://acme.example/careers")
    assert _config(row) == _key("greenhouse", token="acme")
    assert row["notes"] == "confirmed 40 jobs"
    assert tried == [_key("greenhouse", token="acme")]


async def test_workday_link_on_the_page_itself_wins_without_following(monkeypatch):
    want = _key("workday", tenant="acme", site="External", wd="wd1")
    _confirms(monkeypatch, {want: 639})
    seen: list[str] = []
    page = PHENOM_HOME + '<a href="https://acme.wd1.myworkdayjobs.com/en-US/External">Jobs</a>'
    async with _site({"https://careers.acme.example/global/en": page}, seen) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example/global/en")
    assert _config(row) == want
    assert seen == ["https://careers.acme.example/global/en"]


# ---------------------------------- the real adapter is what does the count
async def test_icims_careers_home_confirmed_by_the_real_adapter():
    api = httpx.Response(200, json={"totalCount": 6, "count": 6, "jobs": [
        {"data": {"req_id": str(i), "title": f"Engineer {i}", "slug": str(i)}}
        for i in range(6)]})
    async with _site({"https://careers.acme.example/careers-home/jobs": ICIMS_HOME_PAGE,
                      "https://careers.acme.example/api/jobs": api}) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example/careers-home/jobs")
    assert _config(row) == _key("icims", host="careers.acme.example")
    assert row["notes"] == "confirmed 6 jobs"


async def test_careers_home_markers_with_a_dead_api_emit_nothing_tier_a():
    """Markers alone prove nothing: /api/jobs 404s, so the row is tier B."""
    async with _site({"https://careers.acme.example/careers-home/jobs": ICIMS_HOME_PAGE}) as c:
        row = await discover.resolve(c, "Acme", "", "https://careers.acme.example/careers-home/jobs")
    assert row["ats"] == "custom" and row["ats"] not in TIER_A
    assert row["notes"] == "tier B; icims markers but its API did not confirm"
