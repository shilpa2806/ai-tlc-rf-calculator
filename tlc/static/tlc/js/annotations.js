/*
 * Annotation tools: reference/label lines, spots, the plate region, the line
 * label popup, and the active-tool button highlight. Click handlers for placing
 * lines and spots are attached to / detached from the canvas on demand.
 */

"use strict";

import { state } from "./state.js";
import { notify } from "./ui-kit.js";
import { pointInRegion } from "./geometry.js";
import { commitChange, pointerCanvas, redrawCanvas } from "./canvas.js";

// --- Line-type predicate (was repeated inline throughout) -----------------
export function isHorizontalLine(line) {
    return line.type === "horizontal" || line.type === "min" || line.type === "max";
}

export function setActiveButton(button) {
    document.querySelectorAll(".lab-btn").forEach((b) => b.classList.remove("active"));
    if (button) button.classList.add("active");
}

export function disableOtherActions() {
    state.addingSpots = false;
    state.placingLine = false;
    state.selectingRegion = false;
    state.canvas.removeEventListener("click", drawSpot);
    state.canvas.removeEventListener("click", drawLine);
}

export function selectPlateArea(event) {
    disableOtherActions();
    state.selectingRegion = true;
    if (event && event.currentTarget) setActiveButton(event.currentTarget);
    notify("Drag a box around the plate. Click once to clear.", "info");
}

// Note: the plate region is a detection ROI only. The Min (baseline) and Max
// (solvent front) lines are experimental features inside the plate — not its
// edges — so they are placed by the user, never derived from the plate box.

function startLine(type, horizontal, event) {
    disableOtherActions();
    state.lineType = type;
    state.isHorizontal = horizontal;
    state.placingLine = true;
    state.canvas.addEventListener("click", drawLine);
    if (event && event.currentTarget) setActiveButton(event.currentTarget);
}

export function addMinLine(event) { startLine("min", true, event); }
export function addMaxLine(event) { startLine("max", true, event); }
export function addHorizontalLine(event) { startLine("horizontal", true, event); }
export function addVerticalLine(event) { startLine("vertical", false, event); }

export function drawLine(event) {
    const p = pointerCanvas(event);
    const ox = p.x * window.scaleFactor; // original-image coordinates
    const oy = p.y * window.scaleFactor;
    const isRef = state.lineType === "min" || state.lineType === "max";
    const r = state.plateRegion;

    // With a plate area set, a line must fall inside it (horizontal on y,
    // vertical on x). Block the click but keep the tool armed for a retry.
    if (r) {
        const outside = state.isHorizontal
            ? oy < r.y || oy > r.y + r.height
            : ox < r.x || ox > r.x + r.width;
        if (outside) {
            notify("Place it inside the plate area.", "warning");
            return;
        }
    }

    // Min/Max are singletons.
    if (isRef && state.lines.some((l) => l.type === state.lineType)) {
        notify('A "' + state.lineType + '" line already exists.', "warning");
        state.canvas.removeEventListener("click", drawLine);
        state.placingLine = false;
        return;
    }

    state.currentLine = state.isHorizontal
        ? { id: Date.now(), type: state.lineType, y: oy, text: isRef ? state.lineType : "", kind: "" }
        : { id: Date.now(), type: state.lineType, x: ox, text: "", kind: "" };
    state.lines.push(state.currentLine);
    state.canvas.removeEventListener("click", drawLine);
    state.placingLine = false;

    if (isRef) {
        commitChange(); // no label popup -> commit the add now
    } else {
        showPopup(event.pageX, event.pageY, state.currentLine, false); // commit on submit/cancel
        redrawCanvas();
    }
}

// Marker types offered in the label popup, by line direction (reviewer
// request). Mirrored in tlc/results.py. Free text stays available alongside.
export const LABEL_TYPES = {
    horizontal: ["starting material", "expected product", "confirmed product", "by-product", "reagent"],
    vertical: ["starting material", "product 1", "product 2", "by-product", "reagent"],
};

// How a marker is captioned on the canvas and the saved image.
export function markerCaption(line) {
    const text = line.text || "";
    const kind = line.kind || "";
    if (text && kind) return text + " (" + kind + ")";
    return text || kind;
}

