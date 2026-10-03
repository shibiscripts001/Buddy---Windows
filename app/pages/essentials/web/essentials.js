/* Essentials (page.py): a calculator with a timecode mode, a data rate
   calculator, an aspect ratio calculator, a stopwatch and countdown, and a
   scratchpad. The maths is all here; Python keeps the inputs, the history
   and the notes, and runs the countdown's deadline. */
"use strict";

const {el, send} = Buddy;
const $ = id => document.getElementById(id);
const PANELS = ["calc", "data", "aspect", "timer", "clock", "notes"];

let prefs = {};
let tab = "calc";

/* ---------------------------------------------------------------- tabs -- */

function showTab(name, save) {
    if (!PANELS.includes(name)) name = "calc";
    tab = name;
    for (const b of document.querySelectorAll("#ess-tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
    for (const p of document.querySelectorAll(".ess-panel")) p.hidden = p.dataset.panel !== name;
    if (save) send("tab", {tab: name});
    if (name === "calc") $("calc-expr").focus();
    if (name === "timer") tick();
}

$("ess-tabs").addEventListener("click", e => {
    const b = e.target.closest("button[data-tab]");
    if (b) showTab(b.dataset.tab, true);
});

/* --------------------------------------------------------------- prefs --
   A field marked data-pref="key" keeps its value in Python between runs. */

const prefTimers = {};

function setPref(key, value) {
    prefs[key] = value;
    clearTimeout(prefTimers[key]);
    prefTimers[key] = setTimeout(() => send("pref", {key, value}), 400);
}

for (const f of document.querySelectorAll("[data-pref]")) {
    f.addEventListener(f.type === "checkbox" || f.tagName === "SELECT" ? "change" : "input",
                       () => setPref(f.dataset.pref, f.type === "checkbox" ? f.checked : f.value));
}

function applyPrefs() {
    for (const f of document.querySelectorAll("[data-pref]")) {
        const v = prefs[f.dataset.pref];
        if (v === undefined || v === null) continue;
        if (f.type === "checkbox") f.checked = !!v;
        else if (f.tagName !== "SELECT" || [...f.options].some(o => o.value === String(v))) f.value = String(v);
    }
}

const num = (id, fallback = 0) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) ? v : fallback;
};

function fill(select, groups) {
    select.replaceChildren(...groups.map(g => g.name
        ? el("optgroup", {label: g.name}, g.items.map(i => el("option", {value: i.id, text: i.label})))
        : el("option", {value: g.id, text: g.label})));
}

function copy(text) {
    send("copy", {text});
    Buddy.toast("Copied", 1600);
}

/* ---------------------------------------------------------- frame rates -- */

const RATES = [
    {id: "23.976", label: "23.976", nom: 24, num: 24000, den: 1001},
    {id: "24", label: "24", nom: 24, num: 24, den: 1},
    {id: "25", label: "25", nom: 25, num: 25, den: 1},
    {id: "29.97df", label: "29.97 DF", nom: 30, num: 30000, den: 1001, df: true},
    {id: "29.97", label: "29.97 NDF", nom: 30, num: 30000, den: 1001},
    {id: "30", label: "30", nom: 30, num: 30, den: 1},
    {id: "48", label: "48", nom: 48, num: 48, den: 1},
    {id: "50", label: "50", nom: 50, num: 50, den: 1},
    {id: "59.94df", label: "59.94 DF", nom: 60, num: 60000, den: 1001, df: true},
    {id: "59.94", label: "59.94 NDF", nom: 60, num: 60000, den: 1001},
    {id: "60", label: "60", nom: 60, num: 60, den: 1},
];
const rateOf = id => RATES.find(r => r.id === id) || RATES[0];

/* Timecode <-> frame count. Drop frame skips frame numbers 0 and 1 (0-3 at
   59.94) at the start of every minute except each tenth. */
function tcToFrames(h, m, s, f, r) {
    let frames = ((h * 3600 + m * 60 + s) * r.nom) + f;
    if (r.df) {
        const drop = r.nom / 15;
        const minutes = h * 60 + m;
        frames -= drop * (minutes - Math.floor(minutes / 10));
    }
    return frames;
}

function framesToTc(total, r) {
    const neg = total < 0;
    let f = Math.abs(Math.round(total));
    if (r.df) {
        const drop = r.nom / 15;
        const perMin = r.nom * 60 - drop;
        const per10 = r.nom * 600 - drop * 9;
        const tens = Math.floor(f / per10);
        const rem = f % per10;
        f += drop * 9 * tens + (rem > drop ? drop * Math.floor((rem - drop) / perMin) : 0);
    }
    const ff = f % r.nom;
    const secs = Math.floor(f / r.nom);
    const pad = n => String(n).padStart(2, "0");
    const sep = r.df ? ";" : ":";
    return `${neg ? "-" : ""}${pad(Math.floor(secs / 3600))}:${pad(Math.floor(secs / 60) % 60)}:${pad(secs % 60)}${sep}${pad(ff)}`;
}

