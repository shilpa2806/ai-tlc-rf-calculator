/*
 * TLC RF Calculator — annotation UI entry point.
 *
 * Wires the declarative `data-action` controls to their handlers, binds canvas
 * interaction, and initialises the page. The behaviour is split across sibling
 * ES modules (state, ui-kit, theme, ui, canvas, annotations, interaction,
 * results, sources); this module owns only the action dispatch table and init.
 *
 * Self-contained: no external libraries.
 */

"use strict";

import { pageUrl, state } from "./state.js";
import { preventDefaults } from "./ui-kit.js";
import { initTheme, toggleTheme } from "./theme.js";
import {
    markResultsStale,
    refreshUI,
    toggleHelp,
    toggleImageSource,
    updateAnnotatedHeading,
    updateAnnotatedHighlight,
    updateExportButtons,
} from "./ui.js";
import {
    applyCrop,
    commitChange,
    redoEdit,
    resetCanvas,
    nudgeRotation,
    setRotation,
    undoEdit,
} from "./canvas.js";
import {
    addHorizontalLine,
    addMaxLine,
    addMinLine,
    addSpots,
    addVerticalLine,
    cancelAnnotation,
    disableOtherActions,
    popupOpen,
    selectPlateArea,
    submitAnnotation,
} from "./annotations.js";
import {
    handleDoubleClick,
    handlePointerCancel,
    handlePointerDown,
    handlePointerMove,
    handlePointerUp,
} from "./interaction.js";
import {
    autoDetectLines,
    autoDetectSpots,
    calculateRF,
    copyRfSummary,
    csvToEln,
    imageToEln,
    rerenderResults,
    saveAll,
    saveCSV,
    saveImage,
} from "./results.js";
import { cleanText } from "./annotation-file.js";
import {
    annotationsToEln,
    discardDraft,
    handleAnnotationsFile,
    openAnnotations,
    restoreDraft,
    saveAnnotations,
} from "./annotations-io.js";
import { handleLocalImageFile, handleLocalImageUpload, selectImageSource, triggerFileInput } from "./sources.js";

// Maps the `data-action` attribute on a control to its handler. Handlers that
// need the originating button receive the click event; those that take a value
// receive the element's `data-arg`.
const ACTIONS = {
    toggleTheme,
    toggleImageSource,
    toggleHelp,
    selectImageSource,
    triggerFileInput,
    addSpots,
    autoDetectLines,
    autoDetectSpots,
    selectPlateArea,
    nudgeRotation,
    restoreDraft,
    discardDraft,
    openAnnotations,
    saveAnnotations,
    annotationsToEln,
    addMinLine,
    addMaxLine,
    addHorizontalLine,
    addVerticalLine,
    calculateRF,
    resetCanvas,
    undoEdit,
    redoEdit,
    updateAnnotatedHeading,
    saveCSV,
    saveImage,
    saveAll,
    copyRfSummary,
    csvToEln,
    imageToEln,
    submitAnnotation,
    cancelAnnotation,
};

function handleAction(event) {
    const el = event.currentTarget;
    const fn = ACTIONS[el.dataset.action];
    if (!fn) return;
    if (el.dataset.arg !== undefined) {
        fn(el.dataset.arg);
    } else {
        fn(event);
    }
}

