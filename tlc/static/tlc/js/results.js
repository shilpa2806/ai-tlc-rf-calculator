/*
 * Server round-trips and results: building the image payload, RF calculation
 * (and redrawing the annotated image for a new text size), candidate line and
 * spot detection, the results table (with row highlight/delete), exports
 * (downloads, one ZIP, the Rf summary) and saving image/CSV back to the ELN.
 */

"use strict";

import { elnUrl, state } from "./state.js";
import { getCookie, hideOverlay, notify, showOverlay } from "./ui-kit.js";
import {
    clearResultsStale,
    hasImage,
    setResultsRendering,
    textScale,
    updateAnnotatedHeading,
    updateAnnotatedHighlight,
} from "./ui.js";
import { annotationsText } from "./annotation-file.js";
import { addSpot } from "./annotations.js";
import { clamp } from "./geometry.js";
import { commitChange, redrawCanvas } from "./canvas.js";
import { commitHistory } from "./history.js";

// --- Image payload (shared by calculate + detection) ----------------------
// Resolves to the image fields of a request. A local image is referred to by
// the name it was uploaded under (sources.js); only if that upload failed is
// the whole image sent inline, as before.
export function getImagePayload() {
    const extras = (payload) => {
        payload.rotation = state.rotation; // backend re-orients to match the displayed image
        payload.fineRotation = state.fineRotation;
        payload.textScale = textScale(); // label text-size multiplier for the saved image
        const full = document.getElementById("label-full");
        payload.labelMode = full && full.checked ? "full" : "numbers";
        // Recorded in the CSV's provenance block (how the result was made).
        payload.imageName = state.imageName || "";
        payload.plateRegion = state.plateRegion;
        return payload;
    };
    if (state.isLocalImage) {
        return Promise.resolve(state.localImageUpload).then((stored) =>
            extras(
                stored
                    ? { imageSrc: stored, isLocalImage: true, imageData: null }
                    : {
                          imageSrc: state.imageName || "local_upload.png",
                          isLocalImage: true,
                          imageData: state.image.src,
                      },
            ),
        );
    }
    let payload;
    try {
        const parsed = new URL(state.image.src);
        const filePath = parsed.searchParams.get("file_path");
        payload = {
            imageSrc: decodeURIComponent(filePath).split("/").pop(),
            isLocalImage: false,
            imageData: null,
        };
    } catch (err) {
        payload = { imageSrc: "unknown_image.png", isLocalImage: false, imageData: null };
    }
    return Promise.resolve(extras(payload));
}

// --- RF calculation -------------------------------------------------------
// Every calculation, and every redraw, is numbered; only the latest one's
// reply is shown, so a slow reply never replaces a newer result.
let calcSeq = 0;

function calculationPayload() {
    return getImagePayload().then((image) =>
        Object.assign(
            {
                lines: state.lines,
                spots: state.spots,
                mobilePhase: state.mobilePhase,
                stationaryPhase: state.stationaryPhase,
            },
            image,
        ),
    );
}

// Show a calculation's reply: table, annotated image, and fresh export state.
// A redraw keeps the highlighted row (the rows are the same).
function applyResult(result, keepHighlight = false) {
    const highlighted = keepHighlight ? state.highlightedSpot : null;
    populateTable(result.rf_values);
    const annotated = document.getElementById("annotated-image");
    if (result.annotated_image_url) {
        annotated.src = result.annotated_image_url + "?t=" + Date.now();
        updateAnnotatedHeading("annotated");
    }
    state.csvUrl = result.csv_url || null;
    state.annotatedUrl = result.annotated_image_url || null;
    state.rfSummary = result.details || "";
    state.resultsTime = new Date();
    state.hasResults = true; // results are now current
    clearResultsStale();
    if (highlighted) {
        const row = Array.from(document.querySelectorAll("#values-table tbody tr")).find(
            (r) => Number(r.dataset.x) === highlighted.x && Number(r.dataset.y) === highlighted.y,
        );
        if (row) highlightSpotFromRow(row);
    }
}

