import json
from pathlib import Path
from unittest.mock import patch

import pytest

import fetchers

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def mock_response(payload):
    """A minimal stand-in for requests.Response: .json() returns payload,
    .raise_for_status() is a no-op (as it is for any 2xx real response)."""
    resp = type("MockResponse", (), {})()
    resp.json = lambda: payload
    resp.raise_for_status = lambda: None
    return resp


# ---- is_internship_title -------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Software Engineering Intern", True),
    ("New Grad Software Engineer", True),
    ("Graduate Software Engineer", True),
    ("Software Engineer (Grad)", True),
    ("Data Science Intern/Graduate, NZ", True),
    ("Junior Firmware Engineer", True),
    ("Summer Clerk - Legal", True),
    ("Senior Software Engineer", False),
    ("Software Engineer", False),
    # "grad"/"graduate" must not match as a substring of a longer word.
    ("Undergraduate Research Assistant", False),
    ("", False),
    (None, False),
])
def test_is_internship_title(title, expected):
    assert fetchers.is_internship_title(title) is expected


# ---- is_nz_location --------------------------------------------------------

@pytest.mark.parametrize("location,expected", [
    (None, True),
    ("", True),
    ("   ", True),
    ("Auckland, New Zealand", True),
    ("Remote - New Zealand", True),
    ("Wellington", True),
    ("Sydney, Australia", False),
    ("Kansas, USA", False),
    ("London, UK", False),
])
def test_is_nz_location(location, expected):
    assert fetchers.is_nz_location(location) is expected


# ---- per-ATS fetchers -------------------------------------------------------

def test_fetch_greenhouse():
    with patch("fetchers.requests.get", return_value=mock_response(load_fixture("greenhouse.json"))):
        jobs = fetchers.fetch_greenhouse("example")

    assert len(jobs) == 2
    assert jobs[0]["id"] == "greenhouse:example:1234"
    assert jobs[0]["title"] == "Software Engineer Intern"
    assert jobs[0]["location"] == "Auckland, New Zealand"
    assert jobs[0]["posted_at"] == "2026-06-01T00:00:00Z"
    # Falls back to updated_at when first_published is missing.
    assert jobs[1]["posted_at"] == "2026-05-20T00:00:00Z"


def test_fetch_lever():
    with patch("fetchers.requests.get", return_value=mock_response(load_fixture("lever.json"))):
        jobs = fetchers.fetch_lever("example")

    assert len(jobs) == 2
    assert jobs[0]["id"] == "lever:example:aa11"
    assert jobs[0]["title"] == "Graduate Software Engineer"
    assert jobs[0]["location"] == "Wellington, New Zealand"
    assert jobs[0]["department"] == "Engineering"
    assert jobs[0]["posted_at"] is not None
    # Second job has no "team", falls back to "department" key.
    assert jobs[1]["department"] == "Sales"


def test_fetch_ashby():
    with patch("fetchers.requests.get", return_value=mock_response(load_fixture("ashby.json"))):
        jobs = fetchers.fetch_ashby("example")

    assert len(jobs) == 2
    assert jobs[0]["location"] == "Christchurch"
    assert jobs[0]["department"] == "Data"
    # Second job uses the locationName/team fallback keys.
    assert jobs[1]["location"] == "Auckland, New Zealand"
    assert jobs[1]["department"] == "Sales"


def test_fetch_workable_dedupes_only_repeated_shortcodes():
    with patch("fetchers.requests.get", return_value=mock_response(load_fixture("workable.json"))):
        jobs = fetchers.fetch_workable("example")

    assert len(jobs) == 3
    by_id = {j["id"]: j for j in jobs}
    # The repeated shortcode gets disambiguated with its location...
    assert "workable:example:ABC123:Auckland, New Zealand" in by_id
    assert "workable:example:ABC123:Christchurch, New Zealand" in by_id
    # ...but a shortcode that appears once keeps a stable, un-suffixed id.
    assert "workable:example:XYZ999" in by_id
    assert by_id["workable:example:XYZ999"]["location"] == "Wellington, New Zealand"


def test_fetch_bamboohr():
    with patch("fetchers.requests.get", return_value=mock_response(load_fixture("bamboohr.json"))):
        jobs = fetchers.fetch_bamboohr("example")

    assert len(jobs) == 2
    assert jobs[0]["location"] == "Auckland"
    assert jobs[0]["url"] == "https://example.bamboohr.com/careers/42"
    # Falls back to atsLocation when the primary location has no city.
    assert jobs[1]["location"] == "Wellington, New Zealand"


# ---- fetch_company / fetch_all ---------------------------------------------

def test_fetch_company_normalizes_and_flags_jobs():
    company = {"name": "Example Co", "ats": "greenhouse", "slug": "example"}
    with patch("fetchers.requests.get", return_value=mock_response(load_fixture("greenhouse.json"))):
        jobs = fetchers.fetch_company(company)

    intern_job = next(j for j in jobs if j["title"] == "Software Engineer Intern")
    senior_job = next(j for j in jobs if j["title"] == "Senior Backend Engineer")
    assert intern_job["company"] == "Example Co"
    assert intern_job["is_intern"] is True
    assert intern_job["is_nz"] is True
    assert senior_job["is_intern"] is False
    assert senior_job["is_nz"] is False


def test_fetch_all_isolates_a_single_company_failure():
    companies = [
        {"name": "Good Co", "ats": "greenhouse", "slug": "good"},
        {"name": "Broken Co", "ats": "lever", "slug": "broken"},
    ]

    def fake_fetch_company(company):
        if company["name"] == "Broken Co":
            raise RuntimeError("boom")
        return [{"id": "job-1", "title": "Role", "company": company["name"],
                  "ats": company["ats"], "is_intern": False, "is_nz": True}]

    with patch("fetchers.fetch_company", side_effect=fake_fetch_company):
        jobs, errors = fetchers.fetch_all(companies)

    assert len(jobs) == 1
    assert jobs[0]["company"] == "Good Co"
    assert len(errors) == 1
    assert errors[0]["company"] == "Broken Co"
