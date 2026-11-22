"""A picture of the last page, for a job that didn't finish (failed, needs you, not confirmed).

One picture per job, taken by the browser itself when the application ends: no AI request and
no tokens. It shows on the job's card, so the user sees what the page looked like without
opening the site. A later try at the job replaces it; it is deleted when the job becomes Applied
or is removed; at most MAX_KEPT are kept (the oldest go first). Stored in
<data dir>/job_screenshots, named by a hash of the job's link (never by its title or company).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

from .config import get_data_dir

MAX_KEPT = 500
CAPTURE_SECONDS = 6          # a page that doesn't answer (a print window, a frozen tab) gives no picture
JPEG_QUALITY = 55
MAX_BYTES = 3_000_000
# The statuses whose last page is worth keeping.
KEPT_STATUSES = ("failed", "needs_you")


def folder() -> Path:
    return get_data_dir() / "job_screenshots"


def path_for(url: str) -> Path:
    return folder() / (hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:24] + ".jpg")


def exists(url: str) -> bool:
    try:
        return path_for(url).is_file()
    except OSError:
        return False


def save(url: str, picture: bytes | None) -> bool:
    """Keep `picture` as this job's last page (replacing the one before). False when nothing was kept."""
    if not url or not picture or len(picture) > MAX_BYTES or not picture.startswith(b"\xff\xd8"):
        return False
    try:
        folder().mkdir(parents=True, exist_ok=True)
        target = path_for(url)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(picture)
        tmp.replace(target)
        _prune(keep=target)
        return True
    except OSError:
        return False


def remove(url: str) -> None:
    try:
        path_for(url).unlink(missing_ok=True)
    except OSError:
        pass


def _prune(keep: Path | None = None) -> None:
    """Delete the oldest pictures beyond MAX_KEPT."""
    try:
        pictures = sorted(folder().glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in pictures[:max(0, len(pictures) - MAX_KEPT)]:
        if keep is not None and old == keep:
            continue
        try:
            old.unlink()
        except OSError:
            pass


async def capture(browser_session, seconds: float = CAPTURE_SECONDS) -> bytes | None:
    """The current page as a JPEG, or None when the browser doesn't answer in time. Never raises."""
    async def shot():
        cdp = await browser_session.get_or_create_cdp_session()
        result = await cdp.cdp_client.send.Page.captureScreenshot(
            params={"format": "jpeg", "quality": JPEG_QUALITY}, session_id=cdp.session_id)
        return base64.b64decode((result or {}).get("data") or "")

    try:
        picture = await asyncio.wait_for(shot(), seconds)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    return picture or None


def as_jpeg(picture) -> bytes | None:
    """`picture` (JPEG or PNG bytes, or either as base64 text, as browser-use keeps a step's page) as JPEG."""
    if not picture:
        return None
    try:
        raw = base64.b64decode(picture) if isinstance(picture, str) else bytes(picture)
    except (ValueError, TypeError):
        return None
    if raw.startswith(b"\xff\xd8"):
        return raw
    if not raw.startswith(b"\x89PNG"):
        return None
    try:
        import fitz  # pymupdf, already used for resumes
        pixmap = fitz.Pixmap(raw)
        if pixmap.alpha:
            pixmap = fitz.Pixmap(pixmap, 0)     # JPEG has no transparency
        return pixmap.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    except Exception:
        return None


def settle(url: str, status: str, picture) -> bool:
    """After a try at a job: keep its last page when it didn't finish (True), drop the old one when it
    did. A try that gave no picture leaves the earlier one."""
    if status in KEPT_STATUSES:
        return save(url, as_jpeg(picture))
    if status in ("applied", "closed", "blocked"):
        remove(url)
    return False