function clockText(seconds) {
    const neg = seconds < 0;
    seconds = Math.abs(seconds);
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds - Math.floor(seconds / 60) * 60;
    const ss = s.toFixed(3).padStart(6, "0");
    return (neg ? "-" : "") + (h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`);
}

/* ---------------------------------------------------------- calculator -- */

class CalcError extends Error {}

function tokenize(text, tc) {
    const src = text.replace(/[×xX]/g, "*").replace(/÷/g, "/").replace(/[−–]/g, "-").replace(/,/g, "");
    const out = [];
    const re = tc ? /\s*(?:([\d:;.]+[fFsS]?)|([-+*/()%]))/y : /\s*(?:(\d*\.?\d+(?:[eE][-+]?\d+)?|\d+\.)|([-+*/()%]))/y;
    let i = 0;
    while (i < src.length) {
        if (/^\s+$/.test(src.slice(i))) break;
        re.lastIndex = i;
        const m = re.exec(src);
        if (!m) throw new CalcError(`Can't read "${src.slice(i).trim().slice(0, 12)}"`);
        out.push(m[1] !== undefined ? {lit: m[1]} : {op: m[2]});
        i = re.lastIndex;
    }
    return out;
}

/* A value is {n} (a plain number) or {f} (a frame count, shown as timecode). */
function parse(tokens, tc, rate) {
    let i = 0;
    const peek = () => tokens[i];
    const isOp = (t, ops) => t && t.op && ops.includes(t.op);

    function literal(text, scalar) {
        if (!tc) return {n: parseFloat(text)};
        const unit = /[fFsS]$/.test(text) ? text.slice(-1).toLowerCase() : "";
        const body = unit ? text.slice(0, -1) : text;
        if (unit === "f") {
            const v = parseFloat(body);
            if (!Number.isFinite(v)) throw new CalcError("A frame count is a number, like 48f");
            return {f: Math.round(v)};
        }
        if (unit === "s") {
            const v = parseFloat(body);
            if (!Number.isFinite(v)) throw new CalcError("Seconds are a number, like 2.5s");
            const whole = Math.floor(Math.abs(v));
            const frames = tcToFrames(Math.floor(whole / 3600), Math.floor(whole / 60) % 60, whole % 60,
                                      Math.round((Math.abs(v) - whole) * rate.nom), rate);
            return {f: v < 0 ? -frames : frames};
        }
        if (/[:;]/.test(body) || (/\./.test(body) && !scalar)) {
            const parts = body.split(/[:;.]/).map(p => (p === "" ? 0 : parseInt(p, 10)));
            if (parts.length > 4 || parts.some(p => !Number.isFinite(p))) throw new CalcError(`"${text}" isn't a timecode`);
            while (parts.length < 4) parts.unshift(0);
            return {f: tcToFrames(...parts, rate)};
        }
        if (scalar) return {n: parseFloat(body)};
        if (!/^\d+$/.test(body)) throw new CalcError(`"${text}" isn't a timecode`);
        if (body.length > 8) throw new CalcError("A timecode has at most 8 digits");
        const d = body.padStart(8, "0");
        return {f: tcToFrames(+d.slice(0, 2), +d.slice(2, 4), +d.slice(4, 6), +d.slice(6, 8), rate)};
    }

    function primary(scalar) {
        const t = tokens[i++];
        if (!t) throw new CalcError("Something's missing at the end");
        if (t.lit !== undefined) {
            let v = literal(t.lit, scalar);
            if (isOp(peek(), "%")) {
                i++;
                v = "n" in v ? {n: v.n / 100} : {f: v.f / 100};
            }
            return v;
        }
        if (t.op === "(") {
            const v = expr(scalar);
            if (isOp(peek(), ")")) i++;
            return v;
        }
        if (t.op === "-" || t.op === "+") {
            const v = primary(scalar);
            return t.op === "+" ? v : "n" in v ? {n: -v.n} : {f: -v.f};
        }
        throw new CalcError(`Unexpected "${t.op}"`);
    }

    function term(scalar) {
        let v = primary(scalar);
        while (isOp(peek(), "*/")) {
            const op = tokens[i++].op;
            const w = primary(true);
            if (op === "*") {
                if ("f" in v && "f" in w) throw new CalcError("Two timecodes can't be multiplied");
                v = "f" in v ? {f: v.f * w.n} : "f" in w ? {f: v.n * w.f} : {n: v.n * w.n};
            } else {
                const by = "f" in w ? w.f : w.n;
                if (by === 0) throw new CalcError("Can't divide by zero");
                if ("n" in v && "f" in w) throw new CalcError("A number can't be divided by a timecode");
                v = "f" in v && "n" in w ? {f: v.f / by} : {n: ("f" in v ? v.f : v.n) / by};
            }
        }
        return v;
    }

    function expr(scalar) {
        let v = term(scalar);
        while (isOp(peek(), "+-")) {
            const op = tokens[i++].op;
            const w = term(scalar);
            const sign = op === "+" ? 1 : -1;
            if ("f" in v || "f" in w) {
                const a = "f" in v ? v.f : v.n, b = "f" in w ? w.f : w.n;
                v = {f: a + sign * b};
            } else {
                v = {n: v.n + sign * w.n};
            }
        }
        return v;
    }

    if (!tokens.length) return null;
    const v = expr(false);
    if (i < tokens.length) throw new CalcError(`Unexpected "${tokens[i].op || tokens[i].lit}"`);
    if ("f" in v) v.f = Math.round(v.f);
    if (("n" in v && !Number.isFinite(v.n)) || ("f" in v && !Number.isFinite(v.f))) throw new CalcError("That's too big a number");
    return v;
}

