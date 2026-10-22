#!/usr/bin/env python3
"""Daily sync of the Tongji faculty profile page.

Fetches https://celiang.tongji.edu.cn/info/1300/2388.htm, parses its sections,
compares them against the previous snapshot, and when the content changed:

1. updates _data/tongji.yml (rendered on the website),
2. refreshes _data/tongji/snapshot.json,
3. writes a per-section diff report to _data/tongji/sync_report.md.

Run locally:  python scripts/sync_tongji.py
Deps:         pip install requests beautifulsoup4 pyyaml
Designed to run unattended via .github/workflows/sync-tongji.yml (daily cron).
"""

import difflib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

SOURCE_URL = "https://celiang.tongji.edu.cn/info/1300/2388.htm"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "_data"
SNAPSHOT_PATH = DATA_DIR / "tongji" / "snapshot.json"
REPORT_PATH = DATA_DIR / "tongji" / "sync_report.md"
TONGJI_YML_PATH = DATA_DIR / "tongji.yml"

# Section headers on the source page (normalized text -> yaml key).
HEADERS = {
    "简介": "intro",
    "研究兴趣": "interests",
    "主要科研项目": "projects",
    "主要论文": "publications",
    "讲授课程": "teaching",
}
# The bio paragraph is followed by a recruitment pitch that we track separately.
RECRUITMENT_TRIGGER = "如果你对"
SECTION_ORDER = ["intro", "recruitment", "interests", "projects", "publications", "teaching"]


def fetch_html() -> str:
    try:
        resp = requests.get(
            SOURCE_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; profile-sync/1.0)"},
            timeout=60,
        )
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception:
        # Fallback to stdlib (avoids local proxy/urllib3 quirks)
        import urllib.request

        req = urllib.request.Request(
            SOURCE_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; profile-sync/1.0)"},
        )
        raw = urllib.request.urlopen(req, timeout=60).read()
        for enc in ("utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")


def norm(text: str) -> str:
    """Collapse whitespace and strip for stable comparison."""
    return re.sub(r"\s+", " ", text).strip()


def extract_blocks(html: str):
    """Yield (text, href) for each content block in document order.

    Blocks are <p>/<li>/<td> elements not nested inside another <li>/<td>,
    which avoids duplicates from the CMS's messy markup (p inside li, etc.).
    """
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one("div.v_news_content")
        or soup.select_one("div#vsb_content")
        or soup.select_one("div.teacher-content")
    )
    if container is None:
        raise RuntimeError("Could not locate the article content container")

    blocks = []
    for el in container.descendants:
        if getattr(el, "name", None) not in ("p", "li", "td"):
            continue
        if el.find_parent("li") is not None or el.find_parent("td") is not None:
            continue
        text = norm(el.get_text(" ", strip=True))
        if not text:
            continue
        a = el.find("a", href=True)
        blocks.append((text, a["href"] if a else None))
    return blocks


def parse_sections(blocks):
    """Group blocks into sections keyed by yaml keys; each section is a list
    of {text, url} entries."""
    sections = {}
    current = None
    for text, href in blocks:
        header = norm(text).rstrip("：:")
        if header in HEADERS:
            current = HEADERS[header]
            sections[current] = []
            continue
        if norm(text).startswith(RECRUITMENT_TRIGGER):
            current = "recruitment"
            sections[current] = []
            # skip noisy link-back cell
        if "English Profile" in text:
            continue
        if current is None:
            current = "preamble"
            sections[current] = []
        sections[current].append({"text": text, "url": href})
    return sections


def section_lines(sections):
    """Plain text lines per section, used for diffing and report."""
    return {
        key: [entry["text"] for entry in entries]
        for key, entries in sections.items()
        if entries
    }


def load_snapshot():
    if SNAPSHOT_PATH.exists():
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return {"sections": {}}


def diff_report(old_lines, new_lines):
    lines = []
    for key in SECTION_ORDER:
        old, new = old_lines.get(key, []), new_lines.get(key, [])
        if old == new:
            continue
        lines.append(f"### {key}")
        diff = difflib.unified_diff(old, new, lineterm="", n=1)
        for d in list(diff)[2:]:
            if d.startswith(("+++", "---")):
                continue
            lines.append(f"  {d}")
        lines.append("")
    return "\n".join(lines).strip()


def write_yml(sections, fetched_at):
    data = {
        "fetched_at": fetched_at,
        "source_url": SOURCE_URL,
        "sections": {k: sections[k] for k in SECTION_ORDER if sections.get(k)},
    }
    TONGJI_YML_PATH.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )


def main():
    html = fetch_html()
    sections = parse_sections(extract_blocks(html))
    new_lines = section_lines(sections)

    if not any(new_lines.values()):
        print("Parsing produced no content; aborting without changes.")
        sys.exit(1)

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    old_snapshot = load_snapshot()
    old_lines = old_snapshot.get("sections", {})

    report = diff_report(old_lines, new_lines)

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps({"fetched_at": fetched_at, "sections": new_lines}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    write_yml(sections, fetched_at)

    status = "CHANGED" if report else "NO-CHANGE"
    REPORT_PATH.write_text(
        "# Tongji profile sync report\n\n"
        f"- Source: {SOURCE_URL}\n"
        f"- Last run: {fetched_at}\n"
        f"- Status: {status}\n\n"
        + ("## Changes (previous -> current)\n\n" + report if report else
           "No content differences compared with the previous snapshot.\n"),
        encoding="utf-8",
    )

    if report:
        print(f"Content changed ({fetched_at}). _data/tongji.yml updated.")
        print(report)
    else:
        print(f"No changes detected ({fetched_at}).")


if __name__ == "__main__":
    main()
