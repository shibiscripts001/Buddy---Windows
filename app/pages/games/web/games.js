/* Games (page.py): the tab row, and the settings each game keeps between
   runs. A field marked data-pref="key" keeps its value in Python; a game
   (pong.js) reads them from `prefs` and hears about a change through
   onPrefs. */
"use strict";

const {el, send} = Buddy;
const $ = id => document.getElementById(id);

let prefs = {};
let gameTab = "pong";
const prefListeners = [];
const onPrefs = fn => prefListeners.push(fn);
const tabListeners = [];
const onTab = fn => tabListeners.push(fn);

function showGame(name, save) {
    const tabs = [...document.querySelectorAll("#game-tabs button[data-tab]")].map(b => b.dataset.tab);
    if (!tabs.includes(name)) name = tabs[0];
    gameTab = name;
    for (const b of document.querySelectorAll("#game-tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
    for (const p of document.querySelectorAll(".game-panel")) p.hidden = p.dataset.panel !== name;
    if (save) send("tab", {tab: name});
    for (const fn of tabListeners) fn(name);
}

$("game-tabs").addEventListener("click", e => {
    const b = e.target.closest("button[data-tab]");
    if (b) showGame(b.dataset.tab, true);
});

function setPref(key, value) {
    prefs[key] = value;
    send("pref", {key, value});
    for (const fn of prefListeners) fn(key);
}

for (const f of document.querySelectorAll("[data-pref]")) {
    f.addEventListener("change", () => setPref(f.dataset.pref, f.type === "checkbox" ? f.checked : f.value));
}

function applyPrefs() {
    for (const f of document.querySelectorAll("[data-pref]")) {
        const v = prefs[f.dataset.pref];
        if (v === undefined || v === null) continue;
        if (f.type === "checkbox") f.checked = !!v;
        else if (f.tagName !== "SELECT" || [...f.options].some(o => o.value === String(v))) f.value = String(v);
    }
}

Buddy.on("state", s => {
    prefs = s.prefs || {};
    applyPrefs();
    for (const fn of prefListeners) fn(null);
    showGame(s.tab, false);
});

/* A game's court: the largest w:h box that fits its card, its canvas at
   the screen's own pixel density. Returns canvas pixels per court unit. */
function fitCourt(court, canvas, w, h) {
    const card = court.parentElement;
    const style = getComputedStyle(card);
    const roomW = card.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const roomH = card.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const width = Math.max(160, Math.floor(Math.min(roomW, roomH * w / h)));
    const height = Math.floor(width * h / w);
    court.style.width = `${width}px`;
    court.style.height = `${height}px`;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    return canvas.width / w;
}

/* The theme's colours, for drawing on a canvas. */
function gameColors() {
    const css = getComputedStyle(document.documentElement);
    const v = name => css.getPropertyValue(name).trim();
    return {fg: v("--text-strong") || "#fff", text: v("--text") || "#ddd", dim: v("--text-dim") || "#888",
            accent: v("--primary") || "#e64b3d", font: v("--font-mono") || "monospace"};
}

/* Short beeps made on the spot - no sound files. prime() from a click or
   key press: Chromium only lets a page make sound once it's been used. */
const gameSound = (() => {
    let audio = null;
    function prime() {
        try {
            audio = audio || new AudioContext();
            if (audio.state === "suspended") audio.resume();
        } catch (err) {
            audio = null;
        }
    }
    function blip(on, freq, length = 0.07, type = "square", at = 0) {
        if (!audio || !on) return;
        const t = audio.currentTime + at, osc = audio.createOscillator(), gain = audio.createGain();
        osc.type = type;
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.08, t);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + length);
        osc.connect(gain).connect(audio.destination);
        osc.start(t);
        osc.stop(t + length + 0.02);
    }
    return {prime, blip};
})();
