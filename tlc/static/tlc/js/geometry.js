/*
 * Small pure geometry helpers for constraining annotations to the plate area.
 * Leaf module: imports nothing. All coordinates are original-image pixels, the
 * same units as state.plateRegion / line.y|x / spot.x|y.
 */

"use strict";

export function clamp(value, lo, hi) {
    return Math.max(lo, Math.min(hi, value));
}

export function pointInRegion(x, y, r) {
    return x >= r.x && x <= r.x + r.width && y >= r.y && y <= r.y + r.height;
}
