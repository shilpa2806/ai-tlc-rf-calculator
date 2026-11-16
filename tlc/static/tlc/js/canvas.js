/*
 * Canvas rendering: drawing the image at the current rotation, redrawing all
 * annotations, mapping pointer events to bitmap coordinates, the crop-to-plate
 * view (display only), and rotate/reset.
 */

"use strict";

import { state } from "./state.js";
import { notify } from "./ui-kit.js";
import {
    clearResultsStale,
    hasAnnotations,
    hasImage,
    markResultsStale,
    refreshUI,
    syncPlateInfo,
    updateAnnotatedHeading,
    updateAnnotatedHighlight,
    updateWorkAreaLayout,
} from "./ui.js";
import { deleteLine, editLine, isHorizontalLine, markerCaption, setActiveButton } from "./annotations.js";
import { deleteDraft, saveDraft } from "./drafts.js";
import { commitHistory, redoHistory, resetHistory, undoHistory } from "./history.js";

// Draw an image into the w x h canvas frame, turned clockwise by the preset
// `rot` (0/90/180/270) plus the fine-tune `fine` (degrees), about the centre.
// The frame keeps the preset's size: with a fine turn the photo's corners fall
// outside and the uncovered corners are white -- exactly what the server does
// (tlc/views.py _fine_rotate), so marked coordinates mean the same there.
export function drawImageRotated(context, img, rot, w, h, fine = 0) {
    const swap = rot === 90 || rot === 270;
    const drawW = swap ? h : w; // the image's own size before turning
    const drawH = swap ? w : h;
    context.save();
    if (fine) {
        context.fillStyle = "#ffffff";
        context.fillRect(0, 0, w, h);
    }
    context.translate(w / 2, h / 2);
    context.rotate(((rot + fine) * Math.PI) / 180);
    context.drawImage(img, -drawW / 2, -drawH / 2, drawW, drawH);
    context.restore();
}

export function fitAndDraw(img) {
    const maxHeight = 500;
    const natW0 = img.naturalWidth || img.width;
    const natH0 = img.naturalHeight || img.height;
    // For 90/270 the displayed image's width/height are swapped.
    const swap = state.rotation === 90 || state.rotation === 270;
    const naturalW = swap ? natH0 : natW0;
    const naturalH = swap ? natW0 : natH0;
    let dispW = naturalW;
    let dispH = naturalH;
    if (naturalH > maxHeight) {
        dispW = Math.round((maxHeight * naturalW) / naturalH);
        dispH = maxHeight;
    }
    state.canvas.width = dispW;
    state.canvas.height = dispH;
    drawImageRotated(state.ctx, img, state.rotation, dispW, dispH, state.fineRotation);
    window.scaleFactor = naturalH / dispH;

    const annotated = document.getElementById("annotated-image");
    annotated.style.width = dispW + "px";
    annotated.style.height = dispH + "px";

    updateWorkAreaLayout();
    document.body.dataset.imageReady = "1"; // readiness signal (used by tests)
}

// Colours per line type, as on the saved image (tlc/results.py). Min and Max
// are yellow and magenta on a dark outline: they differ from each other and
// show on white, dark and green fluorescent plates alike (a green baseline
// nearly vanished on F254 plates under UV). `icon` colours the delete cross,
// which sits on a white button where yellow does not read.
const LINE_STYLES = {
    min: { color: "#ffd600", halo: "#202020", icon: "#9a7b00" }, // baseline / origin
    max: { color: "#e600b4", halo: "#202020", icon: "#e600b4" }, // solvent front
    horizontal: { color: "#000000", halo: "#ffffff", icon: "#000000" }, // compound; purple read as grey
    vertical: { color: "#1565c0", halo: null, icon: "#1565c0" }, // lane
};

function lineStyle(line) {
    return LINE_STYLES[line.type] || LINE_STYLES.max;
}

// Label size on the canvas, by the saved image's rule (tlc/results.py,
// draw_annotations: Hershey scale 0.55 * s with s = max(1, min(h, w) / 1000);
// its capitals are ~20 px tall per unit scale, a sans-serif em of ~28 px), so
// the preview and the saved image look alike. Converted from image px to canvas
// px, and never below MIN_LABEL_PX, which stays readable on a 500 px canvas.
// The text-size setting is left out: it applies to the saved image only.
const MIN_LABEL_PX = 11;

export function plateFontPx(width, height, scaleFactor) {
    const s = Math.max(1, Math.min(width, height) / 1000);
    return Math.max(MIN_LABEL_PX, (0.55 * 28 * s) / (scaleFactor || 1));
}