function numberText(n, group) {
    const clean = Number(n.toPrecision(12));
    if (Math.abs(clean) >= 1e15 || (clean !== 0 && Math.abs(clean) < 1e-9)) return String(clean);
    return group ? clean.toLocaleString("en-US", {maximumFractionDigits: 10}) : String(clean);
}

const calc = {mode: "std", history: []};

function calcMode(mode, save) {
    calc.mode = mode === "tc" ? "tc" : "std";
    for (const b of document.querySelectorAll("#calc-mode button")) b.setAttribute("aria-pressed", String(b.dataset.mode === calc.mode));
    $("calc-fps-wrap").hidden = calc.mode !== "tc";
    $("calc-hint-std").hidden = calc.mode === "tc";
    $("calc-hint-tc").hidden = calc.mode !== "tc";
    $("calc-expr").placeholder = calc.mode === "tc" ? "01:00:00:00 + 1000" : "0";
    buildKeys();
    if (save) {
        setPref("calc_mode", calc.mode);
        $("calc-expr").value = "";
    }
    preview();
}

const KEYS = {
    std: [["C", "clear"], ["(", "("], [")", ")"], ["⌫", "back"],
          ["7"], ["8"], ["9"], ["÷", "÷", "op"],
          ["4"], ["5"], ["6"], ["×", "×", "op"],
          ["1"], ["2"], ["3"], ["−", "−", "op"],
          ["0"], ["."], ["%", "%", "op"], ["+", "+", "op"],
          ["=", "equals", "eq"]],
    tc: [["C", "clear"], [":", ":"], ["f", "f"], ["⌫", "back"],
         ["7"], ["8"], ["9"], ["÷", "÷", "op"],
         ["4"], ["5"], ["6"], ["×", "×", "op"],
         ["1"], ["2"], ["3"], ["−", "−", "op"],
         ["0"], ["00"], ["s", "s"], ["+", "+", "op"],
         ["=", "equals", "eq"]],
};
const KEY_TITLES = {clear: "Clear", back: "Delete", f: "Frames", s: "Seconds"};

function buildKeys() {
    $("calc-keys").replaceChildren(...KEYS[calc.mode].map(([label, action = label, kind]) =>
        el(`button.btn${kind === "op" ? ".op" : ""}${kind === "eq" ? ".accent.wide" : ""}`, {
            type: "button", text: label, title: KEY_TITLES[action] || null, translate: "no",
            onmousedown: e => e.preventDefault(),   // the field keeps its caret
            onclick: () => key(action),
        })));
}

function insert(text) {
    const f = $("calc-expr");
    const start = f.selectionStart ?? f.value.length, end = f.selectionEnd ?? f.value.length;
    f.setRangeText(text, start, end, "end");
    f.focus();
    preview();
}

function key(action) {
    const f = $("calc-expr");
    if (action === "clear") {
        f.value = "";
        preview();
        f.focus();
    } else if (action === "back") {
        const start = f.selectionStart, end = f.selectionEnd;
        if (start !== end) f.setRangeText("", start, end, "end");
        else if (start > 0) f.setRangeText("", start - 1, start, "end");
        f.focus();
        preview();
    } else if (action === "equals") {
        equals();
        f.focus();
    } else {
        insert(action);
    }
}

function evaluate(text) {
    const r = rateOf($("calc-fps").value);
    return parse(tokenize(text, calc.mode === "tc"), calc.mode === "tc", r);
}

/* What a value shows as: the big line, the line under it, and the text it
   leaves in the field to carry on from. */
function shown(v) {
    const r = rateOf($("calc-fps").value);
    if (calc.mode === "tc" && "f" in v) {
        const tc = framesToTc(v.f, r);
        return {main: tc, sub: `${v.f.toLocaleString("en-US")} frames · ${clockText(v.f * r.den / r.num)} real time`, carry: tc};
    }
    const n = "n" in v ? v.n : v.f;
    return {main: numberText(n, true), sub: calc.mode === "tc" ? "times" : "", carry: numberText(n, false)};
}

function showResult(main, sub, kind) {
    const result = $("calc-result");
    result.textContent = main;
    result.className = `ess-result${kind ? ` ${kind}` : ""}`;
    result.title = kind === "error" ? "" : "Click to copy";
    $("calc-sub").textContent = sub || "";
}

function preview() {
    const text = $("calc-expr").value;
    if (!text.trim()) return showResult(calc.mode === "tc" ? framesToTc(0, rateOf($("calc-fps").value)) : "0", "", "preview");
    try {
        const v = evaluate(text);
        if (!v) return showResult("0", "", "preview");
        const s = shown(v);
        showResult(s.main, s.sub, "preview");
    } catch (err) {
        showResult(text.trim(), "", "preview");   // half-typed: no error until Enter
    }
}

