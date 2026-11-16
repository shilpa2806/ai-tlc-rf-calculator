# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [2.0.0] — Unreleased

The application is rebuilt on a new stack and interface. The analysis
workflow of version 1 -- the two ONNX models, spot search between the
baseline and the solvent front, line assignment by position, the Rf rule and
nearest-marker assignment -- is kept; a plate-by-plate check on 223 photos
gives identical detection results.

### Changed (breaking)

- New project layout and stack: Django 5.2 on Python 3.14 (`config/`,
  `tlc/`), dependencies managed with uv (`pyproject.toml`, `uv.lock`), a
  multi-stage Docker image served by Gunicorn and WhiteNoise. No database.
- New interface in plain JavaScript modules, with no external libraries or
  CDNs (Materialize CSS and SweetAlert2 are gone): a toolbar grouped by
  task, a collapsible image source, undo/redo, crop view (the Plate Area
  with a 6 % margin), adjustable label size on the saved image, touch and
  pen support, light and dark themes, and a warning when results are out of
  date.
- Endpoints: `/auto_detect_lines/` and `/auto_detect_spots/` become
  `POST /detect-lines/` and `POST /detect-spots/`. Suggest Spots needs the
  baseline and solvent front (`minY`, `maxY`) and searches only between them
  (±12 px); a Plate Area also limits the x-range. Responses name the model
  (file, SHA-256, export date). Candidates need a confidence of at least
  0.25, the cutoff built into `spot_best.onnx` (it returns no lower scores).
