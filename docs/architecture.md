# Architecture & design

Developer reference for the TLC RF Calculator 2: how it is put together and
*why* the main decisions were made. For end-user instructions see the
[README](../README.md); for the Chemotion contract see
[eln-integration.md](eln-integration.md); for the scientific method, the
paper's Supplementary Information (SI), which this document cites by section.

## 1. What it is

A single-page browser tool for measuring **retention factor (Rf)** values on
TLC plate photos. Two trained detectors propose the baseline, solvent front
and spots; the user reviews them on a canvas; the server computes Rf, draws a
full-resolution annotated image and writes the CSV. It runs standalone or as a
Chemotion third-party app.

```text
Rf = (y_baseline − y_spot) / (y_baseline − y_front)        (SI Eq. 8; image y grows downwards)
```

A spot is kept when 0 ≤ Rf ≤ 1, tested before rounding (SI Eq. 9); the
baseline must lie below the front (SI Eq. 4).

- **Backend:** Django 5.2 LTS on Python 3.14, no database. Stateless request
  handlers plus a `media/` scratch directory that prunes itself.
- **Detection:** ONNX Runtime with two YOLOv8s models (SI §6),
  **TLC-SpotDet** (`tlc/ml_models/spot_best.onnx`) and **TLC-LineDet**
  (`line_best.onnx`). OpenCV (headless) + NumPy + Pillow for images.
- **Frontend:** vanilla JavaScript as native ES modules + CSS; no framework,
  no CDN, no bundler.

Version 2 rebuilds the original ONNX tool (v1) on a new stack and UI; its
workflow and detection logic are kept unchanged and checked plate by plate (§7).

## 2. High-level flow

```text
 browser (annotate.html + ES modules)                  Django (config/ + tlc/)
 ────────────────────────────────────                  ───────────────────────
 local photo ─► POST /upload-image/ ─────────────────► header checks → MEDIA (random name)
 ELN (?url=) ─► GET  /fetch-image/  ─────────────────► SSRF-guarded fetch → MEDIA
                GET  /serve-image/  ◄────────────────  confined to MEDIA_ROOT
 rotate / plate area / annotate on <canvas>
 Suggest Lines ► POST /detect-lines/ ────────────────► TLC-LineDet → Min/Max
 Suggest Spots ► POST /detect-spots/ (minY, maxY) ───► TLC-SpotDet in the band
 Calculate RF ─► POST /calculate_rf/ ────────────────► Rf, rows, image, CSV → MEDIA
 Download all ─► POST /download-results/ ────────────► ZIP: CSV + image + annotations
 export ───────► download, or POST multipart to the ELN URL (cross-origin)
```

Requests refer to the image by its stored name. If the upload failed, the
browser falls back to sending the image inline as a data URL.

Endpoints (`tlc/urls.py` → `tlc/views.py`): the page, `upload-image/`,
`fetch-image/`, `serve-image/`, `detect-lines/`, `detect-spots/`,
`calculate_rf/`, `download-results/`, `healthz`.

## 3. Key design choices (and why)

### No database, minimal app set

There are no models: `DATABASES = {}`, and only `django.contrib.staticfiles` and
`tlc` are installed (no admin, auth, sessions or messages). CSRF protection is
cookie-based and needs no session store. The tool is an image-to-numbers
transform; persistence is the ELN's job. Without these apps nothing signs data,
so `SECRET_KEY` is optional (random per start); the comment at the setting
explains when a fixed key becomes necessary, and production refuses to start
if signing apps are added without one.

### Server does the compute, browser does the interaction

Annotation (drag, click, zoom) lives in the browser on a `<canvas>`. Model
inference, decoding 50 MP photos and drawing the full-resolution result run
server-side. (§10 records how this could move into the browser.)

### Vanilla ES modules, no framework or CDN

