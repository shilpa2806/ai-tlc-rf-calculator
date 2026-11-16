"""Check that candidate detection matches version 1, plate by plate.

Runs the **real endpoints** of both apps -- this checkout and a git ref of the
original -- through Django's test client, each in its own environment, over a
directory of plate photos, and compares the JSON they return:

1. Detect Lines on every plate;
2. Detect Spots on every plate where lines were found, with the *same* band
   (the original's lines) given to both, so a line difference cannot leak
   into the spot comparison.

Photos with an EXIF orientation are first written out upright (JPEG), because
the original ignored EXIF for local images and would otherwise see them
sideways -- a known difference, fixed here, that this check is not meant to
re-detect. Each spot is compared by x, y and confidence; the box size that
this version adds to each spot is not compared.

Usage (from the repository root)::

    uv run python tools/legacy_equivalence.py PLATES_DIR [--ref harden-for-deployment]

``--ref`` defaults to ``harden-for-deployment``, whose detection code is the
original's (``upstream/main``) with only lazy model loading added, and which
has a lockfile so its environment can be rebuilt. Nothing is written to the
repository: the worktree and all outputs live in a temporary directory, so
private plate photos never end up in git.

Exit status is 0 only if both apps return identical results for every plate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

# Runs inside either app's checkout and environment; prints one JSON object.
_RUNNER = r"""
import base64, json, mimetypes, os, sys
mode, listfile, bandfile = sys.argv[1:4]
bands = json.load(open(bandfile)) if bandfile != "-" else None
os.environ["SECRET_KEY"] = "equivalence-check-only"
if mode == "legacy":
    os.environ["DJANGO_SETTINGS_MODULE"] = "rf_calculator.settings"
    os.environ["DATA_UPLOAD_MAX_MEMORY_SIZE"] = str(256 * 2**20)
    LINES, SPOTS = "/auto_detect_lines/", "/auto_detect_spots/"
else:
    os.environ.update(DJANGO_SETTINGS_MODULE="config.settings", DEBUG="True",
                      MAX_UPLOAD_MB="256", MAX_IMAGE_MB="192")
    LINES, SPOTS = "/detect-lines/", "/detect-spots/"
sys.path.insert(0, os.getcwd())
import django; django.setup()
from django.test.utils import setup_test_environment; setup_test_environment()
if mode == "current":
    import tlc.views; tlc.views.MAX_IMAGE_PIXELS = 10**9  # compare, don't refuse
from django.test import Client
client = Client()

def post(url, payload):
    r = client.post(url, data=json.dumps(payload), content_type="application/json")
    ok = r.headers.get("Content-Type", "").startswith("application/json")
    return r.status_code, (r.json() if ok else {"error": "non-JSON response"})

out = {}
for path in open(listfile).read().splitlines():
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    data = base64.b64encode(open(path, "rb").read()).decode()
    base = {"isLocalImage": True, "imageSrc": os.path.basename(path),
            "imageData": f"data:{mime};base64,{data}"}
    if bands is None:
        status, body = post(LINES, base)
        out[path] = {"status": status, "minY": body.get("minY"), "maxY": body.get("maxY")}
    elif path in bands:
        status, body = post(SPOTS, base | bands[path])
        out[path] = {"status": status,
                     "spots": [[s["x"], s["y"], s.get("conf")] for s in body.get("spots", [])]}
print(json.dumps(out))
"""


def _run(cwd: Path, python: Path, mode: str, listfile: Path, bandfile: str, work: Path) -> dict:
    runner = work / "runner.py"
    runner.write_text(_RUNNER)
    result = subprocess.run(
        [str(python), str(runner), mode, str(listfile), bandfile],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _upright_copies(plates: list[Path], work: Path) -> list[Path]:
    from PIL import Image, ImageOps

    out = []
    for plate in plates:
        with Image.open(plate) as image:
            orientation = image.getexif().get(274)
            if orientation and orientation != 1:
                upright = work / "upright" / f"{plate.stem}.jpg"
                upright.parent.mkdir(exist_ok=True)
                ImageOps.exif_transpose(image).convert("RGB").save(upright, quality=95)
                out.append(upright)
                continue
        out.append(plate)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "plates", type=Path, help="directory of plate photos (searched recursively)"
    )
    parser.add_argument("--ref", default="harden-for-deployment", help="git ref of the original")
    args = parser.parse_args()

    plates = sorted(p for p in args.plates.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    if not plates:
        print(f"No images under {args.plates}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="tlc-equivalence-") as tmp:
        work = Path(tmp)
        legacy = work / "legacy"
        subprocess.run(
            ["git", "worktree", "add", "--detach", "-q", str(legacy), args.ref],
            cwd=ROOT,
            check=True,
        )
        try:
            env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
            subprocess.run(["uv", "sync", "-q", "--frozen"], cwd=legacy, check=True, env=env)
            apps = {
                "legacy": (legacy, legacy / ".venv" / "bin" / "python"),
                "current": (ROOT, Path(sys.executable)),
            }
            listfile = work / "plates.txt"
            listfile.write_text("\n".join(map(str, _upright_copies(plates, work))) + "\n")

            lines = {m: _run(c, py, m, listfile, "-", work) for m, (c, py) in apps.items()}
            bands = {
                p: {"minY": r["minY"], "maxY": r["maxY"]}
                for p, r in lines["legacy"].items()
                if r["status"] == 200
            }
            bandfile = work / "bands.json"
            bandfile.write_text(json.dumps(bands))
            spots = {
                m: _run(c, py, m, listfile, str(bandfile), work) for m, (c, py) in apps.items()
            }
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(legacy)], cwd=ROOT)

    differ = [
        Path(p).name
        for p in lines["legacy"]
        if lines["legacy"][p] != lines["current"][p]
        or spots["legacy"].get(p) != spots["current"].get(p)
    ]
    n_spots = sum(len(r["spots"]) for r in spots["legacy"].values())
    print(
        f"{len(plates)} plates; lines found on {len(bands)}; {n_spots} spot candidates; "
        f"identical on {len(plates) - len(differ)}, different on {len(differ)}"
    )
    for name in differ:
        print(f"  differs: {name}")
    return 0 if not differ else 1


if __name__ == "__main__":
    sys.exit(main())
