"""Playwright browser tests: the whole workflow in a real browser.

Run with::

    uv run pytest e2e -v
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import Page

# Readiness: the page sets body.dataset.imageReady once an image is drawn.
_IMAGE_READY = "() => document.body.dataset.imageReady === '1'"


def _stub_cdn(page: Page) -> None:
    """Answer requests to external CDN and font hosts locally, so the suite
    never touches the network (the page itself loads nothing external)."""

    def handle(route):
        route.fulfill(status=200, content_type="text/plain", body="")

    for pattern in (
        "**cdnjs.cloudflare.com/**",
        "**cdn.jsdelivr.net/**",
        "**fonts.googleapis.com/**",
    ):
        page.route(pattern, handle)


# Results-table columns: # | Lane | Lane type | Compound | Compound type | Rf.
_COLUMNS = {"n": 0, "lane": 1, "lane_type": 2, "compound": 3, "compound_type": 4, "rf": 5}


def _cell(page: Page, column: str, row: int = 0) -> str:
    """Text of one results-table cell, by column name."""
    cells = page.locator("#values-table tbody tr").nth(row).locator("td")
    return cells.nth(_COLUMNS[column]).inner_text().strip()


def _load_local_image_and_calculate(page: Page, base_url: str, image_path: str) -> str:
    """Run the full local-upload flow; return the first RF value in the table.

    With scaleFactor == 1, clicks map 1:1 to image coordinates. min at y=250,
    max at y=50, spot at y=150 gives RF = (150-250)/(50-250) = 0.5. A constant
    canvas-border offset cancels in the ratio, so the value stays 0.5.
    """
    _stub_cdn(page)
    page.goto(base_url + "/")

    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", image_path)

    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})

    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    return _cell(page, "rf")


def test_local_upload_rf(page, servers, test_image_path):
    """The core flow: upload, place Min/Max and a spot, calculate: Rf 0.5."""
    rf = _load_local_image_and_calculate(page, servers["new"], test_image_path)
    assert float(rf) == 0.5, f"RF={rf!r}"


def test_new_frontend_makes_no_spurious_fetch_on_local_load(page, servers):
    """Loading the page for a local upload fetches no remote image."""
    calls: list[str] = []
    page.on("request", lambda r: "/fetch-image/" in r.url and calls.append(r.url))
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.wait_for_load_state("networkidle")
    assert calls == [], f"unexpected fetch-image calls: {calls}"


def test_vertical_label_flows_to_solvent_column(page, servers, test_image_path):
    """A V-Line label must reach the lane column.

    Exercises the rewritten line-capture + popup path and the backend's
    nearest-line matching end-to-end.
    """
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 200, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 200, "y": 50})

    # Add a labelled vertical line via the popup.
    page.click("button:has-text('V-Line')")
    canvas.click(position={"x": 120, "y": 150})
    page.fill("#line-text", "LaneA")
    page.click("#line-popup button:has-text('Submit')")

    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 260, "y": 150})

    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    solvent = _cell(page, "lane")
    assert solvent == "LaneA", f"lane column was {solvent!r}"


def _load_with_min_max(page, base_url, image_path):
    """Load a local image and place min (y=250) and max (y=50) lines."""
    _stub_cdn(page)
    page.goto(base_url + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", image_path)
    page.wait_for_function(_IMAGE_READY)
    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    return canvas


def test_drag_spot_changes_rf(page, servers, test_image_path):
    """Dragging a spot upward must raise its RF (0.5 -> ~0.75)."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})  # RF 0.5 before drag

    box = canvas.bounding_box()
    page.mouse.move(box["x"] + 150, box["y"] + 150)
    page.mouse.down()
    page.mouse.move(box["x"] + 150, box["y"] + 100, steps=10)  # drag up ~50px
    page.mouse.up()

    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    rf = float(_cell(page, "rf"))
    assert 0.70 <= rf <= 0.80, f"RF after drag was {rf} (expected ~0.75)"


def test_double_click_deletes_spot(page, servers, test_image_path):
    """Double-clicking a spot removes it (two spots -> one row after delete)."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 110, "y": 150})
    canvas.click(position={"x": 210, "y": 150})

    canvas.dblclick(position={"x": 110, "y": 150})  # delete the first spot

    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    rows = page.locator("#values-table tbody tr").count()
    assert rows == 1, f"expected 1 row after deleting one of two spots, got {rows}"


def test_result_row_highlight_and_delete(page, servers, test_image_path):
    """A result row highlights its spot on click and removes it on delete."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 110, "y": 150})
    canvas.click(position={"x": 210, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr")
    assert page.locator("#values-table tbody tr").count() == 2

    first = page.locator("#values-table tbody tr").first
    first.click()
    assert "active" in (first.get_attribute("class") or ""), "row did not highlight"

    first.locator(".row-del").click()
    page.wait_for_function("() => document.querySelectorAll('#values-table tbody tr').length === 1")


def test_deleting_result_row_marks_results_stale(page, servers, test_image_path):
    """Deleting a row makes the annotated image out of date: it must flag the
    results stale (banner + disabled export), not silently keep the old image."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 110, "y": 150})
    canvas.click(position={"x": 210, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert not page.locator("#stale-banner").is_visible()
    assert not page.locator("#export-row .lab-btn").first.is_disabled()

    page.locator("#values-table tbody tr").first.locator(".row-del").click()
    page.wait_for_selector("#stale-banner", state="visible")
    assert page.locator("#export-row .lab-btn").first.is_disabled(), (
        "export not blocked while stale"
    )
    assert page.locator("#values-table tbody tr").count() == 1


def test_spot_sources_reach_the_results(page, servers, test_image_path):
    """Each spot is recorded as manual, detected, or detected then moved."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    scale = page.evaluate("window.scaleFactor")
    candidates = [
        {"x": 150 * scale, "y": 150 * scale, "conf": 0.6},
        {"x": 80 * scale, "y": 120 * scale, "conf": 0.5},
    ]
    page.route(
        "**/detect-spots/",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"spots": candidates, "count": 2, "roi": [0, 0, 1, 1], "model": {}}),
        ),
    )
    with page.expect_response("**/detect-spots/"):
        page.click("#detect-spots-btn")
    page.wait_for_selector("text=Suggested 2 spot(s)")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 220, "y": 180})  # manual

    box = canvas.bounding_box()
    page.mouse.move(box["x"] + 150, box["y"] + 150)
    page.mouse.down()
    page.mouse.move(box["x"] + 150, box["y"] + 110, steps=10)  # move a detected spot
    page.mouse.up()

    with page.expect_response("**/calculate_rf/") as response_info:
        page.click("button:has-text('Calculate RF')")
    rows = response_info.value.json()["rf_values"]
    assert sorted(row["source"] for row in rows) == ["detected", "detected, moved", "manual"]


def _tall_image(tmp_path):
    """A 3000x4000 px photo: the canvas (500 px tall) shows it 8 times smaller."""
    from PIL import Image

    path = tmp_path / "tall.png"
    Image.new("RGB", (3000, 4000), "white").save(path)
    return str(path)


def _route_spots(page: Page, candidates: list[dict]) -> None:
    body = {"spots": candidates, "count": len(candidates), "roi": [0, 0, 1, 1], "model": {}}
    page.route(
        "**/detect-spots/",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        ),
    )


def _detect_spots(page: Page, notice: str) -> None:
    with page.expect_response("**/detect-spots/"):
        page.click("#detect-spots-btn")
    page.wait_for_selector(f"text={notice}", timeout=5000)


# The spots on the plate, in image px: [x, y, detected].
_SPOTS = """async () => {
    const { state } = await import('/static/tlc/js/state.js');
    return state.spots.map((s) => [s.x, s.y, s.auto]);
}"""


def test_close_but_distinct_candidates_are_both_proposed(page, servers, tmp_path):
    """Two nearly touching spots in one lane (60 px boxes, centres 70 px apart)
    are both proposed, however small the photo is shown (here 8 times)."""
    _load_with_min_max(page, servers["new"], _tall_image(tmp_path))
    assert page.evaluate("window.scaleFactor") == 8
    _route_spots(
        page,
        [
            {"x": 1200, "y": 1000, "conf": 0.7, "w": 60, "h": 60},
            {"x": 1200, "y": 1070, "conf": 0.6, "w": 60, "h": 60},
        ],
    )
    _detect_spots(page, "Suggested 2 spot(s)")
    assert page.evaluate(_SPOTS) == [[1200, 1000, True], [1200, 1070, True]]


def test_two_boxes_on_one_spot_become_the_more_confident(page, servers, tmp_path):
    """The model's second box on the same spot (each centre inside the other's
    box) is dropped; the more confident box is kept, whatever the order."""
    _load_with_min_max(page, servers["new"], _tall_image(tmp_path))
    _route_spots(
        page,
        [
            {"x": 1200, "y": 1000, "conf": 0.4, "w": 60, "h": 60},
            {"x": 1203, "y": 1020, "conf": 0.8, "w": 90, "h": 100},
            {"x": 1800, "y": 1500, "conf": 0.5, "w": 60, "h": 60},
        ],
    )
    _detect_spots(page, "Suggested 2 spot(s)")
    assert page.evaluate(_SPOTS) == [[1203, 1020, True], [1800, 1500, True]]


def test_detecting_again_adds_nothing(page, servers, tmp_path):
    """A candidate on a spot already there, placed by hand or by an earlier
    run, is not added again."""
    canvas = _load_with_min_max(page, servers["new"], _tall_image(tmp_path))
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 125})  # about (1200, 1000) in the image
    _route_spots(
        page,
        [
            {"x": 1204, "y": 1004, "conf": 0.7, "w": 80, "h": 80},
            {"x": 1200, "y": 1500, "conf": 0.6, "w": 80, "h": 80},
        ],
    )
    _detect_spots(page, "Suggested 1 spot(s)")
    after_first = page.evaluate(_SPOTS)
    assert len(after_first) == 2 and after_first[1] == [1200, 1500, True]
    _detect_spots(page, "No new spots found")
    assert page.evaluate(_SPOTS) == after_first