export function calculateRF() {
    const minLine = state.lines.find((l) => l.type === "min");
    const maxLine = state.lines.find((l) => l.type === "max");
    if (!minLine || !maxLine) {
        notify('Please add "min" and "max" lines before calculating RF.', "warning");
        return;
    }
    if (state.spots.length === 0) {
        notify("Please add spots before calculating RF.", "warning");
        return;
    }

    const seq = ++calcSeq;
    setResultsRendering(false); // a pending redraw is superseded
    showOverlay("Calculating RF values…");
    calculationPayload()
        .then((payload) => postJson("/calculate_rf/", payload))
        .then((result) => {
            hideOverlay();
            if (seq !== calcSeq) return;
            if (result.rf_values && result.rf_values.length > 0) {
                applyResult(result);
                notify("RF calculation complete: " + result.rf_values.length + " spot(s).", "success");
            } else {
                notify("No RF values calculated. Check your annotations.", "warning");
            }
        })
        .catch((error) => {
            hideOverlay();
            console.error("Error calculating RF:", error);
            notify(error.message || "Failed to calculate RF values.", "error");
        });
}

// Text size and "full labels" change only the saved image, not the numbers:
// redraw it from the same annotations without asking for Calculate RF. Export
// waits for the new image. Stale results are left alone: they need Calculate
// RF anyway, which uses the new setting. If the annotations change while the
// redraw runs, its reply is dropped (it would show the old annotations).
export function rerenderResults() {
    if (!state.hasResults || state.resultsStale) return;
    const seq = ++calcSeq;
    setResultsRendering(true);
    calculationPayload()
        .then((payload) => postJson("/calculate_rf/", payload))
        .then((result) => {
            if (seq !== calcSeq) return;
            setResultsRendering(false);
            if (!state.hasResults || state.resultsStale) return;
            if (result.rf_values && result.rf_values.length > 0) applyResult(result, true);
        })
        .catch((error) => {
            if (seq !== calcSeq) return;
            setResultsRendering(false);
            console.error("Error redrawing the annotated image:", error);
            notify(error.message || "Failed to update the annotated image.", "error");
        });
}

// --- Candidate detection (TLC-SpotDet / TLC-LineDet) ---------------------
// POST JSON; on failure, reject with the server's user-facing ``error`` when
// it sent one (the 400s explain what to fix), else a generic HTTP status.
function postJson(url, payload) {
    return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify(payload),
    }).then((response) =>
        response
            .json()
            .catch(() => ({}))
            .then((body) => {
                // 400s carry a user-facing reason; anything else is generic.
                if (!response.ok) throw new Error(body.error || "HTTP " + response.status);
                return body;
            }),
    );
}

// TLC-SpotDet sometimes returns a second, larger or smaller box on the same
// spot (on 223 plates, 28 % of candidates were such a second box), while
// neighbouring spots lie about a box apart. Two candidates are the same spot
// when the centre of each lies inside the other's box. A spot already on the
// plate has no box (placed by hand, or from an earlier run), so there the
// candidate's box decides. Image px throughout, so the display size does not
// matter. Without a box (an older server), only an identical position counts.
export function sameSpot(a, b) {
    const w = Math.min(a.w ?? Infinity, b.w ?? Infinity);
    const h = Math.min(a.h ?? Infinity, b.h ?? Infinity);
    if (!Number.isFinite(w) || !Number.isFinite(h)) return a.x === b.x && a.y === b.y;
    return Math.abs(a.x - b.x) <= w / 2 && Math.abs(a.y - b.y) <= h / 2;
}

// The candidates to add, in the server's order: of two on the same spot the
// more confident, and none on a spot that is already there.
export function newCandidates(candidates, spots) {
    const kept = [];
    [...candidates]
        .sort((a, b) => (b.conf || 0) - (a.conf || 0))
        .forEach((c) => {
            if (!kept.some((k) => sameSpot(c, k)) && !spots.some((s) => sameSpot(c, s))) kept.push(c);
        });
    return candidates.filter((c) => kept.includes(c));
}