The UI is small and mostly canvas work; a framework would add weight and a build
step. Being 100% self-hosted lets the **CSP** be strict (`default-src 'self'`, no
`unsafe-inline`): every script, style and handler is same-origin, and handlers
are wired through a `data-action` dispatch table (`main.js`), never inline.

### Coordinate model

The subtle core of the frontend. Three spaces:

- **Original-image px**, in the *rotated* frame: all annotation data
  (`line.y`/`line.x`, `spot.x`/`spot.y`, `plateRegion`) and everything sent to
  the server.
- **Canvas bitmap px**: the backing store, fitted to ≤ 500 px tall;
  `window.scaleFactor = originalH / canvasH`.
- **Canvas display (CSS) px**: the element's rendered size, which `max-width`
  shrinks on narrow screens.

`pointerCanvas(e)` maps a pointer to bitmap px through
`getBoundingClientRect()`, so taps stay aligned on a scaled canvas (tested at
360 px). Delete/edit icons live in `#canvas-container` and are placed in display
px.

Canvas labels are sized by the saved image's rule (`plateFontPx`: the size
`draw_annotations` uses, in image px, divided by `scaleFactor`, at least 11 px),
so the preview and the saved image look alike; the text-size setting applies
to the saved image only. The crop view shows the Plate Area plus 6 % of its
size on each side (`cropRect`), clipped to the image.

### Rotation: a preset plus a fine-tune

Rf compares heights, so the baseline must be level. The rotation is a preset
(0/90/180/270) plus a fine-tune of ±45° (`state.rotation`,
`state.fineRotation`). The server applies the preset exactly (`cv2.rotate`) and
the fine turn about the centre **at the same frame size** (`_fine_rotate`:
`warpAffine`, corners falling outside, white where uncovered); the canvas draws
the same turn (`drawImageRotated`). Keeping the size leaves the pixel cap, the
memory estimate and every coordinate unchanged, and avoids a second rounding
rule the two sides would have to share. Unit and e2e tests check that a dot
lands at the same place on both sides. Rotation is allowed only before
annotating, so the Rf axis never has to be re-derived.

### One funnel for edits: `commitChange()`

Every completed edit goes through `canvas.commitChange()`: a history snapshot
(undo/redo), stale-marking of a prior result, a repaint, and saving the
browser draft. `addSpot` is a plain mutator; callers commit at the *gesture*
boundary, so a click or a detection batch is one undo step. Undo and redo share
the same tail. Snapshots carry the rotation the marks were made in, and turning
the image starts a new history, so marks never return in another frame.

### Stale-results guard

The table and annotated image are a snapshot of the last Calculate RF. A later
edit (marks, Plate Area, mobile or stationary phase) shows a banner, dims the
image and **disables export** until recalculation, so an outdated image is
never saved or sent to the ELN. Text size and "full labels" change only the
drawing: a change (debounced) redraws the image by calling `calculate_rf`
again with the same annotations (`rerenderResults`), so the rows stay the
same and the results never go stale; export waits for the new image. Every
calculation and redraw is numbered and only the latest reply is shown; a
redraw's reply is dropped if an edit made the results stale meanwhile.

### Plate Area: a detection region, not the Rf reference

The Plate Area only confines detection (TLC-LineDet sees only the box;
TLC-SpotDet's band is limited to its x-range). It never places Min/Max: the
baseline sits above the plate's bottom edge and the front below its top. While
it is set, lines and spots stay inside it: placement outside is refused, drags
clamp, and redrawing the box drops spots outside it (snapping a spot would
fabricate data).

### Detection (`tlc/detection.py`, `tlc/inference/`): version 1's logic

- **Lines (TLC-LineDet, 1024 px input):** the most confident box per class;
  the two are assigned **by position** (the lower is the baseline), because
  geometry, not the class label, defines the TLC meaning. With only one line
  found, the other is estimated at 10%/90% of the height and flagged
  (`estimated`); the browser draws that line dashed, captioned "Min
  (estimate)" or "Max (estimate)", until the user moves it (the flag is kept
  in undo, drafts and the annotation file). Confidence 0.05.
