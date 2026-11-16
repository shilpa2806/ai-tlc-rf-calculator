/*
 * The annotation file: everything needed to redo or check a result -- the
 * image's identity, rotation, Plate Area, Min/Max, markers with their labels
 * and types, and each spot with how it was placed -- as one versioned JSON.
 *
 * One format, three uses:
 *  - Export: downloaded or uploaded to the ELN next to the CSV;
 *  - Open: loaded back onto the same image to check or continue a result;
 *  - Drafts: kept in the browser after every change (drafts.js).
 *
 * The image itself is not in the file; its SHA-256 ties the file to it.
 *
 * Optional fields a version 1 file may lack (it then reads as before):
 * `mobilePhase` and `stationaryPhase` (free text) and `estimated` on a Min or
 * Max line that Suggest Lines guessed and nobody has moved yet.
 */

"use strict";

import { state } from "./state.js";

export const FORMAT = "tlc-rf-annotations";
export const VERSION = 1;

const SOURCES = { manual: [false, false], detected: [true, false], "detected, moved": [true, true] };

function spotSource(spot) {
    if (!spot.auto) return "manual";
    return spot.moved ? "detected, moved" : "detected";
}

// Free text as one trimmed line, as the server cleans labels (clean_label).
export const MAX_TEXT_CHARS = 100;

export function cleanText(raw) {
    return typeof raw === "string" ? raw.split(/\s+/).filter(Boolean).join(" ").slice(0, MAX_TEXT_CHARS) : "";
}

function round(value) {
    return Math.round(value * 100) / 100; // 0.01 px is far below what matters
}

// SHA-256 of the image bytes, as hex; null where the browser offers no
// crypto.subtle (plain http on a non-local host).
export async function sha256Hex(buffer) {
    if (!(window.crypto && window.crypto.subtle)) return null;
    const digest = await window.crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// The current annotations, as the file's object.
export function serialize() {
    const lines = state.lines.map((line) => {
        const out = { type: line.type };
        if (line.type === "vertical") out.x = round(line.x);
        else out.y = round(line.y);
        if (line.type === "horizontal" || line.type === "vertical") {
            out.text = line.text || "";
            out.kind = line.kind || "";
        } else if (line.estimated) {
            out.estimated = true;
        }
        return out;
    });
    const region = state.plateRegion;
    return {
        format: FORMAT,
        version: VERSION,
        app: document.body.dataset.appVersion || "",
        saved: new Date().toISOString(),
        image: {
            name: state.imageName || "",
            sha256: state.imageSha256 || null,
            width: state.image ? state.image.naturalWidth : null,
            height: state.image ? state.image.naturalHeight : null,
        },
        mobilePhase: state.mobilePhase,
        stationaryPhase: state.stationaryPhase,
        rotation: state.rotation,
        fineRotation: state.fineRotation,
        plateRegion: region
            ? { x: round(region.x), y: round(region.y), width: round(region.width), height: round(region.height) }
            : null,
        lines,
        spots: state.spots.map((s) => ({ x: round(s.x), y: round(s.y), source: spotSource(s) })),
    };
}

const finite = (v) => typeof v === "number" && Number.isFinite(v);

// Check a parsed file. Returns { data } or { error } (a user-facing reason).
export function validate(data) {
    if (!data || typeof data !== "object" || data.format !== FORMAT) {
        return { error: "This is not a TLC annotation file." };
    }
    if (data.version !== VERSION) {
        return { error: "This annotation file is from a newer version of the app." };
    }
    if (![0, 90, 180, 270].includes(data.rotation) || !finite(data.fineRotation) || Math.abs(data.fineRotation) > 45) {
        return { error: "The annotation file has an invalid rotation." };
    }
    if (!Array.isArray(data.lines) || !Array.isArray(data.spots)) {
        return { error: "The annotation file has no lines or spots." };
    }
    const types = ["min", "max", "horizontal", "vertical"];
    for (const line of data.lines) {
        const axis = line && line.type === "vertical" ? "x" : "y";
        if (!line || !types.includes(line.type) || !finite(line[axis])) {
            return { error: "The annotation file has an invalid line." };
        }
    }
    if (data.lines.filter((l) => l.type === "min").length > 1 || data.lines.filter((l) => l.type === "max").length > 1) {
        return { error: "The annotation file has more than one Min or Max line." };
    }
    for (const spot of data.spots) {
        if (!spot || !finite(spot.x) || !finite(spot.y) || !(spot.source in SOURCES)) {
            return { error: "The annotation file has an invalid spot." };
        }
    }
    for (const key of ["mobilePhase", "stationaryPhase"]) {
        if (data[key] !== undefined && typeof data[key] !== "string") {
            return { error: "The annotation file has an invalid plate description." };
        }
    }
    const r = data.plateRegion;
    if (r !== null && r !== undefined && !(finite(r.x) && finite(r.y) && finite(r.width) && finite(r.height))) {
        return { error: "The annotation file has an invalid plate area." };
    }
    return { data };
}

// Whether a file belongs to the loaded image: "same", "different", or
// "unknown" (a hash is missing on either side).
export function imageMatch(data) {
    const theirs = data.image && data.image.sha256;
    if (!theirs || !state.imageSha256) return "unknown";
    return theirs === state.imageSha256 ? "same" : "different";
}

// Replace the current annotations with a validated file's. The caller redraws
// and commits (so the change can be undone).
export function applyAnnotations(data) {
    state.rotation = data.rotation;
    state.fineRotation = data.fineRotation;
    let id = Date.now();
    state.lines = data.lines.map((line) => {
        const out = { id: id++, type: line.type, kind: "" };
        if (line.type === "vertical") out.x = line.x;
        else out.y = line.y;
        if (line.type === "min" || line.type === "max") {
            out.text = line.type;
            out.estimated = line.estimated === true;
        } else {
            out.text = typeof line.text === "string" ? line.text : "";
            out.kind = typeof line.kind === "string" ? line.kind : "";
        }
        return out;
    });
    state.spots = data.spots.map((spot) => {
        const [auto, moved] = SOURCES[spot.source];
        return { id: id++, x: spot.x, y: spot.y, auto, moved };
    });
    const r = data.plateRegion;
    state.plateRegion = r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null;
    state.mobilePhase = cleanText(data.mobilePhase);
    state.stationaryPhase = cleanText(data.stationaryPhase);
}

// The file's text, as exported (download, ELN, or inside "Download all").
export function annotationsText() {
    return JSON.stringify(serialize(), null, 2) + "\n";
}
