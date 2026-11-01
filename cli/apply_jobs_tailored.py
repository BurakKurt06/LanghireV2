"""
Script 3: Apply to collected jobs with tailored resumes.
For each job, fetches the job description from LinkedIn, finds which of the
resume's own skills it mentions, reorders the resume's skills line to lead with
them (never adding a skill), then applies with that resume.

Usage:
  uv run python apply_jobs_tailored.py                          # 1 worker, easy apply
  uv run python apply_jobs_tailored.py --workers 3              # 3 concurrent workers
  uv run python apply_jobs_tailored.py --workers 2 --no-easy-apply
  uv run python apply_jobs_tailored.py --limit 10
"""
import os as _os

# browser-use sends each run's task (with the resume), the values typed into
# forms and the pages visited to its analytics service unless this is off.
_os.environ["ANONYMIZED_TELEMETRY"] = "false"
_os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"

import argparse
import asyncio
import json
import re
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # pymupdf
from browser_use import Agent, BrowserSession

import backend.core.shared_config as config
from backend.core.shared_config import (
    BASE_DIR,
    JOBS_FILE, QA_FILE, CANDIDATE_PROFILE, LOGS_DIR, RESUME_PATH, RESUMES_DIR,
    BLOCKED_DOMAINS, AWS_PROFILE, AWS_REGION, MODEL_ID,
    load_json, refresh_credentials, credential_refresh_loop,
    read_jobs, update_job, get_memory_store,
)

_SKILL_SEPARATORS = re.compile(r"\s*(?:·|•|\||,|;|\n)\s*", re.I)


def _skills_block(page):
    """The text block listing skills: the one right after a short 'Skills' heading (None if there is none)."""
    blocks = [b for b in page.get_text("dict")["blocks"] if "lines" in b]
    texts = ["\n".join("".join(span["text"] for span in line["spans"]) for line in b["lines"]).strip() for b in blocks]
    for i, text in enumerate(texts[:-1]):
        if re.fullmatch(r"(?:(?:technical|core|key|professional|computer)\s+)?skills(?:\s*(?:&|and)\s*(?:tools|technologies))?\s*:? *",
                        text, re.I) and len(_split_skills(texts[i + 1])) >= 2:
            return blocks[i + 1], texts[i + 1]
    return None


def _split_skills(text: str) -> list[str]:
    items = [i.strip(" .") for i in _SKILL_SEPARATORS.split(text or "")]
    return list(dict.fromkeys(i for i in items if i))


def resume_skills(resume_path: str) -> list[str]:
    """The skills the resume itself lists under its Skills heading ([] when there's no such section)."""
    try:
        doc = fitz.open(resume_path)
    except Exception:
        return []
    try:
        for page in doc:
            found = _skills_block(page)
            if found:
                return _split_skills(found[1])
        return []
    finally:
        doc.close()


async def fetch_job_description(job: dict, worker_id: int) -> str:
    """Read the public posting anonymously; no unguarded signed-in browser agent."""
    url = job["url"]

    # If we already fetched and cached the description, reuse it
    cached = read_jobs().get(url, {}).get("description")
    if cached:
        return cached

    try:
        from core import job_page
    except ImportError:
        from backend.core import job_page
    posting = await asyncio.to_thread(job_page.fetch, url)
    description = posting.description if posting is not None else ""

    # Cache description in jobs.json for reuse
    if description:
        update_job(url, description=description)

    return description


def extract_skills_from_description(description: str, job_title: str, skills: list[str] | None = None) -> list[str]:
    """The resume's own skills that the job description mentions (most relevant first).

    Never adds a skill: this used to ask a model for 'plausible' extra skills, and
    then used a hardcoded list (someone else's skills). Now it's the skills the
    user's own resume lists (`skills`, read from it when not given).
    """
    text = (description or "").lower()
    skills = resume_skills(RESUME_PATH) if skills is None else skills
    return [skill for skill in skills if skill.lower() in text]