def test_a_second_click_on_a_spot_places_no_second_spot(page, servers, tmp_path):
    """Placing by hand keeps its guard: a click within 12 screen px of a spot
    (in x and y) adds nothing; one further away adds a spot."""
    canvas = _load_with_min_max(page, servers["new"], _tall_image(tmp_path))
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.wait_for_timeout(600)  # two separate clicks, not a double click
    # 9 px off in x and y: inside the guard, but beyond the 12 px radius at
    # which a press picks the spot up to drag it.
    canvas.click(position={"x": 159, "y": 159})
    page.wait_for_selector("text=Spot already exists here.", timeout=5000)
    assert len(page.evaluate(_SPOTS)) == 1
    canvas.click(position={"x": 150, "y": 200})
    assert len(page.evaluate(_SPOTS)) == 2


def test_auto_detect_populates_spots(page, servers, blob_image_path):
    """Detect Spots (TLC-SpotDet) finds the drawn spots between Min and Max, and
    they flow into the RF calculation."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 200, "y": 280})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 200, "y": 40})

    page.click("button:has-text('Suggest Spots')")
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    rows = page.locator("#values-table tbody tr").count()
    assert rows == 3, f"expected 3 auto-detected spots, table had {rows}"


def test_plate_region_confines_auto_detect(page, servers, blob_image_path):
    """Drawing a plate area limits Detect Spots to that region (2 of 3 blobs)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 200, "y": 280})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 200, "y": 40})

    # Draw a plate area around the top two blobs (at y=100,150), excluding y=220.
    page.click("button:has-text('Plate Area')")
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + 40, box["y"] + 40)
    page.mouse.down()
    page.mouse.move(box["x"] + 260, box["y"] + 190, steps=8)
    page.mouse.up()

    page.click("button:has-text('Suggest Spots')")
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    rows = page.locator("#values-table tbody tr").count()
    assert rows == 2, f"expected 2 spots within the region, table had {rows}"


def test_rotate_reorients_and_locks_after_annotation(page, servers, blob_image_path):
    """A preset re-orients a clean image (layout flips); annotating locks rotation."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)  # 400x300 landscape
    page.wait_for_function(_IMAGE_READY)

    work_area = page.locator("#work-area")
    assert "layout-rows" in (work_area.get_attribute("class") or "")  # landscape -> rows
    page.select_option("#rotation-preset", "90")
    page.wait_for_function(
        "() => document.getElementById('work-area').classList.contains('layout-columns')"
    )  # rotated to portrait -> columns

    # Annotating locks rotation.
    page.click("button:has-text('Add Spots')")
    page.locator("#image-canvas").click(position={"x": 150, "y": 150})
    page.wait_for_function("() => document.getElementById('rotation-preset').disabled")
    assert page.locator("#rotation-fine").is_disabled()
    assert page.locator(".rotation-nudge").first.is_disabled()


def _dot_image(tmp_path, size=(400, 300), at=(300, 80)):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, "white")
    x, y = at
    ImageDraw.Draw(image).ellipse((x - 6, y - 6, x + 6, y + 6), fill="black")
    path = tmp_path / "dot.png"
    image.save(path)
    return str(path)


_DARK_CENTROID = """() => {
    const c = document.getElementById('image-canvas');
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    let sx = 0, sy = 0, n = 0;
    for (let i = 0; i < d.length; i += 4) {
        if (d[i] < 128) { const p = i / 4; sx += p % c.width; sy += Math.floor(p / c.width); n++; }
    }
    return {x: sx / n + 0.5, y: sy / n + 0.5, n, w: c.width, h: c.height};
}"""


def test_fine_rotation_turns_the_canvas_like_the_server(page, servers, tmp_path):
    """The canvas turns about its centre, keeping its size -- the same formula
    the server's _fine_rotate is tested against -- and the angle is sent."""
    import math

    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", _dot_image(tmp_path))
    page.wait_for_function(_IMAGE_READY)

    page.fill("#rotation-fine", "12")
    page.dispatch_event("#rotation-fine", "change")
    dot = page.evaluate(_DARK_CENTROID)
    assert (dot["w"], dot["h"]) == (400, 300)  # same frame
    t = math.radians(12)
    dx, dy = 300.5 - 200, 80.5 - 150
    assert abs(dot["x"] - (200 + dx * math.cos(t) - dy * math.sin(t))) < 1.0
    assert abs(dot["y"] - (150 + dx * math.sin(t) + dy * math.cos(t))) < 1.0

    # Nudges step by 0.5 degrees; the angle travels with requests.
    page.click("[data-action='nudgeRotation'][data-arg='0.5']")
    assert page.input_value("#rotation-fine") == "12.5"
    with page.expect_request("**/detect-lines/") as request_info:
        page.click("#detect-lines-btn")
    body = request_info.value.post_data_json
    assert (body["rotation"], body["fineRotation"]) == (0, 12.5)


def test_fine_rotation_is_clamped(page, servers, blob_image_path):
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)
    page.fill("#rotation-fine", "80")
    page.dispatch_event("#rotation-fine", "change")
    assert page.input_value("#rotation-fine") == "45.0"


def test_deleting_vline_closes_annotation_popup(page, servers, test_image_path):
    """Deleting a just-drawn V-Line must also dismiss its annotation popup."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)

    page.click("button:has-text('V-Line')")
    page.locator("#image-canvas").click(position={"x": 150, "y": 150})
    # The annotation popup opens for the new line.
    page.wait_for_selector("#line-popup", state="visible")

    # Delete the line via its delete icon; the popup must close.
    page.locator(".delete-icon").first.click()
    page.wait_for_selector("#line-popup", state="hidden")
    assert page.locator(".delete-icon").count() == 0


def test_theme_toggle_switches_and_persists(page, servers):
    """The theme button flips light/dark, overrides the OS default, and persists."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.wait_for_selector("#theme-toggle")

    page.click("#theme-toggle")
    first = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert first in ("light", "dark")

    page.click("#theme-toggle")
    second = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert second in ("light", "dark") and second != first, "toggle did not flip the theme"

    # Persisted across a reload.
    page.reload()
    page.wait_for_selector("#theme-toggle")
    after = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert after == second, f"theme not persisted: {after!r} != {second!r}"


def test_plate_area_does_not_create_reference_lines(page, servers, blob_image_path):
    """A Plate Area is a detection region only — it must NOT create Min/Max lines
    (the plate edges are not the baseline / solvent front). Without them neither
    Detect Spots nor Calculate RF is available; once placed, both work."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Plate Area')")
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + 30, box["y"] + 30)
    page.mouse.down()
    page.mouse.move(box["x"] + 380, box["y"] + 270, steps=8)
    page.mouse.up()

    assert page.locator("#detect-spots-btn").is_disabled(), (
        "plate area must not seed Min/Max — Detect Spots should stay disabled"
    )
    assert page.locator("#calculate-btn").is_disabled()

    # Placing Min/Max by hand (inside the plate area, which confines them)
    # enables detection and the calculation.
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 200, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 200, "y": 45})
    page.click("button:has-text('Suggest Spots')")
    page.wait_for_selector("text=/Suggested \\d+ spot/", timeout=20000)
    page.wait_for_function("() => !document.getElementById('calculate-btn').disabled")
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert page.locator("#values-table tbody tr").count() == 3


def test_detect_spots_needs_min_and_max(page, servers, blob_image_path):
    """Version 1's workflow: spot inference is restricted to the Min/Max band, so
    Detect Spots is disabled until both lines exist, while Detect Lines is
    available as soon as an image is loaded."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    assert page.locator("#detect-lines-btn").is_disabled()  # no image yet
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    assert not page.locator("#detect-lines-btn").is_disabled()
    assert page.locator("#detect-spots-btn").is_disabled()
    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 200, "y": 280})
    assert page.locator("#detect-spots-btn").is_disabled(), "Max is still missing"
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 200, "y": 40})
    page.wait_for_function("() => !document.getElementById('detect-spots-btn').disabled")

    # The request carries the band.
    with page.expect_request("**/detect-spots/") as request_info:
        page.click("button:has-text('Suggest Spots')")
    body = request_info.value.post_data_json
    assert body["minY"] > body["maxY"], body


def _route_detect_lines(page: Page, body: dict) -> None:
    """Answer /detect-lines/ with ``body``: the model needs a real plate photo
    to find lines, so the browser side is tested against a stubbed response."""
    import json

    page.route(
        "**/detect-lines/",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        ),
    )


def test_detect_lines_places_min_and_max(page, servers, blob_image_path):
    """Detect Lines places editable Min/Max lines (one undo step), after which
    Detect Spots and Calculate RF work as with hand-placed lines."""
    _stub_cdn(page)
    _route_detect_lines(page, {"minY": 280, "maxY": 40, "estimated": None, "model": {}})
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    page.click("#detect-lines-btn")
    page.wait_for_selector("text=Placed Min and Max", timeout=10000)
    lines = page.evaluate(
        "() => import('/static/tlc/js/state.js').then(m => "
        "m.state.lines.map(l => [l.type, Math.round(l.y)]))"
    )
    assert sorted(lines) == [["max", 40], ["min", 280]], lines

    page.click("button:has-text('Suggest Spots')")
    page.wait_for_selector("text=/Suggested \\d+ spot/", timeout=20000)
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert page.locator("#values-table tbody tr").count() == 3

    # Both lines came from one detection, so a single undo removes both.
    page.click("#undo-btn")
    page.click("#undo-btn")  # the spot batch, then the line pair
    remaining = page.evaluate(
        "() => import('/static/tlc/js/state.js').then(m => m.state.lines.length)"
    )
    assert remaining == 0, remaining


