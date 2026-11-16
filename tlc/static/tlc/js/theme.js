/*
 * Light/dark theme toggle. Overrides the OS preference and persists the choice
 * per browser via localStorage.
 */

"use strict";

const THEME_KEY = "tlc-theme";

export function currentTheme() {
    const explicit = document.documentElement.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
}

export function refreshThemeToggle() {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const isDark = currentTheme() === "dark";
    // Show a sun in dark mode (click -> light) and a moon in light mode.
    button.querySelector("use").setAttribute("href", isDark ? "#i-sun" : "#i-moon");
    const label = isDark ? "Switch to light theme" : "Switch to dark theme";
    button.setAttribute("aria-label", label);
    button.title = label;
}

export function toggleTheme() {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
        localStorage.setItem(THEME_KEY, next);
    } catch (err) {
        /* localStorage unavailable (private mode) — session-only theme. */
    }
    refreshThemeToggle();
}

export function initTheme() {
    try {
        const saved = localStorage.getItem(THEME_KEY);
        if (saved === "light" || saved === "dark") {
            document.documentElement.setAttribute("data-theme", saved);
        }
    } catch (err) {
        /* ignore */
    }
    refreshThemeToggle();
}