// Spots are searched only between the baseline and the solvent front (SI §4),
// so both lines must exist first.
export function autoDetectSpots() {
    if (!hasImage()) {
        notify("Load an image first.", "warning");
        return;
    }
    const minLine = state.lines.find((l) => l.type === "min");
    const maxLine = state.lines.find((l) => l.type === "max");
    if (!minLine || !maxLine) {
        notify("Place the Min and Max lines first (or click Suggest Lines).", "warning");
        return;
    }
    showOverlay("Finding candidate spots…");
    getImagePayload()
        .then((payload) => {
            payload.minY = minLine.y;
            payload.maxY = maxLine.y;
            if (state.plateRegion) payload.region = state.plateRegion;
            return postJson("/detect-spots/", payload);
        })
        .then((result) => {
            hideOverlay();
            const fresh = newCandidates(result.spots || [], state.spots);
            fresh.forEach((s) => addSpot(s.x, s.y, true));
            const added = fresh.length;
            if (added > 0) {
                commitChange(); // one undo entry for the whole detection batch
                notify("Suggested " + added + " spot(s). Review and adjust as needed.", "success");
            } else {
                redrawCanvas();
                notify("No new spots found — add them manually.", "info");
            }
        })
        .catch((error) => {
            hideOverlay();
            console.error("Error detecting spots:", error);
            notify(error.message || "Spot detection failed.", "error");
        });
}

// Place (or move) the Min and Max lines where TLC-LineDet puts them. The server
// assigns them by position — lower = baseline — and estimates one line at
// 10 % / 90 % of the height when only the other was found. That line is marked
// `estimated` (drawn dashed) until the user moves it.
export function autoDetectLines() {
    if (!hasImage()) {
        notify("Load an image first.", "warning");
        return;
    }
    showOverlay("Finding the baseline and solvent front…");
    getImagePayload()
        .then((payload) => {
            if (state.plateRegion) payload.region = state.plateRegion;
            return postJson("/detect-lines/", payload);
        })
        .then((result) => {
            hideOverlay();
            const r = state.plateRegion;
            const place = (type, y) => {
                // Lines must stay inside a plate area, as when placed by hand.
                const yy = r ? clamp(y, r.y, r.y + r.height) : y;
                const estimated = result.estimated === type;
                const existing = state.lines.find((l) => l.type === type);
                if (existing) Object.assign(existing, { y: yy, estimated });
                else state.lines.push({ id: Date.now() + state.lineSeq++, type, y: yy, text: type, estimated });
            };
            place("min", result.minY);
            place("max", result.maxY);
            commitChange(); // both lines are one undo step
            if (result.estimated) {
                const which = result.estimated === "min" ? "Min (baseline)" : "Max (solvent front)";
                notify(
                    "Only one line was found; " + which + " is an estimate (dashed) — drag it into place.",
                    "warning",
                );
            } else {
                notify("Placed Min and Max lines. Review and drag to adjust.", "success");
            }
        })
        .catch((error) => {
            hideOverlay();
            console.error("Error detecting lines:", error);
            notify(error.message || "Line detection failed.", "error");
        });
}

// Rows arrive sorted (by lane label, then Rf) and numbered by the server; the
// number matches the one on the annotated image and in the CSV.
export function populateTable(rfValues) {
    const table = document.getElementById("values-table");
    const tbody = table.getElementsByTagName("tbody")[0];
    tbody.innerHTML = "";
    state.highlightedSpot = null;
    // Type columns appear only when some marker has a type.
    table.classList.toggle("no-lane-type", !rfValues.some((v) => v.solvent_type));
    table.classList.toggle("no-compound-type", !rfValues.some((v) => v.compound_type));
    updateAnnotatedHighlight();
    rfValues.forEach((value, index) => {
        const row = tbody.insertRow();
        if (typeof value.x === "number" && typeof value.y === "number") {
            row.dataset.x = value.x;
            row.dataset.y = value.y;
            row.classList.add("clickable");
            row.addEventListener("click", () => highlightSpotFromRow(row));
        }
        const cells = [
            ["col-n", value.n ?? index + 1],
            ["", value.solvent || ""], // an unmarked lane is blank
            ["col-lane-type", value.solvent_type || ""],
            ["", value.compound || ""], // "Unknown" from the server when unmarked
            ["col-compound-type", value.compound_type || ""],
            ["col-rf", typeof value.rf === "number" ? value.rf.toFixed(2) : ""],
        ];
        cells.forEach(([cls, text]) => {
            const cell = row.insertCell();
            if (cls) cell.className = cls;
            cell.textContent = text;
        });
        const actions = row.insertCell();
        const del = document.createElement("button");
        del.type = "button";
        del.className = "row-del";
        del.setAttribute("aria-label", "Delete spot " + (value.n ?? index + 1));
        del.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-close"/></svg>';
        del.addEventListener("click", (e) => {
            e.stopPropagation();
            deleteResultRow(row);
        });
        actions.appendChild(del);
    });
}

