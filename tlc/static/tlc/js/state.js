/*
 * Shared mutable state for the annotation UI.
 *
 * ES modules make imported bindings read-only in the importer, so all mutable
 * state lives as properties of a single exported object: every module reads and
 * writes `state.foo` rather than re-binding a top-level `let`. `canvas`/`ctx`
 * are assigned in init() (see main.js) — never captured at import time.
 */

"use strict";

export const pageUrl = new URL(window.location.href);

const encodedUrl = pageUrl.searchParams.get("url");
// Chemotion passes one signed URL (via ?url=) that serves the source image on
// GET and accepts the annotated results on POST — the same endpoint for both.
export const elnUrl = encodedUrl ? decodeURIComponent(encodedUrl) : null;

export const state = {
    image: new Image(),
    canvas: null,
    ctx: null,

    lines: [],
    spots: [],
    spotSeq: 0,
    lineSeq: 0,
    rotation: 0, // clockwise preset the image is displayed at (0/90/180/270)
    fineRotation: 0, // clockwise fine-tune on top (deskew), degrees, within ±45
    imageName: "", // the loaded image's file name ("" for ELN images)
    imageSha256: null, // its SHA-256 once hashed: ties drafts and annotation files to it
    imageToken: 0, // bumped per image load, so a late hash is not applied to the next image
    draftOffered: false, // a saved draft is on offer (banner shown)

    currentLine: null, // the H/V line the label popup is open for
    popupEdit: null, // {text, kind} before an edit, so Cancel can restore it
    isHorizontal: true,
    lineType: "",
    addingSpots: false,
    placingLine: false, // a line tool is armed: the next click places a line

    isLocalImage: false,
    // A local image is uploaded once; later requests send only its name.
    localImageUpload: null, // Promise<string|null> for the current image's stored name

    // Drag state.
    draggingSpot: null,
    draggingLine: null,
    isDragging: false,
    dragOffsetX: 0,
    dragOffsetY: 0,
    lineDragOffsetX: 0,
    lineDragOffsetY: 0,
    justDragged: false,

    // Optional plate region-of-interest; confines Detect Lines and Detect Spots.
    plateRegion: null, // {x, y, width, height} in original-image coordinates
    selectingRegion: false, // the "Plate Area" tool is armed
    draggingRegion: false, // a region drag is in progress
    regionStart: null, // {x, y} in display coordinates
    regionCurrent: null, // {x, y} in display coordinates
    highlightedSpot: null, // {x, y} of a result row highlighted on the canvas
    cropView: null, // when cropped-to-plate: {rcx, rcy, k} in canvas display px

    // What the plate was run in: free text, part of the annotations (saved in
    // drafts and annotation files, recorded in the CSV).
    mobilePhase: "",
    stationaryPhase: "",

    hasResults: false, // a calculation has produced a table + annotated image
    resultsStale: false, // annotations changed since: export blocked until recalculated
    rendering: false, // the annotated image is being redrawn (text size, full labels)
    csvUrl: null, // the server-written CSV of the current results
    annotatedUrl: null, // the server-written annotated image of the current results
    rfSummary: "", // the CSV's Details sentence, for "Copy Rf summary"
    resultsTime: null, // when the current results were made: stamps the export names
};