- **Spots (TLC-SpotDet, 1280 px input, NMS in the graph):** inference only on
  the strip between baseline and front, widened by `TLC_BAND_MARGIN` (12 px);
  candidates whose centre lies outside the band itself are dropped. The model
  file has a confidence cutoff of 0.25 built in (it returns no lower scores),
  so `SPOT_CONF_DEFAULT` is 0.25; a lower value would find nothing more.
- **One proposal per spot (browser, `newCandidates` in `results.js`):** the
  model often puts a second, larger or smaller box on the same spot (28 % of
  candidates on 223 plates). The server returns every candidate unchanged,
  with its box size (`w`, `h`); the browser treats two candidates as one spot
  when the centre of each lies inside the other's box, keeps the more
  confident, and skips a candidate whose box holds a spot already on the
  plate, so a second run adds nothing. The rule is in image px, so it does
  not depend on the display size; neighbouring spots, about a box apart, are
  both proposed. Placing by hand keeps a 12-screen-px guard against double
  clicks.
- Models load lazily per worker under a lock; each response names the model
  (file, SHA-256, export date) for traceability (SI 10.3).
- Results are candidates the user reviews; nothing is calculated from them
  until Calculate RF.

`tools/legacy_equivalence.py` runs the detection endpoints of this checkout and
of version 1 on a directory of photos and requires identical lines and spot
candidates (x, y, confidence) (§7).

### Results (`tlc/results.py`)

- **Assignment** (SI Eq. 10/11): each retained spot takes the nearest lane
  marker (V-line, |Δx|) and compound marker (H-line, |Δy|). Markers carry free
  text and a type from a list (reviewer request). An unlabelled lane is blank;
  a spot without a labelled compound marker is "Unknown".
- **Order and numbers:** rows sorted by lane label (natural order, unlabelled
  last), then Rf, then x; numbered 1..n. The number is drawn next to each spot,
  so a crowded image stays readable; "full labels" adds Rf, compound and lane.
- **Annotated image** (SI 8.2): drawn on the full-resolution image: Min
  (yellow) and Max (magenta) on a dark outline, which tell the two apart and
  show on white, dark and green fluorescent plates alike; H-lines (black),
  V-lines (blue, captions staggered so neighbours do not overlap), spot rings
  and labels, on white halos. The canvas uses the same colours. OpenCV 5
  ignores `putText` thickness, so halos are a dilated glyph mask. Sizes scale
  with the image; *text size* is a multiplier on top.