def test_detect_lines_warns_when_one_line_is_estimated(page, servers, blob_image_path):
    _stub_cdn(page)
    _route_detect_lines(page, {"minY": 270, "maxY": 30.0, "estimated": "max", "model": {}})
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    page.click("#detect-lines-btn")
    page.wait_for_selector("text=is an estimate", timeout=10000)


def test_crop_view_zooms_to_plate(page, servers, blob_image_path):
    """The 'crop view' checkbox zooms the work area to the plate box (view-only)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    box = canvas.bounding_box()
    page.click("button:has-text('Plate Area')")
    page.mouse.move(box["x"] + 40, box["y"] + 40)
    page.mouse.down()
    page.mouse.move(box["x"] + 260, box["y"] + 200, steps=8)
    page.mouse.up()

    container = "() => document.getElementById('canvas-container').style.width"
    assert not page.evaluate(container), "container should be unconstrained before crop"
    page.check("#crop-plate")
    page.wait_for_function(f"{container} !== ''")  # zoomed to region
    page.uncheck("#crop-plate")
    page.wait_for_function(f"{container} === ''")  # back to full view


def test_eln_load_fetches_the_image_once(page, servers, test_image_path):
    """Opening the tool from the ELN fetches the image exactly once.

    /fetch-image/ and /serve-image/ are stubbed so no real (SSRF-guarded)
    network request is made; only the number of fetch-image calls is compared.
    """
    png = Path(test_image_path).read_bytes()
    calls: list[str] = []

    page.route(
        "**/fetch-image/**",
        lambda route: (
            calls.append(route.request.url),
            route.fulfill(
                status=200, content_type="application/json", body='{"imagePath": "stub.png"}'
            ),
        )[1],
    )
    page.route(
        "**/serve-image/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=png),
    )
    _stub_cdn(page)

    page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    assert len(calls) == 1, f"fetch-image calls={len(calls)}"


# --- Touch / pointer support ------------------------------------------------
# These use a touch-enabled browser context (the default `page` fixture has
# none). They exercise the Pointer Events path: a tap adds a spot, a touch drag
# moves one, and a double-tap deletes one.


def _touch_page(browser, servers, image_path):
    """A touch-enabled page loaded with an image and min/max lines."""
    context = browser.new_context(viewport={"width": 1600, "height": 1200}, has_touch=True)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    canvas = _load_with_min_max(page, servers["new"], image_path)
    return context, page, canvas, errors


def test_touch_tap_adds_and_double_tap_deletes_spot(browser, servers, test_image_path):
    """Touch: two taps add two spots; a double-tap removes one (1 row remains)."""
    context, page, canvas, errors = _touch_page(browser, servers, test_image_path)
    try:
        box = canvas.bounding_box()
        page.click("button:has-text('Add Spots')")
        page.touchscreen.tap(box["x"] + 110, box["y"] + 150)  # add spot A
        page.touchscreen.tap(box["x"] + 210, box["y"] + 150)  # add spot B
        page.touchscreen.tap(box["x"] + 110, box["y"] + 150)  # 1st tap on A
        page.touchscreen.tap(box["x"] + 110, box["y"] + 150)  # 2nd tap -> delete A

        page.click("button:has-text('Calculate RF')")
        page.wait_for_selector("#values-table tbody tr", timeout=20000)
        rows = page.locator("#values-table tbody tr").count()
        assert rows == 1, f"expected 1 spot after tap-add x2 + double-tap delete, got {rows}"
        assert not errors, f"JS errors: {errors}"
    finally:
        context.close()


def test_touch_drag_moves_spot(browser, servers, test_image_path):
    """Touch drag (synthetic touch PointerEvents) raises a spot's RF 0.5 -> ~0.75."""
    context, page, canvas, errors = _touch_page(browser, servers, test_image_path)
    try:
        page.click("button:has-text('Add Spots')")
        page.touchscreen.tap(canvas.bounding_box()["x"] + 150, canvas.bounding_box()["y"] + 150)

        # A touch drag from y=150 up to y=100 via the pointer handlers directly.
        page.evaluate(
            """({y0, y1}) => {
                const c = document.getElementById('image-canvas');
                const r = c.getBoundingClientRect();
                const pe = (t, cy) => new PointerEvent(t, {
                    pointerId: 1, pointerType: 'touch', bubbles: true,
                    clientX: r.left + 150, clientY: r.top + cy,
                });
                c.dispatchEvent(pe('pointerdown', y0));
                c.dispatchEvent(pe('pointermove', (y0 + y1) / 2));
                c.dispatchEvent(pe('pointermove', y1));
                c.dispatchEvent(pe('pointerup', y1));
            }""",
            {"y0": 150, "y1": 100},
        )

        page.click("button:has-text('Calculate RF')")
        page.wait_for_selector("#values-table tbody tr", timeout=20000)
        rf = float(_cell(page, "rf"))
        assert 0.70 <= rf <= 0.80, f"RF after touch drag was {rf} (expected ~0.75)"
        assert not errors, f"JS errors: {errors}"
    finally:
        context.close()


def test_text_scale_is_sent_in_payload_and_persists(page, servers, test_image_path):
    """The annotation text-size % flows into the calculate_rf payload (as a
    fraction) and the preference survives a reload."""
    sent: list[str | None] = []
    page.on(
        "request",
        lambda r: sent.append(r.post_data) if r.url.endswith("/calculate_rf/") else None,
    )
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.fill("#text-scale", "250")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)

    bodies = [b.replace(" ", "") for b in sent if b]
    assert any('"textScale":2.5' in b for b in bodies), f"textScale missing/wrong: {bodies}"

    page.reload()
    page.wait_for_selector("#text-scale")
    assert page.locator("#text-scale").input_value() == "250", "text-size preference not persisted"


_EXPORT_DISABLED = "() => document.querySelector('#export-row .lab-btn').disabled"


def test_changing_text_size_redraws_the_image_without_recalculation(page, servers, test_image_path):
    """The text size shapes only the saved image: a change redraws it with the
    same annotations (one request for a typed "250"), never marks the results
    stale, and blocks export only while the new image is on its way."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    first_src = page.get_attribute("#annotated-image", "src")

    held: list = []
    page.route("**/calculate_rf/", lambda route: held.append(route))
    page.fill("#text-scale", "")
    page.locator("#text-scale").press_sequentially("250", delay=60)
    page.wait_for_function(_EXPORT_DISABLED)  # while the redraw is in flight
    page.wait_for_timeout(800)  # past the debounce: no second request
    assert len(held) == 1, len(held)
    sent = held[0].request.post_data_json
    assert sent["textScale"] == 2.5 and len(sent["spots"]) == 1, sent
    held[0].continue_()
    page.wait_for_function(f"() => !({_EXPORT_DISABLED})()")
    assert page.get_attribute("#annotated-image", "src") != first_src
    assert not page.locator("#stale-banner").is_visible()

    # Stale results are left alone: Calculate RF will use the new size.
    canvas.click(position={"x": 220, "y": 150})  # another spot
    page.wait_for_selector("#stale-banner", state="visible")
    page.fill("#text-scale", "120")
    page.wait_for_timeout(800)
    assert len(held) == 1, len(held)


def test_editing_after_calc_marks_stale_then_recalc_clears(page, servers, test_image_path):
    """Adding a spot after a calculation flags results stale (banner + pulsing
    Calculate RF + blocked export); recalculating clears it."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert not page.locator("#stale-banner").is_visible()

    canvas.click(position={"x": 220, "y": 150})  # add another spot
    page.wait_for_selector("#stale-banner", state="visible")
    assert page.locator("#export-row .lab-btn").first.is_disabled()
    assert "pulse" in (page.locator("#calculate-btn").get_attribute("class") or "")

    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#stale-banner", state="hidden")
    assert not page.locator("#export-row .lab-btn").first.is_disabled()


# --- Additional UI-behaviour coverage --------------------------------------


def test_reset_clears_annotations_and_disables_calculate(page, servers, test_image_path):
    """Reset empties the results table and re-disables Calculate RF."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)

    page.click("button:has-text('Reset')")
    page.wait_for_function("() => document.querySelectorAll('#values-table tbody tr').length === 0")
    assert page.locator("#calculate-btn").is_disabled(), "Calculate should be disabled after reset"


def test_horizontal_label_flows_to_compound_column(page, servers, test_image_path):
    """An H-Line label reaches the compound column (mirrors the V-Line path)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    page.click("button:has-text('H-Line')")
    canvas.click(position={"x": 150, "y": 150})
    page.fill("#line-text", "CompoundZ")
    page.click("#line-popup button:has-text('Submit')")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 180})  # off the label line so it isn't a line drag

    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    compound = _cell(page, "compound")
    assert compound == "CompoundZ", f"compound column was {compound!r}"


def test_calculate_button_gates_on_required_annotations(page, servers, test_image_path):
    """Calculate RF stays disabled until min + max + at least one spot exist."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)
    assert page.locator("#calculate-btn").is_disabled(), "disabled with image only"

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    assert page.locator("#calculate-btn").is_disabled(), "still disabled without a spot"

    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.wait_for_function("() => !document.getElementById('calculate-btn').disabled")


def test_help_drawer_toggles_open_and_closed(page, servers, test_image_path):
    """The Help button opens and closes the help drawer via its visible close button."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.locator("[data-action='toggleHelp']").first.click()  # toolbar Help button
    page.wait_for_function(
        "() => document.getElementById('help-drawer').classList.contains('open')"
    )
    # The close button must be a visible control (regression: it was white-on-white,
    # so the user got stuck with no way out).
    close_btn = page.locator("#help-drawer .help-close")
    assert close_btn.is_visible(), "help drawer has no visible close button"
    close_btn.click()
    page.wait_for_function(
        "() => !document.getElementById('help-drawer').classList.contains('open')"
    )


