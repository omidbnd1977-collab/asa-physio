#!/usr/bin/env python3
"""Layer 10 — turn site/ into public/ with content-hashed asset names.

Because every asset URL contains its own content hash, a deploy never needs a CDN
purge: new bytes get a new URL. Only the HTML entry points are revalidated, and they
are served with `max-age=0, must-revalidate`. The rule is written down in docs/CACHE.md.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import posixpath
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "site"
OUT = ROOT / "public"
HASHED_EXT = {".css", ".js", ".jpg", ".jpeg", ".png", ".svg", ".webp", ".woff2", ".ico"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def main() -> int:
    if not SRC.exists():
        print(f"error: {SRC} not found — run `python3 work/build_pages.py` first", file=sys.stderr)
        return 1
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    manifest: dict[str, str] = {}

    # 1) copy + hash every non-HTML asset
    for path in sorted(SRC.rglob("*")):
        if path.is_dir() or path.suffix.lower() == ".html":
            continue
        rel = path.relative_to(SRC).as_posix()
        data = path.read_bytes()
        if path.suffix.lower() in HASHED_EXT:
            new_name = f"{path.stem}.{digest(data)}{path.suffix}"
            new_rel = (path.parent.relative_to(SRC) / new_name).as_posix().lstrip("./")
        else:
            new_rel = rel
        dest = OUT / new_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        manifest[rel] = new_rel

    # 2) rewrite references inside CSS, then re-hash the CSS itself
    for rel, new_rel in list(manifest.items()):
        if not rel.endswith(".css"):
            continue
        f = OUT / new_rel
        text = f.read_text(encoding="utf-8")
        for src_rel, dst_rel in manifest.items():
            if src_rel == rel:
                continue
            base_src = pathlib.PurePosixPath(src_rel).name
            base_dst = pathlib.PurePosixPath(dst_rel).name
            text = text.replace(base_src, base_dst)
        new_data = text.encode()
        rehashed = f.with_name(f"{pathlib.PurePosixPath(rel).stem}.{digest(new_data)}{f.suffix}")
        f.unlink()
        rehashed.write_bytes(new_data)
        manifest[rel] = rehashed.relative_to(OUT).as_posix()

    # 3) rewrite HTML
    pattern = re.compile(r'(?P<attr>(?:href|src)\s*=\s*")(?P<url>[^"]+)"')
    for path in sorted(SRC.rglob("*.html")):
        rel = path.relative_to(SRC).as_posix()
        page_dir = posixpath.dirname(rel) or "."
        html = path.read_text(encoding="utf-8")

        def sub(m: re.Match[str], page_dir: str = page_dir) -> str:
            url = m.group("url")
            if url.startswith(("http://", "https://", "data:", "tel:", "mailto:", "#")):
                return m.group(0)
            parts = re.match(r"^([^?#]*)(\?[^#]*)?(#.*)?$", url)
            path_part, query, frag = parts.group(1), parts.group(2) or "", parts.group(3) or ""
            clean = path_part.lstrip("./")
            if clean in manifest:
                # Relative, not root-absolute: the same bundle is served from "/" on
                # Render and from "/asa-physio/" on GitHub Pages. A leading slash only
                # works for the first of those and 404s on the second.
                target = posixpath.relpath(manifest[clean], page_dir)
                return f'{m.group("attr")}{target}{query}{frag}"'
            return m.group(0)

        html = pattern.sub(sub, html)
        dest = OUT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        manifest[rel] = rel

    (OUT / "asset-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\n", encoding="utf-8"
    )
    hashed = sum(1 for k, v in manifest.items() if k != v)
    total_kb = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1024
    print(
        f"public/: {len(list(OUT.rglob('*')))} files, {total_kb:.0f} KB, "
        f"{hashed} fingerprinted assets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