- **CSV:** the table's columns in the table's order (`spot`, lane label and
  type, compound label and type, `RF value`), then `spot source` (manual /
  detected / detected, moved), the CHMO line and the "Details" sentence kept
  from version 1, and a closing `Provenance:` block (app version, image
  name and SHA-256, rotation, Plate Area, mobile and stationary phase, both
  models' SHA-256, UTC time). Cells are guarded against spreadsheet formulas.
  Chemotion stores the CSV as an attachment and parses nothing in it.
- **Details sentence** (`details_sentence`): Chemotion's TLC free-text style,
  `TLC (<mobile phase>); Rf (UV-Vis in nm, staining) = 0.53 (A)[h1], ...`,
  with version 1's placeholder "solvents" when no mobile phase is given. The
  response returns it as `details` for **Copy Rf summary**, so the browser
  never rebuilds it.
- **Mobile and stationary phase:** optional free text, cleaned like marker
  labels (`clean_label`: one line, 100 characters).

### Annotation file and drafts (`annotation-file.js`, `drafts.js`)

One versioned JSON (`tlc-rf-annotations`, version 1) holds everything needed to
redo a result: image name and SHA-256, mobile and stationary phase, rotation,
Plate Area, Min/Max (with `estimated` while a line is a guess), markers, spots
with their source. The phases and `estimated` are optional, so a file without
them still opens and the version stays 1. It is exported next to the CSV (download or ELN),
opened back onto the same image (checked by SHA-256, refused for another),
and kept in `localStorage` after every change as a draft keyed by the image
hash. A draft is only ever *offered* (Restore / Discard), never restored by
itself; Reset deletes it; the newest 20 are kept. The image is hashed in the
browser (`crypto.subtle`); without it, drafts are off.

### Exports: one name, one ZIP

Every exported file, downloaded or uploaded to the ELN, is named
`<stem>_<YYYY-MM-DD_HH-MM>_rf.csv`, `..._annotated.png` or
`..._annotations.json` (`exportName` in `results.js`): the stem is the image's
name without its extension (`tlc_plate` for an ELN image), the time that of
the result, so the three files of one result sort together. **Download all**
POSTs the CSV and image URLs of the last result plus the annotation file's text
to `/download-results/`. The server accepts only the two files of one
calculation (`rf_values_<token>.csv` and `annotated_<token>.png`, the same
token), resolves them with `_resolve_within_media`, requires the annotations
to parse as a `tlc-rf-annotations` object, cleans the stem, and streams a ZIP
built in a temporary file (a 50 MP plate's PNG is tens of MB). Clicking a
table row rings the spot on the canvas and, positioned from the row's image
px, over the annotated image (`updateAnnotatedHighlight`, kept in place on
resize).

### Images: one decoder, checked before decoding

- Every image (upload, inline fallback, ELN) goes through one decoder that
  applies the EXIF orientation, so the server sees what the browser shows.
  (Version 1 ignored EXIF for local uploads and analysed sideways-stored phone
  photos sideways; its ELN images were oriented correctly.)
- Size limits are checked from the file header **before** decoding:
  `MAX_IMAGE_MB` (25) and `MAX_IMAGE_MEGAPIXELS` (64; phone photos reach 50 MP).
  Uploads keep their real format under a random name.
- A local image is uploaded once (`/upload-image/`); later requests send its
  name (~100 bytes) instead of the whole image (~17 MB as base64).

### Memory: admission by free memory, a decoded-image cache

Running out of memory kills a worker; CPU overload only slows requests. So:

- **Admission** (`tlc/admission.py`): an image job states the memory it needs,
  estimated from the pixel count, the job kind and whether the image is
  already decoded (measured: ~760 MB for a 50 MP photo decoded afresh, 90–320 MB
  when cached), and starts only when that fits in the free memory (container
  cgroup, else `/proc/meminfo`) minus a reserve and all workers' running
  claims (shared in a `flock`ed file). Otherwise it waits up to 30 s, then
  answers 503 "busy". Freed heap is returned after each job (`malloc_trim`).
- **Cache** (`tlc/imagecache.py`): each worker keeps recently decoded images
  (read-only; a job that draws copies), so the three steps on one plate decode
  it once: 7.3 s instead of 10.4 s for a 50 MP photo.
- ONNX Runtime's CPU memory arena is **off**: it held ~500 MB per worker after
  one large plate, for no measured gain.
- Container stress test (2 workers × 4 threads, 50 MP photo): 1.5 GB and 2 GB
  served 8 and 16 concurrent requests (peak ~1.27 GB); 1 GB answered 503.

### Security model

- **SSRF** (`_safe_address`, `_fetch_following_safe_redirects`): http(s)
  only, host must resolve to a public address, optional allowlist from
  `ELN_ORIGIN`; redirects are followed but **every hop is re-validated**. Each
  hop connects to the address that was validated (pinned, so a changed DNS
  answer cannot redirect it; TLS still verifies the host name), and the whole
  download has a 60 s deadline. If the server process has `HTTP_PROXY` /
  `HTTPS_PROXY` set, a plain-http URL is sent to the proxy by host name and the
  proxy resolves it itself, so that fetch is not pinned; an https URL is
  tunnelled (`CONNECT`) to the pinned address and still verifies the
  certificate against the host name.
- **Path traversal** (`_resolve_within_media`): `basename` + `realpath` +
  `commonpath`, so `/serve-image/` cannot leave `MEDIA_ROOT`.
- **CSRF** on every POST; **strict CSP**; secure cookies, HSTS, nosniff and
  `DENY` framing when `DEBUG` is off; non-root container. `/healthz` is exempt
  from the HTTPS redirect so probes over plain HTTP get 200.
- **ELN upload** carries no custom headers (in particular not our CSRF token):
  a CORS simple request, with `attachmentName` naming the file.
- Endpoints are unauthenticated and heavy: put rate limiting at the reverse
  proxy for internet-facing deployments.

### Config derivation (fewer env vars)

A deployment sets `ALLOWED_HOSTS` and `ELN_ORIGIN`. `CSRF_TRUSTED_ORIGINS`
derives `https://<host>` from `ALLOWED_HOSTS`; `ELN_ORIGIN` sets both the CSP
`connect-src` and the fetch allowlist (`ELN_ALLOWED_HOSTS`). Each derived value
can still be overridden.

## 4. Frontend module map (`tlc/static/tlc/js/`)

Loaded as `<script type="module" src="js/main.js">`. Leaf → entry:

| Module | Responsibility |
| --- | --- |
| `state.js` | The single mutable `state` object + `elnUrl`. Imports nothing. |
| `geometry.js` | Pure `clamp` / `pointInRegion`. |
| `history.js` | Undo/redo snapshot stack + cursor; snapshots carry the rotation. |
| `ui-kit.js` | Toasts, busy overlay, cookie helper. |
| `theme.js` | Light/dark toggle (persisted). |
| `ui.js` | Headings, source card, hint, layout, enable states, stale-results and export controller, plate description fields, highlight ring over the annotated image, `textScale()`. |
| `canvas.js` | Draw/redraw (line colours, label size `plateFontPx`, estimated lines dashed), rotation (`drawImageRotated`, `setRotation`), `pointerCanvas`, crop view (`cropRect`), reset, `commitChange`, undo/redo. |
| `annotations.js` | Line/spot tools, label popup (type list + text), edit, delete. |
| `interaction.js` | Pointer drag, region box, double-click(-tap) delete, drag clamping. |
| `results.js` | Image payload, detection and Rf requests, redraw for text size, results table, export names, downloads, Download all, Copy Rf summary, ELN upload. |
| `annotation-file.js` | The annotation JSON: serialise, validate, apply; image SHA-256. |
| `drafts.js` | Drafts in `localStorage`, keyed by image hash. |
| `annotations-io.js` | Image identity, draft banner, open/export annotation files. |
| `sources.js` | ELN/local source, upload once, ELN fetch. |
| `main.js` | `data-action` dispatch table, inputs, keyboard shortcuts, init. |

`state.canvas`/`ctx` are assigned in `init()`, never captured at import. Import
cycles between modules are safe because every cross-module export is a hoisted
function declaration. Shared mutable state lives on `state`.

## 5. Backend module map

| File | Responsibility |
| --- | --- |
| `config/settings.py` | Env-driven settings, CSP config, derivations, the `SECRET_KEY` rule. |
| `config/env.py` | `env_bool/int/list`, `csrf_origins_from_hosts`, `host_of`, `signs_data`. |
| `config/middleware.py` | `ContentSecurityPolicyMiddleware`. |
| `tlc/views.py` | Endpoints; image checks, decoding, rotation; Rf (`rf_from_positions`); the results ZIP (`download_results`); security helpers. |
| `tlc/detection.py` | Spot and line detection (version 1's band and assignment logic). |
| `tlc/inference/` | Lazy, locked ONNX sessions (`spot.py`, `line.py` wrappers); model identity. |
| `tlc/results.py` | Rows, numbering, annotated image, CSV + provenance, Details sentence. |
| `tlc/admission.py` | Admits image jobs by free memory. |
| `tlc/imagecache.py` | Recently decoded images per worker. |
| `tlc/media.py` | Writes to `MEDIA_ROOT`; automatic pruning. |
| `tlc/management/commands/cleanup_media.py` | Prune media files now. |
| `tools/legacy_equivalence.py` | Plate-by-plate comparison with version 1 (not shipped in the image). |

## 6. Testing

- **Unit** (`tlc/tests.py`, 179): Rf equations and validation, detection and
  line assignment (model calls stubbed where needed), model loading, results
  rows, CSV and provenance (with mobile and stationary phase), the annotated
  image, the results ZIP, rotation maths, image checks and EXIF, upload/fetch,
  admission and cache, media pruning, security helpers, settings derivation,
  `SECRET_KEY`.
- **E2E** (`e2e/test_regression.py`, Playwright + Chromium, 84): drives the
  real UI: the Rf flow, detection, estimated lines, labels and types,
  numbering, CSV, export names, Download all, Copy Rf summary, ELN upload,
  rotation (canvas vs server), annotation files and drafts, plate
  description, touch, phone width, stale results and the text-size redraw,
  undo/redo. `conftest.py` fails a test on
  any uncaught page error.
- **JS units without npm** (`e2e/test_js_units.py`, 7): the page `import()`s
  modules and asserts on pure exports.

Run: `DEBUG=True uv run python manage.py test` and `uv run pytest e2e`.
Lint and types: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy`.

## 7. Equivalence with version 1

`tools/legacy_equivalence.py PLATES_DIR` builds version 1 from a git ref
(`harden-for-deployment`: its detection code with lazy loading and a lockfile)
in a temporary worktree, runs the line and spot detection endpoints of both apps on
every photo through Django's test client, and requires identical line
positions and spot candidates (x, y, confidence; the box size that version 2
adds is not compared). Photos with an EXIF orientation are written upright
first, since version 1 ignored EXIF for local uploads. It is not part of CI because the plate photos are private and never
committed. Last run 2026-11-16: **223 plates, 4,941 spot candidates, all
identical.**

## 8. Build, CI, deployment

- **Dev:** `uv sync`; `DEBUG=True uv run python manage.py runserver`.
- **CI:** GitHub Actions (`.github/workflows/ci.yml`) runs lint, format, types,
  unit and e2e tests on pushes to `main` and on pull requests; the Docker image
  is built and pushed only for a release.
- **Container:** multi-stage `Dockerfile` (uv sync + collectstatic → slim
  runtime, ~810 MB with the models), non-root user, TCP health check, build context limited by an allowlist
  `.dockerignore`,
  `gunicorn -c gunicorn.conf.py`. Listens on 8050; `/healthz` for probes.
  WhiteNoise serves static files; the app serves media (put a reverse proxy in
  front).
- **Sizing** (`gunicorn.conf.py`): 2 workers × 4 threads by default. Each
  worker loads its own models (~130 MB). With 50 MP photos, 1.5 GB is enough
  for 2 workers; smaller containers answer "busy" rather than crash.
- **Static caveat:** `main.js` is hashed by `ManifestStaticFilesStorage`, but
  its relative `import "./x.js"` statements are not rewritten, so sibling
  modules are served unhashed. Clear caches after a breaking change to them.

Verify a deployment with `uv run python manage.py check --deploy`.

```bash
docker build -t tlc-rf-calculator .
docker run --rm -p 8050:8050 \
  -e ALLOWED_HOSTS="tlc.example.org" \
  -e ELN_ORIGIN="https://eln.example.org" \
  tlc-rf-calculator
```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts. |
| `ELN_ORIGIN` | *(empty)* | ELN origin (e.g. `https://eln.example.org`); sets CSP `connect-src` + fetch allowlist. |
| `SECRET_KEY` | *(random per start)* | Optional: nothing in the app signs data with it. Set one fixed value before adding logins, sessions, messages or signed links; production refuses to start with those and no key. |
| `DEBUG` | `False` | Debug mode. Never enable in production. |
| `MAX_IMAGE_MB` | `25` | Max image file size (MB), local upload or ELN fetch; also enforced in the browser. |
| `MAX_IMAGE_MEGAPIXELS` | `64` | Max image size; phone photos of plates reach 50 MP. |
| `MAX_UPLOAD_MB` | derived | Max request-body size (MB); fits a base64-encoded `MAX_IMAGE_MB` image (the inline fallback). |
| `MEDIA_MAX_AGE_HOURS` | `24` | Delete media files unused for this long; `0` turns automatic sweeps off. |
| `MEDIA_SWEEP_MINUTES` | `15` | Minimum time between automatic sweeps, per worker. |
| `IMAGE_JOB_RESERVE_MB` | `300` | Memory kept free when admitting an image job, at most a tenth of the total. |
| `IMAGE_JOB_WAIT_SECONDS` | `30` | How long a job waits for memory before the client gets 503 "busy". |
| `IMAGE_CACHE_MB` | `256` | Decoded images kept per worker; `0` turns it off. |
| `IMAGE_JOB_CLAIMS_FILE` | temp dir | File in which workers share their memory claims (one per deployment). |
| `ONNX_INTRA_OP_THREADS` | `2` | Threads per inference; workers × threads × this ≈ peak cores. |
| `ONNX_CPU_MEM_ARENA` | `False` | ONNX Runtime's memory arena; on, it holds ~500 MB per worker for no measured gain. |
| `SECURE_SSL_REDIRECT` | `True` (prod) | Redirect HTTP→HTTPS (`/healthz` exempt). |
| `SECURE_HSTS_SECONDS` | `31536000` | HSTS max-age (prod). |
| `GUNICORN_WORKERS`/`THREADS`/`TIMEOUT`/`BIND` | `2`/`4`/`120`/`0.0.0.0:8050` | Gunicorn tuning (see `gunicorn.conf.py`). |

Derived, override only if needed: `CSRF_TRUSTED_ORIGINS` (from `ALLOWED_HOSTS`),
`CSP_CONNECT_SRC` and `ELN_ALLOWED_HOSTS` (from `ELN_ORIGIN`).

### Maintenance

Files in `MEDIA_ROOT` (uploaded and ELN images, annotated images, CSVs) are
deleted once unused for `MEDIA_MAX_AGE_HOURS`. The app checks after writing a
file, at most every `MEDIA_SWEEP_MINUTES` per worker (`tlc/media.py`), so the
container needs no cron job. Reading an image for a request counts as using it
(its access time is set), so a plate still being worked on keeps its image. To
sweep now, or from cron with `MEDIA_MAX_AGE_HOURS=0`:

```bash
uv run python manage.py cleanup_media [--hours 24] [--dry-run]
```

## 9. ELN (Chemotion) integration

Chemotion opens the tool with one signed URL (`?url=<jwt endpoint>`) used for
**both** the image GET and the results POST. The server proxies the GET
(`/fetch-image/`) to avoid browser CORS and to SSRF-guard it; results are POSTed
cross-origin straight from the browser. Export offers three downloads (CSV,
Image, Annotations), all three as one ZIP, a copy of the Rf summary and, for
an ELN-sourced plate, the same three files as ELN uploads, each a separate
one-file POST named like the download. Full contract and worked config:
[eln-integration.md](eln-integration.md).

## 10. Possible future: in the browser

The app could become a static site: Rf calculation and the annotated image are
plain `<canvas>` work, and both models could run in the browser with
**ONNX Runtime Web** (WASM/WebGPU). The open questions are the model download
(~58 MB), decoding 50 MP photos in the browser (canvas size limits on phones),
and the ELN image GET, which would need CORS on Chemotion (the `/fetch-image/`
proxy exists to avoid that). Not implemented; recorded as a direction.