function highlightSpotFromRow(row) {
    if (row.dataset.x === undefined) return;
    state.highlightedSpot = { x: Number(row.dataset.x), y: Number(row.dataset.y) };
    document
        .querySelectorAll("#values-table tbody tr.active")
        .forEach((r) => r.classList.remove("active"));
    row.classList.add("active");
    redrawCanvas();
}

function deleteResultRow(row) {
    if (row.dataset.x !== undefined) {
        const rx = Number(row.dataset.x);
        const ry = Number(row.dataset.y);
        state.spots = state.spots.filter((s) => Math.abs(s.x - rx) > 1 || Math.abs(s.y - ry) > 1);
        if (
            state.highlightedSpot &&
            Math.abs(state.highlightedSpot.x - rx) < 2 &&
            Math.abs(state.highlightedSpot.y - ry) < 2
        ) {
            state.highlightedSpot = null;
        }
    }
    row.remove();

    // Deleting the spot is an undoable edit. If none remain, clear the results
    // entirely; otherwise commitChange() records it and flags the results stale.
    if (state.spots.length > 0) {
        commitChange();
    } else {
        const annotated = document.getElementById("annotated-image");
        if (annotated) annotated.removeAttribute("src");
        state.hasResults = false;
        state.csvUrl = null;
        state.annotatedUrl = null;
        clearResultsStale();
        updateAnnotatedHeading("editing");
        commitHistory();
        redrawCanvas();
    }
}

// --- Export names -----------------------------------------------------------
// Every exported file -- download, ELN upload, or inside the ZIP -- is named
// after the image (its name without the extension; "tlc_plate" for an ELN
// image), the time of the result, and what it holds, e.g.
// "plate7_2026-11-16_14-32_rf.csv". The three files of one result share the
// stem, so they sort together.
const EXPORT_SUFFIX = { csv: "_rf.csv", image: "_annotated.png", annotations: "_annotations.json" };

export function exportBase(date = state.resultsTime || new Date()) {
    const pad = (n) => String(n).padStart(2, "0");
    const stamp =
        date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
        "_" + pad(date.getHours()) + "-" + pad(date.getMinutes());
    const stem = (state.imageName || "").replace(/\.[^.]*$/, "") || "tlc_plate";
    return stem + "_" + stamp;
}

export function exportName(kind, date) {
    return exportBase(date) + EXPORT_SUFFIX[kind];
}

// Save a blob under a name. A blob URL is used because a server response may
// name the file itself (Content-Disposition), which browsers prefer to `download`.
export function downloadBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// --- CSV export -----------------------------------------------------------
// The CSV is written by the server with the calculation (tlc/results.py), so
// it always matches the table and the numbered image. Export is disabled while
// results are stale, so the stored file is never out of date when used.
export function saveCSV() {
    if (!state.csvUrl) {
        notify("Calculate RF values first.", "warning");
        return;
    }
    const link = document.createElement("a");
    link.href = state.csvUrl;
    link.download = exportName("csv");
    document.body.appendChild(link);
    link.click();
    link.remove();
    notify("CSV downloaded.", "success");
}

