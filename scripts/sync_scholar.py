#!/usr/bin/env python3
"""Daily sync of the Google Scholar publication list.

Fetches the Google Scholar profile publications
(https://scholar.google.com/citations?user=_Xdi93IAAAAJ) and:

1. parses every publication (title / authors / year / venue / link),
2. filters to papers from the last 10 years with "Huang" in the authors,
3. flags corresponding-author papers using the `corresponding_titles`
   whitelist carried inside _data/scholar_pubs.yml (Google Scholar does not
   mark corresponding authors, so the whitelist is the source of truth),
4. updates _data/scholar_pubs.yml only when the publication set changed,
5. writes a snapshot and a diff report to _data/scholar-meta/.

If Google Scholar blocks the request (CAPTCHA / rate limit), the script exits
gracefully and leaves the existing data untouched, so the website keeps
serving the last known good list.

Run locally:  python scripts/sync_scholar.py
Deps:         pip install requests beautifulsoup4 pyyaml
Designed to run unattended via .github/workflows/scholar-sync.yml (daily cron).
"""

import difflib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests
import yaml
from bs4 import BeautifulSoup

SCHOLAR_USER = "_Xdi93IAAAAJ"
SCHOLAR_URL = (
    "https://scholar.google.com/citations"
    f"?hl=en&user={SCHOLAR_USER}&view_op=list_works&sortby=pubdate&pagesize=100"
)
SCHOLAR_BASE = "https://scholar.google.com"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "_data"
SCHOLAR_META_DIR = DATA_DIR / "scholar-meta"
SNAPSHOT_PATH = SCHOLAR_META_DIR / "snapshot.json"
REPORT_PATH = SCHOLAR_META_DIR / "sync_report.md"
SCHOLAR_YML_PATH = DATA_DIR / "scholar_pubs.yml"

