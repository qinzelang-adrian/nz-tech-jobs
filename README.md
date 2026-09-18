# NZ Tech Departures 🛫

![Tests](https://github.com/qinzelang-adrian/nz-tech-jobs/actions/workflows/tests.yml/badge.svg)

**[Live demo](https://nz-tech-departures.onrender.com/)**

![NZ Tech Departures screenshot](static/screenshot.png)

A job board that aggregates open roles from New Zealand tech companies, with
a one-click filter for internship / graduate positions. Data comes straight
from each company's applicant tracking system (ATS) **public JSON API** (not
HTML scraping — it's more stable and "more legitimate": these endpoints are
what the company's own careers page calls, so anyone can access them without
logging in or needing an API key).

Currently supports five common ATS providers: **Greenhouse / Lever / Ashby /
Workable / BambooHR**.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Then open `http://localhost:5050` in your browser. A background job kicks
off a refresh automatically on startup (and every `REFRESH_INTERVAL_MINUTES`
minutes after that, default 60), so listings should appear within a few
seconds without you touching anything — the "Refresh job data" button is
there for triggering an immediate re-fetch on demand.

Flask's debug mode (auto-reload + interactive debugger) is off by default;
set `FLASK_DEBUG=1` if you want it while developing. Leave it off if you
ever expose this beyond localhost — the debugger allows arbitrary code
execution to anyone who can reach it.

## Deployment

The app is ready to run behind a production WSGI server (`gunicorn`, already
in `requirements.txt`) instead of Flask's built-in dev server. It deploys to
[Render](https://render.com) from the `render.yaml` Blueprint in this repo:

1. Sign in to Render with your GitHub account.
2. In the Render Dashboard, click **New → Blueprint** and pick this repo.
   Render reads `render.yaml` and creates a free web service with the build
   command, start command, region, health check, and environment variables
   already filled in — there's nothing to configure by hand.
3. Render prompts for `REFRESH_TOKEN` during the sync. Leave it blank unless
   you want to lock down the refresh endpoint (see below).
4. Once the first deploy finishes, Render gives you a public
   `*.onrender.com` URL — that's your live demo link.

Prefer clicking through the dashboard instead? Choose **New → Web Service**,
pick the repo, and set: runtime **Python 3**, build command
`pip install -r requirements.txt`, start command
`gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`. Keep it at
**1 worker** — each worker process starts its own copy of the background
scheduler, so more than one would trigger duplicate refreshes. The extra
threads just let the single worker keep serving other requests (health checks
included) while a refresh is in flight.

### Environment variables

- `REFRESH_INTERVAL_MINUTES` — override the default 60-minute auto-refresh.
- `REFRESH_TOKEN` — if set, `POST /api/refresh` requires a matching
  `X-Refresh-Token` header. Left unset, that endpoint stays open to anyone
  who can reach the site (fine for a low-traffic demo; a cooldown and
  in-progress guard already stop it from being spammed either way).
- `GUNICORN_CMD_ARGS` — set to `--access-logfile -` by `render.yaml`, and
  you should keep it that way. Render's Python runtime otherwise defaults it
  to `--preload ...`, and `--preload` makes gunicorn import `app.py` in the
  master process *before* forking the worker, which starts the scheduler in
  the master. Threads don't survive `fork()`, so the worker ends up with a
  scheduler that reports itself running but never fires, while the master's
  copy runs refreshes from a different process than the one serving
  `POST /api/refresh` — which defeats the `_refresh_lock` that's supposed to
  keep those two off SQLite at the same time.

### Free-tier caveats

Render's free web services are fine for a demo, but worth knowing about:

- **They spin down after 15 minutes without inbound traffic**, and take about
  a minute to spin back up on the next request. Nothing runs while a service
  is spun down, so the 60-minute scheduled refresh only fires while someone
  is actually using the site. In practice this is mostly self-correcting: the
  scheduler is configured with `next_run_time=datetime.now()`, so a refresh
  kicks off immediately on every cold start.
- **The filesystem is ephemeral.** `data/jobs.db` (SQLite) is lost on every
  redeploy, restart, *and* spin-down, and free services can't attach a
  [persistent disk](https://render.com/docs/disks). Combined with the point
  above, that means a cold start serves an empty board for the few seconds it
  takes the startup refresh to repopulate it. For anything longer-lived,
  either move to a paid instance with a disk mounted at `data/`, or port
  `db.py` to [Render Postgres](https://render.com/docs/postgresql).
- **750 free instance hours per workspace per month.** Spun-down time doesn't
  count against them.
- Render may suspend a free service that generates an
  [unusually high volume of outbound traffic](https://render.com/docs/free#service-initiated-traffic-threshold).
  This app calls one external ATS API per configured company per refresh, so
  a sane `REFRESH_INTERVAL_MINUTES` keeps it well clear — just don't set it
  to something like `1` with a hundred companies configured.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

Unit tests cover the per-ATS parsing logic in `fetchers.py` (using recorded
JSON fixtures under `tests/fixtures/` instead of hitting real APIs), the
SQLite storage logic in `db.py` (LIKE-wildcard escaping, `first_seen_at`
preservation across refreshes, stale-job cleanup), and `app.py`'s refresh
orchestration and `/api/refresh` route (company list and ATS calls are
stubbed out, so these don't hit real APIs or the network either). They run
on every push via [GitHub Actions](.github/workflows/tests.yml).

`app.py` starts its background scheduler as soon as it's imported (so it
also runs under a production WSGI server, not just `python app.py`); the
test suite sets `DISABLE_SCHEDULER=1` (see `tests/conftest.py`) before
importing it so `pytest` doesn't kick off a real scheduled refresh in the
background every run.

## Project structure

```
├── app.py            # Flask backend + API routes
├── fetchers.py        # Talks to the Greenhouse/Lever/Ashby/Workable/BambooHR public APIs
├── db.py              # SQLite storage: jobs, applied-status, and refresh history (data/jobs.db, created automatically on first run)
├── companies.json     # Company list config (the main extensibility point, see below)
├── find_slug.py        # CLI tool: paste a careers page URL, auto-detect ATS type and slug
├── templates/index.html
├── static/style.css, script.js   # Frontend board (departures-board style)
└── tests/              # pytest suite for fetchers.py, db.py, and app.py
```

## Companies currently included

`companies.json` starts with a handful of verified companies (Halter,
Partly, Sharesies, Education Perfect, Parrot Analytics, Soul Machines,
Karbon, Cin7, Kami, Pushpay, Serko, Theta, Laybuy, Orion Health) — just a
starting point. **The real value of a job aggregator
is the company list you curate yourself**, which is also a great talking
point in interviews: it shows you've researched the NZ tech ecosystem and
built a system that can keep growing, instead of hardcoding a handful of
entries.

### How to add more companies

1. Find the target company's careers page (e.g. Google "company name
   careers").
2. Run:
   ```bash
   python find_slug.py https://the-careers-page-url
   ```
   It will automatically figure out whether the company uses Greenhouse /
   Lever / Ashby / Workable / BambooHR, and print a config snippet you can
   paste straight into `companies.json`.
3. If detection fails, the company is probably using an in-house ATS (many
   large companies use Workday, for example) — this project doesn't support
   that yet, so just skip it.

There are plenty more NZ tech companies worth watching — it's worth going
through their careers pages yourself and verifying each one with the tool
above. Note that some big names (Xero, ServiceNow, Rocket Lab, Seequent,
LawVu, Dexibit) block simple automated requests to their careers pages
entirely (403 responses), so they'll need a manual check rather than
`find_slug.py`. A few others (Vend/Lightspeed, Timely, Serato, Mint
Innovation, First AML, Fisher & Paykel Appliances, Meridian Energy) render
their job board client-side with JavaScript or run an in-house/Workday
system, which `find_slug.py` can't see either — same "skip it" situation as
an in-house ATS. And occasionally a company's hosted job board page loads
fine in a browser but its public API is switched off (Chainlink Labs is one
example) — `find_slug.py` will detect the ATS but fail verification, which
is its way of telling you not to trust that guess.
The process itself is good job-search research.

## Features

- **Shows every open role by default**: not just internships — the full
  listing from each configured company
- **Keyword search**: filter by job title / location / department
- **Company filter**: pick a single company from the dropdown
- **Intern/graduate toggle**: automatically detects keywords like intern /
  graduate / junior / trainee in the title, one click to narrow down to
  just those
- **NZ-only toggle (on by default)**: several of these companies (Halter,
  Pushpay, Partly...) have expanded overseas and post far more roles abroad
  than in NZ. Each job's location is matched against NZ region/city names;
  anything with a location that's clearly not NZ is hidden unless you flip
  the switch off. Jobs with no location on file at all are kept visible
  rather than hidden, since some ATS records just don't report one.
- **Manual refresh**: click the button to re-fetch the latest listings from
  every company in real time, and automatically flag jobs that have been
  taken down (they get cleaned out of the database)
- **Failure isolation**: if one company's API is down, it won't affect the
  results from the others — the error is shown in the refresh status bar. A
  company that responds successfully but with an unexpectedly empty list
  (rather than an HTTP error) is treated the same way: its existing listings
  are left alone instead of being wiped, in case it was just a transient
  glitch upstream
- **Scheduled auto-refresh**: an APScheduler background job re-fetches every
  company on an interval (default 60 min, override with the
  `REFRESH_INTERVAL_MINUTES` env var), so listings stay current even if you
  never touch the button. The frontend also polls every 2 minutes to pick up
  whatever the background job found. (On a free Render instance this only
  ticks while the service is awake — see [Free-tier caveats](#free-tier-caveats).)
- **"NEW" badge**: any job first seen in the last 24 hours gets a badge in
  the listing, using the `first_seen_at` timestamp already tracked per job
- **Applied tracking**: check "Applied" on a row to mark it (persisted in
  its own `applied_jobs` table, independent of whether the listing later
  closes) — applied rows dim so you can see at a glance what's left to do

## Ideas for extra credit

- **Email/Telegram notifications**: get notified automatically when a new
  job appears (the `first_seen_at` tracking and scheduled refresh are
  already there — this would hook into that instead of re-fetching)

## Tech stack

Python + Flask + SQLite + APScheduler (background auto-refresh) + vanilla
HTML/CSS/JS frontend (no frontend framework, kept simple and
straightforward so every line is easy to explain in an interview).

## License

[MIT](LICENSE)
