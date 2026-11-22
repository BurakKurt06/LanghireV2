"""Raise the app's version everywhere it is written (README → Releasing lists the files).

Usage: python scripts/bump_version.py 1.3.2 1.3.3
Only the app's own version lines change; package-lock.json's first two (the root package).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace(path: str, old: str, new: str, count: int = 0) -> int:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    changed, n = re.subn(re.escape(old), new, text, count=count)
    if n:
        file.write_text(changed, encoding="utf-8", newline="")
    return n


def main(old: str, new: str) -> None:
    done = {
        "package.json": replace("package.json", f'"version": "{old}"', f'"version": "{new}"', 1),
        "package-lock.json": replace("package-lock.json", f'"version": "{old}"', f'"version": "{new}"', 2),
        "src-tauri/tauri.conf.json": replace("src-tauri/tauri.conf.json", f'"version": "{old}"', f'"version": "{new}"', 1),
        "backend/main.py": replace("backend/main.py", f'"{old}"', f'"{new}"'),
        "src/main.tsx": replace("src/main.tsx", f"langhirev2@{old}", f"langhirev2@{new}"),
    }
    for path, n in done.items():
        print(f"{path}: {n} line(s)")
    if not all(done.values()):
        sys.exit("some file had no version line to change")


if __name__ == "__main__":
    main(*sys.argv[1:3])