function equals() {
    const f = $("calc-expr");
    const text = f.value.trim();
    if (!text) return;
    try {
        const v = evaluate(text);
        if (!v) return;
        const s = shown(v);
        showResult(s.main, s.sub, "");
        if (text !== s.carry) {
            calc.history.unshift({expr: text, result: s.main, mode: calc.mode});
            calc.history = calc.history.slice(0, 20);
            send("history", {items: calc.history});
            drawHistory();
        }
        f.value = s.carry;
    } catch (err) {
        showResult(err instanceof CalcError ? err.message : "That can't be worked out", "", "error");
    }
}

function drawHistory() {
    $("calc-history").replaceChildren(...(calc.history.length ? calc.history.map(h => el("button.ess-history-row", {
        type: "button", title: "Use this answer", translate: "no",
        onclick: () => {
            if (h.mode !== calc.mode) calcMode(h.mode, true);
            const f = $("calc-expr");
            f.value = h.result.replace(/,/g, "");
            f.focus();
            preview();
        },
    }, [el("span.muted", {text: h.expr}), el("b", {text: `= ${h.result}`})])) : [el("div.muted.small.ess-history-empty", {text: "Your answers show here."})]));
}

$("calc-expr").addEventListener("input", preview);
$("calc-expr").addEventListener("keydown", e => {
    if (e.key === "Enter" || (e.key === "=" && !e.ctrlKey && !e.metaKey)) {
        e.preventDefault();
        equals();
    } else if (e.key === "Escape") {
        e.preventDefault();
        key("clear");
    }
});
$("calc-result").addEventListener("click", () => {
    if (!$("calc-result").classList.contains("error")) copy($("calc-result").textContent.replace(/,/g, ""));
});
$("calc-mode").addEventListener("click", e => {
    const b = e.target.closest("button[data-mode]");
    if (b && b.dataset.mode !== calc.mode) calcMode(b.dataset.mode, true);
    $("calc-expr").focus();
});
$("calc-fps").addEventListener("change", preview);
$("calc-history-clear").addEventListener("click", () => {
    calc.history = [];
    send("history", {items: []});
    drawHistory();
});

/* ----------------------------------------------------------- data rate -- */

// Mb/s at 1920x1080, 29.97 fps - Apple's and Avid's published figures,
// scaled by frame size and rate. RAW: 12-bit sensor data / the ratio.
// Uncompressed: bits per pixel.
const CODECS = [
    {name: "Apple ProRes", items: [
        {id: "prores_proxy", label: "ProRes 422 Proxy", base: 45},
        {id: "prores_lt", label: "ProRes 422 LT", base: 102},
        {id: "prores_422", label: "ProRes 422", base: 147},
        {id: "prores_hq", label: "ProRes 422 HQ", base: 220},
        {id: "prores_4444", label: "ProRes 4444", base: 330},
        {id: "prores_xq", label: "ProRes 4444 XQ", base: 500},
    ]},
    {name: "Avid DNxHR", items: [
        {id: "dnxhr_lb", label: "DNxHR LB", base: 45},
        {id: "dnxhr_sq", label: "DNxHR SQ", base: 145},
        {id: "dnxhr_hq", label: "DNxHR HQ", base: 220},
        {id: "dnxhr_hqx", label: "DNxHR HQX", base: 220},
        {id: "dnxhr_444", label: "DNxHR 444", base: 440},
    ]},
    {name: "Blackmagic RAW", items: [
        {id: "braw_3", label: "Blackmagic RAW 3:1", ratio: 3},
        {id: "braw_5", label: "Blackmagic RAW 5:1", ratio: 5},
        {id: "braw_8", label: "Blackmagic RAW 8:1", ratio: 8},
        {id: "braw_12", label: "Blackmagic RAW 12:1", ratio: 12},
    ]},
    {name: "Uncompressed", items: [
        {id: "unc_8_422", label: "8-bit 4:2:2", bpp: 16},
        {id: "unc_10_422", label: "10-bit 4:2:2 (v210)", bpp: 128 / 6},
        {id: "unc_10_rgb", label: "10-bit RGB (DPX)", bpp: 32},
        {id: "unc_16_rgb", label: "16-bit RGB", bpp: 48},
    ]},
    {name: "Other", items: [
        {id: "custom", label: "H.264, H.265 or other - enter the bitrate", custom: true},
    ]},
];
const CODEC_BY_ID = Object.fromEntries(CODECS.flatMap(g => g.items).map(c => [c.id, c]));

const FRAMES = [
    {id: "1280x720", label: "HD 720 – 1280 × 720", w: 1280, h: 720},
    {id: "1920x1080", label: "HD 1080 – 1920 × 1080", w: 1920, h: 1080},
    {id: "2048x1080", label: "2K DCI – 2048 × 1080", w: 2048, h: 1080},
    {id: "2560x1440", label: "QHD – 2560 × 1440", w: 2560, h: 1440},
    {id: "3840x2160", label: "UHD – 3840 × 2160", w: 3840, h: 2160},
    {id: "4096x2160", label: "4K DCI – 4096 × 2160", w: 4096, h: 2160},
    {id: "6144x3456", label: "6K – 6144 × 3456", w: 6144, h: 3456},
    {id: "7680x4320", label: "8K UHD – 7680 × 4320", w: 7680, h: 4320},
    {id: "1080x1920", label: "Vertical HD – 1080 × 1920", w: 1080, h: 1920},
    {id: "2160x3840", label: "Vertical UHD – 2160 × 3840", w: 2160, h: 3840},
    {id: "custom", label: "Custom size", w: 0, h: 0},
];
const FRAME_BY_ID = Object.fromEntries(FRAMES.map(f => [f.id, f]));
const DR_FPS = ["23.976", "24", "25", "29.97", "30", "48", "50", "59.94", "60", "120"];

