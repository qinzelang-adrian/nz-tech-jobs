import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, render_template

import db
import fetchers

app = Flask(__name__)
COMPANIES_PATH = Path(__file__).parent / "companies.json"
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
REFRESH_INTERVAL_MINUTES = int(os.environ.get("REFRESH_INTERVAL_MINUTES", "60"))
AUTO_REFRESH_JOB_ID = "auto_refresh"

# Optional: set REFRESH_TOKEN in the environment to require callers of
# POST /api/refresh to send a matching X-Refresh-Token header. Left unset,
# the endpoint stays open (its previous behavior).
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")
REFRESH_COOLDOWN_SECONDS = 30

db.init_db()

_companies_cache = {"mtime": None, "data": None}
scheduler = BackgroundScheduler(daemon=True)
_refresh_lock = threading.Lock()
_last_refresh_at = 0.0


def load_companies():
    """Read companies.json, reloading only when the file has changed on disk."""
    mtime = COMPANIES_PATH.stat().st_mtime
    if _companies_cache["mtime"] != mtime:
        with open(COMPANIES_PATH, encoding="utf-8") as f:
            _companies_cache["data"] = json.load(f)
        _companies_cache["mtime"] = mtime
    return _companies_cache["data"]


def run_refresh():
    """Fetch every configured company and sync the database. Shared by the
    manual /api/refresh endpoint and the background scheduler. Serialized by
    _refresh_lock so a manual click and the scheduled run never write to
    SQLite at the same time."""
    global _last_refresh_at
    with _refresh_lock:
        companies = load_companies()
        previous_counts = {row["company"]: row["cnt"] for row in db.get_companies_with_counts()}

        all_jobs, errors = fetchers.fetch_all(companies)
        db.upsert_jobs(all_jobs)
        jobs_by_company = Counter(j["company"] for j in all_jobs)
        failed = {e["company"] for e in errors}

        # A company can "succeed" (no HTTP error) but still return zero jobs —
        # e.g. a transient empty response from the ATS. If it previously had
        # jobs, treat a sudden zero as suspicious rather than assuming every
        # posting really closed at once, and skip stale-removal for it this
        # run; a genuine zero will just get removed on the next good refresh.
        ok_companies = []
        for c in companies:
            name = c["name"]
            if name in failed:
                continue
            if jobs_by_company[name] == 0 and previous_counts.get(name, 0) > 0:
                errors.append({
                    "company": name,
                    "error": "returned zero jobs unexpectedly (had jobs before); "
                             "skipped removal this run in case it's a transient glitch",
                })
                continue
            ok_companies.append(name)
        db.remove_stale(ok_companies, [j["id"] for j in all_jobs])

        # Companies removed from companies.json entirely (not just failed this
        # run) still have old rows in the DB — purge those too.
        configured_names = {c["name"] for c in companies}
        orphaned = [row["company"] for row in db.get_companies_with_counts()
                    if row["company"] not in configured_names]
        if orphaned:
            db.remove_stale(orphaned, [])

        db.log_refresh(len(all_jobs), errors)
        _last_refresh_at = time.monotonic()
        return len(all_jobs), errors


def start_scheduler():
    scheduler.add_job(
        run_refresh, "interval", minutes=REFRESH_INTERVAL_MINUTES,
        next_run_time=datetime.now(), id=AUTO_REFRESH_JOB_ID,
    )
    scheduler.start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/companies")
def api_companies():
    configured = load_companies()
    counts = {c["company"]: c for c in db.get_companies_with_counts()}
    out = []
    for c in configured:
        row = counts.get(c["name"], {})
        out.append({
            "name": c["name"],
            "ats": c["ats"],
            "note": c.get("note", ""),
            "count": row.get("cnt", 0),
            "intern_count": row.get("intern_cnt", 0) or 0,
        })
    out.sort(key=lambda c: c["name"].lower())
    return jsonify(out)


@app.route("/api/jobs")
def api_jobs():
    q = request.args.get("q", "").strip()
    company = request.args.get("company", "").strip()
    intern_only = request.args.get("intern_only", "false").lower() == "true"
    nz_only = request.args.get("nz_only", "true").lower() == "true"
    sort = request.args.get("sort", "posted_desc")
    jobs = db.query_jobs(q=q or None, company=company or None,
                          intern_only=intern_only, nz_only=nz_only, sort=sort)
    return jsonify(jobs)


@app.route("/api/jobs/<job_id>/applied", methods=["POST"])
def api_mark_applied(job_id):
    db.mark_applied(job_id)
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/applied", methods=["DELETE"])
def api_unmark_applied(job_id):
    db.unmark_applied(job_id)
    return jsonify({"ok": True})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if REFRESH_TOKEN and request.headers.get("X-Refresh-Token") != REFRESH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    if time.monotonic() - _last_refresh_at < REFRESH_COOLDOWN_SECONDS:
        return jsonify({"error": "refreshed too recently, try again shortly"}), 429
    if _refresh_lock.locked():
        return jsonify({"error": "a refresh is already running"}), 409
    total, errors = run_refresh()
    return jsonify({"total": total, "errors": errors})


@app.route("/api/status")
def api_status():
    next_run = None
    job = scheduler.get_job(AUTO_REFRESH_JOB_ID) if scheduler.running else None
    if job and job.next_run_time:
        next_run = job.next_run_time.isoformat()
    return jsonify({
        "last_refresh": db.get_last_refresh(),
        "auto_refresh_interval_minutes": REFRESH_INTERVAL_MINUTES,
        "next_auto_refresh": next_run,
    })


# Runs at import time (not just under `python app.py`) so the scheduler also
# starts when a production WSGI server like gunicorn imports this module
# directly. Under gunicorn, run with a single worker — each worker process
# would otherwise start its own duplicate scheduler.
#
# Flask's own dev-server reloader (debug=True, only reachable by running
# `python app.py` directly) re-executes this module in a child process; only
# that child sets WERKZEUG_RUN_MAIN, so the parent watcher process must skip
# starting the scheduler itself. That check must not apply under gunicorn:
# gunicorn imports this module as "app" (never "__main__") and never spawns
# a reloader, so WERKZEUG_RUN_MAIN is simply never set there — checking it
# unconditionally would mean the scheduler never starts at all if FLASK_DEBUG
# is ever left on in a gunicorn deployment.
_is_dev_reloader_parent = (
    __name__ == "__main__" and DEBUG and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
)
# DISABLE_SCHEDULER lets the test suite import this module (to exercise
# run_refresh() and the Flask routes directly) without a real network-hitting
# refresh firing in the background the moment it's imported.
if not _is_dev_reloader_parent and os.environ.get("DISABLE_SCHEDULER") != "1":
    start_scheduler()

if __name__ == "__main__":
    app.run(debug=DEBUG, port=int(os.environ.get("PORT", 5050)))