def customize_resume(job: dict, extra_skills: list[str]) -> str:
    """A copy of the resume whose Skills line lists the same skills, the job's first. Returns its path.

    Only the resume's own skills are used (read from its Skills section): nothing is
    added or dropped. Without a clear Skills section the resume is copied unchanged.
    """
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)

    # Build safe filename from job details (empty strings need fallback too)
    company = re.sub(r"[^\w\s-]", "", job.get("company") or "unknown").strip().replace(" ", "_")[:30]
    title = re.sub(r"[^\w\s-]", "", job.get("title") or "unknown").strip().replace(" ", "_")[:30]
    # Build filename from candidate profile name
    from backend.core.shared_config import load_json, CANDIDATE_PROFILE
    profile = load_json(CANDIDATE_PROFILE, {})
    candidate_name = config.safe_folder_name(profile.get("name") or "Candidate")
    filename = f"{candidate_name}_{company}_{title}.pdf"
    output_path = RESUMES_DIR / filename

    own = resume_skills(RESUME_PATH)
    own_lower = {s.lower() for s in own}
    first = [skill for skill in extra_skills if skill.lower() in own_lower]
    all_skills = first + [skill for skill in own if skill not in first]
    new_skills_text = " · ".join(all_skills)

    doc = fitz.open(RESUME_PATH)
    original = doc.tobytes()
    reordered = False
    for page in doc if all_skills else []:
        found = _skills_block(page)
        if found:
            block = found[0]
            block_rect = fitz.Rect(block["bbox"])
            first_span = block["lines"][0]["spans"][0]
            font_size = first_span["size"]
            c = first_span["color"]
            font_color = ((c >> 16) & 0xFF) / 255, ((c >> 8) & 0xFF) / 255, (c & 0xFF) / 255

            # Redact old skills text
            page.add_redact_annot(block_rect, fill=(1, 1, 1))
            page.apply_redactions()

            # Expand rect height to fit more skills (left column has room below)
            expanded_rect = fitz.Rect(
                block_rect.x0, block_rect.y0,
                block_rect.x1, min(block_rect.y1 + 40, page.rect.y1),
            )
            # Never draw into the next section, even if the replacement needs more lines.
            for other in page.get_text("dict")["blocks"]:
                rect = fitz.Rect(other["bbox"])
                if rect.y0 >= block_rect.y1 and rect.x0 < block_rect.x1 and rect.x1 > block_rect.x0:
                    expanded_rect.y1 = min(expanded_rect.y1, rect.y0 - 2)

            # Use the full Bitter-Regular font (embedded subset lacks glyphs for new text)
            bitter_font = Path(__file__).parent / "fonts" / "Bitter-Regular.ttf"

            if bitter_font.exists():
                page.insert_font(fontname="Bitter", fontfile=str(bitter_font))
                remaining = page.insert_textbox(
                    expanded_rect,
                    new_skills_text,
                    fontsize=font_size,
                    fontname="Bitter",
                    color=font_color,
                    align=fitz.TEXT_ALIGN_LEFT,
                )
            else:
                remaining = page.insert_textbox(
                    expanded_rect,
                    new_skills_text,
                    fontsize=font_size,
                    fontname="helv",
                    color=font_color,
                    align=fitz.TEXT_ALIGN_LEFT,
                )
            rendered = " ".join(page.get_text().split())
            if remaining < 0 or any(" ".join(skill.split()) not in rendered for skill in own):
                # insert_textbox does not insert on overflow; restore before saving.
                doc.close()
                doc = fitz.open(stream=original, filetype="pdf")
                first, all_skills = [], own
                print("    📄 Skills replacement could not preserve the content/layout; keeping the original resume.")
            else:
                reordered = True
            break

    doc.save(str(output_path))
    doc.close()

    # Also save a metadata sidecar so we know what skills were added
    meta_path = output_path.with_suffix(".json")
    meta = {
        "job_url": job.get("url", ""),
        "job_title": job.get("title", ""),
        "company": job.get("company", ""),
        "resume_skills": own,
        "emphasised_skills": first,
        "all_skills": all_skills,
        "reordered": reordered,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"    📄 Created tailored resume: {filename} (skills reordered, job's first: {', '.join(first) or 'none'})")
    return str(output_path)


