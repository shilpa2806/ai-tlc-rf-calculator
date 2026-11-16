/*
 * Drafts: the annotations of each image are kept in this browser's local
 * storage after every change, keyed by the image's SHA-256, so a reload or a
 * closed tab does not lose the work. Loading the same image again offers the
 * draft in a banner -- it is never restored by itself. Reset and Discard
 * delete it. Only the newest MAX_DRAFTS are kept.
 *
 * Storage can be missing or full (private windows, quotas); every access is
 * guarded and failure only means no draft.
 */

"use strict";

import { state } from "./state.js";
import { serialize, validate } from "./annotation-file.js";

const PREFIX = "tlc-draft:";
const INDEX = "tlc-draft-index"; // [{ sha, saved }], newest last
const MAX_DRAFTS = 20;

function readIndex() {
    try {
        const index = JSON.parse(localStorage.getItem(INDEX) || "[]");
        return Array.isArray(index) ? index : [];
    } catch (err) {
        return [];
    }
}

function writeIndex(index) {
    try {
        localStorage.setItem(INDEX, JSON.stringify(index));
    } catch (err) {
        /* storage unavailable */
    }
}

function forget(sha) {
    try {
        localStorage.removeItem(PREFIX + sha);
    } catch (err) {
        /* storage unavailable */
    }
    writeIndex(readIndex().filter((entry) => entry.sha !== sha));
}

function hasAnnotations() {
    return (
        state.lines.length > 0 ||
        state.spots.length > 0 ||
        !!state.plateRegion ||
        !!state.mobilePhase ||
        !!state.stationaryPhase
    );
}

// Save (or, with nothing annotated, delete) the current image's draft.
export function saveDraft() {
    const sha = state.imageSha256;
    if (!sha || state.draftOffered) return; // don't overwrite a draft still on offer
    if (!hasAnnotations()) {
        forget(sha);
        return;
    }
    const saved = new Date().toISOString();
    try {
        localStorage.setItem(PREFIX + sha, JSON.stringify(serialize()));
    } catch (err) {
        return; // full or unavailable: no draft
    }
    const index = readIndex().filter((entry) => entry.sha !== sha);
    index.push({ sha, saved });
    while (index.length > MAX_DRAFTS) {
        const old = index.shift();
        try {
            localStorage.removeItem(PREFIX + old.sha);
        } catch (err) {
            /* storage unavailable */
        }
    }
    writeIndex(index);
}

// The saved draft for the current image, validated, or null.
export function findDraft() {
    const sha = state.imageSha256;
    if (!sha) return null;
    let data;
    try {
        data = JSON.parse(localStorage.getItem(PREFIX + sha) || "null");
    } catch (err) {
        return null;
    }
    if (!data) return null;
    const checked = validate(data);
    if (checked.error) {
        forget(sha);
        return null;
    }
    return checked.data;
}

export function deleteDraft() {
    if (state.imageSha256) forget(state.imageSha256);
}