// How a line is captioned: Min/Max as on the saved image, marked while they
// are only an estimate; markers by their label.
function lineCaption(line) {
    if (line.type === "min" || line.type === "max") {
        const name = line.type === "min" ? "Min" : "Max";
        return line.estimated ? name + " (estimate)" : name;
    }
    return markerCaption(line);
}

export function redrawCanvas() {
    const canvas = state.canvas;
    const ctx = state.ctx;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawImageRotated(ctx, state.image, state.rotation, canvas.width, canvas.height, state.fineRotation);

    const fontPx = plateFontPx(state.image.naturalWidth, state.image.naturalHeight, window.scaleFactor);
    ctx.font = Math.round(fontPx) + "px sans-serif";
    ctx.lineWidth = 2;

    // Lines — each type has its own colour so Min and Max are told apart, and
    // the matching delete cross is drawn in the same colour. An estimated Min
    // or Max (Suggest Lines found only the other) is dashed until it is moved.
    state.lines.forEach((line) => {
        const { color, halo } = lineStyle(line);
        const caption = lineCaption(line);
        let from, to, at;
        if (isHorizontalLine(line)) {
            const y = line.y / window.scaleFactor;
            [from, to, at] = [[0, y], [canvas.width, y], [10, y - 5]];
        } else {
            // Label at the bottom (the delete cross sits at the top).
            const x = line.x / window.scaleFactor;
            [from, to, at] = [[x, 0], [x, canvas.height], [x + 5, canvas.height - 6]];
        }
        ctx.setLineDash(line.estimated ? [8, 6] : []);
        if (halo) {
            ctx.save();
            ctx.strokeStyle = halo;
            ctx.lineWidth = 4;
            ctx.beginPath();
            ctx.moveTo(...from);
            ctx.lineTo(...to);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.lineJoin = "round";
            ctx.lineWidth = Math.max(3, fontPx / 5);
            if (caption) ctx.strokeText(caption, ...at);
            ctx.restore();
        }
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(...from);
        ctx.lineTo(...to);
        ctx.stroke();
        ctx.setLineDash([]);
        if (caption) ctx.fillText(caption, ...at);
    });

    // Rebuild delete icons, placed right at the line on the canvas. Icons live
    // in #canvas-container (CSS pixels), so bitmap coordinates must be scaled by
    // the canvas's rendered/bitmap ratio — otherwise the icon drifts from the
    // line proportionally to its position (a low Min/Max line pushed its × off
    // the canvas). In crop view toContainer already maps to display pixels.
    document.querySelectorAll(".delete-icon").forEach((el) => el.remove());
    const fixed = state.cropView ? 8 : 12; // margin from the canvas edge
    const ox = state.cropView ? 0 : canvas.offsetLeft;
    const oy = state.cropView ? 0 : canvas.offsetTop;
    const dsx = canvas.width ? canvas.clientWidth / canvas.width : 1;
    const dsy = canvas.height ? canvas.clientHeight / canvas.height : 1;
    // Right edge of the visible canvas (clipped viewport when cropped).
    const container = document.getElementById("canvas-container");
    const rightEdge = state.cropView ? container.clientWidth : ox + canvas.clientWidth;
    state.lines.forEach((line) => {
        const color = lineStyle(line).icon;
        const editable = line.type === "horizontal" || line.type === "vertical";
        if (isHorizontalLine(line)) {
            // Cross on the RIGHT (the label sits at the left), pencil beside it.
            const by = line.y / window.scaleFactor;
            const y = state.cropView ? toContainer(0, by).y : by * dsy;
            addDeleteIcon(line, rightEdge - fixed, oy + y, color);
            if (editable) addEditIcon(line, rightEdge - fixed - 24, oy + y);
        } else {
            // Cross at the TOP (the label sits at the bottom), pencil below it.
            const bx = line.x / window.scaleFactor;
            const x = state.cropView ? toContainer(bx, 0).x : bx * dsx;
            addDeleteIcon(line, ox + x, oy + fixed, color);
            if (editable) addEditIcon(line, ox + x, oy + fixed + 24);
        }
    });

    // Spots.
    ctx.strokeStyle = "orange";
    state.spots.forEach((spot) => {
        ctx.beginPath();
        ctx.arc(spot.x / window.scaleFactor, spot.y / window.scaleFactor, 10, 0, 2 * Math.PI);
        ctx.stroke();
    });

    // Highlight the spot for the selected result row.
    if (state.highlightedSpot) {
        ctx.save();
        ctx.strokeStyle = "#2f6fed";
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(
            state.highlightedSpot.x / window.scaleFactor,
            state.highlightedSpot.y / window.scaleFactor,
            15,
            0,
            2 * Math.PI
        );
        ctx.stroke();
        ctx.restore();
    }

    // Optional plate region (dashed): in-progress drag or a stored region.
    const sf = window.scaleFactor || 1;
    let rect = null;
    if (state.draggingRegion && state.regionStart && state.regionCurrent) {
        rect = [
            Math.min(state.regionStart.x, state.regionCurrent.x),
            Math.min(state.regionStart.y, state.regionCurrent.y),
            Math.abs(state.regionCurrent.x - state.regionStart.x),
            Math.abs(state.regionCurrent.y - state.regionStart.y),
        ];
    } else if (state.plateRegion) {
        rect = [
            state.plateRegion.x / sf,
            state.plateRegion.y / sf,
            state.plateRegion.width / sf,
            state.plateRegion.height / sf,
        ];
    }
    if (rect) {
        ctx.save();
        ctx.strokeStyle = "#2f6f4f";
        ctx.setLineDash([6, 4]);
        ctx.strokeRect(rect[0], rect[1], rect[2], rect[3]);
        ctx.restore();
    }

    refreshUI();
    updateAnnotatedHighlight();
}