function init() {
    state.canvas = document.getElementById("image-canvas");
    state.ctx = state.canvas.getContext("2d");

    // Bind all declarative controls (replaces inline onclick handlers).
    document.querySelectorAll("[data-action]").forEach((el) => {
        el.addEventListener("click", handleAction);
    });

    initTheme();

    // "Crop view" preference: persisted; re-applies the crop on toggle.
    const cropToggle = document.getElementById("crop-plate");
    if (cropToggle) {
        try {
            cropToggle.checked = localStorage.getItem("tlc-crop-view") === "1";
        } catch (err) {
            /* ignore */
        }
        cropToggle.addEventListener("change", () => {
            try {
                localStorage.setItem("tlc-crop-view", cropToggle.checked ? "1" : "0");
            } catch (err) {
                /* ignore */
            }
            applyCrop();
        });
    }

    // Text size and "full labels" shape only the saved image: a change redraws
    // it (debounced, so typing "250" is one redraw) without a recalculation.
    let rerenderTimer = null;
    const scheduleRerender = () => {
        clearTimeout(rerenderTimer);
        rerenderTimer = setTimeout(rerenderResults, 400);
    };

    // "Full labels" on the saved image: persisted.
    const fullLabels = document.getElementById("label-full");
    if (fullLabels) {
        try {
            fullLabels.checked = localStorage.getItem("tlc-label-full") === "1";
        } catch (err) {
            /* localStorage unavailable */
        }
        fullLabels.addEventListener("change", () => {
            try {
                localStorage.setItem("tlc-label-full", fullLabels.checked ? "1" : "0");
            } catch (err) {
                /* ignore */
            }
            scheduleRerender();
        });
    }

    // Annotation text size (%) of the saved image: persisted. The preview keeps
    // the plate-proportional size (canvas.js, plateFontPx).
    const textScaleInput = document.getElementById("text-scale");
    if (textScaleInput) {
        try {
            const saved = localStorage.getItem("tlc-text-scale");
            if (saved) textScaleInput.value = saved;
        } catch (err) {
            /* localStorage unavailable */
        }
        textScaleInput.addEventListener("input", () => {
            try {
                localStorage.setItem("tlc-text-scale", textScaleInput.value);
            } catch (err) {
                /* ignore */
            }
            scheduleRerender();
        });
    }

    // Mobile and stationary phase: annotations like the marks. Typing updates
    // the state and flags results stale at once (they are in the CSV); leaving
    // the field records the change for undo and the draft.
    [
        ["mobile-phase", "mobilePhase"],
        ["stationary-phase", "stationaryPhase"],
    ].forEach(([id, key]) => {
        const input = document.getElementById(id);
        if (!input) return;
        input.addEventListener("input", () => {
            state[key] = cleanText(input.value);
            markResultsStale();
        });
        input.addEventListener("change", () => {
            state[key] = cleanText(input.value);
            input.value = state[key];
            commitChange();
        });
    });

    // The highlight ring over the annotated image follows its displayed size.
    window.addEventListener("resize", updateAnnotatedHighlight);
    const annotatedImage = document.getElementById("annotated-image");
    if (annotatedImage) annotatedImage.addEventListener("load", updateAnnotatedHighlight);

    // Rotation: a preset, plus a fine-tune previewed live while typing.
    const rotationPreset = document.getElementById("rotation-preset");
    const rotationFine = document.getElementById("rotation-fine");
    if (rotationPreset && rotationFine) {
        // An empty or unfinished field ("", "-", "2.") reads as NaN, which
        // setRotation treats as "keep the current angle" (Number("") is 0).
        const typedFine = () => (rotationFine.value.trim() === "" ? NaN : Number(rotationFine.value));
        rotationPreset.addEventListener("change", () => setRotation(Number(rotationPreset.value), typedFine()));
        rotationFine.addEventListener("input", () => {
            if (!Number.isFinite(typedFine())) return; // mid-typing
            setRotation(Number(rotationPreset.value), typedFine(), false); // preview; leave the field as typed
        });
        // On commit (blur/Enter) the field is clamped and tidied to one decimal.
        rotationFine.addEventListener("change", () => setRotation(Number(rotationPreset.value), typedFine()));
    }

    refreshUI();

    const fileInput = document.getElementById("local-image-input");
    if (fileInput) fileInput.addEventListener("change", handleLocalImageUpload);
    const annotationsInput = document.getElementById("annotations-input");
    if (annotationsInput) annotationsInput.addEventListener("change", handleAnnotationsFile);

    const uploadArea = document.querySelector(".upload-area");
    if (uploadArea) {
        ["dragenter", "dragover", "dragleave", "drop"].forEach((evt) =>
            uploadArea.addEventListener(evt, preventDefaults, false)
        );
        ["dragenter", "dragover"].forEach((evt) =>
            uploadArea.addEventListener(evt, () => uploadArea.classList.add("dragover"))
        );
        ["dragleave", "drop"].forEach((evt) =>
            uploadArea.addEventListener(evt, () => uploadArea.classList.remove("dragover"))
        );
        uploadArea.addEventListener("drop", (e) => {
            if (e.dataTransfer.files.length > 0) handleLocalImageFile(e.dataTransfer.files[0]);
        });
    }

    state.canvas.addEventListener("pointerdown", handlePointerDown);
    state.canvas.addEventListener("pointermove", handlePointerMove);
    state.canvas.addEventListener("pointerup", handlePointerUp);
    state.canvas.addEventListener("pointercancel", handlePointerCancel);
    state.canvas.addEventListener("dblclick", handleDoubleClick);

    // Enter in the label popup submits it.
    document.getElementById("line-popup").addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            submitAnnotation();
        }
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            // Escape closes the help drawer first if it's open (it's a modal).
            const drawer = document.getElementById("help-drawer");
            if (drawer && drawer.classList.contains("open")) {
                drawer.classList.remove("open");
                return;
            }
            disableOtherActions();
            if (popupOpen()) cancelAnnotation(); // a new line stays, unlabelled
            return;
        }
        // Undo / redo (ignore while typing in the line-label input).
        if (!(e.ctrlKey || e.metaKey) || e.target.tagName === "INPUT") return;
        const key = e.key.toLowerCase();
        if (key === "z" && !e.shiftKey) {
            e.preventDefault();
            undoEdit();
        } else if (key === "y" || (key === "z" && e.shiftKey)) {
            e.preventDefault();
            redoEdit();
        }
    });

    // Default to ELN when a url param is present, otherwise local upload.
    updateExportButtons(); // without ?url= there is no ELN to upload to
    selectImageSource(pageUrl.searchParams.get("url") ? "eln" : "local");
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