function sizeText(gb) {
    if (!Number.isFinite(gb)) return "–";
    if (gb >= 1000) return `${(gb / 1000).toFixed(gb >= 10000 ? 1 : 2)} TB`;
    if (gb >= 1) return `${gb.toFixed(gb >= 100 ? 0 : 1)} GB`;
    return `${(gb * 1000).toFixed(0)} MB`;
}

function rateText(mbps) {
    return mbps >= 1000 ? `${(mbps / 1000).toFixed(2)} Gb/s` : `${mbps.toFixed(mbps >= 100 ? 0 : 1)} Mb/s`;
}

function spanText(secs) {
    if (!Number.isFinite(secs) || secs <= 0) return "–";
    if (secs >= 360000) return `${Math.round(secs / 3600).toLocaleString("en-US")} h`;
    const h = Math.floor(secs / 3600), m = Math.floor(secs / 60) % 60;
    if (h) return `${h} h ${m} min`;
    return m ? `${m} min ${Math.floor(secs % 60)} s` : `${Math.floor(secs)} s`;
}

function frameSize(selectId, wId, hId, wrapId) {
    const f = FRAME_BY_ID[$(selectId).value] || FRAMES[1];
    $(wrapId).hidden = f.id !== "custom";
    return f.id === "custom" ? {w: Math.max(0, num(wId)), h: Math.max(0, num(hId))} : {w: f.w, h: f.h};
}

let drSummary = "";

function dataRate() {
    const codec = CODEC_BY_ID[$("dr-codec").value] || CODEC_BY_ID.prores_422;
    $("dr-custom-wrap").hidden = !codec.custom;
    $("dr-res-wrap").hidden = $("dr-fps-wrap").hidden = !!codec.custom;
    const {w, h} = codec.custom ? {w: 0, h: 0} : frameSize("dr-res", "dr-w", "dr-h", "dr-wh");
    if (codec.custom) $("dr-wh").hidden = true;
    const fpsId = $("dr-fps").value;
    const fps = fpsId.includes(".") ? Math.round(parseFloat(fpsId)) * 1000 / 1001 : parseFloat(fpsId);
    let video;
    if (codec.custom) video = Math.max(0, num("dr-custom"));
    else if (codec.base) video = codec.base * (w * h) / (1920 * 1080) * fps / (30000 / 1001);
    else if (codec.ratio) video = w * h * 12 * fps / codec.ratio / 1e6;
    else video = w * h * codec.bpp * fps / 1e6;
    const channels = Math.max(0, Math.min(64, Math.round(num("dr-audio"))));
    const total = video + channels * 48000 * 24 / 1e6;
    const secs = Math.max(0, num("dr-dh")) * 3600 + Math.max(0, num("dr-dm")) * 60 + Math.max(0, num("dr-ds"));
    const gbPerSec = total / 8 / 1000;
    const driveGb = Math.max(0, num("dr-drive")) * parseFloat($("dr-unit").value);

    if (!(total > 0)) {
        for (const id of ["dr-rate", "dr-size", "dr-fits"]) $(id).textContent = "–";
        $("dr-rate-sub").textContent = $("dr-size-sub").textContent = "";
        drSummary = "";
        return;
    }
    $("dr-rate").textContent = rateText(total);
    $("dr-rate-sub").textContent = `${(total / 8).toFixed(1)} MB/s · ${sizeText(gbPerSec * 3600)} per hour`;
    $("dr-size").textContent = secs ? sizeText(gbPerSec * secs) : "–";
    $("dr-size-sub").textContent = secs ? `for ${clockText(secs).replace(/\.000$/, "")}` : "";
    $("dr-fits").textContent = driveGb ? spanText(driveGb / gbPerSec) : "–";
    const what = codec.custom ? codec.label.split(" - ")[0] : `${codec.label}, ${w} × ${h}, ${fpsId} fps`;
    drSummary = `${what}${channels ? `, ${channels} audio ch` : ""}: ${rateText(total)} (${(total / 8).toFixed(1)} MB/s)`
        + (secs ? `. ${clockText(secs).replace(/\.000$/, "")} = ${sizeText(gbPerSec * secs)}` : "")
        + (driveGb ? `. ${sizeText(driveGb)} holds ${spanText(driveGb / gbPerSec)}` : "") + ".";
}

$("dr-copy").addEventListener("click", () => { if (drSummary) copy(drSummary); });
for (const id of ["dr-codec", "dr-custom", "dr-res", "dr-w", "dr-h", "dr-fps", "dr-audio", "dr-dh", "dr-dm", "dr-ds", "dr-drive", "dr-unit"]) {
    $(id).addEventListener($(id).tagName === "SELECT" ? "change" : "input", dataRate);
}

/* -------------------------------------------------------- aspect ratio -- */

