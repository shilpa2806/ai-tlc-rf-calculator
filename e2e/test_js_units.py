"""Unit tests for the frontend's pure logic — no npm.

The page already loads the ES modules, so each test dynamically ``import()``s a
module in the browser and asserts on its exported pure functions. This gives
real unit coverage of `geometry`, `isHorizontalLine`, and the `textScale` clamp
without adding a Node/JS test toolchain.
"""

from __future__ import annotations


def test_geometry_clamp_and_point_in_region(page, servers):
    page.goto(servers["new"] + "/")
    result = page.evaluate(
        """async () => {
            const g = await import('/static/tlc/js/geometry.js');
            const R = { x: 0, y: 0, width: 10, height: 20 };
            return {
                clamp_below: g.clamp(-3, 0, 10),
                clamp_above: g.clamp(99, 0, 10),
                clamp_inside: g.clamp(4, 0, 10),
                in_true: g.pointInRegion(5, 5, R),
                in_edge: g.pointInRegion(10, 20, R),
                in_false_x: g.pointInRegion(11, 5, R),
                in_false_y: g.pointInRegion(5, 21, R),
            };
        }"""
    )
    assert result == {
        "clamp_below": 0,
        "clamp_above": 10,
        "clamp_inside": 4,
        "in_true": True,
        "in_edge": True,
        "in_false_x": False,
        "in_false_y": False,
    }


def test_is_horizontal_line_by_type(page, servers):
    page.goto(servers["new"] + "/")
    result = page.evaluate(
        """async () => {
            const a = await import('/static/tlc/js/annotations.js');
            return {
                min: a.isHorizontalLine({ type: 'min' }),
                max: a.isHorizontalLine({ type: 'max' }),
                horizontal: a.isHorizontalLine({ type: 'horizontal' }),
                vertical: a.isHorizontalLine({ type: 'vertical' }),
            };
        }"""
    )
    assert result == {"min": True, "max": True, "horizontal": True, "vertical": False}


def test_text_scale_reads_and_clamps(page, servers):
    page.goto(servers["new"] + "/")
    result = page.evaluate(
        """async () => {
            const ui = await import('/static/tlc/js/ui.js');
            const el = document.getElementById('text-scale');
            const read = (v) => { el.value = v; return ui.textScale(); };
            return { def: read('100'), hi: read('9999'), lo: read('1'), bad: read('abc') };
        }"""
    )
    # 100% -> 1.0; clamped to [0.1, 5.0]; non-numeric -> 1.0
    assert result == {"def": 1.0, "hi": 5.0, "lo": 0.1, "bad": 1.0}


def test_candidates_on_one_spot_keep_the_more_confident(page, servers):
    page.goto(servers["new"] + "/")
    result = page.evaluate(
        """async () => {
            const r = await import('/static/tlc/js/results.js');
            const box = (x, y, conf, w, h) => ({ x, y, conf, w, h });
            const a = box(100, 100, 0.4, 40, 40);
            const twin = box(104, 115, 0.9, 80, 80);  // centres inside each other's box
            const next = box(100, 145, 0.6, 40, 40);  // a box further on: another spot
            const pick = (cands, spots) => r.newCandidates(cands, spots).map((c) => c.conf);
            return {
                same: r.sameSpot(a, twin),
                neighbour: r.sameSpot(a, next),
                handPlaced: r.sameSpot(a, { x: 115, y: 85 }),
                notInBox: r.sameSpot(a, { x: 121, y: 100 }),
                noBoxes: r.sameSpot({ x: 1, y: 2 }, { x: 1, y: 2.5 }),
                run: pick([a, twin, next], []),
                again: pick([a, twin, next], [{ x: 104, y: 115 }, { x: 100, y: 145 }]),
            };
        }"""
    )
    assert result == {
        "same": True,
        "neighbour": False,
        "handPlaced": True,
        "notInBox": False,
        "noBoxes": False,
        "run": [0.9, 0.6],
        "again": [],
    }