def test_escape_closes_help_drawer(page, servers, test_image_path):
    """Pressing Escape closes the help drawer (it was previously inescapable)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.locator("[data-action='toggleHelp']").first.click()
    page.wait_for_function(
        "() => document.getElementById('help-drawer').classList.contains('open')"
    )
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => !document.getElementById('help-drawer').classList.contains('open')"
    )


def test_escape_closes_line_popup(page, servers, test_image_path):
    """Pressing Escape hides the open line-label popup (keydown handler)."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('V-Line')")
    canvas.click(position={"x": 120, "y": 150})
    page.wait_for_selector("#line-popup", state="visible")
    page.keyboard.press("Escape")
    page.wait_for_selector("#line-popup", state="hidden")


def test_image_source_collapses_on_load_and_reexpands(page, servers, test_image_path):
    """The source card collapses once an image loads and re-expands on click."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)
    page.wait_for_function(
        "() => document.getElementById('source-body').classList.contains('hidden')"
    )

    page.click("#source-change")  # the "Change" button re-expands the source card
    page.wait_for_function(
        "() => !document.getElementById('source-body').classList.contains('hidden')"
    )


def test_line_delete_icon_stays_on_scaled_canvas(page, servers, wide_image_path):
    """A reference line's delete cross must sit on the line even when the canvas
    is CSS-scaled — a low Min/Max line previously pushed its × off the canvas."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", wide_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    scaled = page.evaluate(
        "() => { const c = document.getElementById('image-canvas');"
        " return c.clientWidth < c.width; }"
    )
    assert scaled, "canvas is not CSS-scaled here; the test would not exercise the bug"

    cb = canvas.bounding_box()
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": cb["width"] * 0.5, "y": cb["height"] * 0.92})  # low line

    icon = page.locator(".delete-icon").first
    icon.wait_for(state="visible")
    ib = icon.bounding_box()
    icy = ib["y"] + ib["height"] / 2
    expected = cb["y"] + cb["height"] * 0.92
    assert cb["y"] - 15 <= icy <= cb["y"] + cb["height"] + 15, (
        "delete cross fell outside the canvas"
    )
    assert abs(icy - expected) < 25, (
        f"delete cross not on the line (icon y={icy:.0f}, line y≈{expected:.0f})"
    )


# --- Confining annotations to the plate area --------------------------------


def _draw_region(page, canvas, fx0, fy0, fx1, fy1):
    """Arm Plate Area and drag a box given as canvas fractions."""
    box = canvas.bounding_box()
    page.click("button:has-text('Plate Area')")
    page.mouse.move(box["x"] + box["width"] * fx0, box["y"] + box["height"] * fy0)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * fx1, box["y"] + box["height"] * fy1, steps=6)
    page.mouse.up()


def test_min_line_click_blocked_outside_plate_area(page, servers, blob_image_path):
    """With a plate area, a Min-line click above it places nothing; inside works."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    _draw_region(page, canvas, 0.15, 0.30, 0.85, 0.85)  # box in the lower area

    page.click("button:has-text('Min Line')")
    canvas.click(
        position={
            "x": canvas.bounding_box()["width"] * 0.5,
            "y": canvas.bounding_box()["height"] * 0.05,
        }
    )
    # A line places a .delete-icon; a blocked click places none.
    assert page.locator(".delete-icon").count() == 0, "min line was placed outside the plate area"

    canvas.click(
        position={
            "x": canvas.bounding_box()["width"] * 0.5,
            "y": canvas.bounding_box()["height"] * 0.6,
        }
    )
    page.wait_for_function("() => document.querySelectorAll('.delete-icon').length === 1")


def test_spot_click_blocked_outside_plate_area(page, servers, blob_image_path):
    """With a plate area, an Add-Spots click outside it adds no spot."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    _draw_region(page, canvas, 0.15, 0.30, 0.85, 0.85)
    w = canvas.bounding_box()["width"]
    h = canvas.bounding_box()["height"]
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": w * 0.5, "y": h * 0.8})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": w * 0.5, "y": h * 0.35})

    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": w * 0.5, "y": h * 0.05})  # above the box -> blocked
    assert page.locator("#calculate-btn").is_disabled(), "spot was added outside the plate area"

    canvas.click(position={"x": w * 0.5, "y": h * 0.6})  # inside -> added
    page.wait_for_function("() => !document.getElementById('calculate-btn').disabled")


def test_redraw_deletes_spots_outside_and_keeps_inside(page, servers, blob_image_path):
    """Redrawing a smaller plate area deletes spots outside the new box (lines clamp)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    w = canvas.bounding_box()["width"]
    h = canvas.bounding_box()["height"]

    _draw_region(page, canvas, 0.08, 0.08, 0.92, 0.95)  # big box covering everything
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": w * 0.5, "y": h * 0.9})  # baseline near bottom
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": w * 0.5, "y": h * 0.12})  # front near top
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": w * 0.3, "y": h * 0.75})  # lower spot (kept)
    canvas.click(position={"x": w * 0.7, "y": h * 0.20})  # upper spot (will be dropped)

    # Redraw a smaller box covering only the lower half; the upper spot is dropped,
    # the Max line clamps down to the new top edge.
    _draw_region(page, canvas, 0.08, 0.50, 0.92, 0.95)

    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert page.locator("#values-table tbody tr").count() == 1, (
        "spot outside the redrawn area was not dropped"
    )


def test_drag_line_is_clamped_to_plate_area(page, servers, blob_image_path):
    """Dragging a Min line below the plate area stops it at the bottom edge."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    box = canvas.bounding_box()
    _draw_region(page, canvas, 0.2, 0.2, 0.8, 0.7)  # region bottom at 0.7 of the canvas

    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": box["width"] * 0.5, "y": box["height"] * 0.5})  # inside

    # Drag the line well below the plate bottom; it must clamp to the 0.7 edge.
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.98, steps=10)
    page.mouse.up()

    icon = page.locator(".delete-icon").first
    icon.wait_for(state="visible")
    ib = icon.bounding_box()
    icy = ib["y"] + ib["height"] / 2
    edge = box["y"] + box["height"] * 0.7  # plate-area bottom, in screen px
    assert abs(icy - edge) < 15, (
        f"min line not clamped to plate bottom (icon y={icy:.0f}, edge={edge:.0f})"
    )


# --- Undo / redo ------------------------------------------------------------


def test_undo_redo_spot_edits(page, servers, test_image_path):
    """Undo removes the last edit and redo re-applies it; recalculating reflects
    the current spots. Undo/redo buttons enable/disable correctly."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    # Placing Min + Max already produced undoable history.
    assert not page.locator("#undo-btn").is_disabled()
    assert page.locator("#redo-btn").is_disabled()

    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 130, "y": 150})
    canvas.click(position={"x": 210, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert page.locator("#values-table tbody tr").count() == 2

    # Undo drops the second spot; recalculate -> 1 row.
    page.click("#undo-btn")
    assert not page.locator("#redo-btn").is_disabled()
    page.click("button:has-text('Calculate RF')")
    page.wait_for_function("() => document.querySelectorAll('#values-table tbody tr').length === 1")

    # Redo brings it back; recalculate -> 2 rows.
    page.click("#redo-btn")
    page.click("button:has-text('Calculate RF')")
    page.wait_for_function("() => document.querySelectorAll('#values-table tbody tr').length === 2")


def test_undo_keyboard_shortcut(page, servers, test_image_path):
    """Ctrl+Z undoes the last edit (a placed spot)."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 130, "y": 150})
    canvas.click(position={"x": 210, "y": 150})
    page.keyboard.press("Control+z")  # undo the 2nd spot
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert page.locator("#values-table tbody tr").count() == 1


def test_unmarked_lane_blank_compound_unknown_in_table(page, servers, test_image_path):
    """With no V-Line/H-Line, the results table shows a blank lane (Mixture) and
    an "Unknown" compound (Substance) — the lane name is not invented."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert _cell(page, "lane") == "", "unmarked lane should be blank"
    assert _cell(page, "compound") == "Unknown"


# --- Export buttons: downloads vs ELN uploads --------------------------------

_EXPORT_VISIBLE = (
    "() => Array.from(document.querySelectorAll('#export-row .lab-btn'))"
    ".filter((b) => b.offsetParent !== null).length"
)


def test_export_local_shows_only_download_pair(page, servers, test_image_path):
    """A local upload shows the three downloads, Download all and Copy Rf
    summary (the ELN uploads are hidden)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)
    assert page.evaluate(_EXPORT_VISIBLE) == 5
    assert not page.locator("#export-row .export-eln").first.is_visible()


def test_export_eln_shows_all_eight(page, servers, test_image_path):
    """An ELN-sourced plate shows all eight buttons (download + upload × CSV,
    Image, Annotations; Download all; Copy Rf summary)."""
    png = Path(test_image_path).read_bytes()
    page.route(
        "**/fetch-image/**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"imagePath": "stub.png"}'
        ),
    )
    page.route(
        "**/serve-image/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=png),
    )
    _stub_cdn(page)
    page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")
    page.wait_for_function(_IMAGE_READY)
    assert page.evaluate(_EXPORT_VISIBLE) == 8


