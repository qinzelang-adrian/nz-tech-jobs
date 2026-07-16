import sqlite3
import json
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

import fetchers

DB_PATH = Path(__file__).parent / "data" / "jobs.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with closing(get_conn()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                company TEXT,
                title TEXT,
                location TEXT,
                department TEXT,
                ats TEXT,
                url TEXT,
                posted_at TEXT,
                is_intern INTEGER,
                is_nz INTEGER,
                fetched_at TEXT,
                first_seen_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS refresh_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT,
                total_jobs INTEGER,
                errors TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applied_jobs (
                job_id TEXT PRIMARY KEY,
                applied_at TEXT
            )
        """)

        # Migration: older DBs predate the is_nz column. Add it and backfill
        # from the already-stored location text so existing jobs don't
        # vanish from the NZ-only filter until their next refresh.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "is_nz" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN is_nz INTEGER")
            rows = conn.execute("SELECT id, location FROM jobs").fetchall()
            conn.executemany(
                "UPDATE jobs SET is_nz = ? WHERE id = ?",
                [(int(fetchers.is_nz_location(r["location"])), r["id"]) for r in rows],
            )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_intern ON jobs(is_intern)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_nz ON jobs(is_nz)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_posted_at ON jobs(posted_at)")
        conn.commit()


def upsert_jobs(jobs: list):
    if not jobs:
        return
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn:
        ids = [j["id"] for j in jobs]
        placeholders = ",".join("?" * len(ids))
        existing_rows = conn.execute(
            f"SELECT id, first_seen_at FROM jobs WHERE id IN ({placeholders})", ids
        ).fetchall()
        first_seen_by_id = {r["id"]: r["first_seen_at"] for r in existing_rows}

        rows = [
            (
                j["id"], j["company"], j["title"], j.get("location"), j.get("department"),
                j["ats"], j.get("url"), j.get("posted_at"), int(bool(j["is_intern"])),
                int(bool(j["is_nz"])), now, first_seen_by_id.get(j["id"], now),
            )
            for j in jobs
        ]
        conn.executemany("""
            INSERT INTO jobs (id, company, title, location, department, ats, url,
                               posted_at, is_intern, is_nz, fetched_at, first_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                location=excluded.location,
                department=excluded.department,
                url=excluded.url,
                posted_at=excluded.posted_at,
                is_intern=excluded.is_intern,
                is_nz=excluded.is_nz,
                fetched_at=excluded.fetched_at
        """, rows)
        conn.commit()


def remove_stale(company_names: list, seen_ids: list):
    """Delete jobs belonging to these companies that no longer appear in this fetch (i.e. the posting was closed)."""
    if not company_names:
        return 0
    with closing(get_conn()) as conn:
        placeholders = ",".join("?" * len(company_names))
        rows = conn.execute(
            f"SELECT id FROM jobs WHERE company IN ({placeholders})", company_names
        ).fetchall()
        seen = set(seen_ids)
        to_delete = [r["id"] for r in rows if r["id"] not in seen]
        if to_delete:
            conn.executemany("DELETE FROM jobs WHERE id=?", [(i,) for i in to_delete])
            conn.commit()
        return len(to_delete)


def query_jobs(q=None, company=None, intern_only=False, nz_only=True, sort="posted_desc"):
    sql = """
        SELECT jobs.*, applied_jobs.applied_at AS applied_at
        FROM jobs
        LEFT JOIN applied_jobs ON applied_jobs.job_id = jobs.id
        WHERE 1=1
    """
    params = []
    if q:
        # Escape SQL LIKE wildcards so a literal "%" or "_" typed in the search
        # box is matched literally instead of matching everything.
        escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND (title LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' OR department LIKE ? ESCAPE '\\')"
        like = f"%{escaped_q}%"
        params += [like, like, like]
    if company:
        sql += " AND company = ?"
        params.append(company)
    if intern_only:
        sql += " AND is_intern = 1"
    if nz_only:
        sql += " AND is_nz = 1"
    if sort == "posted_asc":
        sql += " ORDER BY posted_at ASC"
    elif sort == "company":
        sql += " ORDER BY company ASC, title ASC"
    else:
        sql += " ORDER BY posted_at DESC"
    with closing(get_conn()) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def mark_applied(job_id: str):
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO applied_jobs (job_id, applied_at) VALUES (?, ?) "
            "ON CONFLICT(job_id) DO NOTHING",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def unmark_applied(job_id: str):
    with closing(get_conn()) as conn:
        conn.execute("DELETE FROM applied_jobs WHERE job_id = ?", (job_id,))
        conn.commit()


def get_companies_with_counts():
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT company, COUNT(*) as cnt, SUM(is_intern) as intern_cnt "
            "FROM jobs GROUP BY company ORDER BY company"
        ).fetchall()
        return [dict(r) for r in rows]


def get_last_refresh():
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM refresh_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def log_refresh(total: int, errors: list):
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO refresh_log (ran_at, total_jobs, errors) VALUES (?,?,?)",
            (datetime.now(timezone.utc).isoformat(), total, json.dumps(errors, ensure_ascii=False)),
        )
        conn.commit()
