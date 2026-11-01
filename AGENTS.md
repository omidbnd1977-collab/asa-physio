# Working notes for this repository

## Base44 sandbox preview

```bash
docker compose -f docker-compose.base44.yml up -d --build   # web on host port 3000
```

- One service, `web`, from `Dockerfile.base44` (Python deps only — no source is baked in).
  The repository is bind-mounted at `/srv`, deps live in the image, and uvicorn runs with
  `--reload` on `api/`. There is no Node/JS toolchain: the public site is plain HTML/CSS/JS.
- `STATIC_DIR=/srv/site` in the preview, so pages are served straight from the source
  tree — editing `site/*.css|js|html` is visible on reload with no build step.
  Production still uses `public/`, produced by `python build_static.py`.
- `/` serves `site/dental.html` (the dental template) when that file exists and falls back
  to `index.html`. `api/main.py` handles this in the catch-all static route, not in the
  `root` route — the catch-all is registered first and wins for `/`.
- Preview-only override: when `BASE44_PREVIEW_MODE` is exactly `"1"`, non-fingerprinted
  static files are served `Cache-Control: no-store`. Without it, assets get
  `max-age=3600` and CSS edits appear not to apply. Unset/other values keep production
  caching untouched.
- SQLite (`data/preview.db`) is created and migrated by the app's lifespan; no DB service.

## Checks

```bash
docker compose -f docker-compose.base44.yml exec -T web sh -c \
  'pip install -q -r requirements-dev.txt && python -m pytest -q tests'   # 194 tests
docker compose -f docker-compose.base44.yml exec -T web sh -c 'python build_static.py'
```

`build_static.py` fingerprints assets and rewrites relative references **for pages and
assets at the `site/` root only** — a page in a sub-directory would keep un-hashed
`style.css` links and ship broken. Keep new templates as `site/<name>.html` +
`site/<name>.css|js`. Fonts are referenced from CSS by basename
(`assets/fonts/Vazirmatn-Bold.woff2`) so the rewriter can swap in the hashed name.

The dental template (`site/dental.html|css|js`) has no images on purpose: the crystal
tooth and clinic showcase are inline SVG/CSS, so nothing external is fetched and the
page still renders if images are blocked.