def test_eln_upload_names_the_file_and_sends_no_csrf_token(page, servers, test_image_path):
    """Results go to the ELN's token URL with a readable `attachmentName`, and
    without our CSRF token (meaningless there, and it forces a preflight)."""
    png = Path(test_image_path).read_bytes()
    page.route(
        "**/fetch-image/**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"imagePath": "stub.png"}'
        ),
    )
    page.route(
        "**/serve-image/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=png),
    )
    result = {
        "rf_values": [
            {
                "n": 1,
                "x": 150,
                "y": 150,
                "rf": 0.5,
                "solvent": "",
                "solvent_type": "",
                "compound": "Unknown",
                "compound_type": "",
            }
        ],
        "annotated_image_url": "/media/stub-annotated.png",
        "csv_url": "/media/stub.csv",
    }
    page.route(
        "**/calculate_rf/**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(result)
        ),
    )
    page.route(
        "**/media/stub.csv",
        lambda route: route.fulfill(status=200, content_type="text/csv", body="spot\n1\n"),
    )
    page.route(
        "**/media/stub-annotated.png",
        lambda route: route.fulfill(status=200, content_type="image/png", body=png),
    )
    uploads = []

    def eln(route):
        uploads.append(route.request)
        route.fulfill(
            status=200,
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
            body='{"message": "File uploaded successfully"}',
        )

    page.route("http://eln.example/**", eln)
    _stub_cdn(page)
    page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr")
    page.click("[data-action='csvToEln']")
    page.wait_for_selector("text=Saved to ELN")

    assert len(uploads) == 1
    request = uploads[0]
    assert request.method == "POST"
    assert "x-csrftoken" not in {k.lower() for k in request.headers}
    body = request.post_data_buffer.decode("utf-8", "replace")
    # Named like the downloads; an ELN image has no file name of its own.
    assert re.search(
        r'name="attachmentName"\r\n\r\ntlc_plate_\d{4}-\d\d-\d\d_\d\d-\d\d_rf\.csv', body
    )


def test_eln_upload_failure_is_reported(page, servers, test_image_path):
    """A refused upload (e.g. the token's upload limit) says so, not "Saved"."""
    png = Path(test_image_path).read_bytes()
    page.route(
        "**/fetch-image/**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"imagePath": "stub.png"}'
        ),
    )
    page.route(
        "**/serve-image/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=png),
    )
    page.route(
        "**/calculate_rf/**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "rf_values": [
                        {
                            "n": 1,
                            "x": 150,
                            "y": 150,
                            "rf": 0.5,
                            "solvent": "",
                            "solvent_type": "",
                            "compound": "Unknown",
                            "compound_type": "",
                        }
                    ],
                    "annotated_image_url": "/media/a.png",
                    "csv_url": "/media/s.csv",
                }
            ),
        ),
    )
    page.route("**/media/s.csv", lambda route: route.fulfill(status=200, body="spot\n"))
    page.route(
        "http://eln.example/**",
        lambda route: route.fulfill(
            status=403, headers={"Access-Control-Allow-Origin": "*"}, body="{}"
        ),
    )
    _stub_cdn(page)
    page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")
    page.wait_for_function(_IMAGE_READY)
    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr")
    page.click("[data-action='csvToEln']")
    page.wait_for_selector("text=Failed to save to ELN")


def test_min_above_max_is_explained_not_computed(page, servers, test_image_path):
    """SI Eq. 4: the baseline must lie below the solvent front. Swapped lines
    get the server's explanation instead of a generic failure."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 50})  # Min near the top: wrong
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("text=baseline below", timeout=10000)
    assert page.locator("#values-table tbody tr").count() == 0


def test_detect_lines_is_confined_to_the_plate_area(page, servers, blob_image_path):
    """With a Plate Area drawn, Detect Lines sends it, so the model only sees
    the plate (photos with much background otherwise get a line on it)."""
    _stub_cdn(page)
    _route_detect_lines(page, {"minY": 250, "maxY": 60, "estimated": None, "model": {}})
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Plate Area')")
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + 30, box["y"] + 30)
    page.mouse.down()
    page.mouse.move(box["x"] + 380, box["y"] + 270, steps=8)
    page.mouse.up()

    with page.expect_request("**/detect-lines/") as request_info:
        page.click("#detect-lines-btn")
    region = request_info.value.post_data_json.get("region")
    assert region and region["width"] > 300 and region["height"] > 200, region


# --- Reviewer items 4-9 -----------------------------------------------------

_LINES = (
    "() => import('/static/tlc/js/state.js')"
    ".then(m => m.state.lines.map(l => [l.type, l.text, l.kind]))"
)


def _add_marker(page, canvas, button, x, y, text="", kind=""):
    page.click(f"button:has-text('{button}')")
    canvas.click(position={"x": x, "y": y})
    if kind:
        page.select_option("#line-kind", kind)
    page.fill("#line-text", text)
    page.click("#line-popup button:has-text('Submit')")


def test_min_max_buttons_hide_once_placed(page, servers, test_image_path):
    """Item 4: Min/Max are singletons, moved by dragging, so their buttons go
    away once placed and come back if the line is deleted (or undone)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", test_image_path)
    page.wait_for_function(_IMAGE_READY)
    canvas = page.locator("#image-canvas")

    page.click("#min-line-btn")
    canvas.click(position={"x": 150, "y": 250})
    assert page.locator("#min-line-btn").is_hidden()
    assert page.locator("#max-line-btn").is_visible()
    page.click("#max-line-btn")
    canvas.click(position={"x": 150, "y": 50})
    assert page.locator("#max-line-btn").is_hidden()

    page.locator(".delete-icon:not(.edit-icon)").first.click()  # the Min line's cross
    page.wait_for_selector("#min-line-btn", state="visible")
    page.click("#undo-btn")
    page.wait_for_selector("#min-line-btn", state="hidden")


def test_h_line_is_drawn_black(page, servers, test_image_path):
    """Item 5: the H-line was purple, which read as grey on green plates."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    _add_marker(page, canvas, "H-Line", 150, 150, "SM")
    # Darkest pixel in a short column across the line (the click may land a
    # pixel off, and the line's edges are anti-aliased against its halo).
    darkest = page.evaluate(
        "() => { const d = document.getElementById('image-canvas').getContext('2d')"
        ".getImageData(200, 140, 1, 20).data; let m = 255;"
        " for (let i = 0; i < d.length; i += 4) m = Math.min(m, Math.max(d[i], d[i+1], d[i+2]));"
        " return m; }"
    )
    assert darkest < 40, f"darkest H-line pixel is {darkest}, expected black"


def test_label_types_reach_table_and_can_be_edited(page, servers, test_image_path):
    """Items 6 and 7: a type from the dropdown plus free text; the label can be
    edited afterwards (pencil icon), and Cancel on an edit changes nothing."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    _add_marker(page, canvas, "H-Line", 150, 150, "SM-1", "starting material")
    _add_marker(page, canvas, "V-Line", 100, 150, "R1", "product 1")
    options = page.locator("#line-kind option").all_inner_texts()
    assert "product 2" in options and "expected product" not in options, options

    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 115, "y": 170})  # beside the V-line, not on it
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert (_cell(page, "compound"), _cell(page, "compound_type")) == ("SM-1", "starting material")
    assert (_cell(page, "lane"), _cell(page, "lane_type")) == ("R1", "product 1")

    # Edit the H-line: the popup opens pre-filled; changing it marks results stale.
    page.locator(".edit-icon").first.click()
    assert page.input_value("#line-text") == "SM-1"
    assert page.input_value("#line-kind") == "starting material"
    page.select_option("#line-kind", "confirmed product")
    page.fill("#line-text", "SM-2")
    page.click("#line-popup button:has-text('Submit')")
    page.wait_for_selector("#stale-banner", state="visible")
    assert ["horizontal", "SM-2", "confirmed product"] in page.evaluate(_LINES)

    # Undo restores the previous label, type included.
    page.click("#undo-btn")
    assert ["horizontal", "SM-1", "starting material"] in page.evaluate(_LINES)

    # Cancel on an edit leaves the label as it was.
    page.locator(".edit-icon").first.click()
    page.fill("#line-text", "discarded")
    page.click("#line-popup button:has-text('Cancel')")
    assert ["horizontal", "SM-1", "starting material"] in page.evaluate(_LINES)


def test_table_is_numbered_sorted_by_lane_and_compact(page, servers, test_image_path):
    """Item 9: rows sorted by V-line label (natural order), then Rf; numbered
    like the image; no Status column; type columns hidden when unused; the
    table is as wide as its content."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    _add_marker(page, canvas, "V-Line", 250, 150, "Lane 10")
    _add_marker(page, canvas, "V-Line", 60, 150, "Lane 2")
    page.click("button:has-text('Add Spots')")
    for x, y in ((265, 150), (75, 100), (75, 200)):  # beside the V-lines, not on them
        canvas.click(position={"x": x, "y": y})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr >> nth=2", timeout=20000)

    rows = [(_cell(page, "n", i), _cell(page, "lane", i), _cell(page, "rf", i)) for i in range(3)]
    assert rows == [("1", "Lane 2", "0.25"), ("2", "Lane 2", "0.75"), ("3", "Lane 10", "0.50")]
    headers = page.locator("#values-table thead th:visible").all_inner_texts()
    assert "Status" not in headers and "Lane type" not in headers, headers
    table_w = page.locator("#values-table").bounding_box()["width"]
    panel_w = page.locator(".table-scroll").bounding_box()["width"]
    assert table_w < panel_w, (table_w, panel_w)


def test_csv_download_is_the_server_file(page, servers, test_image_path):
    """The CSV comes from the server, with the table's columns in the table's
    order, the CHMO header line, and labels properly quoted."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    _add_marker(page, canvas, "V-Line", 150, 150, "A, crude", "product 1")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 165, "y": 100})  # beside the V-line, not on it
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)

    with page.expect_download() as info:
        page.click("[data-action='saveCSV']")
    text = Path(info.value.path()).read_text(encoding="utf-8")
    assert text.startswith("Measurement type;thin layer chromatography (TLC);CHMO:0001007\n")
    assert '1,"A, crude",product 1,Unknown,,0.75' in text, text


