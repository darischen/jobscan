"""Core data model and title matching.

Two orthogonal gates, deliberately separate:

  role_of(title)   -> which engineering family, or None
  level_of(title)  -> "early" | "senior" | "above_senior" | "unmarked"

A title has to clear the role gate before level is ever consulted. That
ordering is what keeps "Junior Procurement Specialist" out: it never
reaches the level check, because no role family matches. Level words are
not evidence of a technical role, and role words are not evidence of
seniority.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass
class Job:
    company: str
    title: str
    url: str
    ticker: str = ""
    location: str = ""
    ats: str = ""
    posted_at: str | None = None      # ISO 8601 UTC, or None if the board hides it
    posted_source: str = ""           # which field the date came from
    raw_id: str = ""
    department: str = ""
    bucket: str = ""
    role: str = ""                    # which family matched

    @property
    def key(self) -> str:
        """Stable identity across runs. Prefers the board's own id over the URL."""
        seed = f"{self.company.lower()}|{self.ats}|{self.raw_id or self.url}"
        return hashlib.sha1(seed.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Gate 1: role. An allowlist of engineering titles, one pattern per family.
#
# Add a family, do not widen an existing one. Widening is untraceable; a new
# key shows up in the audit counts.
# ---------------------------------------------------------------------------

ROLE_FAMILIES: dict[str, str] = {
    # The last alternative is a ladder spelling, Netflix "Software Engineering
    # 5" and Microsoft "Software Engineering II": the same role, not a widening.
    "software":  r"software\s+(dev(elopment)?\s+)?(engineer|developer) | \bswe\b | \bsde\b | sw\s+engineer | software\s+dev\b | software\s+engineering\s+(\d|i{1,3}|iv)",
    "ai":        r"\bai\s*/?\s*ml\s+engineer | (ai|artificial\s+intelligence)\s+(engineer|developer) | applied\s+(ai|ml)\s+(engineer|scientist) | (llm|genai|generative\s+ai)\s+engineer | agent(ic)?\s+engineer",
    "ml":        r"machine\s+learning\s+(engineer|scientist) | \bmle\b | ml\s+(engineer|infra(structure)?|platform|ops) | deep\s+learning\s+engineer | research\s+engineer",
    "data":      r"data\s+engineer | analytics\s+engineer | data\s+(platform|infrastructure)\s+engineer",
    # back-end / back end as frontend already allowed front-end / front end.
    "backend":   r"back[\s\-]?end\s+(engineer|developer) | server[\s\-]side\s+engineer",
    "frontend":  r"frontend\s+(engineer|developer) | front[\s\-]end\s+(engineer|developer)",
    "fullstack": r"full[\s\-]?stack\s+(engineer|developer)",
    "infra":     r"(platform|infrastructure|systems?|reliability)\s+engineer | \bsre\b | site\s+reliability | devops\s+engineer | cloud\s+engineer",
    "fde":       r"forward[\s\-]deployed\s+(engineer|software)",
    # Added 2026-10-13 from an audit of 78,538 US postings; counts are the
    # postings each family recovered from role_miss that day.
    "sysde":     r"systems?\s+development\s+engineer | \bsysde\b",                       # 219, Amazon and Google
    "mobile":    r"(ios|android|mobile)\s+(\w+\s+){0,2}(engineer|developer)",           # 63
    # Member of Technical Staff is the software title at AI labs; the
    # lookahead keeps out "Member of Technical Staff - Mechanical Engineer".
    "mts":       r"member\s+of\s+(the\s+)?technical\s+staff(?!.*\b(mechanical|electrical|hardware|design|process)\s+engineer) | software\s+engineering\s+[slp]?mts",  # ~70
    "developer": r"(java|python|golang|c\+\+|c\#|scala|kotlin|swift|ruby|node(\.js)?|react|javascript|typescript|web|application|applications|quantitative|quant|salesforce|etl|mainframe|ui)\s+(\w+\s+)?developer",  # 75
    "compiler":  r"(compiler|kernel|runtime)\s+engineer(ing)?",                          # 56
    "programmer": r"programmer",                                                         # 22, game studios and mainframe
    "test":      r"\bsdet\b | test\s+automation\s+engineer | qa\s+automation\s+engineer",  # ~40
    # Off by default. Enable by adding the key to DEFAULT_ROLES.
    "datasci":     r"data\s+scientist",
    "solutions":   r"solutions?\s+(engineer|architect)",
    "product_eng": r"product\s+engineer",
    "security":    r"security\s+engineer | application\s+security",
    "hardware":    r"hardware\s+engineer | (asic|rtl|fpga|verification)\s+engineer",
    "embedded":    r"embedded\s+(software\s+)?engineer | firmware\s+engineer",
    "bie":         r"business\s+intelligence\s+engineer",    # 46; SQL analytics, Amazon mostly
}

DEFAULT_ROLES = ("software", "ai", "ml", "data", "backend",
                 "frontend", "fullstack", "infra", "fde",
                 "sysde", "mobile", "mts", "developer", "compiler",
                 "programmer", "test")

_COMPILED = {k: re.compile(rf"\b(?:{v})\b", re.I | re.X)
             for k, v in ROLE_FAMILIES.items()}


def role_of(title: str, families: tuple[str, ...] = DEFAULT_ROLES) -> str | None:
    """First matching family, in the order given. None means not a fit."""
    for fam in families:
        pat = _COMPILED.get(fam)
        if pat and pat.search(title):
            return fam
    return None


# ---------------------------------------------------------------------------
# Gate 2: level. Only consulted once a role family matched.
#
# Three tiers, because "not senior" and "above senior" are different
# rejections. Senior and mid are worth reading. Staff and up, and anything
# with people management, are not.
# ---------------------------------------------------------------------------

LEVEL_ABOVE_SENIOR = re.compile(r"""\b(
    staff | principal | distinguished | fellow
  | lead\s+(\w+\s+){0,2}(engineer|developer|scientist)
  | tech(nical)?\s+lead | team\s+lead
  | manager | \bmgr\b | director | head\s+of | \bvp\b | chief | president
  | architect
  | (engineer(ing)?|developer|scientist)\s*(vi|vii|viii|6|7|8)\b
  | level\s*[6-9]\b | \bl[6-9]\b
  | \b(lmts|pmts)\b
  | \b(1[0-9]|[89])\s*\+?\s*years
)\b""", re.I | re.X)

LEVEL_SENIOR = re.compile(r"""\b(
    senior | \bsr\.?
  | (engineer(ing)?|developer|scientist)\s*(ii|iii|iv|v|2|3|4|5)\b
  | \b(sde|swe|sysde)\s*(ii|iii|iv|2|3|4)\b
  | \bsmts\b
  | associate\s*(ii|iii|iv|2|3|4)\b
  | level\s*[2-5]\b | \bl[4-5]\b
  | \b[3-7]\s*\+?\s*years
)\b""", re.I | re.X)

LEVEL_EARLY = re.compile(r"""\b(
    new\s?grad(uate)?
  | recent\s+grad(uate)?
  | early\s+career | early[\s\-]in[\s\-]career
  | (university|college)\s+grad(uate)?
  | campus | entry[\s\-]level
  | junior | jr\.?
  # Only in front of the role. On its own it is a pay band at a bank:
  # Morgan Stanley's "Associate, Software Engineer" is not a new grad req.
  | associate\s+(\w+\s+){0,3}(engineer|developer|scientist|programmer)
  | \b(sde|swe|sysde)\s*(i|1)\b
  | graduate\s+(engineer|program|scheme)
  | (engineer|developer|scientist)\s*(i|1)\b
  | level\s*1\b | \bl3\b | grade\s*1\b
  | rotational
  | 20\d\d\s+(grad|start)
)\b""", re.I | re.X)


_MTS = re.compile(r"member\s+of\s+(the\s+)?technical\s+staff", re.I)


def level_of(title: str) -> str:
    """above_senior | senior | early | unmarked.

    Checked most restrictive first, so "Senior Staff Engineer" reads
    above_senior and "Senior Associate Engineer" reads senior.
    "Member of Technical Staff" is a title, not a level, so its "staff"
    is ignored.
    """
    title = _MTS.sub(" ", title)
    if LEVEL_ABOVE_SENIOR.search(title):
        return "above_senior"
    if LEVEL_SENIOR.search(title):
        return "senior"
    if LEVEL_EARLY.search(title):
        return "early"
    return "unmarked"


# ---------------------------------------------------------------------------
# Gate 0: hard exclusions, checked before anything else.
#
# Contract and part-time are NOT excluded: they are real employment lines.
# Internships and co-ops are, because they are not.
# ---------------------------------------------------------------------------

EXCLUDE = re.compile(r"""\b(
    intern(ship)?s? | co[\s\-]?op | \bcoop\b
  | postdoc | phd\s+(required|candidate)
  | apprentice
)\b""", re.I | re.X)

# Business functions. They mark a title "excluded" only when no engineering
# role matched: on their own they also name the team an engineer works on,
# and "Software Development Engineer, Amazon Marketing Cloud" was being
# dropped as a marketing job.
NON_ENGINEERING = re.compile(r"""\b(
    sales | account\s+(executive|manager) | recruiter | marketing
  | customer\s+success | business\s+development
)\b""", re.I | re.X)


BUCKETS = ("explicit_early", "unleveled", "senior", "above_senior",
           "excluded", "role_miss")
DEFAULT_KEEP = ("explicit_early", "unleveled", "senior")


def classify(title: str, families: tuple[str, ...] = DEFAULT_ROLES) -> tuple[str, str]:
    """Return (bucket, role_family). role_family is '' when nothing matched."""
    if EXCLUDE.search(title):
        return "excluded", ""
    fam = role_of(title, families)
    if not fam:
        return ("excluded" if NON_ENGINEERING.search(title) else "role_miss"), ""
    lvl = level_of(title)
    if lvl == "above_senior":
        return "above_senior", fam
    if lvl == "senior":
        return "senior", fam
    if lvl == "early":
        return "explicit_early", fam
    # A bare "Software Engineer" at a large cap is frequently the new grad
    # req, so unmarked titles are kept rather than discarded.
    return "unleveled", fam