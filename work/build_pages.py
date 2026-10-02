#!/usr/bin/env python3
"""Validate the canonical static site source before the static asset build."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "site"

EXPECTED_PAGES = (
    "index.html",
    "services.html",
    "treatments.html",
    "about.html",
    "gallery.html",
    "faq.html",
    "contact.html",
)

EXPECTED_ASSETS = (
    "style.css",
    "assets/clinic.jpg",
    "assets/hands.jpg",
    "assets/heel.jpg",
    "assets/hero.jpg",
    "assets/knee.jpg",
    "assets/laser.jpg",
    "assets/mark.svg",
    "assets/neck.jpg",
    "assets/shoulder.jpg",
    "assets/fonts/Vazirmatn-Regular.woff2",
    "assets/fonts/Vazirmatn-Medium.woff2",
    "assets/fonts/Vazirmatn-Bold.woff2",
    "assets/fonts/Vazirmatn-ExtraBold.woff2",
)

def main() -> int:
    if not SITE.is_dir():
        print(f"error: {SITE} not found", file=sys.stderr)
        return 1

    missing = [
        rel
        for rel in (*EXPECTED_PAGES, *EXPECTED_ASSETS)
        if not (SITE / rel).is_file()
    ]

    if missing:
        print("error: missing required site files:", file=sys.stderr)
        for rel in missing:
            print(f"  - {rel}", file=sys.stderr)
        return 1

    print(
        f"site/: {len(EXPECTED_PAGES)} pages, "
        f"{len(EXPECTED_ASSETS)} required assets — ready for static build"
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
