import time

import pytest

import app
import db


def make_company(name, ats="greenhouse", slug="acme"):
    return {"name": name, "ats": ats, "slug": slug}


def make_job(job_id, company, title="Software Engineer"):
    return {
        "id": job_id, "company": company, "title": title, "location": "Auckland",
        "department": "Engineering", "ats": "greenhouse", "url": f"https://example.com/{job_id}",
        "posted_at": "2026-06-01T00:00:00Z", "is_intern": False, "is_nz": True,
    }


@pytest.fixture(autouse=True)
def reset_refresh_state(monkeypatch):
    """run_refresh()/api_refresh() share module-level lock/cooldown state;
    give each test a clean slate so tests don't leak into one another."""
    monkeypatch.setattr(app, "_last_refresh_at", 0.0)
    yield


def stub_fetch_all(jobs=(), errors=()):
    return lambda companies: (list(jobs), list(errors))


def test_run_refresh_skips_removal_when_company_unexpectedly_returns_zero(monkeypatch):
    companies = [make_company("Acme")]
    monkeypatch.setattr(app, "load_companies", lambda: companies)

    monkeypatch.setattr(app.fetchers, "fetch_all", stub_fetch_all(jobs=[make_job("gh:acme:1", "Acme")]))
    total, errors = app.run_refresh()
    assert total == 1
    assert errors == []
    assert [j["id"] for j in db.query_jobs()] == ["gh:acme:1"]

    # Acme "succeeds" this run (no error reported) but comes back with zero
    # jobs, even though it had one a moment ago — should be treated as a
    # possible glitch, not a real mass-closure.
    monkeypatch.setattr(app.fetchers, "fetch_all", stub_fetch_all(jobs=[]))
    total, errors = app.run_refresh()

    assert total == 0
    assert [j["id"] for j in db.query_jobs()] == ["gh:acme:1"]
    assert any(e["company"] == "Acme" for e in errors)


def test_run_refresh_still_removes_closed_postings_for_healthy_companies(monkeypatch):
    companies = [make_company("Acme")]
    monkeypatch.setattr(app, "load_companies", lambda: companies)

    monkeypatch.setattr(app.fetchers, "fetch_all", stub_fetch_all(jobs=[
        make_job("gh:acme:1", "Acme"), make_job("gh:acme:2", "Acme"),
    ]))
    app.run_refresh()

    # Next run only sees job 1 — job 2's posting genuinely closed.
    monkeypatch.setattr(app.fetchers, "fetch_all", stub_fetch_all(jobs=[make_job("gh:acme:1", "Acme")]))
    total, errors = app.run_refresh()

    assert total == 1
    assert errors == []
    assert [j["id"] for j in db.query_jobs()] == ["gh:acme:1"]


def test_run_refresh_does_not_flag_company_that_never_had_jobs(monkeypatch):
    companies = [make_company("NewCo")]
    monkeypatch.setattr(app, "load_companies", lambda: companies)
    monkeypatch.setattr(app.fetchers, "fetch_all", stub_fetch_all(jobs=[]))

    total, errors = app.run_refresh()

    assert total == 0
    assert errors == []


def test_run_refresh_leaves_failed_companys_jobs_untouched(monkeypatch):
    companies = [make_company("Acme"), make_company("Flaky")]
    monkeypatch.setattr(app, "load_companies", lambda: companies)

    monkeypatch.setattr(app.fetchers, "fetch_all", stub_fetch_all(
        jobs=[make_job("gh:flaky:1", "Flaky")],
    ))
    app.run_refresh()

    # Flaky's API errors out entirely this run.
    monkeypatch.setattr(app.fetchers, "fetch_all", stub_fetch_all(
        jobs=[], errors=[{"company": "Flaky", "error": "boom"}],
    ))
    total, errors = app.run_refresh()

    assert total == 0
    assert [j["id"] for j in db.query_jobs()] == ["gh:flaky:1"]
    assert [e["company"] for e in errors] == ["Flaky"]


def test_api_refresh_returns_totals(monkeypatch):
    monkeypatch.setattr(app, "load_companies", lambda: [])
    client = app.app.test_client()

    res = client.post("/api/refresh")

    assert res.status_code == 200
    assert res.get_json() == {"total": 0, "errors": []}


def test_api_refresh_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(app, "load_companies", lambda: [])
    monkeypatch.setattr(app, "REFRESH_TOKEN", "secret123")
    client = app.app.test_client()

    denied = client.post("/api/refresh")
    assert denied.status_code == 401

    allowed = client.post("/api/refresh", headers={"X-Refresh-Token": "secret123"})
    assert allowed.status_code == 200


def test_api_refresh_enforces_cooldown(monkeypatch):
    monkeypatch.setattr(app, "load_companies", lambda: [])
    monkeypatch.setattr(app, "_last_refresh_at", time.monotonic())
    client = app.app.test_client()

    res = client.post("/api/refresh")

    assert res.status_code == 429


def test_api_refresh_rejects_concurrent_calls(monkeypatch):
    monkeypatch.setattr(app, "load_companies", lambda: [])
    client = app.app.test_client()

    app._refresh_lock.acquire()
    try:
        res = client.post("/api/refresh")
        assert res.status_code == 409
    finally:
        app._refresh_lock.release()
