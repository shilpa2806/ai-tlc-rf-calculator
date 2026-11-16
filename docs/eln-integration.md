# ELN integration (Chemotion third-party app)

This tool is registered in Chemotion as a **third-party app**. Chemotion opens
it in the browser and hands over a single signed URL that is used both to load
the plate image and to save annotated results back.

Upstream user docs: <https://chemotion.net/docs/services/third_party_apps/tlc>
(a local reference copy is kept, gitignored, under `eln_related_docs/`).

## How the tool is launched

Chemotion navigates the user to the tool with one query parameter, `url`:

```text
https://<tlc-host>/?url=<url-encoded Chemotion endpoint>
```

Example (decoded):

```text
https://<eln-host>/api/v1/public/third_party_apps/<JWT>
```

The `url` value is a Chemotion `public/third_party_apps` endpoint carrying a
short-lived **JWT**. Decoding the JWT payload (base64url, not verified client
side) shows the fields Chemotion signs:

```json
{ "appID": 13, "userID": 28, "attID": 5667, "exp": 1785296944 }
```

| Field    | Meaning                                             |
| -------- | --------------------------------------------------- |
| `appID`  | The registered third-party app id                   |
| `userID` | The Chemotion user                                  |
| `attID`  | The attachment (the TLC image) this URL is bound to |
| `exp`    | Expiry (Unix seconds) — the URL is time-limited     |

## The contract: one URL, GET and POST

The single signed endpoint is used for **both** directions — this is by design,
not a bug:

- **GET** → returns the source plate image (the attachment `attID`). The
  endpoint commonly **redirects** (302) to the actual storage location.
- **POST** (`multipart/form-data`, fields `file` and `attachmentName`) →
  uploads a result file (annotated PNG, CSV or annotation JSON) back onto the
  same attachment/app. Chemotion names the new attachment `attachmentName`
  (`tlc_plate_<date>_<time>_rf.csv`, `tlc_plate_<date>_<time>_annotated.png`,
  `tlc_plate_<date>_<time>_annotations.json`, the names the downloads get;
  `<time>` is the time of the result); without it, `<app name>-<upload
  name>`. One token allows 10 uploads. Chemotion stores the files as
  attachments and parses none of them. The request
  carries no custom headers (in particular not our CSRF token), so it is a
  CORS "simple request" with no preflight.

In the frontend this is a single `elnUrl` (`tlc/static/tlc/js/state.js`):
`loadELNImage()` (`js/sources.js`) GETs it via the server-side proxy below, and
`postToEln()` (`js/results.js`) POSTs results to it directly from the browser.

## How the tool handles it

- `GET /fetch-image/?url=<eln url>` — the server proxies the fetch (avoids
  browser CORS on download and keeps the image on the tool's origin). The URL
  is SSRF-validated; **redirects are followed but every hop is re-validated**
  against the same policy (`_fetch_following_safe_redirects` in `tlc/views.py`),
  so Chemotion's redirect-to-storage works safely. Each hop connects to the
  address that was validated, and the download has a 60 s deadline. Behind an
  `HTTP_PROXY` / `HTTPS_PROXY` set for the server, only https URLs stay pinned
  (tunnelled to the validated address, certificate checked against the host
  name); a plain-http URL is resolved by the proxy itself. The image is checked from
  its header (format, `MAX_IMAGE_MB`, `MAX_IMAGE_MEGAPIXELS`), saved under a
  random name in its own format in `MEDIA_ROOT`, and only that name is
  returned. Unused files are deleted after `MEDIA_MAX_AGE_HOURS` (24).
- `GET /serve-image/?file_path=<name>` — serves the fetched image back to the
  canvas, confined to `MEDIA_ROOT`.
- `POST /detect-lines/`, `POST /detect-spots/`, `POST /calculate_rf/` —
  same-origin; CSRF enforced. They refer to the image by its stored name.
- `POST /download-results/` — same-origin, CSRF enforced: one ZIP with a
  result's CSV and annotated image (by their stored names, which must belong
  to one calculation) and the annotation file sent by the browser.
- **Save back to ELN** — `postToEln()` POSTs directly from the browser to
  `elnUrl` (cross-origin to Chemotion), so it depends on the deployment config
  below.

## Deployment requirements

For the same-origin fetch/serve/calculate flow, nothing special is needed. For
**save-back to Chemotion** (a cross-origin browser POST) both of these must hold:

1. **CSP + fetch allowlist** — set a single `ELN_ORIGIN`. It adds the Chemotion
   origin to the strict `connect-src` (so the browser POST is allowed) and
   restricts which hosts the server will fetch images from:

   ```bash
   ELN_ORIGIN="https://<eln-host>"
   ```

   (The individual `CSP_CONNECT_SRC` / `ELN_ALLOWED_HOSTS` still override it if
   you need them to differ.)

2. **CORS** — the Chemotion endpoint must permit cross-origin POSTs from the
   tool's origin. This is Chemotion server configuration, outside this repo.

### Worked example

Given the tool at `tlc.example.org` and a Chemotion instance at
`eln.example.org`, the full configuration is two variables:

```bash
ALLOWED_HOSTS="tlc.example.org"   # CSRF_TRUSTED_ORIGINS derived as https://tlc.example.org
ELN_ORIGIN="https://eln.example.org"   # sets CSP connect-src + fetch allowlist
```
