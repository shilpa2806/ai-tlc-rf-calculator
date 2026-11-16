# TLC RF Calculator

A web tool for measuring **retention factor (Rf)** values on photos of **thin
layer chromatography (TLC)** plates.

Load a photo of a plate, from your computer or straight from the Chemotion
electronic lab notebook (ELN). Two trained models propose the baseline, the
solvent front and the spots; you check and correct them in the browser. The
tool then computes the Rf of every spot, draws a numbered, annotated image,
and exports the results as CSV, or sends them back to the ELN.

```text
Rf = (distance from baseline to spot) / (distance from baseline to solvent front)
```

Rf runs from 0 (a spot on the baseline) to 1 (a spot at the solvent front).
Spots outside that range are left out.

## Using the tool

1. **Load a plate photo.** Choose **Upload local** (drag and drop, or pick a
   JPG, PNG or WebP of up to 25 MB; phone photos of 50 megapixels are fine), or
   **From ELN** when Chemotion opened the tool. Optionally note the
   **Mobile phase** (solvent mixture, e.g. *CH/EtOAc 4:1*) and the
   **Stationary phase** (plate material, e.g. *silica gel 60 F254*); both are
   recorded with the result.
2. **Straighten it if needed.** Rf compares heights, so the baseline should be
   level. **Rotate** turns the photo by a quarter turn (0°, 90°, 180°, 270°);
   the box next to it fine-tunes by up to ±45°, and the arrows step by 0.5°.
   Rotation is available until you start annotating.
3. **(Optional) Draw a Plate Area.** Click **Plate Area** and drag a box around
   the plate. Detection then ignores everything outside it, which helps on
   photos with a lot of background. **Crop view** zooms to the box, with a
   small margin around it.
4. **Place the baseline and solvent front.** **Suggest Lines** proposes them;
   or click **Min Line** and click on the baseline (where the samples were
   applied), then **Max Line** and click on the solvent front. Drag either
   line to adjust it. Min is drawn in yellow, Max in magenta. If only one
   line is found, the other is an estimate: it is dashed and labelled
   *Min (estimate)* or *Max (estimate)* until you drag it into place.
5. **Mark the spots.** **Suggest Spots** searches between the two lines and
   proposes candidates; **Add Spots** places them by hand. Check for spots it
   missed (faint or touching spots in particular). Drag a spot to move it;
   double-click (double-tap on a touch screen) to delete it.
6. **(Optional) Label lanes and compounds.** **V-Line** marks a lane and
   **H-Line** a compound. Pick a type from the list (for example *starting
   material* or *product 1*), type a label, or both. The ✎ beside a marker
   edits it later. Each spot belongs to the nearest lane and the nearest
   compound marker; an unlabelled lane is left blank and an unmarked compound
   shows as "Unknown".
7. **Click Calculate RF.** The results table and the annotated image appear.
   Rows are numbered, sorted by lane and then by Rf, and the same number
   stands next to each spot on the image. Click a row to ring its spot on
   both images.
8. **Export.** Download the **CSV**, the annotated **Image** or the
   **Annotations** file, or all three as one ZIP with **Download all**; for a
   plate from the ELN, upload any of them back to it. The files are named
   after the photo and the time of the result, e.g.
   `plate7_2026-11-16_14-32_rf.csv`, `..._annotated.png` and
   `..._annotations.json` (`tlc_plate_...` for an ELN image). **Copy Rf
   summary** copies the Rf values as one line in the style of Chemotion's TLC
   description, e.g. `TLC (CH/EtOAc 4:1); Rf (UV-Vis in nm, staining) =
   0.53 (SM)[h1], ...`, to paste into the ELN.

### Good to know

- **Suggested lines and spots are only suggestions.** The models can be
  wrong or miss a spot. Nothing is calculated until you have checked them and
  clicked **Calculate RF**.
- **Undo and redo** every edit with the toolbar arrows or **Ctrl+Z** /
  **Ctrl+Y** (**Ctrl+Shift+Z**).
- **Your work is kept.** The annotations of each image are saved in your
  browser as you go. If the page is reloaded, loading the same photo again
  offers to restore them. **Reset** clears the annotations and that saved
  copy.
- **Annotations file.** The downloaded *Annotations* file (JSON) holds the
  lines, spots, labels, rotation, Plate Area and the mobile and stationary
  phase. **Open annotations** loads it
  back onto the same photo, to check a result later or continue it. It is
  refused for a different photo.
- **What the CSV records.** Besides the results, each spot's source (placed by
  hand, detected, or detected and then moved) and, at the end, how the result
  was made: app version, the photo's name and checksum, rotation, Plate Area,
  mobile and stationary phase, the two models and the time of calculation.
  Its *Details* line is the Rf summary, with the mobile phase in place of
  "solvents" when given.
- **Labels on the image.** By default each spot carries its number. Tick
  **full labels** to add its Rf, compound and lane. Labels on the plate are
  sized from the photo, as on the saved image. **Text size** (10–500%)
  scales the text of the saved image; changing it or **full labels** redraws
  the annotated image without a recalculation.
- **Out-of-date results are flagged.** If you change anything after
  calculating, a banner says so and export stays disabled until you
  recalculate, so an outdated image is never saved.
- **Help** (toolbar) has a short step-by-step reminder. Light and dark themes
  switch at the top right.

### Tips for good results

- Photograph the plate straight on, well lit, at high resolution.
- Straighten the photo first if the baseline is not level.
- On photos with much background, draw the Plate Area before detecting.
- Check the proposed lines before suggesting spots: spots are searched only
  between them.

## For developers and deployers

A Django 5.2 app with a vanilla JavaScript front end, no database, and two
ONNX models for detection. See:

- **[docs/architecture.md](docs/architecture.md)**: design, module map,
  detection, security, memory use, testing, and deployment (Docker,
  environment variables).
- **[docs/eln-integration.md](docs/eln-integration.md)**: the Chemotion
  third-party-app contract and its deployment requirements.
- **[CHANGELOG.md](CHANGELOG.md)**: what changed in each version.

Quick start:

```bash
uv sync
DEBUG=True uv run python manage.py runserver   # then open http://localhost:8000/
```

A deployment needs only `ALLOWED_HOSTS` and, for the ELN, `ELN_ORIGIN`:

```bash
docker build -t tlc-rf-calculator .
docker run --rm -p 8050:8050 \
  -e ALLOWED_HOSTS="tlc.example.org" -e ELN_ORIGIN="https://eln.example.org" \
  tlc-rf-calculator
```

## Authors

Shilpa Bai Thotli, Nicole Jung, and contributors

Karlsruhe Institute of Technology (KIT), Germany.

## License

The code is under the MIT License, see [LICENSE](LICENSE).

The trained models in `tlc/ml_models/` (`spot_best.onnx`, `line_best.onnx`)
are not: they were trained and exported with Ultralytics YOLOv8 and are under
the GNU Affero General Public License v3.0 (AGPL-3.0), as declared in their
metadata. See [tlc/ml_models/LICENSE](tlc/ml_models/LICENSE). The AGPL-3.0
applies to them wherever they are used, including its conditions for running
them as a network service.