const RATIOS = [
    {id: "2.39", label: "2.39:1 – Scope", r: 2.39},
    {id: "2.35", label: "2.35:1", r: 2.35},
    {id: "2", label: "2:1", r: 2},
    {id: "1.85", label: "1.85:1 – Flat", r: 1.85},
    {id: "1.78", label: "16:9", r: 16 / 9},
    {id: "1.33", label: "4:3", r: 4 / 3},
    {id: "1", label: "1:1 – Square", r: 1},
    {id: "0.8", label: "4:5 – Portrait", r: 4 / 5},
    {id: "0.5625", label: "9:16 – Vertical", r: 9 / 16},
    {id: "custom", label: "Custom ratio", r: 0},
];

const gcd = (a, b) => (b ? gcd(b, a % b) : a);

function ratioText(w, h) {
    if (!(w > 0 && h > 0)) return "";
    const dec = `${(w / h).toFixed(2)}:1`;
    if (Number.isInteger(w) && Number.isInteger(h)) {
        const g = gcd(w, h), a = w / g, b = h / g;
        if (a <= 64 && b <= 64) return `${a}:${b} · ${dec}`;
    }
    return dec;
}

const roundTo = (x, even) => (even ? Math.max(2, Math.round(x / 2) * 2) : Math.max(1, Math.round(x)));
let resizeFrom = "w";

function resize() {
    const w = num("ar-w"), h = num("ar-h");
    $("ar-ratio").textContent = ratioText(w, h);
    if (!(w > 0 && h > 0)) return;
    const even = $("ar-even").checked;
    if (resizeFrom === "w" && $("ar-nw").value !== "") $("ar-nh").value = roundTo(num("ar-nw") * h / w, even);
    else if (resizeFrom === "h" && $("ar-nh").value !== "") $("ar-nw").value = roundTo(num("ar-nh") * w / h, even);
}

$("ar-nw").addEventListener("input", () => { resizeFrom = "w"; resize(); });
$("ar-nh").addEventListener("input", () => { resizeFrom = "h"; resize(); });
for (const id of ["ar-w", "ar-h"]) $(id).addEventListener("input", resize);
$("ar-even").addEventListener("change", resize);

function fit() {
    const {w, h} = frameSize("fit-frame", "fit-w", "fit-h", "fit-wh");
    const choice = RATIOS.find(r => r.id === $("fit-ratio").value) || RATIOS[0];
    $("fit-custom-wrap").hidden = choice.id !== "custom";
    const target = choice.id === "custom" ? num("fit-custom") : choice.r;
    if (!(w > 0 && h > 0 && target > 0)) {
        $("fit-size").textContent = $("fit-bars").textContent = "–";
        return;
    }
    const frame = w / h;
    let pw = w, ph = h, bars = "No bars - it's the frame's own shape";
    if (Math.abs(target - frame) > 0.005) {
        if (target > frame) {
            ph = Math.min(h, roundTo(w / target, true));
            const each = (h - ph) / 2;
            bars = `${Number.isInteger(each) ? each : `${Math.floor(each)} / ${Math.ceil(each)}`} px top and bottom`;
        } else {
            pw = Math.min(w, roundTo(h * target, true));
            const each = (w - pw) / 2;
            bars = `${Number.isInteger(each) ? each : `${Math.floor(each)} / ${Math.ceil(each)}`} px left and right`;
        }
    }
    $("fit-size").textContent = `${pw} × ${ph}`;
    $("fit-bars").textContent = bars;
    // The little picture: the frame at most 160 x 110, the shape inside it.
    const scale = Math.min(160 / w, 110 / h);
    Object.assign($("fit-preview").style, {width: `${Math.round(w * scale)}px`, height: `${Math.round(h * scale)}px`});
    Object.assign($("fit-pic").style, {width: `${pw * scale}px`, height: `${ph * scale}px`});
}

for (const id of ["fit-frame", "fit-ratio", "fit-w", "fit-h", "fit-custom"]) {
    $(id).addEventListener($(id).tagName === "SELECT" ? "change" : "input", fit);
}

/* --------------------------------------------------------------- timer -- */

function watchText(ms, cs) {
    ms = Math.max(0, ms);
    const total = Math.floor(ms / 1000);
    const h = Math.floor(total / 3600), m = Math.floor(total / 60) % 60, s = total % 60;
    const pad = n => String(n).padStart(2, "0");
    const head = h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
    return cs ? `${head}.${pad(Math.floor(ms / 10) % 100)}` : head;
}

// Stopwatch: kept in prefs, so it carries on across a restart.
const sw = {running: false, start: 0, acc: 0, laps: []};
const swElapsed = () => sw.acc + (sw.running ? Date.now() - sw.start : 0);

function swSave() {
    setPref("sw_run", sw.running);
    setPref("sw_start", sw.start);
    setPref("sw_acc", sw.acc);
}

function swDraw() {
    $("sw-time").textContent = watchText(swElapsed(), true);
    $("sw-start").textContent = sw.running ? "Pause" : sw.acc ? "Resume" : "Start";
    $("sw-lap").disabled = !sw.running;
    $("sw-reset").disabled = sw.running || !sw.acc;
}

function swDrawLaps() {
    $("sw-laps").replaceChildren(...sw.laps.map((lap, i) => el("div.ess-lap", {translate: "no"}, [
        el("span", {text: `#${sw.laps.length - i}`}),
        el("span", {text: watchText(lap.split, true)}),
        el("span", {text: watchText(lap.total, true)}),
    ])));
}

