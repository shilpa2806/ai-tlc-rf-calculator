/*
 * Image sources: the ELN/local source toggle, local file upload (picker and
 * drag-and-drop), and fetching an image from the ELN.
 */

"use strict";

import { elnUrl, state } from "./state.js";
import { getCookie, notify } from "./ui-kit.js";
import {
    clearResultsStale,
    collapseImageSource,
    refreshUI,
    syncPlateInfo,
    updateAnnotatedHeading,
    updateExportButtons,
} from "./ui.js";
import { applyCrop, fitAndDraw, syncRotationControls } from "./canvas.js";
import { resetHistory } from "./history.js";
import { beginImage, identifyImage } from "./annotations-io.js";

export function triggerFileInput() {
    document.getElementById("local-image-input").click();
}

// Shows the chosen source's input. state.isLocalImage is left alone: it
// describes the loaded image and changes only when a new one starts its session.
export function selectImageSource(source) {
    const eln = document.getElementById("eln-source-btn");
    const local = document.getElementById("local-source-btn");
    eln.classList.toggle("active", source === "eln");
    eln.classList.toggle("btn-lab-primary", source === "eln");
    eln.classList.toggle("btn-lab-outline", source !== "eln");
    local.classList.toggle("active", source === "local");
    local.classList.toggle("btn-lab-primary", source === "local");
    local.classList.toggle("btn-lab-outline", source !== "local");

    document.getElementById("local-upload-section").style.display =
        source === "local" ? "block" : "none";

    if (source === "eln") loadELNImage();
}

export function handleLocalImageUpload(event) {
    const file = event.target.files[0];
    if (file) handleLocalImageFile(file);
}

export function handleLocalImageFile(file) {
    if (!file.type.startsWith("image/")) {
        notify("Please select a valid image file.", "error");
        return;
    }
    // The server's limit, passed in by the page so the two cannot disagree.
    const maxMb = Number(document.body.dataset.maxImageMb) || 25;
    if (file.size > maxMb * 1024 * 1024) {
        notify("The image must be smaller than " + maxMb + " MB.", "error");
        return;
    }
    const load = ++latestLoad;
    const reader = new FileReader();
    reader.onload = (e) => loadLocalImage(e.target.result, file.name, file, load);
    reader.readAsDataURL(file);
}

// Store the file on the server once. Resolves to its stored name, or null if
// the upload fails -- requests then fall back to sending the image inline.
function uploadLocalImage(file) {
    const form = new FormData();
    form.append("image", file, file.name);
    return fetch("/upload-image/", {
        method: "POST",
        headers: { "X-CSRFToken": getCookie("csrftoken") },
        body: form,
    })
        .then((response) => response.json().then((body) => (response.ok ? body.imagePath : null)))
        .catch(() => null);
}

// Counts image loads as they are requested. Only the latest may start a session,
// so an earlier pick that decodes later (and its upload) is dropped.
let latestLoad = 0;

// Switch to a newly decoded image and start a clean session on it: identity,
// rotation, marks, results and undo history all belong to the image before.
// Called only once the image has decoded, so a failed load changes nothing.
function startImageSession(image, { name, label, isLocal, upload, bytes }) {
    const token = beginImage(name);
    state.image = image;
    state.isLocalImage = isLocal;
    state.localImageUpload = upload;
    state.rotation = 0;
    state.fineRotation = 0;
    syncRotationControls();
    state.lines = [];
    state.spots = [];
    state.plateRegion = null;
    state.highlightedSpot = null;
    // Another photo may be another plate: its description starts empty (a
    // draft or annotation file brings it back).
    state.mobilePhase = "";
    state.stationaryPhase = "";
    syncPlateInfo();
    document.querySelectorAll(".delete-icon").forEach((el) => el.remove());
    const tbody = document.querySelector("#values-table tbody");
    if (tbody) tbody.innerHTML = "";
    state.hasResults = false;
    state.csvUrl = null;
    clearResultsStale();
    fitAndDraw(image);
    resetHistory(); // fresh image = undo baseline
    updateAnnotatedHeading("editing");
    updateExportButtons();
    collapseImageSource(label);
    applyCrop(); // no region yet -> ensure crop view is reset
    refreshUI();
    identifyImage(bytes, token); // then offer a saved draft, if any
}

function loadLocalImage(dataURL, fileName, file, load) {
    // Start the upload while the image is decoded; it is kept only if it decodes.
    const upload = file ? uploadLocalImage(file) : Promise.resolve(null);
    const bytes = file ? file.arrayBuffer() : null;
    const image = new Image();
    image.onload = () => {
        if (load !== latestLoad) return; // superseded by a later pick
        startImageSession(image, { name: fileName, label: fileName, isLocal: true, upload, bytes });
        notify('Image "' + fileName + '" loaded.', "success");
    };
    image.onerror = () => notify("Failed to load image. Please try again.", "error");
    image.src = dataURL;
}

function loadELNImage() {
    if (!elnUrl) return;
    const load = ++latestLoad;
    fetch("/fetch-image/?url=" + encodeURIComponent(elnUrl))
        .then((response) => response.json())
        .then((data) => {
            if (!data.imagePath) {
                notify("ELN image not found.", "error");
                return;
            }
            const src = "/serve-image/?file_path=" + encodeURIComponent(data.imagePath);
            fetch(src).then((response) => {
                if (response.ok) {
                    const bytes = response.arrayBuffer();
                    const image = new Image();
                    image.onload = () => {
                        if (load !== latestLoad) return; // superseded by a later pick
                        startImageSession(image, {
                            name: "",
                            label: "ELN image",
                            isLocal: false,
                            upload: null,
                            bytes,
                        });
                    };
                    image.onerror = () => notify("Error loading ELN image.", "error");
                    image.src = src;
                } else {
                    notify("Error loading ELN image.", "error");
                }
            });
        })
        .catch((error) => {
            console.error("Error fetching ELN image:", error);
            notify("Error loading ELN image.", "error");
        });
}
