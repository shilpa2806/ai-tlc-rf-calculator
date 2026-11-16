/*
 * Contextual UI state: the annotated-image heading, the collapsible image
 * source card, the help drawer, the work-area layout (rows vs. columns), the
 * hint bar, and the enabled/disabled state of the Calculate and Rotate buttons.
 *
 * Reads state and the DOM only — no canvas imports, so it sits below canvas.js
 * in the dependency graph.
 */

"use strict";

import { elnUrl, state } from "./state.js";
import { canRedo, canUndo } from "./history.js";

// Annotation text-size multiplier from the toolbar input, as a fraction
// (100% -> 1.0), clamped to [0.1, 5.0] to match the server-side clamp.
export function textScale() {
    const el = document.getElementById("text-scale");
    const pct = Number(el ? el.value : 100);
    // Empty / invalid / non-positive (e.g. a cleared field) -> default 100%.
    if (!Number.isFinite(pct) || pct <= 0) return 1;
    return Math.max(0.1, Math.min(5, pct / 100));
}

export function hasImage() {
    const img = state.image;
    return img && img.src && (img.complete ? img.naturalWidth > 0 : true);
}

export function updateAnnotatedHeading(stateName) {
    const heading = document.getElementById("annotated-heading");
    const annotated = document.getElementById("annotated-image");
    const placeholder = document.getElementById("result-placeholder");
    if (!heading) return;
    if (stateName === "annotated") {
        heading.textContent = "Annotated image";
        annotated.style.display = "block";
        if (placeholder) placeholder.style.display = "none";
    } else {
        heading.textContent = "Result";
        annotated.style.display = "none";
        if (placeholder) placeholder.style.display = "";
    }
}

export function toggleImageSource() {
    const body = document.getElementById("source-body");
    const change = document.getElementById("source-change");
    const collapsed = body.classList.toggle("hidden");
    if (change) change.setAttribute("aria-expanded", String(!collapsed));
}

export function collapseImageSource(name) {
    document.getElementById("source-body").classList.add("hidden");
    document.getElementById("source-summary").classList.add("shown");
    const change = document.getElementById("source-change");
    if (change) change.setAttribute("aria-expanded", "false");
    document.getElementById("source-name").textContent = name;
}

export function toggleHelp() {
    document.getElementById("help-drawer").classList.toggle("open");
}

export function updateWorkAreaLayout() {
    const area = document.getElementById("work-area");
    if (!area || !state.image) return;
    // Use the displayed (rotation-adjusted) dimensions; a fine turn keeps them.
    const swap = state.rotation === 90 || state.rotation === 270;
    const w = swap ? state.image.naturalHeight : state.image.naturalWidth;
    const h = swap ? state.image.naturalWidth : state.image.naturalHeight;
    // Portrait (tall) -> side-by-side columns; landscape (wide) -> stacked rows.
    const landscape = w > h;
    area.classList.toggle("layout-rows", landscape);
    area.classList.toggle("layout-columns", !landscape);
}

export function updateHint() {
    const hint = document.getElementById("hint");
    if (!hint) return;
    let msg = "";
    if (!hasImage()) {
        msg = "";
    } else if (!hasMinAndMax()) {
        msg = "Start with Suggest Lines to place Min and Max (or place them yourself).";
    } else if (estimatedLine()) {
        const which = estimatedLine().type === "min" ? "Min (baseline)" : "Max (solvent front)";
        msg = which + " is an estimate (dashed): drag it onto the plate's line.";
    } else if (state.spots.length === 0) {
        msg = "Now click Suggest Spots — it searches between Min and Max (or Add Spots to place them yourself).";
    } else {
        msg = "Ready — click Calculate RF.";
    }
    hint.textContent = msg;
    hint.classList.toggle("show", msg !== "");
}

// A Min or Max line Suggest Lines guessed (only the other was found), until
// the user moves it.
export function estimatedLine() {
    return state.lines.find((l) => l.estimated) || null;
}

export function hasMinAndMax() {
    return state.lines.some((l) => l.type === "min") && state.lines.some((l) => l.type === "max");
}

export function updateCalculateEnabled() {
    const btn = document.getElementById("calculate-btn");
    if (!btn) return;
    btn.disabled = !(hasImage() && hasMinAndMax() && state.spots.length > 0);
}

// Min and Max are singletons that are moved by dragging, so their buttons
// disappear once the line exists and return if it is deleted or undone.
export function updateReferenceButtons() {
    for (const type of ["min", "max"]) {
        const btn = document.getElementById(type + "-line-btn");
        if (btn) btn.hidden = state.lines.some((l) => l.type === type);
    }
}

// Suggest Lines needs only an image; Suggest Spots also needs Min and Max,
// because spot inference is restricted to the band between them.
export function updateDetectEnabled() {
    const lines = document.getElementById("detect-lines-btn");
    if (lines) lines.disabled = !hasImage();
    const spots = document.getElementById("detect-spots-btn");
    if (spots) spots.disabled = !(hasImage() && hasMinAndMax());
}

