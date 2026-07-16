"""
A small helper for adding more companies to companies.json.

Usage:
    python find_slug.py https://some-companys-careers-page-url

It fetches the careers page HTML and tries to detect traces of Greenhouse /
Lever / Ashby / Workable / BambooHR, then verifies the guess by actually
calling that ATS's public API before printing a config snippet you can
paste straight into companies.json.

If detection fails, the company is probably using an in-house ATS (many
large companies use Workday, for example) — this project doesn't support
that yet, so skip it or handle it manually.
"""

import re
import sys

import requests

import fetchers

# Generic subdomains that show up in shared scripts/widgets and aren't
# actually the company's own BambooHR slug.
BAMBOOHR_SUBDOMAIN_BLOCKLIST = {"www", "app", "api", "cdn", "static", "assets"}

SIGNATURES = [
    ("greenhouse", r"boards\.greenhouse\.io/([\w-]+)"),
    ("lever", r"jobs\.lever\.co/([\w.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)"),
    ("workable", r"apply\.workable\.com/([\w-]+)"),
    ("bamboohr", r"([\w-]+)\.bamboohr\.com"),
]


def detect(url: str):
    """Return a list of (ats, slug) candidates found in the page, in signature order."""
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    html = r.text
    candidates = []
    for ats, pattern in SIGNATURES:
        for m in re.findall(pattern, html):
            if ats == "bamboohr" and m.lower() in BAMBOOHR_SUBDOMAIN_BLOCKLIST:
                continue
            candidates.append((ats, m))
            break  # first valid match per ATS type is enough
    return candidates


def verify(ats: str, slug: str):
    """Actually call the ATS's public API to confirm the slug is real. Returns (ok, detail)."""
    fetch_fn = fetchers.FETCHERS.get(ats)
    if not fetch_fn:
        return False, f"no fetcher registered for {ats}"
    try:
        jobs = fetch_fn(slug)
        return True, f"{len(jobs)} job(s) found"
    except Exception as e:
        return False, str(e)


def print_snippet(ats: str, slug: str):
    print("\nAdd this to the array in companies.json:\n")
    print('  {')
    print(f'    "name": "Company name (fill this in)",')
    print(f'    "ats": "{ats}",')
    print(f'    "slug": "{slug}"')
    print('  }')


def main():
    if len(sys.argv) < 2:
        print("Usage: python find_slug.py <company careers page URL>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Checking: {url}")
    try:
        candidates = detect(url)
    except Exception as e:
        print(f"Failed to fetch: {e}")
        sys.exit(1)

    if not candidates:
        print("\nNo Greenhouse / Lever / Ashby / Workable / BambooHR traces detected.")
        print("This company may use an in-house ATS or another provider - this project doesn't support auto-fetching for it yet.")
        return

    verified = None
    for ats, slug in candidates:
        print(f"\nDetected ATS type: {ats}")
        print(f"slug: {slug}")
        ok, detail = verify(ats, slug)
        if ok:
            print(f"[OK] Verified against the live API - {detail}")
            verified = (ats, slug)
            break
        else:
            print(f"[FAIL] Could not verify ({detail}) - trying next candidate if any")

    if verified:
        print_snippet(*verified)
    else:
        ats, slug = candidates[0]
        print(f"\nNone of the {len(candidates)} candidate(s) could be verified against a live API.")
        print("Printing the first guess anyway - double-check it manually before using it:")
        print_snippet(ats, slug)


if __name__ == "__main__":
    main()