def test_full_labels_switch_reaches_the_server(page, servers, test_image_path):
    """Item 8: numbers only by default; "full labels" asks for Rf, compound and
    lane next to each number. It changes only the saved image, which is redrawn
    at once: the results do not go stale."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    with page.expect_request("**/calculate_rf/") as info:
        page.click("button:has-text('Calculate RF')")
    assert info.value.post_data_json["labelMode"] == "numbers"
    page.wait_for_selector("#values-table tbody tr", timeout=20000)

    with page.expect_request("**/calculate_rf/") as info:
        page.check("#label-full")
    assert info.value.post_data_json["labelMode"] == "full"
    assert not page.locator("#stale-banner").is_visible()


# --- Local image uploaded once -------------------------------------------------


def test_local_image_is_uploaded_once_then_referenced(page, servers, test_image_path):
    """The image travels once; detection and calculation send only its name."""
    uploads, bodies = [], []
    page.on("request", lambda r: "/upload-image/" in r.url and uploads.append(r))
    page.on(
        "request",
        lambda r: (
            any(p in r.url for p in ("/calculate_rf/", "/detect-"))
            and bodies.append(r.post_data_json)
        ),
    )
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Suggest Spots')")
    page.wait_for_selector("text=/spot/i", timeout=20000)
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)

    assert len(uploads) == 1
    assert len(bodies) == 2
    for body in bodies:
        assert body["imageData"] is None and body["imageSrc"].endswith(".png"), body
    assert float(_cell(page, "rf")) == 0.5


def test_failed_upload_falls_back_to_sending_the_image(page, servers, test_image_path):
    page.route("**/upload-image/", lambda route: route.fulfill(status=500, body="{}"))
    bodies = []
    page.on("request", lambda r: "/calculate_rf/" in r.url and bodies.append(r.post_data_json))
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    assert bodies[0]["imageData"].startswith("data:image/"), "fallback did not send the image"
    assert float(_cell(page, "rf")) == 0.5


def test_placing_a_line_across_another_does_not_move_it(page, servers, test_image_path):
    """With a line tool armed, clicking where it crosses an existing line places
    the new line only. It used to start a drag of the existing line too (nudging
    it) and the post-drag guard then swallowed the next click."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    _add_marker(page, canvas, "H-Line", 150, 150, "SM")
    before = page.evaluate(_LINES.replace("[l.type, l.text, l.kind]", "[l.type, l.y]"))
    _add_marker(page, canvas, "V-Line", 100, 150, "R1")  # right on the H-line
    after = page.evaluate(_LINES.replace("[l.type, l.text, l.kind]", "[l.type, l.y]"))
    assert after[:3] == before, (before, after)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 115, "y": 170})  # immediately: must not be swallowed
    assert (
        page.evaluate("() => import('/static/tlc/js/state.js').then(m => m.state.spots.length)")
        == 1
    )


# --- Annotation files and drafts ---------------------------------------------


def _annotate_and_calculate(page, canvas):
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    _add_marker(page, canvas, "V-Line", 120, 150, text="Lane A", kind="product 1")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 135, "y": 150})
    canvas.click(position={"x": 135, "y": 100})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr")


def _load_local(page, servers, image_path):
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", image_path)
    page.wait_for_function(_IMAGE_READY)
    page.wait_for_function("() => document.body.dataset.imageIdentified === '1'")
    return page.locator("#image-canvas")


def test_annotations_export_and_reopen(page, servers, test_image_path, tmp_path):
    """Downloaded annotations reopen onto the same image and give the same Rf."""
    canvas = _load_local(page, servers, test_image_path)
    _annotate_and_calculate(page, canvas)
    before = [_cell(page, "rf", row) for row in range(2)]

    with page.expect_download() as download_info:
        page.click("[data-action='saveAnnotations']")
    path = tmp_path / "annotations.json"
    download_info.value.save_as(path)
    data = json.loads(path.read_text())
    assert data["format"] == "tlc-rf-annotations" and data["version"] == 1
    assert len(data["image"]["sha256"]) == 64
    assert [line["type"] for line in data["lines"]] == ["min", "max", "vertical"]
    assert data["lines"][2]["text"] == "Lane A" and data["lines"][2]["kind"] == "product 1"
    assert [spot["source"] for spot in data["spots"]] == ["manual", "manual"]

    page.click("[data-action='resetCanvas']")
    page.set_input_files("#annotations-input", str(path))
    page.wait_for_selector("text=Annotations opened.")
    page.click("button:has-text('Calculate RF')")
    page.wait_for_function("() => document.querySelectorAll('#values-table tbody tr').length === 2")
    assert [_cell(page, "rf", row) for row in range(2)] == before
    assert _cell(page, "lane") == "Lane A"


def test_annotations_for_another_image_are_refused(page, servers, test_image_path, tmp_path):
    canvas = _load_local(page, servers, test_image_path)  # hashed: the check is real
    other = {
        "format": "tlc-rf-annotations",
        "version": 1,
        "image": {"name": "other.jpg", "sha256": "0" * 64},
        "rotation": 0,
        "fineRotation": 0,
        "plateRegion": None,
        "lines": [{"type": "min", "y": 250}],
        "spots": [],
    }
    path = tmp_path / "other.json"
    path.write_text(json.dumps(other))
    page.set_input_files("#annotations-input", str(path))
    page.wait_for_selector('text=belong to a different image ("other.jpg")')
    assert canvas.is_visible()
    assert page.evaluate("() => document.querySelectorAll('#values-table tbody tr').length") == 0


def test_invalid_annotation_file_is_explained(page, servers, test_image_path, tmp_path):
    _load_local(page, servers, test_image_path)
    path = tmp_path / "notes.json"
    path.write_text('{"hello": 1}')
    page.set_input_files("#annotations-input", str(path))
    page.wait_for_selector("text=This is not a TLC annotation file.")


def test_draft_is_offered_after_reload_and_restores(page, servers, test_image_path):
    """Work survives a reload: loading the same image offers the draft; it is
    never restored by itself."""
    canvas = _load_local(page, servers, test_image_path)
    _annotate_and_calculate(page, canvas)
    page.reload()
    canvas = _load_local(page, servers, test_image_path)
    page.wait_for_selector("#draft-banner:not([hidden])")
    assert "2 spots" in page.inner_text("#draft-text")
    assert page.evaluate("() => document.querySelectorAll('#values-table tbody tr').length") == 0
    page.click("[data-action='restoreDraft']")
    page.wait_for_selector("text=Annotations restored.")
    assert page.locator("#draft-banner").is_hidden()
    page.click("button:has-text('Calculate RF')")
    page.wait_for_function("() => document.querySelectorAll('#values-table tbody tr').length === 2")


def test_reset_and_discard_delete_the_draft(page, servers, test_image_path):
    canvas = _load_local(page, servers, test_image_path)
    _annotate_and_calculate(page, canvas)
    page.click("[data-action='resetCanvas']")
    page.reload()
    _load_local(page, servers, test_image_path)  # hashed: a draft would be offered now
    assert page.locator("#draft-banner").is_hidden()

    canvas = page.locator("#image-canvas")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.reload()
    _load_local(page, servers, test_image_path)
    page.wait_for_selector("#draft-banner:not([hidden])")
    page.click("[data-action='discardDraft']")
    page.reload()
    _load_local(page, servers, test_image_path)
    assert page.locator("#draft-banner").is_hidden()


def test_annotations_reopen_with_rotation(page, servers, blob_image_path, tmp_path):
    """The file carries the rotation; opening it re-applies it before the marks."""
    canvas = _load_local(page, servers, blob_image_path)
    page.select_option("#rotation-preset", "90")
    page.fill("#rotation-fine", "2.5")
    page.dispatch_event("#rotation-fine", "change")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr")
    with page.expect_download() as download_info:
        page.click("[data-action='saveAnnotations']")
    path = tmp_path / "rotated.json"
    download_info.value.save_as(path)

    page.click("[data-action='resetCanvas']")
    page.select_option("#rotation-preset", "0")
    page.fill("#rotation-fine", "0")
    page.dispatch_event("#rotation-fine", "change")
    page.set_input_files("#annotations-input", str(path))
    page.wait_for_selector("text=Annotations opened.")
    assert page.input_value("#rotation-preset") == "90"
    assert page.input_value("#rotation-fine") == "2.5"


def test_phone_width_fits_and_taps_land_where_shown(browser, servers, tmp_path):
    """At 360 px nothing scrolls sideways; the canvas is scaled down to fit and
    a tap still lands on the image point shown under it."""
    page = browser.new_page(viewport={"width": 360, "height": 800})
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", _dot_image(tmp_path, size=(600, 400), at=(300, 200)))
    page.wait_for_function(_IMAGE_READY)
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") == 0

    canvas = page.locator("#image-canvas")
    box = canvas.bounding_box()
    assert box["width"] < 360  # scaled down from its 600 px
    h = box["height"]
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": box["width"] / 2, "y": h * 0.8})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": box["width"] / 2, "y": h * 0.2})
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": box["width"] * 0.3, "y": h * 0.5})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr")
    assert float(_cell(page, "rf")) == 0.5
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") == 0
    page.close()


# --- Review fixes: rotation with history, typing an angle, image loads --------

_ROWS = "() => document.querySelectorAll('#values-table tbody tr').length"


def _min_max_spot(page, canvas):
    page.click("button:has-text('Min Line')")
    canvas.click(position={"x": 150, "y": 250})
    page.click("button:has-text('Max Line')")
    canvas.click(position={"x": 150, "y": 50})
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})


def test_turning_the_image_leaves_nothing_to_redo(page, servers, blob_image_path):
    """Marks undone at 0 deg must not be redone onto the image turned to 90."""
    canvas = _load_local(page, servers, blob_image_path)
    _min_max_spot(page, canvas)
    for _ in range(3):
        page.click("#undo-btn")
    page.select_option("#rotation-preset", "90")
    page.wait_for_function("() => document.getElementById('image-canvas').width === 300")
    assert page.locator("#redo-btn").is_disabled()
    page.keyboard.press("Control+y")
    assert page.input_value("#rotation-preset") == "90"
    assert page.locator("#calculate-btn").is_disabled()  # no marks came back


