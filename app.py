import json
import os
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

db.init_db()

_companies_cache = {"mtime": None, "data": None}
scheduler = BackgroundScheduler(daemon=True)


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
    manual /api/refresh endpoint and the background scheduler."""
    companies = load_companies()
    all_jobs, errors = fetchers.fetch_all(companies)
    db.upsert_jobs(all_jobs)
    failed = {e["company"] for e in errors}
    ok_companies = [c["name"] for c in companies if c["name"] not in failed]
    db.remove_stale(ok_companies, [j["id"] for j in all_jobs])

    # Companies removed from companies.json entirely (not just failed this
    # run) still have old rows in the DB — purge those too.
    configured_names = {c["name"] for c in companies}
    orphaned = [row["company"] for row in db.get_companies_with_counts()
                if row["company"] not in configured_names]
    if orphaned:
        db.remove_stale(orphaned, [])

    db.log_refresh(len(all_jobs), errors)
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
# directly. Flask's reloader (debug=True) re-executes this module in a child
# process; only that child sets WERKZEUG_RUN_MAIN, so this guard keeps the
# scheduler from starting twice. Under gunicorn, run with a single worker —
# each worker process would otherwise start its own duplicate scheduler.
if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    start_scheduler()

if __name__ == "__main__":
    app.run(debug=DEBUG, port=int(os.environ.get("PORT", 5050)))