- Suggest Spots proposes each spot once. When the model returns two boxes on
  the same spot (the centre of each inside the other's box), only the more
  confident is added; neighbouring spots are both proposed, whatever the
  image size, and running it again adds nothing already on the plate.
  Version 1 added every candidate. Each candidate now carries its box size
  (`w`, `h`).
- A spot is kept when **0 ≤ Rf ≤ 1**, tested before rounding; before, 0 and
  1 were excluded after rounding, so a spot on the baseline was dropped.
- Calculate RF refuses a baseline at or above the solvent front, and
  positions that are not numbers, with a message.
- Image limits: 25 MB per file (`MAX_IMAGE_MB`) and 64 megapixels
  (`MAX_IMAGE_MEGAPIXELS`), enough for phone photos of plates (13 MB, 50 MP);
  the page reads the file limit from the server. Version 1 could not process
  local images above about 1.9 MB (its request-size limit).
- The CSV is written by the server and its columns follow the results
  table: `spot, mixture_label (X axis), mixture_type, substance_label (Y
  axis), substance_type, RF value`, then a seventh column, `spot source`
  (before: `mixture_label (X axis), substance_label (Y axis), RF value`).
  The `CHMO:0001007` line and the `Details:` line are kept; the `Details:`
  line names the mobile phase in place of the placeholder "solvents" when
  one is given. Rf is written
  with two decimals, as in the table (`0.50`, before `0.5`), in the column
  and in the `Details:` line. Labels are quoted, so a comma no longer shifts
  the columns; cells cannot start a spreadsheet formula.
- Results are numbered: the number is drawn next to each spot on the
  annotated image and is the row in the table and the CSV; rows are sorted
  by lane label, then Rf.

### Added

- **Plate Area**: a box around the plate that confines detection, with
  lines and spots kept inside it.
- With a Plate Area set, **Suggest Lines** searches only inside it, so a line
  is not put on the background of an uncropped photo.
- `tools/legacy_equivalence.py`: plate-by-plate comparison of detection with
  version 1 (223 plates, 4,941 spot candidates, identical).
- Marker types: H- and V-lines get a type from a list (for example
  *starting material*, *product 1*) next to the free text, and can be edited
  after placing. The table shows lane and compound type columns; "full
  labels" prints Rf, compound and lane on the image. Min and Max buttons hide
  once placed; H-lines are black.
- `POST /upload-image/`: a local image is stored once and later requests
  refer to it by name, instead of each carrying the whole image (~17 MB for a
  13 MB phone photo).
- Stored files (uploads, ELN images, results) are deleted once unused for
  `MEDIA_MAX_AGE_HOURS` (24); no cron job needed. `cleanup_media` does the
  same on demand.
- Image jobs start only when the memory they need is free (estimated from
  the pixel count; container limits respected; shared between workers), and
  otherwise wait, then answer 503 "busy, try again". Each worker keeps
  recently decoded images (`IMAGE_CACHE_MB`), so the three steps on a plate
  decode it once. In a 1.5 GB container, eight 50 MP requests at once
  succeed.
- Results record how they were made: a `spot source` column (manual,
  detected, or detected then moved) and a closing `Provenance:` block in the
  CSV with the app version, the image name and SHA-256, rotation, Plate Area,
  mobile and stationary phase, both models' SHA-256 and the time of
  calculation.
- **Mobile phase** and **stationary phase**: two optional free-text fields
  (for example *CH/EtOAc 4:1*, *silica gel 60 F254*), kept with the
  annotations (undo, drafts, annotation files) and recorded with the result.
- **Download all**: the CSV, the annotated image and the annotation file of
  a result as one ZIP (`POST /download-results/`).
- **Copy Rf summary**: copies the CSV's `Details:` sentence (Rf values in
  the style of Chemotion's TLC description) to paste into the ELN.
- Clicking a results row rings its spot on the annotated image as well as on
  the plate.
- **Rotation**: a quarter turn plus a fine-tune of up to ±45° (0.5° steps),
  to straighten a tilted plate before annotating; Rf assumes a level
  baseline. The browser and the server apply the same turn.
- **Annotation files**: the annotations, with the mobile and stationary
  phase, are exported as versioned JSON next to the CSV (download or ELN).
  **Open annotations** loads one back onto the same image, checked by
  SHA-256, to check or continue a result.
- **Drafts**: annotations are kept in the browser as you work; loading the
  same image again offers to restore them. Reset deletes the draft.
- Tests and checks: Django unit tests (Rf rules, detection, results, image
  handling, security helpers, settings) and Playwright browser tests of the
  whole workflow, plus ruff and mypy, run in GitHub Actions on every push
  and pull request.

### Changed

- Server sizing for the models: 2 Gunicorn workers x 4 threads, a 120 s
  timeout, `ONNX_INTRA_OP_THREADS` capping ONNX Runtime's threads; models
  load lazily, once per worker, behind a lock.
- `SECRET_KEY` is optional: nothing in the app signs data with it, so a
  random key is made at start-up. A deployment needs only `ALLOWED_HOSTS`
  and `ELN_ORIGIN`.
- Downloads and ELN uploads share one name: the image's name, the time of
  the result and the content, as in `plate7_2026-11-16_14-32_rf.csv`,
  `..._annotated.png` and `..._annotations.json` (`tlc_plate_...` for an ELN
  image). Undo and redo keep annotations together with the rotation they
  were made in. An image that fails to load leaves the current work
  untouched, and loading the ELN image starts a clean session.
- The detection buttons are **Suggest Lines** and **Suggest Spots**
  (version 1: *Auto Detect Lines*, *Auto Detect Spots*): they propose
  candidates to check. When only one line is found, the estimated other one
  is drawn dashed and captioned *Min (estimate)* or *Max (estimate)* until
  it is moved.
- Min and Max are yellow and magenta on a dark outline, on the plate and on
  the annotated image (version 1: both yellow on the canvas, green and red on
  the saved image), so they stay apart and visible on green fluorescent
  plates.
- The working canvas is titled *Plate* (version 1: *Original Image*). Its
  labels are sized from the photo like those of the annotated image; the
  text size applies to the annotated image only, and changing it or *full
  labels* redraws the annotated image without a recalculation.
- README and developer docs (`docs/architecture.md`,
  `docs/eln-integration.md`) rewritten; the in-app Help covers rotation and
  annotation files.

### Fixed

- The TLC-LineDet class ids were hard-coded the wrong way round; they are
  read from the model (no effect on results, which are assigned by position).
- Locally uploaded phone photos stored sideways (with an EXIF orientation)
  are analysed the way the browser shows them; version 1 analysed them
  sideways. ELN images were already oriented correctly.
- Uploads to the ELN no longer send this app's CSRF token to Chemotion, and
  name the file (`tlc_plate_2026-11-09_14-32_rf.csv`). A refused upload is
  reported as a failure; it used to say "Saved".
- Image size limits are checked from the file header before decoding.
  `/healthz` answers over plain HTTP so health probes work behind HTTPS.
- At phone width the page no longer scrolls sideways; the canvas scales to
  fit and taps land where shown.

### Security

- CSRF protection on every POST (endpoints were `csrf_exempt`), a strict
  Content Security Policy, SSRF-guarded ELN image fetch (public hosts only,
  every redirect re-checked), `/serve-image/` confined to the media folder,
  secure cookies and HSTS in production, and a non-root container.
- The ELN image fetch connects to the address that was checked (closing a
  DNS-rebinding gap; TLS still verifies the host name), has an overall
  60 s deadline, and reports a dropped connection as a failed fetch. Result
  files are named by a random token only. The Docker build context is an
  allowlist, so local files such as `.env` cannot reach the image.

## [1.0.1] — 2026-05-31

Updated application code, README and user-guide images.

## [1.0.0] — 2026-05-30

First release: browser annotation of TLC plate images, ONNX-assisted line
and spot detection (TLC-LineDet, TLC-SpotDet), Rf calculation, CSV and
annotated-image export, and Chemotion ELN load and save.