async def apply_to_job(job: dict, profile: dict, qa: dict, applied_labels: list[str], easy_apply: bool, worker_id: int) -> str:
    """Apply to a single job with a tailored resume. Returns final status."""
    from cli.apply_jobs import apply_to_job as _base_apply

    url = job["url"]
    title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")

    # --- Step 1: Fetch job description ---
    print(f"  📋 [W{worker_id}] Fetching job description...")
    description = ""
    try:
        description = await fetch_job_description(job, worker_id)
        if description:
            print(f"  📋 [W{worker_id}] Got description ({len(description)} chars)")
    except Exception as e:
        print(f"  ⚠️  [W{worker_id}] Description fetch failed: {e}")

    # --- Step 2: Extract skills and customize resume ---
    resume_path = None  # None = use default in base apply
    try:
        if description:
            extra_skills = extract_skills_from_description(description, title)
            if extra_skills:
                resume_path = customize_resume(job, extra_skills)
                update_job(url, tailored_resume=resume_path, added_skills=extra_skills)
            else:
                print(f"  ℹ️  [W{worker_id}] No extra skills found, using base resume")
    except Exception as e:
        print(f"  ⚠️  [W{worker_id}] Resume customization failed: {e}, using base resume")

    # --- Step 3: Delegate to shared apply logic ---
    return await _base_apply(job, profile, qa, applied_labels, easy_apply, worker_id, resume_path_override=resume_path)


async def worker(name: str, worker_id: int, queue: asyncio.Queue, profile: dict, qa: dict, applied_labels: list, easy_apply: bool, stats: dict, cancel_flag: dict | None = None):
    """Worker that pulls jobs from queue and applies with tailored resumes."""
    while True:
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print(f"  🛑 [{name}] Stop requested — halting")
            break
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        status = await apply_to_job(job, profile, qa, applied_labels, easy_apply, worker_id)
        stats[status] = stats.get(status, 0) + 1

        # On retry, put back in queue
        if status == "retry":
            queue.put_nowait(job)

        queue.task_done()


async def main():
    """Main entry point — parse args, filter jobs, dispatch workers."""
    parser = argparse.ArgumentParser(description="Apply to collected jobs with tailored resumes")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent workers")
    parser.add_argument("--limit", type=int, help="Max jobs to process")
    parser.add_argument("--easy-apply", dest="easy_apply", action="store_true", default=True)
    parser.add_argument("--no-easy-apply", dest="easy_apply", action="store_false")
    parser.add_argument("--run-mode", choices=("auto", "trial", "training"), default="trial")
    args = parser.parse_args()
    from cli.apply_jobs import set_run_mode
    set_run_mode(args.run_mode)
    try:
        from core import request_gate
    except ImportError:
        from backend.core import request_gate
    request_gate.use_console_reviews()

    jobs = load_json(JOBS_FILE, {})
    profile = load_json(CANDIDATE_PROFILE, {})
    qa = load_json(QA_FILE, {})
    LOGS_DIR.mkdir(exist_ok=True)
    RESUMES_DIR.mkdir(exist_ok=True)

    # Filter pending jobs by type
    pending = [
        j for j in jobs.values()
        if j.get("status") == "pending"
        and (j.get("easy_apply") is True) == args.easy_apply
    ]

    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        print("No pending jobs to apply to. Run collect_jobs.py first.")
        return

    applied_labels = [
        f"{j.get('title', '')} at {j.get('company', '')}"
        for j in jobs.values() if j.get("status") == "applied"
    ]

    mode = "Easy Apply" if args.easy_apply else "Non-Easy Apply"
    print(f"🎯 Applying to {len(pending)} {mode} jobs with {args.workers} worker(s)")
    print(f"📄 Resumes will be saved to: {RESUMES_DIR}/\n")

    queue = asyncio.Queue()
    for job in pending:
        queue.put_nowait(job)

    stats = {}
    num_workers = min(args.workers, len(pending))

    # Background credential refresh every 14 min
    cred_task = asyncio.create_task(credential_refresh_loop(14))

    workers = []
    for i in range(num_workers):
        if i > 0:
            await asyncio.sleep(5)  # stagger browser launches
        workers.append(
            asyncio.create_task(worker(f"W{i+1}", i+1, queue, profile, qa, applied_labels, args.easy_apply, stats))
        )
    await asyncio.gather(*workers)
    cred_task.cancel()

    print(f"\n{'='*60}")
    print(f"Results: {stats}")
    total_applied = sum(1 for j in load_json(JOBS_FILE, {}).values() if j.get("status") == "applied")
    print(f"Total applied across all runs: {total_applied}")
    print(f"Tailored resumes saved in: {RESUMES_DIR}/")

    # Memory stats
    mem_stats = get_memory_store().get_stats()
    print(f"🧠 Agent memory: {mem_stats['total_memories']} memories across {mem_stats['unique_domains']} domains")


if __name__ == "__main__":
    asyncio.run(main())