export function addDeleteIcon(line, x, y, color) {
    const icon = document.createElement("span");
    icon.className = "delete-icon";
    icon.innerHTML = "&times;";
    icon.style.left = x + "px";
    icon.style.top = y + "px";
    if (color) {
        icon.style.color = color;
        icon.style.borderColor = color;
    }
    icon.onclick = () => deleteLine(line.id);
    document.getElementById("canvas-container").appendChild(icon);
    line.deleteIcon = icon;
}

// Pencil next to an H/V line's cross: reopens its label popup (item 7).
function addEditIcon(line, x, y) {
    const icon = document.createElement("span");
    // Shares .delete-icon so redrawCanvas removes and repositions it too.
    icon.className = "delete-icon edit-icon";
    icon.textContent = "\u270E";
    icon.title = "Edit label";
    icon.setAttribute("role", "button");
    icon.setAttribute("aria-label", "Edit label");
    icon.style.left = x + "px";
    icon.style.top = y + "px";
    icon.onclick = (e) => {
        const r = icon.getBoundingClientRect();
        editLine(line.id, r.left + window.scrollX + 12, r.bottom + window.scrollY + 4);
        e.stopPropagation();
    };
    document.getElementById("canvas-container").appendChild(icon);
}

// Map a pointer event to canvas *bitmap* coordinates. Using the rendered rect
// (not event.offsetX) keeps clicks aligned with the image even when the canvas
// is CSS-scaled by max-width — the cause of the fresh-load misalignment.
export function pointerCanvas(e) {
    const canvas = state.canvas;
    const rect = canvas.getBoundingClientRect();
    const sx = rect.width ? canvas.width / rect.width : 1;
    const sy = rect.height ? canvas.height / rect.height : 1;
    return { x: (e.clientX - rect.left) * sx, y: (e.clientY - rect.top) * sy };
}

// --- Crop-to-plate view (display only; coordinates and saved image unchanged) --
export function cropEnabled() {
    const box = document.getElementById("crop-plate");
    return !!(box && box.checked && state.plateRegion && hasImage());
}

// Map canvas-display coordinates to #canvas-container coordinates, accounting
// for the crop zoom (delete icons live in the container, not on the canvas).
export function toContainer(dx, dy) {
    const cropView = state.cropView;
    if (!cropView) return { x: dx, y: dy };
    return { x: (dx - cropView.rcx) * cropView.k, y: (dy - cropView.rcy) * cropView.k };
}

// Apply/clear the crop by CSS-zooming the canvas inside an overflow-hidden
// container. The bitmap, annotation coordinates, and saved image are untouched;
// pointer mapping stays correct because it reads getBoundingClientRect().
// The crop view keeps this much of the photo around the Plate Area on each
// side (a share of the box's size), so the plate's edges stay in view.
export const CROP_MARGIN = 0.06;

