/*
 * Undo/redo of annotation state (reference/label lines, spots, plate region,
 * the plate description) and the rotation they were made in: marks are coordinates in the rotated
 * frame, so the two are always restored together.
 *
 * A stack of deep-copied snapshots with a cursor: `commit()` records the state
 * after a completed edit (dropping any redo tail), `undo()`/`redo()` move the
 * cursor and restore. Leaf module — imports state only. Snapshots deliberately
 * omit the transient `deleteIcon` DOM refs (redrawCanvas rebuilds them).
 */

"use strict";

import { state } from "./state.js";

let stack = [];
let cursor = -1;

function snapshot() {
    return {
        lines: state.lines.map((l) => ({
            id: l.id,
            type: l.type,
            x: l.x,
            y: l.y,
            text: l.text,
            kind: l.kind || "",
            estimated: !!l.estimated,
        })),
        spots: state.spots.map((s) => ({ id: s.id, x: s.x, y: s.y, auto: s.auto, moved: !!s.moved })),
        plateRegion: state.plateRegion ? { ...state.plateRegion } : null,
        rotation: state.rotation,
        fineRotation: state.fineRotation,
        mobilePhase: state.mobilePhase,
        stationaryPhase: state.stationaryPhase,
    };
}

function restore(snap) {
    state.lines = snap.lines.map((l) => ({ ...l }));
    state.spots = snap.spots.map((s) => ({ ...s }));
    state.plateRegion = snap.plateRegion ? { ...snap.plateRegion } : null;
    state.rotation = snap.rotation;
    state.fineRotation = snap.fineRotation;
    state.mobilePhase = snap.mobilePhase;
    state.stationaryPhase = snap.stationaryPhase;
}

// Baseline the history at the current state (call on load / reset / init).
export function resetHistory() {
    stack = [snapshot()];
    cursor = 0;
}

// Record the state after a completed edit; drops any redo tail.
export function commitHistory() {
    if (cursor < 0) {
        resetHistory();
        return;
    }
    stack = stack.slice(0, cursor + 1);
    stack.push(snapshot());
    cursor = stack.length - 1;
}

export function canUndo() {
    return cursor > 0;
}

export function canRedo() {
    return cursor >= 0 && cursor < stack.length - 1;
}

export function undoHistory() {
    if (!canUndo()) return false;
    cursor -= 1;
    restore(stack[cursor]);
    return true;
}

export function redoHistory() {
    if (!canRedo()) return false;
    cursor += 1;
    restore(stack[cursor]);
    return true;
}