$("sw-start").addEventListener("click", () => {
    if (sw.running) {
        sw.acc += Date.now() - sw.start;
        sw.running = false;
    } else {
        sw.start = Date.now();
        sw.running = true;
    }
    swSave();
    swDraw();
    tick();
});
$("sw-lap").addEventListener("click", () => {
    const total = swElapsed();
    sw.laps.unshift({total, split: total - (sw.laps[0] ? sw.laps[0].total : 0)});
    swDrawLaps();
});
$("sw-reset").addEventListener("click", () => {
    sw.running = false;
    sw.acc = sw.start = 0;
    sw.laps = [];
    swSave();
    swDraw();
    swDrawLaps();
});

// Countdown: Python holds the deadline (page.py) and says when it's done.
const cd = {ends_at: null, left_ms: 0, total_ms: 0};
const cdRemaining = () => (cd.ends_at !== null ? Math.max(0, cd.ends_at - Date.now()) : cd.left_ms);
const cdIdle = () => cd.ends_at === null && !cd.left_ms;
const cdSetMs = () => (Math.max(0, num("cd-h")) * 3600 + Math.max(0, num("cd-m")) * 60 + Math.max(0, num("cd-s"))) * 1000;

function cdDraw() {
    const idle = cdIdle();
    const left = idle ? cdSetMs() : cdRemaining();
    if (!$("cd-time").classList.contains("done") || !idle) {
        $("cd-time").classList.remove("done");
        $("cd-time").textContent = watchText(Math.ceil(left / 1000) * 1000, false);
    }
    $("cd-bar").style.width = idle || !cd.total_ms ? "0" : `${Math.min(100, (1 - left / cd.total_ms) * 100)}%`;
    $("cd-start").textContent = cd.ends_at !== null ? "Pause" : idle ? "Start" : "Resume";
    $("cd-start").disabled = idle && !cdSetMs();
    $("cd-reset").disabled = idle;
    for (const f of document.querySelectorAll("#cd-set input, #cd-presets button")) f.disabled = !idle;
}

let audio = null;

function primeAudio() {
    try {
        audio = audio || new AudioContext();
        if (audio.state === "suspended") audio.resume();
    } catch (err) {
        audio = null;
    }
}

function chime() {
    if (!audio) return;
    const now = audio.currentTime;
    [0, 0.45, 0.9, 1.6, 2.05, 2.5].forEach((at, i) => {
        const osc = audio.createOscillator(), gain = audio.createGain();
        osc.type = "sine";
        osc.frequency.value = [880, 1175, 1568][i % 3];
        gain.gain.setValueAtTime(0.0001, now + at);
        gain.gain.exponentialRampToValueAtTime(0.25, now + at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + at + 0.4);
        osc.connect(gain).connect(audio.destination);
        osc.start(now + at);
        osc.stop(now + at + 0.42);
    });
}

function cdStart(ms) {
    primeAudio();
    $("cd-time").classList.remove("done");
    send("countdown", {action: "start", ms});
}

$("cd-start").addEventListener("click", () => {
    if (cd.ends_at !== null) send("countdown", {action: "pause"});
    else cdStart(cdSetMs());
});
$("cd-reset").addEventListener("click", () => {
    $("cd-time").classList.remove("done");
    send("countdown", {action: "reset"});
});
for (const id of ["cd-h", "cd-m", "cd-s"]) $(id).addEventListener("input", () => { $("cd-time").classList.remove("done"); cdDraw(); });
$("cd-presets").replaceChildren(...[1, 5, 10, 15, 30, 60].map(min => el("button.btn", {
    type: "button", text: min === 60 ? "1 h" : `${min} min`, translate: "no",
    onclick: () => {
        const h = Math.floor(min / 60), m = min % 60;
        $("cd-h").value = h;
        $("cd-m").value = m;
        $("cd-s").value = 0;
        setPref("cd_h", String(h));
        setPref("cd_m", String(m));
        setPref("cd_s", "0");
        cdStart(min * 60000);
    },
})));

Buddy.on("countdown", c => {
    Object.assign(cd, c);
    cdDraw();
    tick();
});

Buddy.on("countdown_done", () => {
    $("cd-time").textContent = "00:00";
    $("cd-time").classList.add("done");
    $("cd-bar").style.width = "100%";
    chime();
    Buddy.toast("Time's up", 4000);
});

// One redraw loop while something is running and the Timer tab is open.
let ticking = false;

function tick() {
    swDraw();
    cdDraw();
    if (ticking || tab !== "timer" || !(sw.running || cd.ends_at !== null)) return;
    ticking = true;
    requestAnimationFrame(() => {
        ticking = false;
        tick();
    });
}

/* --------------------------------------------------------------- notes --
   "General" notes (scope "") and a set per project. Each save says which
   set it was typed in, so a switch while a save is pending can't put the
   text in the wrong project's notes. */

const NEW_PROJECT = "\u0000new";
let notesTimer = null;
let notesScope = "";
let notesState = {scope: "", projects: [], open: "", follow: true};

