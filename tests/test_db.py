import pytest

import db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point db.py at a fresh SQLite file per test instead of the real
    data/jobs.db, so tests can't clobber real data or leak state between
    each other."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_jobs.db")
    db.init_db()


def make_job(job_id, title="Software Engineer", company="Example Co",
             location="Auckland", is_intern=False, is_nz=True):
    return {
        "id": job_id, "company": company, "title": title, "location": location,
        "department": "Engineering", "ats": "greenhouse",
        "url": f"https://example.com/{job_id}", "posted_at": "2026-06-01T00:00:00Z",
        "is_intern": is_intern, "is_nz": is_nz,
    }


def test_query_jobs_escapes_percent_wildcard():
    db.upsert_jobs([
        make_job("job-1", title="100% Remote Engineer"),
        make_job("job-2", title="Software Engineer"),
    ])

    results = db.query_jobs(q="%")

    assert [r["id"] for r in results] == ["job-1"]


def test_query_jobs_escapes_underscore_wildcard():
    db.upsert_jobs([
        make_job("job-1", title="Full_Stack_Engineer"),
        make_job("job-2", title="Software Engineer"),
    ])

    results = db.query_jobs(q="_")

    assert [r["id"] for r in results] == ["job-1"]


def test_upsert_preserves_first_seen_at_across_refreshes():
    db.upsert_jobs([make_job("job-1", title="Software Engineer")])
    first_seen = db.query_jobs()[0]["first_seen_at"]

    # Same job reappears in a later refresh with an updated title.
    db.upsert_jobs([make_job("job-1", title="Senior Software Engineer")])
    row = db.query_jobs()[0]

    assert row["title"] == "Senior Software Engineer"
    assert row["first_seen_at"] == first_seen


def test_remove_stale_only_affects_named_companies():
    db.upsert_jobs([
        make_job("job-1", company="Company A"),
        make_job("job-2", company="Company A"),
        make_job("job-3", company="Company B"),
    ])

    # Company A's refresh only saw job-1 this time; job-2's posting closed.
    deleted = db.remove_stale(["Company A"], ["job-1"])

    remaining_ids = {r["id"] for r in db.query_jobs()}
    assert deleted == 1
    assert remaining_ids == {"job-1", "job-3"}


def test_query_jobs_nz_only_filter():
    db.upsert_jobs([
        make_job("job-1", is_nz=True),
        make_job("job-2", is_nz=False),
    ])

    assert [r["id"] for r in db.query_jobs(nz_only=True)] == ["job-1"]
    nz_and_overseas = {r["id"] for r in db.query_jobs(nz_only=False)}
    assert nz_and_overseas == {"job-1", "job-2"}