// The part of the canvas the crop view shows: the Plate Area plus the margin,
// clipped to the canvas, in canvas px.
export function cropRect(region, sf, canvasW, canvasH) {
    const mx = (region.width / sf) * CROP_MARGIN;
    const my = (region.height / sf) * CROP_MARGIN;
    const x0 = Math.max(0, region.x / sf - mx);
    const y0 = Math.max(0, region.y / sf - my);
    const x1 = Math.min(canvasW, (region.x + region.width) / sf + mx);
    const y1 = Math.min(canvasH, (region.y + region.height) / sf + my);
    return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

export function applyCrop() {
    const canvas = state.canvas;
    const container = document.getElementById("canvas-container");
    const props = ["position", "left", "top", "width", "height", "maxWidth"];
    if (cropEnabled()) {
        const view = cropRect(state.plateRegion, window.scaleFactor || 1, canvas.width, canvas.height);
        const rcx = view.x;
        const rcy = view.y;
        const rcw = view.width;
        const rch = view.height;
        const k = rch > 0 ? Math.min(3, 500 / rch) : 1;
        state.cropView = { rcx, rcy, k };
        container.style.overflow = "hidden";
        container.style.width = Math.round(rcw * k) + "px";
        container.style.height = Math.round(rch * k) + "px";
        container.style.margin = "0 auto";
        canvas.style.position = "absolute";
        canvas.style.left = Math.round(-rcx * k) + "px";
        canvas.style.top = Math.round(-rcy * k) + "px";
        canvas.style.width = Math.round(canvas.width * k) + "px";
        canvas.style.height = Math.round(canvas.height * k) + "px";
        canvas.style.maxWidth = "none";
    } else {
        state.cropView = null;
        container.style.overflow = "";
        container.style.width = "";
        container.style.height = "";
        container.style.margin = "";
        props.forEach((p) => (canvas.style[p] = ""));
    }
    redrawCanvas();
}

// --- Rotation: a preset plus a fine-tune (deskew) ------------------------
// Only on a clean image: turning would move every mark.
export const MAX_FINE_ROTATION = 45;

// `sync` false is the live preview while typing: the field is left as typed
// (rewriting it under the cursor turned "12" into "1.0" and broke "2.5").
// A fine angle that is not a number keeps the current one.
export function setRotation(preset, fine, sync = true) {
    if (!hasImage() || hasAnnotations()) {
        syncRotationControls();
        return;
    }
    const value = Number(fine);
    const clamped = Number.isFinite(value)
        ? Math.max(-MAX_FINE_ROTATION, Math.min(MAX_FINE_ROTATION, value))
        : state.fineRotation;
    const presetChanged = preset !== state.rotation;
    state.rotation = preset;
    state.fineRotation = Math.round(clamped * 10) / 10;
    if (sync) syncRotationControls();
    if (presetChanged) {
        fitAndDraw(state.image); // 90/270 swap the frame
    } else {
        redrawCanvas();
    }
    // A clean image in a new frame: start the history here, so no undo or redo
    // brings back marks made in the old frame (or turns the image back).
    resetHistory();
    refreshUI();
}

export function nudgeRotation(step) {
    setRotation(state.rotation, state.fineRotation + Number(step));
}

// Show the current rotation in the controls (also after a refused change).
export function syncRotationControls() {
    const preset = document.getElementById("rotation-preset");
    const fine = document.getElementById("rotation-fine");
    if (preset) preset.value = String(state.rotation);
    if (fine) fine.value = state.fineRotation.toFixed(1);
}

export function resetCanvas() {
    const canvas = state.canvas;
    const ctx = state.ctx;
    state.lines = [];
    state.spots = [];
    state.plateRegion = null;
    state.selectingRegion = false;
    state.highlightedSpot = null;
    document.querySelectorAll(".delete-icon").forEach((el) => el.remove());
    if (state.image && state.image.complete) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(state.image, 0, 0, canvas.width, canvas.height);
    }
    const tbody = document.querySelector("#values-table tbody");
    if (tbody) tbody.innerHTML = "";
    state.hasResults = false;
    state.csvUrl = null;
    clearResultsStale();
    updateAnnotatedHeading("editing");
    setActiveButton(null);
    // The plate description stays: Reset clears the marks and results, not
    // what the plate was run in.
    resetHistory(); // clean slate becomes the undo baseline
    deleteDraft(); // the draft goes with the annotations
    applyCrop(); // region cleared -> exit crop view (redraws + refreshUI)
    notify("Canvas reset.", "info");
}

// --- Edit commit + undo/redo ----------------------------------------------
// Shared tail for any annotation-state change: flag a prior result stale and
// re-fit the crop view (which repaints and refreshes the contextual UI).
// applyCrop() is a harmless reshape when the plate region is unchanged, so it is
// safe for every edit — this is the one place the "single funnel" lives.
function afterAnnotationChange() {
    saveDraft();
    markResultsStale();
    applyCrop();
}

// Record a completed edit for undo/redo, then run the shared tail.
export function commitChange() {
    commitHistory();
    afterAnnotationChange();
}

// A snapshot carries the rotation its marks were made in (an opened file may
// have changed it), so re-fit the frame when the preset differs.
function afterHistoryChange(presetBefore) {
    state.highlightedSpot = null; // may point at a spot that no longer exists
    syncRotationControls();
    syncPlateInfo();
    if (state.rotation !== presetBefore) fitAndDraw(state.image); // 90/270 swap the frame
    afterAnnotationChange();
}

export function undoEdit() {
    const preset = state.rotation;
    if (undoHistory()) afterHistoryChange(preset);
}

export function redoEdit() {
    const preset = state.rotation;
    if (redoHistory()) afterHistoryChange(preset);
}
