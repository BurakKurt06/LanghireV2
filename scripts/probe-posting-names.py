"""Does a rendered page give a job's real title and employer where the plain page fetch doesn't? Read-only.

For each URL (a public job posting: one page load, no clicks, no forms, no sign-in) this compares
  static    job_page.fetch(): the app's plain HTTP read of the page (what pasted links get today)
  rendered  the same page opened in a fresh, temporary, headless browser profile (the application
            browser's network rules: Gmail and private addresses unreachable), read once it has
            stopped changing, and parsed by the same job_page.parse()
and reports per site whether each one found a real title and a real employer (not a placeholder
like "Pasted Link", "Candidate Experience site" or "Careers").

Usage:
  python scripts/probe-posting-names.py <urls.txt> [--out results.json] [--show-names] [--headed]

The URL file is never part of the repo (keep it outside, or under tmp/, which is git-ignored).
The report names sites by host only; --show-names prints the names found, for a local check.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _ok_title(posting) -> bool:
    from core import job_page
    return bool(posting and posting.title and not job_page.is_placeholder(posting.title))


def _ok_company(posting) -> bool:
    from core import job_page
    return bool(posting and posting.company and not job_page.is_placeholder_company(posting.company))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("urls")
    parser.add_argument("--out")
    parser.add_argument("--show-names", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    from core import job_page, posting_reader

    urls = [u.strip() for u in Path(args.urls).read_text(encoding="utf-8").splitlines() if u.strip()]
    rows = []
    for index, url in enumerate(urls, 1):
        row = {"index": index, "host": urlsplit(url).hostname or ""}
        began = time.monotonic()
        static = await asyncio.to_thread(job_page.fetch, url)
        row.update(static_read=static is not None, static_title=_ok_title(static), static_company=_ok_company(static),
                   static_closed=bool(static and static.closed), static_s=round(time.monotonic() - began, 1))
        began = time.monotonic()
        rendered = await posting_reader.read_rendered(url, headless=not args.headed)
        row.update(rendered_read=rendered is not None, rendered_title=_ok_title(rendered),
                   rendered_company=_ok_company(rendered), rendered_closed=bool(rendered and rendered.closed),
                   rendered_s=round(time.monotonic() - began, 1))
        if args.show_names:
            row["names"] = {"static": [getattr(static, "title", ""), getattr(static, "company", "")],
                            "rendered": [getattr(rendered, "title", ""), getattr(rendered, "company", "")]}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def count(key):
        return sum(1 for r in rows if r.get(key))
    summary = {
        "urls": len(rows),
        "static": {"title": count("static_title"), "company": count("static_company"), "closed": count("static_closed")},
        "rendered": {"title": count("rendered_title"), "company": count("rendered_company"), "closed": count("rendered_closed")},
        "either": {"title": sum(1 for r in rows if r.get("static_title") or r.get("rendered_title")),
                   "company": sum(1 for r in rows if r.get("static_company") or r.get("rendered_company"))},
    }
    print("SUMMARY", json.dumps(summary))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False),
                                  encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
