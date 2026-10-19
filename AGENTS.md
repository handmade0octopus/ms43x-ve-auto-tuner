# MS43 VE Auto-Tune — Agent Guide

A local/web tool that auto-tunes the eight MAP-based VE tables (VANOS overlap
steps 1–8) of a BMW Siemens MS43 ECU running the MS43X custom firmware
(speed-density mode), from CSV datalogs. Single FastAPI backend + Datastar
frontend; ships as a Windows exe, runs from source, or as a Docker container.

Never commit ROMs, XDFs, datalogs, real file paths or personal/operational
notes — the repo must stay free of anything that ties it to a specific
person, car or server.

## Layout

```
server.py        FastAPI app: state, endpoints, HTML fragments, modes, entry point
core/xdf.py      XDF parsing, VE-table-family discovery, shared-axis resolution
core/logs.py     CSV log parsing, channel auto-guessing, MAP scale guess
core/tuner.py    3D-histogram tuner: binning, correction modes, apply, write_binary
core/robustio.py  retry-hardened file IO (Windows AV/indexer locks)
web/index.html   single-page UI (server-rendered markers + Datastar attributes)
web/static/      style.css, vendored datastar.js (v1.0.4 — do not upgrade casually)
deploy/          Dockerfile, docker-compose.yml, easy build scripts
legacy/          superseded PyQt desktop version, kept for reference only
```

## Non-negotiable conventions

- **Datastar v1 syntax only**: signals `$name`, actions `@post()/@get()`,
  colon modifiers (`data-on:click`, `data-attr:disabled`, `data-class:x`).
  Old v0 syntax (`data-on-click`, bare `post(...)`) is broken.
- Every `patch_elements` MUST pass `mode='inner'` — the default morph corrupts
  this DOM.
- The page is a static template with `<!--SERVER:name-->` markers; GET `/`
  replaces each marker with a server-rendered fragment. The server's `STATE`
  is authoritative; client signals only carry inputs.
- `data-bind` + `data-on:change` ordering works (bind fires before the post).
- File IO goes through `core/robustio.py` (retries WinError 6/32-style locks).
- Run Python with UTF-8 (`-X utf8`). Do not write files with PowerShell
  `Set-Content -Encoding UTF8` (BOM + mojibake).

## Runtime modes

- **desktop** (default): pywebview app window; closing it stops the server.
  Native open/save dialogs run in a short-lived subprocess
  (`--pick <kind> --out <file>` / `--save <src> --out <file>`) because tkinter
  needs its own main thread.
- **web** (`VE_WEB=1` / `--web`): browser-only. No dialogs; uploads via
  `/api/upload` (drag & drop or hidden file inputs), downloads via
  `/api/download/*`. **Per-browser sessions**: a `ve_sid` cookie selects an
  `AppState` held in RAM (LRU 16, 24 h TTL) through a ContextVar proxy
  (`STATE` reads route to the request's session). Nothing is ever written to
  disk in web mode — no state.json, no uploads dir; the container is
  `read_only: true`.
- `--browser` (default browser instead of the window), `--no-ui` (headless).

## State rules (break these and things fail subtly)

- ALL AppState defaults belong in `AppState.__init__`. Module-level
  `STATE.x = ...` assignments do NOT reach new sessions (they only mutate the
  desktop default instance) — this caused a real bug once.
- `_sync_channels` only applies client channel overrides when the browser's
  `channelsLog` fingerprint (rendered inside `chanmap_frag`) matches the hash
  of the current log header. A stale tab must never clobber server-side
  channel detection. Empty/out-of-range values are ignored; `-1` is honored
  (explicit "not available").
- `state_store_path()` honors `VE_DATA_DIR`; desktop persistence only.
- Uploads capped by `VE_MAX_UPLOAD_MB` (default 256).

## Tuning semantics (do not change casually)

- Correction modes: `afr` = AFR/target; `afr+trim` = **trim priority**
  (trim ≠ 0 → `1+trim/100` is the correction; trim = 0 → AFR/target);
  `trim` = `1+trim/100` only. `err` (E sums) intentionally stays the pure-AFR
  error for the parameter views.
- The *active VE table* channel is fractional: 4.3 means 70% VE4 + 30% VE5
  (samples split between adjacent tables when `blend_tables` is on).
- Only VE bytes of the output binary differ from the input — tests assert
  exact byte diffs, keep that invariant.
- Seeded default rule is `accelerator pedal (%) > 1` (throttle-on samples);
  `rules_seed_ver` v2 migrates old v1 `<`-defaults in desktop state.

## Testing

No test fixtures are committed (never commit real ROMs/logs). Local checks:
- Import server and drive endpoints in-process with a synthetic `samples`
  dict for `Tuner.analyze` (see dict keys: rpm, mapkpa, avt, afr, target, trim).
- Full-flow smoke: run `python server.py --no-ui --web --port 8123` with
  `VE_DATA_DIR` pointed at an empty temp dir, upload XDF+BIN+CSV via
  `/api/upload`, POST `/api/analyze`, GET `/api/download/bin` and diff bytes
  against the input (must differ in VE region only).
- Session isolation: repeat the flow with two cookie jars; a fresh jar must
  see a clean state and 404 downloads.
- Container smoke: `deploy/build-container.sh` then hit `http://localhost:8080`.

## Build & release

- Windows exe: `build.bat` (PyInstaller onefile, packs `web/`, collects
  uvicorn + webview submodules). Kill running `MS43VEAutoTune` processes
  before rebuilding (file locks).
- Docker: `deploy/build-container.sh|bat` (build + run on :8080).
- Releases: push a `v*` tag — the GitHub workflow builds the exe and attaches
  it to the release. Keep release notes honest about what changed.