function notesCount() {
    const text = $("notes").value;
    const words = (text.match(/\S+/g) || []).length;
    $("notes-count").textContent = text ? `${words.toLocaleString("en-US")} words · ${text.length.toLocaleString("en-US")} characters` : "";
}

function notesSave() {
    clearTimeout(notesTimer);
    notesTimer = null;
    send("notes", {scope: notesScope, text: $("notes").value});
    $("notes-saved").textContent = "Saved";
}

$("notes").addEventListener("input", () => {
    notesCount();
    $("notes-saved").textContent = "Saving…";
    clearTimeout(notesTimer);
    notesTimer = setTimeout(notesSave, 600);
});
$("notes").addEventListener("blur", () => { if (notesTimer) notesSave(); });
$("notes").addEventListener("keydown", e => {
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey) {   // a tab in the text, not a jump away
        e.preventDefault();
        $("notes").setRangeText("\t", $("notes").selectionStart, $("notes").selectionEnd, "end");
        $("notes").dispatchEvent(new Event("input"));
    }
});
$("notes-copy").addEventListener("click", () => { if ($("notes").value) copy($("notes").value); });
$("notes-clear").addEventListener("click", async () => {
    if (!$("notes").value) return;
    const ok = await Buddy.confirm({title: "Clear the notes?",
                                    text: notesScope ? "These project notes go, and the project leaves the list unless it's open in Resolve. This can't be undone."
                                                     : "Everything in the general notes goes. This can't be undone.",
                                    ok: "Clear", danger: true});
    if (!ok) return;
    $("notes").value = "";
    notesCount();
    notesSave();
});
addEventListener("beforeunload", () => { if (notesTimer) notesSave(); });

function drawScopes() {
    const {projects, open} = notesState;
    const select = $("notes-scope");
    select.replaceChildren(
        el("option", {value: "", text: "General – all projects"}),
        projects.length ? el("optgroup", {label: "Projects"}, projects.map(name => name === open
            ? el("option", {value: name, text: `${name} (open in Resolve)`})
            : el("option", {value: name, text: name, translate: "no"}))) : null,
        el("option", {value: NEW_PROJECT, text: "New project notes…"}),
    );
    select.value = notesScope;
    $("notes-follow").checked = notesState.follow;
    $("notes-no-resolve").hidden = !!open || projects.length > 0;
    $("notes").placeholder = notesScope ? "Notes for this project - saved as you type."
                                        : "Jot anything down - it's saved as you type.";
}

function askProjectName() {
    const field = el("input.field", {type: "text", maxlength: 200, value: notesState.open && !notesState.projects.includes(notesState.open) ? notesState.open : "",
                                     placeholder: "Project name", "aria-label": "Project name"});
    const done = close => {
        const name = field.value.trim().replace(/\s+/g, " ");
        if (!name) return field.focus();
        close();
        send("notes_scope", {scope: name});
    };
    field.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); done(dialog.close); } });
    const dialog = Buddy.modal({
        title: "New project notes",
        body: [el("p.modal-text", {text: "Which project are these notes for? Use the name it has in Resolve and they'll come up when you open it."}), field],
        buttons: [{label: "Cancel"}, {label: "Start notes", kind: "accent", onClick: done}],
        onClose: () => { $("notes-scope").value = notesScope; },
    });
}

$("notes-scope").addEventListener("change", e => {
    const scope = e.target.value;
    if (scope === NEW_PROJECT) return askProjectName();
    if (notesTimer) notesSave();
    send("notes_scope", {scope});
});
$("notes-follow").addEventListener("change", e => send("notes_follow", {on: e.target.checked}));

Buddy.on("notes_state", n => {
    if (notesTimer) notesSave();   // what was typed goes to the notes it was typed in
    const switched = n.scope !== notesScope;
    notesState = n;
    notesScope = n.scope;
    if (switched || document.activeElement !== $("notes")) {
        $("notes").value = n.text;
        $("notes-saved").textContent = "";
    }
    drawScopes();
    notesCount();
    if (switched && tab === "notes" && n.scope && n.scope === n.open && n.follow) Buddy.toast(`Showing the notes for ${n.scope}`, 2200);
});

/* --------------------------------------------------------------- setup -- */

fill($("calc-fps"), RATES);
fill($("dr-codec"), CODECS);
fill($("dr-res"), FRAMES);
fill($("dr-fps"), DR_FPS.map(id => ({id, label: id})));
fill($("fit-frame"), FRAMES);
fill($("fit-ratio"), RATIOS);
$("dr-codec").value = "prores_422";
$("dr-res").value = "1920x1080";
$("dr-fps").value = "23.976";
$("fit-frame").value = "1920x1080";
$("fit-ratio").value = "2.39";

function drawAll() {
    calcMode(prefs.calc_mode, false);
    drawHistory();
    dataRate();
    resize();
    fit();
    sw.running = !!prefs.sw_run;
    sw.start = Number(prefs.sw_start) || 0;
    sw.acc = Number(prefs.sw_acc) || 0;
    swDraw();
    cdDraw();
    notesCount();
}

Buddy.on("state", s => {
    prefs = s.prefs || {};
    applyPrefs();
    calc.history = s.history || [];
    drawAll();
    showTab(s.tab, false);
});

drawAll();
showTab("calc", false);
