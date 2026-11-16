/*
 * Annotation files in the page: identifying the loaded image, offering a
 * saved draft, opening an annotation file, and exporting one (download or
 * ELN). The format itself is in annotation-file.js; storage in drafts.js.
 */

"use strict";

import { state } from "./state.js";
import { notify } from "./ui-kit.js";
import { annotationsText, applyAnnotations, imageMatch, sha256Hex, validate } from "./annotation-file.js";
import { deleteDraft, findDraft } from "./drafts.js";
import { commitChange, fitAndDraw, syncRotationControls } from "./canvas.js";
import { syncPlateInfo } from "./ui.js";
import { downloadBlob, exportName, postToEln } from "./results.js";

// --- The loaded image's identity --------------------------------------------
// Called when an image starts loading: forget the previous identity and hide
// any banner that belonged to it. Returns a token for identifyImage.
export function beginImage(name) {
    state.imageToken = (state.imageToken || 0) + 1;
    state.imageSha256 = null;
    state.imageName = name || "";
    delete document.body.dataset.imageIdentified; // readiness signal (used by tests)
    hideDraftBanner();
    return state.imageToken;
}

// Once the image's bytes are hashed: record the SHA-256 and offer its draft.
// `bytes` resolves to an ArrayBuffer; a stale token (another image loaded
// meanwhile) is ignored.
export function identifyImage(bytes, token) {
    Promise.resolve(bytes)
        .then((buffer) => (buffer ? sha256Hex(buffer) : null))
        .then((sha) => {
            if (token !== state.imageToken) return;
            state.imageSha256 = sha;
            offerDraft();
            document.body.dataset.imageIdentified = "1";
        })
        .catch(() => {
            /* no hash: no drafts, and opened files cannot be checked */
        });
}

// --- Draft banner -----------------------------------------------------------
function hideDraftBanner() {
    state.draftOffered = false;
    const banner = document.getElementById("draft-banner");
    if (banner) banner.hidden = true;
}

function offerDraft() {
    const draft = findDraft();
    if (!draft) return;
    const banner = document.getElementById("draft-banner");
    if (!banner) return;
    const when = new Date(draft.saved);
    const text = Number.isNaN(when.getTime())
        ? "earlier"
        : when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    const count = draft.spots.length;
    document.getElementById("draft-text").textContent =
        "Unsaved annotations for this image from " + text + " (" + count + " spot" + (count === 1 ? "" : "s") + ").";
    state.draftOffered = true;
    banner.hidden = false;
}

function replaceAnnotations(data) {
    document.querySelectorAll(".delete-icon").forEach((el) => el.remove());
    applyAnnotations(data);
    syncRotationControls();
    syncPlateInfo();
    fitAndDraw(state.image); // the rotation may change the frame
    commitChange(); // undoable, marks results stale, saves the draft
}

export function restoreDraft() {
    const draft = findDraft();
    hideDraftBanner();
    if (!draft) return;
    replaceAnnotations(draft);
    notify("Annotations restored.", "success");
}

export function discardDraft() {
    hideDraftBanner();
    deleteDraft();
    if (state.lines.length || state.spots.length || state.plateRegion || state.mobilePhase || state.stationaryPhase) {
        commitChange(); // re-save current work
    }
}

// --- Open an annotation file --------------------------------------------------
export function openAnnotations() {
    if (!state.image) {
        notify("Load the image first, then open its annotations.", "warning");
        return;
    }
    document.getElementById("annotations-input").click();
}

export function handleAnnotationsFile(event) {
    const input = event.target;
    const file = input.files && input.files[0];
    input.value = ""; // the same file can be chosen again
    if (!file) return;
    file.text()
        .then((text) => {
            let parsed;
            try {
                parsed = JSON.parse(text);
            } catch (err) {
                notify("This is not a TLC annotation file.", "error");
                return;
            }
            const checked = validate(parsed);
            if (checked.error) {
                notify(checked.error, "error");
                return;
            }
            const match = imageMatch(checked.data);
            if (match === "different") {
                const name = checked.data.image && checked.data.image.name;
                notify(
                    "These annotations belong to a different image" + (name ? ' ("' + name + '")' : "") + ".",
                    "error",
                );
                return;
            }
            hideDraftBanner();
            replaceAnnotations(checked.data);
            notify(
                match === "same"
                    ? "Annotations opened."
                    : "Annotations opened. The image could not be checked against them.",
                match === "same" ? "success" : "warning",
            );
        })
        .catch(() => notify("Could not read the file.", "error"));
}

// --- Export -----------------------------------------------------------------
function annotationsBlob() {
    return new Blob([annotationsText()], { type: "application/json" });
}

export function saveAnnotations() {
    if (!state.hasResults) {
        notify("Calculate RF values first.", "warning");
        return;
    }
    downloadBlob(annotationsBlob(), exportName("annotations"));
    notify("Annotations downloaded.", "success");
}

export function annotationsToEln() {
    if (!state.hasResults) {
        notify("Calculate RF values first.", "warning");
        return;
    }
    postToEln(annotationsBlob(), exportName("annotations"));
}