// Rotation is only allowed on a clean image (no annotations to invalidate),
// so the reference-line / RF axis never has to change.
export function hasAnnotations() {
    return state.spots.length > 0 || state.lines.length > 0 || !!state.plateRegion;
}

export function updateRotateEnabled() {
    const locked = hasAnnotations() || !hasImage();
    document.querySelectorAll(".rotation-control").forEach((control) => {
        control.disabled = locked;
    });
}

// --- Stale-results handling -----------------------------------------------
// The results table + annotated image are a snapshot from the last Calculate RF.
// Any annotation change afterwards makes them out of date: we keep them on
// screen but flag them (banner + pulsing Calculate RF) and block export until
// the user recalculates, so a stale image can't be saved / pushed to the ELN.

// Export only fresh results that are not being redrawn.
function updateExportEnabled() {
    const disabled = !state.hasResults || state.resultsStale || state.rendering;
    document.querySelectorAll("#export-row .lab-btn").forEach((b) => {
        b.disabled = disabled;
    });
}

// While the annotated image is redrawn for a new text size or label mode, the
// image on screen is not the one export would send.
export function setResultsRendering(rendering) {
    state.rendering = rendering;
    const annotated = document.getElementById("annotated-image");
    if (annotated) annotated.setAttribute("aria-busy", String(rendering));
    updateExportEnabled();
}

// The "upload to ELN" export buttons only make sense for an ELN-sourced plate
// (so only with ?url=); for a local upload they're hidden, leaving the
// download buttons.
export function updateExportButtons() {
    document.querySelectorAll("#export-row .export-eln").forEach((b) => {
        b.classList.toggle("hidden", !elnUrl || state.isLocalImage);
    });
}

export function markResultsStale() {
    if (!state.hasResults) return; // nothing calculated yet; writes below are idempotent
    state.resultsStale = true;
    const banner = document.getElementById("stale-banner");
    if (banner) banner.classList.add("show");
    const annotated = document.getElementById("annotated-image");
    if (annotated) annotated.classList.add("stale");
    const calc = document.getElementById("calculate-btn");
    if (calc) calc.classList.add("pulse");
    updateExportEnabled();
}

// Called on a fresh calculation (results now current) and on reset/new image
// (no results). `hasResults` is set by the caller.
export function clearResultsStale() {
    state.resultsStale = false;
    const banner = document.getElementById("stale-banner");
    if (banner) banner.classList.remove("show");
    const annotated = document.getElementById("annotated-image");
    if (annotated) annotated.classList.remove("stale");
    const calc = document.getElementById("calculate-btn");
    if (calc) calc.classList.remove("pulse");
    updateExportEnabled(); // export enabled only with fresh results
}

// Show the plate description held in state (after undo, a draft or a file).
export function syncPlateInfo() {
    const mobile = document.getElementById("mobile-phase");
    const stationary = document.getElementById("stationary-phase");
    if (mobile) mobile.value = state.mobilePhase;
    if (stationary) stationary.value = state.stationaryPhase;
}

// The ring over the annotated image for the highlighted result row. Row
// positions are full-resolution image px, the image is shown scaled; the ring
// is placed over the image's content box, so borders and resizing are allowed for.
export function updateAnnotatedHighlight() {
    const ring = document.getElementById("annotated-ring");
    const img = document.getElementById("annotated-image");
    const wrap = document.getElementById("annotated-wrap");
    if (!ring || !img || !wrap) return;
    const spot = state.highlightedSpot;
    if (!spot || !state.hasResults || !img.naturalWidth || !img.clientWidth) {
        ring.hidden = true;
        return;
    }
    const kx = img.clientWidth / img.naturalWidth;
    const ky = img.clientHeight / img.naturalHeight;
    const imgBox = img.getBoundingClientRect();
    const wrapBox = wrap.getBoundingClientRect();
    // The spot ring's radius on the saved image (tlc/results.py), a bit wider.
    const s = Math.max(1, Math.min(img.naturalWidth, img.naturalHeight) / 1000);
    const size = Math.max(18, 2 * 1.8 * Math.max(4, Math.round(9 * s)) * kx);
    ring.style.left = imgBox.left - wrapBox.left + img.clientLeft + spot.x * kx + "px";
    ring.style.top = imgBox.top - wrapBox.top + img.clientTop + spot.y * ky + "px";
    ring.style.width = size + "px";
    ring.style.height = size + "px";
    ring.hidden = false;
}

function refreshUndoRedo() {
    const undo = document.getElementById("undo-btn");
    const redo = document.getElementById("redo-btn");
    if (undo) undo.disabled = !canUndo();
    if (redo) redo.disabled = !canRedo();
}

// Refresh contextual UI after any annotation change.
export function refreshUI() {
    updateHint();
    updateCalculateEnabled();
    updateDetectEnabled();
    updateReferenceButtons();
    updateRotateEnabled();
    refreshUndoRedo();
}