// --- Save back to the ELN -------------------------------------------------
// Chemotion uses `attachmentName` as the file's name; without it, it names
// the file "<app name>-<upload name>".
// Sent cross-origin to the ELN's token URL. No CSRF header: the token is ours,
// means nothing to the ELN, and a custom header would force a CORS preflight.
export function postToEln(blob, name) {
    if (!elnUrl) {
        notify("No ELN target configured.", "warning");
        return;
    }
    const formData = new FormData();
    formData.append("file", blob, name);
    formData.append("attachmentName", name);
    fetch(elnUrl, { method: "POST", body: formData })
        .then((response) => {
            if (!response.ok) throw new Error("HTTP " + response.status);
        })
        .then(() => notify('Saved to ELN as "' + name + '".', "success"))
        .catch((error) => {
            console.error("Error saving to ELN:", error);
            notify("Failed to save to ELN.", "error");
        });
}

// Download the annotated image (the server always renders PNG).
export function saveImage() {
    const annotated = document.getElementById("annotated-image");
    if (!annotated || !annotated.src) {
        notify("Calculate RF values first.", "warning");
        return;
    }
    fetch(annotated.src)
        .then((response) => {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.blob();
        })
        .then((blob) => {
            downloadBlob(blob, exportName("image"));
            notify("Annotated image downloaded.", "success");
        })
        .catch(() => notify("Failed to load the annotated image.", "error"));
}

// Download all three files of the result as one ZIP, built by the server from
// the stored CSV and image plus the annotation file, which lives here.
export function saveAll() {
    if (!state.hasResults || !state.csvUrl || !state.annotatedUrl) {
        notify("Calculate RF values first.", "warning");
        return;
    }
    const base = exportBase();
    fetch("/download-results/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify({
            csv: state.csvUrl,
            image: state.annotatedUrl,
            annotations: annotationsText(),
            name: base,
        }),
    })
        .then((response) => {
            if (response.ok) return response.blob();
            return response
                .json()
                .catch(() => ({}))
                .then((body) => {
                    throw new Error(body.error || "HTTP " + response.status);
                });
        })
        .then((blob) => {
            downloadBlob(blob, base + ".zip");
            notify("Results downloaded (ZIP).", "success");
        })
        .catch((error) => {
            console.error("Error downloading the results:", error);
            notify(error.message || "Download failed.", "error");
        });
}

// Copy the CSV's Details sentence -- Rf values in the style of Chemotion's TLC
// free text -- to paste into the ELN. The server writes it (one source for
// the CSV and here); where the clipboard is unavailable (plain http, or
// permission refused), it is shown to copy by hand.
export function copyRfSummary() {
    const text = state.rfSummary;
    if (!state.hasResults || !text) {
        notify("Calculate RF values first.", "warning");
        return;
    }
    const fallback = () => notify("Could not copy. Rf summary: " + text, "warning", 15000);
    if (!(navigator.clipboard && navigator.clipboard.writeText)) {
        fallback();
        return;
    }
    navigator.clipboard.writeText(text).then(() => notify("Rf summary copied.", "success"), fallback);
}

// Upload the annotated image to the ELN (re-encoded from the full-res result).
export function imageToEln() {
    const annotated = document.getElementById("annotated-image");
    if (!annotated || !annotated.src) {
        notify("Annotated image not found. Calculate RF values first.", "error");
        return;
    }
    const sourceImage = new Image();
    sourceImage.crossOrigin = "anonymous";
    sourceImage.onload = function () {
        const exportCanvas = document.createElement("canvas");
        exportCanvas.width = sourceImage.naturalWidth;
        exportCanvas.height = sourceImage.naturalHeight;
        exportCanvas.getContext("2d").drawImage(sourceImage, 0, 0);
        exportCanvas.toBlob((blob) => postToEln(blob, exportName("image")), "image/png");
    };
    sourceImage.onerror = () => notify("Failed to load annotated image.", "error");
    sourceImage.src = annotated.src;
}

// Upload the results CSV to the ELN.
export function csvToEln() {
    if (!state.csvUrl) {
        notify("No CSV data to save. Calculate RF values first.", "warning");
        return;
    }
    fetch(state.csvUrl)
        .then((response) => {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.blob();
        })
        .then((blob) => postToEln(blob, exportName("csv")))
        .catch(() => notify("Failed to load the CSV.", "error"));
}