LAST_N_YEARS = 10
OWN_NAME_RE = re.compile(r"\bHuang\b", re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def normalize_title(title: str) -> str:
    """Normalize a title for whitelist matching (lowercase, alnum only)."""
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def fetch_profile() -> Optional[str]:
    """Fetch the Scholar profile HTML; None when blocked."""
    try:
        resp = requests.get(SCHOLAR_URL, headers=HEADERS, timeout=30)
    except Exception as exc:  # network errors, broken local proxies, etc.
        print(f"WARN: could not fetch Scholar profile: {exc}")
        return None
    if resp.status_code != 200:
        print(f"WARN: Scholar returned HTTP {resp.status_code}")
        return None
    html = resp.text
    blocked_markers = ("unusual traffic", "CaptchaRedirect", "gs_captcha")
    if any(marker in html for marker in blocked_markers):
        print("WARN: Google Scholar served a CAPTCHA - keeping previous data")
        return None
    return html


def parse_publications(html: str) -> List[dict]:
    """Parse the profile publication table into a list of dicts."""
    soup = BeautifulSoup(html, "html.parser")
    pubs: list[dict] = []
    for row in soup.select("tr.gsc_a_tr"):
        link = row.select_one("a.gsc_a_at")
        if link is None:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        url = SCHOLAR_BASE + href if href.startswith("/") else href

        grays = [g.get_text(" ", strip=True) for g in row.select("div.gs_gray")]
        authors = grays[0] if grays else ""
        venue = " ".join(grays[1:]) if len(grays) > 1 else ""

        year_cell = row.select_one("td.gsc_a_h")
        year = None
        if year_cell:
            match = re.search(r"\d{4}", year_cell.get_text(strip=True))
            if match:
                year = int(match.group())

        if not title or year is None:
            continue
        pubs.append(
            {
                "title": title,
                "authors": authors,
                "year": year,
                "venue": venue,
                "url": url,
            }
        )
    return pubs


def filter_and_flag(
    pubs: List[dict],
    corresponding_titles: set,
) -> Tuple[List[dict], List[dict]]:
    """Keep last-10-years papers authored by Huang; flag corresponding ones.

    Returns (kept, rejected_new) where rejected_new lists recent Huang papers
    that are NOT in the corresponding whitelist (so the owner can review them).
    """
    this_year = datetime.now(timezone.utc).year
    min_year = this_year - LAST_N_YEARS
    kept: list[dict] = []
    rejected_new: list[dict] = []
    for pub in pubs:
        if pub["year"] < min_year:
            continue
        if not OWN_NAME_RE.search(pub["authors"]):
            continue
        if normalize_title(pub["title"]) in corresponding_titles:
            pub["corresponding"] = True
            kept.append(pub)
        else:
            pub["corresponding"] = False
            rejected_new.append(pub)
    kept.sort(key=lambda p: (p["year"], p["title"]), reverse=True)
    return kept, rejected_new


def load_existing() -> dict:
    if SCHOLAR_YML_PATH.exists():
        with open(SCHOLAR_YML_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    else:
        data = {}
    data.setdefault("corresponding_titles", [])
    data.setdefault("publications", [])
    return data


def build_report(
    existing_pubs: List[dict],
    kept: List[dict],
    rejected_new: List[dict],
) -> str:
    lines = [
        f"# Scholar sync report - {utc_now()}",
        "",
        f"- Kept (corresponding, last {LAST_N_YEARS} years): {len(kept)}",
        f"- New recent papers awaiting corresponding-author review: {len(rejected_new)}",
        "",
    ]

    existing_titles = {normalize_title(p.get("title", "")) for p in existing_pubs}
    kept_titles = {normalize_title(p["title"]) for p in kept}

    added = [t for t in (p["title"] for p in kept) if normalize_title(t) not in existing_titles]
    removed = [
        p["title"]
        for p in existing_pubs
        if p.get("corresponding") and normalize_title(p.get("title", "")) not in kept_titles
    ]

    if added:
        lines += ["## Newly matched corresponding papers", ""]
        lines += [f"+ {title}" for title in added]
        lines.append("")
    if removed:
        lines += ["## No longer matched (check Scholar page)", ""]
        lines += [f"- {title}" for title in removed]
        lines.append("")
    if rejected_new:
        lines += [
            "## Recent papers NOT marked corresponding (add the title to"
            " `corresponding_titles` in _data/scholar_pubs.yml to publish them)",
            "",
        ]
        lines += [f"? {p['title']} ({p['year']}) - {p['authors']}" for p in rejected_new]
        lines.append("")
    if not (added or removed or rejected_new):
        lines += ["No changes detected.", ""]
    return "\n".join(lines)


def main() -> int:
    existing = load_existing()
    whitelist = {normalize_title(t) for t in existing.get("corresponding_titles", []) if t}

    html = fetch_profile()
    if html is None:
        return 0  # graceful: keep previous data

    scraped = parse_publications(html)
    if not scraped:
        print("WARN: no publications parsed (page layout change?) - keeping previous data")
        return 0

    kept, rejected_new = filter_and_flag(scraped, whitelist)
    print(f"Parsed {len(scraped)} publications; kept {len(kept)} corresponding papers.")

    # Only rewrite the data file when the kept set actually changed, so the
    # daily cron does not create no-op commits.
    old_key = {
        (p.get("title", ""), p.get("year"), p.get("url", ""), bool(p.get("corresponding")))
        for p in existing["publications"]
    }
    new_key = {(p["title"], p["year"], p["url"], p["corresponding"]) for p in kept}

    SCHOLAR_META_DIR.mkdir(parents=True, exist_ok=True)

    if new_key != old_key:
        added = [t for (t, *_rest) in new_key - old_key]
        print("Publication set changed; updating _data/scholar_pubs.yml")
        doc = {
            "fetched_at": utc_now(),
            "source_url": SCHOLAR_URL,
            "corresponding_titles": existing["corresponding_titles"],
            "publications": kept,
        }
        with open(SCHOLAR_YML_PATH, "w", encoding="utf-8") as fh:
            yaml.dump(
                doc,
                fh,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=1000,
            )
        report = build_report(existing["publications"], kept, rejected_new)
    else:
        print("No changes detected - data file left untouched")
        report = build_report(existing["publications"], kept, rejected_new)

    snapshot = {
        "fetched_at": utc_now(),
        "profile": SCHOLAR_URL,
        "scraped_total": len(scraped),
        "kept_corresponding": len(kept),
        "pending_review": len(rejected_new),
    }
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    REPORT_PATH.write_text(report, encoding="utf-8")

    if rejected_new:
        print(f"{len(rejected_new)} recent paper(s) awaiting corresponding-author review:")
        for pub in rejected_new:
            print(f"  - {pub['title']} ({pub['year']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
