"""
Fetches job listings from each company's applicant tracking system (ATS)
public JSON API. Supports: Greenhouse, Lever, Ashby, Workable, BambooHR —
these are all public, no-API-key-required endpoints (the company's own
careers page calls these same endpoints to render its job list).

If a company uses an in-house ATS (not one of the five above), it isn't
supported here — use find_slug.py to check first, or look at the careers
page manually.
"""

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from datetime import datetime, timezone

REQUEST_TIMEOUT = 15
USER_AGENT = "nz-intern-jobs-dashboard/1.0 (personal project, respectful polling)"

# If these keywords appear in a job title, it's likely an internship/graduate/junior role
INTERN_KEYWORDS = [
    "internship", "intern", "graduate", "grad", "new grad",
    "entry level", "entry-level", "junior", "trainee", "cadet",
    "student", "co-op", "coop", "placement", "vacation programme",
    "summer clerk",
]

# Word-boundary match so e.g. "grad" doesn't require a trailing space/hyphen
# (catches "Software Engineer (Grad)") but still skips "undergrad".
_INTERN_PATTERN = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(k) for k in INTERN_KEYWORDS) + r")(?![a-z])",
    re.IGNORECASE,
)


def is_internship_title(title: str) -> bool:
    return bool(_INTERN_PATTERN.search(title or ""))


# NZ regions, main cities/towns, and country names. Used to tell apart
# domestic postings (e.g. "Waikato", "Canterbury") from the same companies'
# overseas roles (e.g. "Kansas", "Queensland") once they expand abroad.
NZ_LOCATION_KEYWORDS = [
    "new zealand", "aotearoa", "nz",
    "northland", "auckland", "waikato", "bay of plenty", "gisborne",
    "hawke's bay", "hawkes bay", "taranaki", "manawatu", "whanganui",
    "wellington", "tasman", "nelson", "marlborough", "west coast",
    "canterbury", "otago", "southland", "chatham islands",
    "hamilton", "tauranga", "christchurch", "dunedin",
    "palmerston north", "napier", "hastings", "rotorua",
    "new plymouth", "whangarei", "invercargill", "timaru",
    "taupo", "masterton", "levin", "ashburton", "blenheim",
    "queenstown", "pukekohe", "whakatane", "gore",
    "oamaru", "feilding", "wanaka", "motueka", "greymouth",
    "kaitaia", "kerikeri", "morrinsville",
]

_NZ_LOCATION_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in NZ_LOCATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def is_nz_location(location) -> bool:
    """True if the location clearly refers to New Zealand. Missing/blank
    locations (some ATS records just don't report one) default to True too —
    hiding them would lose real roles from these NZ companies, whereas the
    actual noise problem is postings explicitly located elsewhere."""
    if not location or not location.strip():
        return True
    return bool(_NZ_LOCATION_PATTERN.search(location))


def _epoch_ms_to_iso(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def fetch_greenhouse(slug: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"greenhouse:{slug}:{j.get('id')}",
            "title": j.get("title"),
            "location": (j.get("location") or {}).get("name"),
            "department": None,
            "url": j.get("absolute_url"),
            "posted_at": j.get("first_published") or j.get("updated_at"),
        })
    return jobs


def fetch_lever(slug: str):
    url = f"https://api.lever.co/v0/postings/{slug}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT, params={"mode": "json"},
                      headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data:
        cats = j.get("categories", {}) or {}
        jobs.append({
            "id": f"lever:{slug}:{j.get('id')}",
            "title": j.get("text"),
            "location": cats.get("location"),
            "department": cats.get("team") or cats.get("department"),
            "url": j.get("hostedUrl"),
            "posted_at": _epoch_ms_to_iso(j.get("createdAt")),
        })
    return jobs


def fetch_ashby(slug: str):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or j.get("locationName")
        jobs.append({
            "id": f"ashby:{slug}:{j.get('id')}",
            "title": j.get("title"),
            "location": loc,
            "department": j.get("department") or j.get("team"),
            "url": j.get("jobUrl") or j.get("applyUrl"),
            "posted_at": j.get("publishedAt") or j.get("updatedAt"),
        })
    return jobs


def fetch_workable(slug: str):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT, params={"details": "true"},
                      headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    raw_jobs = data.get("jobs", [])

    # Some Workable accounts publish the same opening to multiple locations,
    # reusing the same shortcode for every location variant. Only disambiguate
    # the id with the location when a shortcode actually repeats, so most jobs
    # keep a stable id across refreshes.
    shortcode_counts = Counter(j.get("shortcode") or j.get("id") for j in raw_jobs)

    jobs = []
    for j in raw_jobs:
        loc = j.get("location")
        if isinstance(loc, dict):
            loc = loc.get("location_str") or ", ".join(
                filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])
            )
        if not loc:
            # Current Workable API puts location on top-level city/state/country
            # fields (or the locations[] array) instead of a "location" key.
            first_loc = (j.get("locations") or [{}])[0]
            loc = ", ".join(filter(None, [
                j.get("city") or first_loc.get("city"),
                j.get("state") or first_loc.get("region"),
                j.get("country") or first_loc.get("country"),
            ])) or None
        base_id = j.get("shortcode") or j.get("id")
        job_id = f"workable:{slug}:{base_id}"
        if shortcode_counts[base_id] > 1:
            job_id += f":{loc or 'unspecified'}"
        jobs.append({
            "id": job_id,
            "title": j.get("title"),
            "location": loc,
            "department": j.get("department"),
            "url": j.get("url") or j.get("shortlink"),
            "posted_at": j.get("published_on") or j.get("created_at"),
        })
    return jobs


def fetch_bamboohr(slug: str):
    url = f"https://{slug}.bamboohr.com/careers/list"
    r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("result", []):
        loc = j.get("location") or {}
        ats_loc = j.get("atsLocation") or {}
        location = loc.get("city") or ", ".join(
            filter(None, [ats_loc.get("city"), ats_loc.get("state"), ats_loc.get("country")])
        ) or None
        job_id = j.get("id")
        jobs.append({
            "id": f"bamboohr:{slug}:{job_id}",
            "title": j.get("jobOpeningName"),
            "location": location,
            "department": j.get("departmentLabel"),
            "url": f"https://{slug}.bamboohr.com/careers/{job_id}",
            "posted_at": None,
        })
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "bamboohr": fetch_bamboohr,
    "workable": fetch_workable,
}


def fetch_company(company: dict):
    """Fetch a single company and return its normalized job list. Raises on failure; the caller handles it."""
    fn = FETCHERS.get(company["ats"])
    if not fn:
        raise ValueError(f"Unsupported ATS type: {company['ats']}")
    raw_jobs = fn(company["slug"])
    out = []
    for j in raw_jobs:
        title = j.get("title") or "(Untitled)"
        out.append({
            **j,
            "title": title,
            "company": company["name"],
            "ats": company["ats"],
            "is_intern": is_internship_title(title),
            "is_nz": is_nz_location(j.get("location")),
        })
    return out


def fetch_all(companies: list, max_workers: int = 8):
    """Fetch every company in parallel (network-bound I/O). A single company's
    failure doesn't affect the others."""
    all_jobs = []
    errors = []
    if not companies:
        return all_jobs, errors
    with ThreadPoolExecutor(max_workers=min(max_workers, len(companies))) as pool:
        future_to_company = {pool.submit(fetch_company, c): c for c in companies}
        for future in as_completed(future_to_company):
            c = future_to_company[future]
            try:
                all_jobs.extend(future.result())
            except Exception as e:
                errors.append({"company": c["name"], "error": str(e)})
    return all_jobs, errors