def test_undo_restores_the_rotation_the_marks_were_made_in(
    page, servers, blob_image_path, tmp_path
):
    """Opening a file saved at 90 over work done at 0, then undoing, brings the
    0-deg work back at 0 deg (and redo the file's marks at 90)."""
    canvas = _load_local(page, servers, blob_image_path)  # 400x300
    _min_max_spot(page, canvas)
    turned = {
        "format": "tlc-rf-annotations",
        "version": 1,
        "image": {"name": "blobs.png"},  # no hash: opened as "could not be checked"
        "rotation": 90,
        "fineRotation": 3,
        "plateRegion": None,
        "lines": [{"type": "min", "y": 350}, {"type": "max", "y": 50}],
        "spots": [{"x": 150, "y": 200, "source": "manual"}],
    }
    path = tmp_path / "turned.json"
    path.write_text(json.dumps(turned))
    page.set_input_files("#annotations-input", str(path))
    page.wait_for_selector("text=Annotations opened.")
    assert page.input_value("#rotation-preset") == "90"

    page.click("#undo-btn")
    assert page.input_value("#rotation-preset") == "0"
    assert page.input_value("#rotation-fine") == "0.0"
    assert page.evaluate("() => document.getElementById('image-canvas').width") == 400
    with page.expect_request("**/calculate_rf/") as request_info:
        page.click("#calculate-btn")
    body = request_info.value.post_data_json
    assert (body["rotation"], body["fineRotation"]) == (0, 0)
    assert max(line["y"] for line in body["lines"]) < 300  # 0-deg frame: 300 tall
    page.wait_for_selector("#values-table tbody tr")

    page.click("#redo-btn")
    assert page.input_value("#rotation-preset") == "90"
    assert page.input_value("#rotation-fine") == "3.0"
    assert page.evaluate("() => document.getElementById('image-canvas').width") == 300


def test_typing_a_fine_angle_is_not_rewritten_under_the_cursor(page, servers, tmp_path):
    """Real key presses: the field keeps what is typed, is tidied on blur, and an
    emptied field restores the angle instead of zeroing it."""
    _load_local(page, servers, _dot_image(tmp_path))
    fine = page.locator("#rotation-fine")

    fine.fill("")
    fine.press_sequentially("12")
    assert fine.input_value() == "12"
    fine.press("Tab")
    assert fine.input_value() == "12.0"

    fine.fill("")
    fine.press_sequentially("2.5")
    assert fine.input_value() == "2.5"
    fine.press("Tab")
    assert fine.input_value() == "2.5"

    fine.fill("")
    fine.press("Tab")
    assert fine.input_value() == "2.5"
    with page.expect_request("**/detect-lines/") as request_info:
        page.click("#detect-lines-btn")
    assert request_info.value.post_data_json["fineRotation"] == 2.5


def test_an_image_that_cannot_be_decoded_leaves_the_session_alone(page, servers, blob_image_path):
    """A file the browser cannot decode changes nothing: the plate, its rotation,
    its marks and its upload stay as they were."""
    canvas = _load_local(page, servers, blob_image_path)
    page.select_option("#rotation-preset", "90")
    page.wait_for_function("() => document.getElementById('image-canvas').width === 300")
    _min_max_spot(page, canvas)

    page.set_input_files(
        "#local-image-input",
        {"name": "x.png", "mimeType": "image/png", "buffer": b"not an image at all"},
    )
    page.wait_for_selector("text=Failed to load image")
    assert page.input_value("#rotation-preset") == "90"
    with page.expect_request("**/calculate_rf/") as request_info:
        page.click("#calculate-btn")
    body = request_info.value.post_data_json
    assert body["rotation"] == 90 and body["imageName"] == "blobs.png"
    assert body["imageData"] is None and body["imageSrc"].endswith(".png")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)


def _stub_eln_image(page, image_path):
    png = Path(image_path).read_bytes()
    page.route(
        "**/fetch-image/**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"imagePath": "stub.png"}'
        ),
    )
    page.route(
        "**/serve-image/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=png),
    )


def _wait_identified(page):
    page.wait_for_function("() => document.body.dataset.imageIdentified === '1'")


def test_switching_to_the_eln_image_starts_a_clean_session(
    page, servers, test_image_path, blob_image_path
):
    """Marks made on a local plate do not follow onto the ELN image."""
    _stub_eln_image(page, test_image_path)
    _stub_cdn(page)
    page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")
    _wait_identified(page)

    page.click("#source-change")  # re-expand the collapsed source card
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_selector('text=Image "blobs.png" loaded.')
    _wait_identified(page)
    canvas = page.locator("#image-canvas")
    _min_max_spot(page, canvas)
    page.click("#calculate-btn")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)

    page.click("#source-change")
    page.click("#eln-source-btn")
    page.wait_for_function("() => document.getElementById('image-canvas').width === 300")
    _wait_identified(page)
    assert page.locator("#calculate-btn").is_disabled()
    assert page.locator("#undo-btn").is_disabled()
    assert page.evaluate(_ROWS) == 0


def _calculation_request(page):
    """Click Calculate and return the request body; the server is not reached."""
    page.route("**/calculate_rf/**", lambda route: route.abort())
    with page.expect_request("**/calculate_rf/**") as request_info:
        page.click("#calculate-btn")
    return request_info.value.post_data_json


def test_choosing_local_without_a_file_keeps_the_eln_session(page, servers, test_image_path):
    """Clicking "Upload local" only shows the file input: until a file loads,
    the ELN image stays an ELN image (uploads offered, sent by reference)."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")  # without ?url= there is no ELN to upload to
    assert not page.locator("#export-row .export-eln").first.is_visible()

    _stub_eln_image(page, test_image_path)
    page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")
    _wait_identified(page)
    page.click("#source-change")
    page.click("#local-source-btn")  # ... but no file is picked
    assert page.evaluate(_EXPORT_VISIBLE) == 8

    _min_max_spot(page, page.locator("#image-canvas"))
    body = _calculation_request(page)
    assert body["isLocalImage"] is False and body["imageSrc"] == "stub.png", body
    assert body["imageData"] is None


def test_only_the_latest_image_load_starts_a_session(
    page, servers, test_image_path, blob_image_path
):
    """An image that finishes loading after a later pick is dropped. The ELN
    image is held back at the server stub until a local file has loaded, so
    the earlier request is certain to decode last."""
    held = []
    png = Path(test_image_path).read_bytes()

    def serve(route):
        if not held:
            held.append(route)  # the ELN fetch: answered below
        else:
            route.fulfill(status=200, content_type="image/png", body=png)

    page.route(
        "**/fetch-image/**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"imagePath": "stub.png"}'
        ),
    )
    page.route("**/serve-image/**", serve)
    _stub_cdn(page)
    with page.expect_request("**/serve-image/**"):
        page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")

    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_selector('text=Image "blobs.png" loaded.')
    _wait_identified(page)

    # Release the ELN image; once its <img> request is done, give it time to decode.
    with page.expect_request_finished(
        lambda r: "/serve-image/" in r.url and r.resource_type == "image"
    ):
        held[0].fulfill(status=200, content_type="image/png", body=png)
    page.wait_for_timeout(500)

    assert page.inner_text("#source-name") == "blobs.png"
    assert page.evaluate("() => document.getElementById('image-canvas').width") == 400
    _min_max_spot(page, page.locator("#image-canvas"))
    body = _calculation_request(page)
    assert body["isLocalImage"] is True and body["imageName"] == "blobs.png", body
    assert body["imageData"] is None and body["imageSrc"].endswith(".png"), body


# Export names: image stem, time of the result, what the file holds.
_STAMPED = r"{}_\d{{4}}-\d\d-\d\d_\d\d-\d\d_{}"


def test_saved_image_is_named_png_after_the_current_image(page, servers, test_image_path, tmp_path):
    """The server renders PNG, so the download is <stem>_<time>_annotated.png;
    an ELN image (no file name) gets a neutral name, not the last local file's."""
    import cv2
    import numpy as np

    photo = tmp_path / "photo.jpg"
    cv2.imwrite(str(photo), np.full((300, 260, 3), 240, dtype=np.uint8))  # unlike the ELN image
    _stub_eln_image(page, test_image_path)
    _stub_cdn(page)
    page.goto(servers["new"] + "/?url=http%3A%2F%2Feln.example%2Fplate.png")
    _wait_identified(page)

    page.click("#source-change")  # re-expand the collapsed source card
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", str(photo))
    page.wait_for_selector('text=Image "photo.jpg" loaded.')
    canvas = page.locator("#image-canvas")
    _min_max_spot(page, canvas)
    page.click("#calculate-btn")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    with page.expect_download() as download_info:
        page.click("[data-action='saveImage']")
    name = download_info.value.suggested_filename
    assert re.fullmatch(_STAMPED.format("photo", "annotated.png"), name), name

    # The ELN image: calculation stubbed (the stub image is not on the server).
    png = Path(test_image_path).read_bytes()
    result = {
        "rf_values": [
            {
                "n": 1,
                "x": 150,
                "y": 150,
                "rf": 0.5,
                "solvent": "",
                "solvent_type": "",
                "compound": "Unknown",
                "compound_type": "",
            }
        ],
        "annotated_image_url": "/media/stub-annotated.png",
        "csv_url": "/media/stub.csv",
    }
    page.route(
        "**/calculate_rf/**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(result)
        ),
    )
    page.route(
        "**/media/stub-annotated.png*",  # ?t=cache-buster
        lambda route: route.fulfill(status=200, content_type="image/png", body=png),
    )
    page.click("#source-change")
    page.click("#eln-source-btn")
    page.wait_for_function("() => document.getElementById('image-canvas').width === 300")
    _wait_identified(page)
    page.click("[data-action='resetCanvas']")  # (the fix makes this a no-op)
    _min_max_spot(page, canvas)
    page.click("#calculate-btn")
    page.wait_for_selector("#values-table tbody tr")
    with page.expect_download() as download_info:
        page.click("[data-action='saveImage']")
    name = download_info.value.suggested_filename
    assert re.fullmatch(_STAMPED.format("tlc_plate", "annotated.png"), name), name


# --- Plate panel, line colours and estimates, plate description, exports -----