// The popup labels a new H/V line or edits an existing one. For an edit, the
// previous label is kept so Cancel can restore it.
function showPopup(x, y, line, editing) {
    const popup = document.getElementById("line-popup");
    const input = document.getElementById("line-text");
    const select = document.getElementById("line-kind");
    state.currentLine = line;
    state.popupEdit = editing ? { text: line.text || "", kind: line.kind || "" } : null;

    select.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "— type (optional) —";
    select.appendChild(none);
    (LABEL_TYPES[line.type] || []).forEach((kind) => {
        const option = document.createElement("option");
        option.value = kind;
        option.textContent = kind;
        select.appendChild(option);
    });
    select.value = line.kind || "";
    input.value = line.text || "";
    document.getElementById("line-popup-title").textContent =
        (editing ? "Edit " : "Label ") + (line.type === "vertical" ? "lane (V-Line)" : "compound (H-Line)");

    popup.style.left = x + "px";
    popup.style.top = y + "px";
    popup.style.display = "block";
    setTimeout(() => select.focus(), 50);
}

export function hidePopup() {
    const popup = document.getElementById("line-popup");
    if (popup) popup.style.display = "none";
    const input = document.getElementById("line-text");
    if (input) input.value = "";
    state.currentLine = null;
    state.popupEdit = null;
}

export function popupOpen() {
    const popup = document.getElementById("line-popup");
    return !!popup && popup.style.display === "block";
}

export function submitAnnotation() {
    const line = state.currentLine;
    if (line) {
        line.text = document.getElementById("line-text").value.trim();
        line.kind = document.getElementById("line-kind").value;
    }
    const edit = state.popupEdit;
    const changed = !edit || !line || edit.text !== line.text || edit.kind !== line.kind;
    hidePopup();
    if (changed) commitChange(); // a new line, or a changed label
    else redrawCanvas();
}

export function cancelAnnotation() {
    const edit = state.popupEdit;
    hidePopup();
    if (edit) {
        redrawCanvas(); // editing: nothing was changed
    } else {
        commitChange(); // new line: keep it unlabelled, but record it for undo
    }
}

// Reopen the popup for an existing H/V line (the pencil icon beside it).
export function editLine(id, x, y) {
    const line = state.lines.find((l) => l.id === id);
    if (!line || line.type === "min" || line.type === "max") return;
    disableOtherActions();
    showPopup(x, y, line, true);
}

export function deleteLine(id) {
    const index = state.lines.findIndex((l) => l.id === id);
    if (index === -1) return;
    if (state.lines[index].deleteIcon) state.lines[index].deleteIcon.remove();
    // If the annotation popup is open for this line, close it too.
    if (state.currentLine && state.currentLine.id === id) hidePopup();
    state.lines.splice(index, 1);
    commitChange();
}

export function deleteSpot(id) {
    state.spots = state.spots.filter((s) => s.id !== id);
    commitChange();
}

export function addSpots(event) {
    disableOtherActions();
    state.addingSpots = true;
    state.canvas.addEventListener("click", drawSpot);
    if (event && event.currentTarget) setActiveButton(event.currentTarget);
}

export function addSpot(origX, origY, auto = false) {
    // A double click must not place two spots by hand. Detection candidates
    // are screened by their box size instead (results.js, newCandidates), so
    // that close but distinct spots are both kept.
    if (!auto) {
        const duplicate = state.spots.some(
            (s) => Math.abs(s.x - origX) < 12 * window.scaleFactor &&
                   Math.abs(s.y - origY) < 12 * window.scaleFactor
        );
        if (duplicate) return false;
    }
    // `auto` marks spots proposed by Detect Spots (model candidates, as opposed
    // to spots placed by hand). This is a plain mutator — callers commit at the
    // gesture boundary (a single history entry per click / per detection batch).
    state.spots.push({ id: Date.now() + state.spotSeq++, x: origX, y: origY, auto });
    return true;
}

export function drawSpot(event) {
    if (!state.addingSpots || state.isDragging || state.justDragged) return;
    const p = pointerCanvas(event);
    const ox = p.x * window.scaleFactor;
    const oy = p.y * window.scaleFactor;
    if (state.plateRegion && !pointInRegion(ox, oy, state.plateRegion)) {
        notify("Place spots inside the plate area.", "warning");
        return;
    }
    if (!addSpot(ox, oy)) {
        notify("Spot already exists here.", "warning");
        return;
    }
    commitChange();
}