def test_canvas_labels_follow_the_saved_images_size_rule(page, servers):
    """Canvas text uses the saved image's rule (0.55 Hershey units x s, s from
    the shorter side), in canvas px, never below a readable minimum."""
    page.goto(servers["new"] + "/")
    result = page.evaluate(
        """async () => {
            const c = await import('/static/tlc/js/canvas.js');
            const px = (w, h, sf) => Math.round(c.plateFontPx(w, h, sf) * 10) / 10;
            return { small: px(300, 300, 1), phone: px(3000, 4000, 8), big: px(6000, 6000, 2) };
        }"""
    )
    # 300 px: s = 1, 0.55 * 28 = 15.4 px; a phone photo on a 500 px canvas
    # (5.8 px) is held at the minimum; 6000 px shown at half size: 46.2 px.
    assert result == {"small": 15.4, "phone": 11, "big": 46.2}


def test_crop_view_keeps_a_margin_around_the_plate_area(page, servers):
    page.goto(servers["new"] + "/")
    result = page.evaluate(
        """async () => {
            const c = await import('/static/tlc/js/canvas.js');
            return {
                inside: c.cropRect({ x: 100, y: 100, width: 100, height: 50 }, 1, 300, 300),
                edge: c.cropRect({ x: 0, y: 200, width: 100, height: 100 }, 2, 300, 150),
            };
        }"""
    )
    # 6 % of the box on each side, clipped to the canvas (the second in image
    # px at scale factor 2: canvas box 0..50 x 100..150).
    assert result["inside"] == {"x": 94, "y": 97, "width": 112, "height": 56}
    assert result["edge"] == {"x": 0, "y": 97, "width": 53, "height": 53}


def test_annotation_file_carries_plate_description_and_estimates(page, servers):
    """Mobile/stationary phase and an estimated Min/Max survive a round trip;
    a file without them (written before they existed) still opens."""
    page.goto(servers["new"] + "/")
    result = page.evaluate(
        """async () => {
            const { state } = await import('/static/tlc/js/state.js');
            const f = await import('/static/tlc/js/annotation-file.js');
            state.lines = [
                { id: 1, type: 'min', y: 250, text: 'min', estimated: true },
                { id: 2, type: 'max', y: 50, text: 'max' },
            ];
            state.spots = [];
            state.mobilePhase = 'CH/EtOAc 4:1';
            state.stationaryPhase = 'silica gel 60 F254';
            const data = JSON.parse(f.annotationsText());
            const roundTrip = { ...data };
            state.mobilePhase = state.stationaryPhase = '';
            f.applyAnnotations(f.validate(roundTrip).data);
            const restored = [state.mobilePhase, state.stationaryPhase,
                              state.lines.map((l) => [l.type, l.estimated])];
            const old = { ...data };
            delete old.mobilePhase;
            delete old.stationaryPhase;
            old.lines = old.lines.map(({ estimated, ...rest }) => rest);
            f.applyAnnotations(f.validate(old).data);
            return {
                version: data.version,
                written: [data.mobilePhase, data.stationaryPhase,
                          data.lines.map((l) => !!l.estimated)],
                restored,
                old: [state.mobilePhase, state.stationaryPhase,
                      state.lines.map((l) => l.estimated)],
                bad: f.validate({ ...data, mobilePhase: 5 }).error || null,
                cleaned: f.cleanText('  a\\n  b  ' + 'x'.repeat(200)).length,
            };
        }"""
    )
    assert result == {
        "version": 1,
        "written": ["CH/EtOAc 4:1", "silica gel 60 F254", [True, False]],
        "restored": ["CH/EtOAc 4:1", "silica gel 60 F254", [["min", True], ["max", False]]],
        "old": ["", "", [False, False]],
        "bad": "The annotation file has an invalid plate description.",
        "cleaned": 100,
    }