def _state(page: Page, expr: str):
    """Evaluate the JS expression ``expr`` with the app's ``state`` in scope
    (as an arrow function: the page's CSP forbids eval)."""
    return page.evaluate(
        "() => import('/static/tlc/js/state.js').then((m) => { const state = m.state;"
        f" return {expr}; }})"
    )


def test_plate_panel_labels_follow_the_plate_not_the_text_size(page, servers, test_image_path):
    """The working canvas is "Plate"; its labels are sized from the plate like
    the saved image's (15 px on a 300 px image), and the text-size setting,
    which applies to the saved image only, leaves them alone."""
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    assert page.inner_text(".panel-canvas .panel-title") == "Plate"
    page.fill("#text-scale", "300")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})  # redraws the canvas
    assert _state(page, "state.ctx.font") == "15px sans-serif"


def _pixels(page: Page, x: int, y: int, w: int, h: int) -> list[list[int]]:
    """RGB of a canvas block, row by row."""
    return page.evaluate(
        """([x, y, w, h]) => {
            const d = document.getElementById('image-canvas').getContext('2d')
                .getImageData(x, y, w, h).data;
            const out = [];
            for (let i = 0; i < d.length; i += 4) out.push([d[i], d[i + 1], d[i + 2]]);
            return out;
        }""",
        [x, y, w, h],
    )


def _is_yellow(p: list[int]) -> bool:
    return p[0] > 200 and p[1] > 170 and p[2] < 90


def test_min_is_yellow_on_a_dark_outline(page, servers, test_image_path):
    """Green Min nearly vanished on green fluorescent plates: it is yellow with
    a dark outline now (as on the saved image), Max magenta."""
    _load_with_min_max(page, servers["new"], test_image_path)
    y = round(_state(page, "state.lines.find((l) => l.type === 'min').y"))
    column = _pixels(page, 200, y - 4, 1, 9)
    assert any(_is_yellow(p) for p in column), column
    # The dark outline: grey pixels well below the plate's 240 (anti-aliased).
    assert any(max(p) < 150 and max(p) - min(p) < 10 for p in column), column
    y = round(_state(page, "state.lines.find((l) => l.type === 'max').y"))
    column = _pixels(page, 200, y - 4, 1, 9)
    assert any(p[0] > 200 and p[1] < 60 and p[2] > 140 for p in column), column


def test_an_estimated_line_is_dashed_until_moved(page, servers, blob_image_path):
    """With only one line found, the other is drawn dashed and captioned as an
    estimate, and the hint says so, until the user drags it."""
    _stub_cdn(page)
    _route_detect_lines(page, {"minY": 270, "maxY": 30.0, "estimated": "max", "model": {}})
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)
    assert page.inner_text("#detect-lines-btn").strip() == "Suggest Lines"
    assert page.inner_text("#detect-spots-btn").strip() == "Suggest Spots"

    page.click("#detect-lines-btn")
    page.wait_for_selector("text=is an estimate (dashed)")
    estimated = "state.lines.map((l) => [l.type, !!l.estimated])"
    assert sorted(_state(page, estimated)) == [["max", True], ["min", False]]
    assert "Max (solvent front) is an estimate" in page.inner_text("#hint")

    def gaps(y: int) -> int:  # background pixels along the line's row
        return sum(1 for p in _pixels(page, 100, y, 200, 1) if min(p) > 200)

    assert gaps(30) > 20, "estimated line is not dashed"
    assert gaps(270) == 0, "detected line should be solid"

    box = page.locator("#image-canvas").bounding_box()
    page.mouse.move(box["x"] + 200, box["y"] + 30)
    page.mouse.down()
    page.mouse.move(box["x"] + 200, box["y"] + 40, steps=5)
    page.mouse.up()
    assert sorted(_state(page, estimated)) == [["max", False], ["min", False]]
    assert gaps(40) == 0, "a moved line is solid"
    page.click("#undo-btn")  # the estimate comes back with the position
    assert sorted(_state(page, estimated)) == [["max", True], ["min", False]]


def test_crop_view_shows_a_margin_around_the_plate_area(page, servers, blob_image_path):
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    page.click("#local-source-btn")
    page.set_input_files("#local-image-input", blob_image_path)
    page.wait_for_function(_IMAGE_READY)
    box = page.locator("#image-canvas").bounding_box()
    page.click("button:has-text('Plate Area')")
    page.mouse.move(box["x"] + 100, box["y"] + 60)
    page.mouse.down()
    page.mouse.move(box["x"] + 300, box["y"] + 260, steps=8)
    page.mouse.up()
    page.check("#crop-plate")
    view = _state(page, "[state.cropView.rcx, state.cropView.rcy, state.plateRegion]")
    region = view[2]
    assert abs(view[0] - (region["x"] - 0.06 * region["width"])) < 0.01, view
    assert abs(view[1] - (region["y"] - 0.06 * region["height"])) < 0.01, view


def _calculate_simple(page: Page, base_url: str, image_path: str):
    canvas = _load_with_min_max(page, base_url, image_path)
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    page.click("button:has-text('Calculate RF')")
    page.wait_for_selector("#values-table tbody tr", timeout=20000)
    return canvas


def test_plate_description_is_recorded_with_the_result(page, servers, test_image_path):
    """Mobile and stationary phase go with Calculate RF into the CSV, mark a
    result stale when changed, are undoable, and fill the Rf summary."""
    _stub_cdn(page)
    page.goto(servers["new"] + "/")
    assert not page.locator("#mobile-phase").is_visible()  # no image yet
    canvas = _load_with_min_max(page, servers["new"], test_image_path)
    page.fill("#mobile-phase", "CH/EtOAc 4:1")
    page.fill("#stationary-phase", "silica gel 60 F254")
    page.click("button:has-text('Add Spots')")
    canvas.click(position={"x": 150, "y": 150})
    with page.expect_response("**/calculate_rf/") as response_info:
        page.click("button:has-text('Calculate RF')")
    body = response_info.value.request.post_data_json
    assert (body["mobilePhase"], body["stationaryPhase"]) == ("CH/EtOAc 4:1", "silica gel 60 F254")
    assert response_info.value.json()["details"].startswith("TLC (CH/EtOAc 4:1); Rf ")
    page.wait_for_selector("#values-table tbody tr")
    with page.expect_download() as info:
        page.click("[data-action='saveCSV']")
    text = Path(info.value.path()).read_text(encoding="utf-8")
    assert "mobile phase;CH/EtOAc 4:1\nstationary phase;silica gel 60 F254\n" in text, text

    page.fill("#mobile-phase", "CH/EtOAc 2:1")
    page.wait_for_selector("#stale-banner", state="visible")
    page.locator("#mobile-phase").blur()
    page.click("#undo-btn")
    assert page.input_value("#mobile-phase") == "CH/EtOAc 4:1"
    assert _state(page, "state.mobilePhase") == "CH/EtOAc 4:1"


def test_table_row_rings_its_spot_on_the_annotated_image(page, servers, test_image_path):
    _calculate_simple(page, servers["new"], test_image_path)
    page.wait_for_function("() => document.getElementById('annotated-image').naturalWidth > 0")
    ring = page.locator("#annotated-ring")
    assert ring.is_hidden()
    page.locator("#values-table tbody tr").first.click()
    ring.wait_for(state="visible")

    def offset() -> tuple[float, float]:
        img = page.locator("#annotated-image").bounding_box()
        r = ring.bounding_box()
        spot = _state(page, "state.highlightedSpot")
        x, y = float(spot["x"]), float(spot["y"])
        k = page.evaluate(
            "() => { const i = document.getElementById('annotated-image');"
            " return i.clientWidth / i.naturalWidth; }"
        )
        cx, cy = r["x"] + r["width"] / 2, r["y"] + r["height"] / 2
        return cx - (img["x"] + 1 + x * k), cy - (img["y"] + 1 + y * k)

    dx, dy = offset()
    assert abs(dx) < 1.5 and abs(dy) < 1.5, (dx, dy)
    page.set_viewport_size({"width": 700, "height": 900})  # the image shrinks
    page.wait_for_timeout(200)
    dx, dy = offset()
    assert abs(dx) < 1.5 and abs(dy) < 1.5, (dx, dy)


def test_download_all_is_one_zip_with_the_three_files(page, servers, test_image_path):
    import io
    import zipfile

    _calculate_simple(page, servers["new"], test_image_path)
    with page.expect_download() as info:
        page.click("[data-action='saveAll']")
    name = info.value.suggested_filename
    assert re.fullmatch(r"tlc_plate_\d{4}-\d\d-\d\d_\d\d-\d\d\.zip", name), name
    archive = zipfile.ZipFile(io.BytesIO(Path(info.value.path()).read_bytes()))
    stem = name.removesuffix(".zip")
    assert sorted(archive.namelist()) == [
        f"{stem}_annotated.png",
        f"{stem}_annotations.json",
        f"{stem}_rf.csv",
    ]
    assert json.loads(archive.read(f"{stem}_annotations.json"))["format"] == "tlc-rf-annotations"
    assert archive.read(f"{stem}_rf.csv").startswith(b"Measurement type;")


def test_copy_rf_summary(page, servers, test_image_path):
    """The CSV's Details sentence, as the server wrote it, goes to the clipboard."""
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _calculate_simple(page, servers["new"], test_image_path)
    page.click("[data-action='copyRfSummary']")
    page.wait_for_selector("text=Rf summary copied.")
    copied = page.evaluate("() => navigator.clipboard.readText()")
    assert copied == "TLC (solvents); Rf (UV-Vis in nm, staining) = 0.50 (Unknown)[h1].", copied


def test_copy_rf_summary_without_a_clipboard_shows_it(page, servers, test_image_path):
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', { value: undefined })")
    _calculate_simple(page, servers["new"], test_image_path)
    page.click("[data-action='copyRfSummary']")
    page.wait_for_selector("text=Could not copy. Rf summary: TLC (solvents); Rf")
