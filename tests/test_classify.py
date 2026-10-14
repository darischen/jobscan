"""Title classification. Offline.

Every title here is a real posting from the 2026-10-13 scan of 119 boards,
78,538 US postings, which is where each rule below was found.
"""
from __future__ import annotations

import pytest

from jobscan.core import classify


@pytest.mark.parametrize("title,bucket", [
    # Already right before the audit; kept so a change cannot break them.
    ("Software Engineer", "unleveled"),
    ("Software Development Engineer", "unleveled"),
    ("Associate Software Engineer", "explicit_early"),
    ("IBM Associate Data Engineer 2027 - AI & Analytics", "explicit_early"),
    ("Software Engineer I", "explicit_early"),
    ("Senior Software Engineer", "senior"),
    ("Staff Software Engineer", "above_senior"),
    ("Software Development Engineer Intern - Summer 2027 (USA)", "excluded"),
    ("2027 Undergrad Software Engineer Intern/Co-op", "excluded"),
    ("Mechanical Engineer, Data Center Retrofit Design", "role_miss"),
    ("Engineering Operations Technician, DCEO", "role_miss"),
    ("Account Executive, Corporate", "excluded"),
])
def test_unchanged(title: str, bucket: str):
    assert classify(title)[0] == bucket


@pytest.mark.parametrize("title", [
    "Software Development Engineer, Amazon Marketing Cloud Assistant",
    "SDE II, Sales Abuse Prevention",
    "Data Engineer, Marketing Insights and Technology",
    "Machine Learning Engineer, Advertising & Marketing Performance Intelligence",
])
def test_a_business_word_in_the_team_name_does_not_exclude_an_engineering_role(title):
    """'sales' and 'marketing' were exclusions on their own, so an engineering
    role on a sales or marketing team was dropped as if it were a sales job."""
    assert classify(title)[0] in ("unleveled", "senior", "explicit_early")


@pytest.mark.parametrize("title,family", [
    ("System Development Engineer I, Enterprise Productivity", "sysde"),
    ("Systems Development Engineer, Sovereign Cloud Operations, Google Public Sector", "sysde"),
    ("Back-end Engineer - Data Platforms", "backend"),
    ("Entry Level Back End Developer - Poughkeepsie, NY - 2027", "backend"),
    ("Android Engineer, Social", "mobile"),
    ("iOS Engineer - Wallet", "mobile"),
    ("Member of Technical Staff, Agentic Systems - Games", "mts"),
    ("Software Engineering MTS", "mts"),
    ("C++ Developer", "developer"),
    ("Quantitative Developer Systematic Trading - Associate", "developer"),
    ("Application Developer - IT Service Management", "developer"),
    ("Senior Compiler Engineer, Agentic Compiler Systems", "compiler"),
    ("Animation Systems Programmer", "programmer"),
    ("SDET, Wallet Payments & Commerce", "test"),
    ("Senior Test Automation Engineer", "test"),
    ("Software Engineering 5 - Ads Conversion Attribution", "software"),
    ("Software Engineering II", "software"),
])
def test_software_roles_the_role_gate_used_to_miss(title, family):
    bucket, fam = classify(title)
    assert fam == family and bucket != "role_miss", (bucket, fam)


def test_business_intelligence_engineer_is_a_family_but_off_by_default():
    assert classify("Business Intelligence Engineer II, OTS")[0] == "role_miss"
    assert classify("Business Intelligence Engineer II, OTS",
                    families=("bie",)) == ("senior", "bie")


@pytest.mark.parametrize("title", [
    "Member of Technical Staff - Imagine Safety",
    "Member of Technical Staff, Frontier AI & Robotics",
])
def test_member_of_technical_staff_is_not_a_staff_level(title):
    """'staff' read as above senior, which buried every MTS posting."""
    assert classify(title) == ("unleveled", "mts")


@pytest.mark.parametrize("title", [
    "Member of Technical Staff – Mechanical Engineer, Manipulation",
    "MTS, AI Optimized Memory Digital Design Engineer",
])
def test_hardware_mts_titles_stay_out(title):
    assert classify(title)[1] != "mts"


@pytest.mark.parametrize("title,bucket", [
    ("SDE II", "senior"),
    ("SDE II, Sales Abuse Prevention", "senior"),
    ("SDE I, Last Mile", "explicit_early"),
    ("System Development Engineer II, Lambda/Serverless", "senior"),
    ("Software Engineering II - CTJ - Poly", "senior"),
    ("Software Engineering 6 - Ads Finance", "above_senior"),
    ("Senior Software Engineering MTS", "senior"),
    ("Software Engineering SMTS", "senior"),
    ("Software Engineering LMTS", "above_senior"),
])
def test_levels_written_the_way_these_companies_write_them(title, bucket):
    assert classify(title)[0] == bucket


@pytest.mark.parametrize("title,bucket", [
    # A band at a bank, not a junior title.
    ("Associate, Software Engineer", "unleveled"),
    ("Data Engineer - Financial Crimes - Associate", "unleveled"),
    ("Software Engineer Associate III - Databricks", "senior"),
    # Directly before the role it still means entry level.
    ("Associate Full Stack Developer 2027 - Digital Product", "explicit_early"),
    ("Missionforce - Associate Forward Deployed Engineer (FDE), Public Sector",
     "explicit_early"),
])
def test_associate_counts_as_early_only_in_front_of_the_role(title, bucket):
    assert classify(title)[0] == bucket
