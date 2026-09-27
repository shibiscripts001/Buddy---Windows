/*
 * Text Animator's view. page.py owns every setting and makes every Resolve call; this
 * draws what it sends and reports what the user did. The placement canvases are
 * canvas.js.
 */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
const $$ = sel => [...document.querySelectorAll(sel)];

$("refresh").append(icon("refresh"));
$("conv-arrow").append(icon("arrow"));
for (const node of $$("[data-action]")) node.addEventListener("click", () => send(node.dataset.action));
for (const b of $$("[data-undo]")) { if (!b.textContent) b.append(icon("undo")); b.onclick = () => send("undo"); }
for (const b of $$("[data-redo]")) {
    if (!b.textContent) { const i = icon("undo"); i.style.transform = "scaleX(-1)"; b.append(i); }
    b.onclick = () => send("redo");
}

// --------------------------------------------------------------- tabs --

let TAB = "subtitles";
function showTab(tab) {
    TAB = tab;
    for (const b of $("tabs").querySelectorAll("button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    for (const p of $$(".panel")) p.hidden = p.id !== `panel-${tab}`;
    closeGuides();
}
$("tabs").onclick = e => {
    const b = e.target.closest("button[data-tab]");
    if (b && b.dataset.tab !== TAB) { showTab(b.dataset.tab); send("tab", {tab: b.dataset.tab}); }
};
showTab("subtitles");

// Undo / Redo anywhere on the page, outside a text box.
document.addEventListener("keydown", e => {
    if (e.target.closest("input, textarea, select") || !(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "z") return;
    e.preventDefault();
    send(e.shiftKey ? "redo" : "undo");
});

// -------------------------------------------------------------- state --

let STATE = null;

function trackOptions(select, count, value, extra = 0) {
    const n = Math.max(count + extra, value, 4);
    if (select.options.length !== n) {
        select.replaceChildren(...Array.from({length: n}, (_, i) => el("option", {
            value: i + 1, text: i + 1 > count ? `${i + 1} (new)` : String(i + 1),
        })));
    }
    select.value = String(value);
}

Buddy.on("state", s => {
    STATE = s;
    if (s.tab !== TAB) showTab(s.tab);
    // The timeline's name is the user's; the rest is translated piece by piece.
    const n = s.tracks.video;
    $("timeline").replaceChildren(...(s.timeline ? [
        el("span", {text: s.timeline, translate: "no"}), ` · ${s.resolution[0]}×${s.resolution[1]} · `,
        el("span", {text: n === 1 ? "1 video track" : `${n} video tracks`}),
    ] : []));
    trackOptions($("sub-track"), s.tracks.subtitle, s.sub_track);
    trackOptions($("target-track"), s.tracks.video, s.target_track, 1);
    if (OPT) {
        trackOptions($("style-track"), s.tracks.video, OPT.tracks.style_track);
        trackOptions($("anim-track"), s.tracks.video, OPT.tracks.anim_track);
    }
    $("bounding-on").checked = s.bounding.on;
    $("apply-bounding").disabled = !s.bounding.on;
    canvases.layout.setBounding(s.bounding);
    drawPreview();
});

$("sub-track").onchange = e => send("set", {name: "sub_track", value: Number(e.target.value)});
$("target-track").onchange = e => send("set", {name: "target_track", value: Number(e.target.value)});
$("bounding-on").onchange = e => send("bounding", {on: e.target.checked});

// ------------------------------------------------------------ options --

let OPT = null;
let FONTS = [];
const set = (name, value) => send("set", {name, value});

Buddy.on("fonts", list => {
    FONTS = list;
    $("font").replaceChildren(...list.map(f => el("option", {value: f, text: f})));
    if (OPT) $("font").value = OPT.font_display;
});
$("font").onchange = e => set("font_name", e.target.value);

function buildSlider(host, name, spec) {
    const input = el("input", {type: "range", min: spec.min, max: spec.max, step: spec.step});
    const value = el("b.val");
    const show = () => { value.textContent = Number(input.value).toFixed(spec.decimals); };
    input.addEventListener("input", () => { show(); drawPreview(); });
    input.addEventListener("change", () => set(name, Number(input.value)));
    // Double-click puts it back to its default.
    input.addEventListener("dblclick", () => { input.value = spec.default; show(); set(name, spec.default); });
    host.replaceChildren(el("label.lbl.slider", {title: `Double-click to reset to ${spec.default}`}, [
        el("span.row", {}, [el("span", {text: spec.label}), el("span.spacer"), value]), input,
    ]));
    host._input = input;
    host._show = show;
}

function swatch(host, name, hex) {
    let button = host.firstElementChild;
    if (!button) {
        button = el("button.colour", {type: "button", title: "Choose a colour", onclick: () => Buddy.pickColor({
            hex: OPT.colors[name], title: "Choose a colour", at: button.getBoundingClientRect(),
            onPick: value => { OPT.colors[name] = value; swatch(host, name, value); drawPreview(); set(name, value); },
        })}, [el("i"), el("span")]);
        host.append(button);
    }
    button.firstChild.style.background = hex;
    button.lastChild.textContent = hex;
}

Buddy.on("options", o => {
    OPT = o;
    for (const host of $$("[data-slider]")) {
        const name = host.dataset.slider, spec = o.sliders[name];
        if (!host._input) buildSlider(host, name, spec);
        if (document.activeElement !== host._input) host._input.value = spec.value;
        host._show();
    }
    for (const host of $$("[data-color]")) swatch(host, host.dataset.color, o.colors[host.dataset.color]);
    for (const input of $$("[data-toggle-input]")) input.checked = o.toggles[input.dataset.toggleInput];
    for (const group of $$("[data-toggle]")) group.classList.toggle("off", !o.toggles[group.dataset.toggle]);
    if (FONTS.length) $("font").value = o.font_display;

    segmented($("style-scope"), null, o.style_scope, v => set("style_scope", v));
    $("style-track-row").hidden = o.style_scope !== "track";
    segmented($("anim-direction"), o.anim_directions.map(d => [d, d.replace("From ", "")]), o.anim_direction, v => set("anim_direction", v));
    segmented($("anim-speed"), o.anim_speeds.map(s => [s, s]), o.anim_speed, v => set("anim_speed", v));
    $("direction-row").hidden = !o.anim_preset.startsWith("Slide");
    presetCards($("anim-presets"), ANIMATIONS.filter(a => o.anim_presets.includes(a.id)), o.anim_preset, v => set("anim_preset", v));
    presetCards($("presets"), LAYOUTS.filter(l => o.layout_presets.includes(l.id)), o.layout_preset, v => { set("layout_preset", v); });
    $("preset-note").textContent = (LAYOUTS.find(l => l.id === o.layout_preset) || {}).note || "";
    if (STATE) {
        trackOptions($("style-track"), STATE.tracks.video, o.tracks.style_track);
        trackOptions($("anim-track"), STATE.tracks.video, o.tracks.anim_track);
    }
    drawPreview();
});

for (const input of $$("[data-toggle-input]")) {
    input.onchange = () => {
        const group = input.closest("[data-toggle]");
        if (group && group.dataset.toggle === input.dataset.toggleInput) group.classList.toggle("off", !input.checked);
        set(input.dataset.toggleInput, input.checked);
    };
}
$("style-track").onchange = e => set("style_track", Number(e.target.value));
$("anim-track").onchange = e => set("anim_track", Number(e.target.value));

function segmented(node, choices, value, onPick) {
    if (choices && node.children.length !== choices.length) {
        node.replaceChildren(...choices.map(([v, label]) => el("button", {type: "button", dataset: {value: v}, text: label})));
    }
    for (const b of node.children) {
        b.setAttribute("aria-pressed", String(b.dataset.value === value));
        b.onclick = () => onPick(b.dataset.value);
    }
}

const ANIMATIONS = [
    {id: "Pop (Scale)", title: "Pop", text: "Scales up from nothing with a little overshoot."},
    {id: "Bounce (Extra Rebound)", title: "Bounce", text: "Like Pop, with an extra rebound."},
    {id: "Fade (Opacity)", title: "Fade", text: "Fades in and out."},
    {id: "Slide (Direction + Fade)", title: "Slide", text: "Slides in from a side while fading."},
];
const LAYOUTS = [
    {id: "Hero + Stack", title: "Hero + Stack", text: "One big word, the rest above and below it.",
     note: "Click a word in the preview to make it the hero – otherwise it's the middle word by timing."},
    {id: "Side-by-Side", title: "Side-by-Side", text: "Every word in one row, evenly spaced.",
     note: "Shrinks the row to fit the frame if it has to."},
    {id: "Grid", title: "Grid", text: "The words in a tidy grid.", note: ""},
];

function presetCards(node, list, value, onPick) {
    if (node.children.length !== list.length) {
        node.replaceChildren(...list.map(p => el("button.preset", {type: "button", dataset: {value: p.id}}, [
            el("b", {text: p.title}), el("span.muted.small", {text: p.text}),
        ])));
    }
    for (const b of node.children) {
        b.setAttribute("aria-pressed", String(b.dataset.value === value));
        b.onclick = () => onPick(b.dataset.value);
    }
}

// ------------------------------------------------------- style preview --

function rgba(hex, alpha) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/* A CSS approximation of the Text+ look being set up - Resolve renders the real one. */
function drawPreview() {
    if (!OPT) return;
    const box = $("preview"), sample = $("sample");
    const val = name => {
        const host = document.querySelector(`[data-slider="${name}"]`);
        return host && host._input ? Number(host._input.value) : OPT.sliders[name].value;
    };
    const w = box.clientWidth || 400;
    // Size is a fraction of frame width; this box stands in for a 16:9 frame.
    const fs = Math.max(8, val("font_size") * w * 0.62);
    const t = OPT.toggles;
    // A clip's own text is the user's; only the stand-in is translated.
    const userSample = (STATE && STATE.sample) || "";
    sample.translate = !userSample;
    sample.textContent = userSample || "Sample Text";
    sample.style.fontFamily = `"${OPT.font_css.family}", sans-serif`;
    sample.style.fontWeight = OPT.font_css.weight;
    sample.style.fontStyle = OPT.font_css.italic ? "italic" : "normal";
    sample.style.fontSize = `${fs}px`;
    sample.style.color = OPT.colors.font_color;
    sample.style.webkitTextStroke = t.outline_on
        ? `${Math.max(0.5, val("outline_thickness") * fs * 2)}px ${rgba(OPT.colors.outline_color, val("outline_opacity"))}` : "0";
    sample.style.paintOrder = "stroke fill";
    sample.style.textShadow = t.shadow_on
        // Offsets are fractions of the frame width, like Size.
        ? `${val("shadow_offset_x") * w}px ${-val("shadow_offset_y") * w}px ${val("shadow_blur") * w / 400}px ${rgba(OPT.colors.shadow_color, val("shadow_opacity"))}` : "none";
    if (t.background_on) {
        const ph = Math.max(2, 6 + val("background_extend_horizontal") * w), pv = Math.max(2, 4 + val("background_extend_vertical") * w * 0.56);
        sample.style.background = rgba(OPT.colors.background_color, val("background_opacity"));
        sample.style.padding = `${pv}px ${ph}px`;
        sample.style.borderRadius = `${val("background_corner_radius") * fs}px`;
    } else {
        sample.style.background = "none";
        sample.style.padding = "0";
    }
}
let previewWidth = 0;
new ResizeObserver(() => {
    if ($("preview").clientWidth === previewWidth) return;
    previewWidth = $("preview").clientWidth;
    drawPreview();
}).observe($("preview"));

// ----------------------------------------------------------- canvases --

const canvases = {
    layout: PlacementCanvas($("canvas-layout"), {tab: "layout", multi: false}),
    words: PlacementCanvas($("canvas-words"), {tab: "words", multi: true}),
};
Buddy.on("canvas", c => canvases[c.tab] && canvases[c.tab].update(c));

let OVERLAY = null;
Buddy.on("overlay", o => {
    OVERLAY = o;
    for (const c of Object.values(canvases)) c.setOverlay(o);
    if (!$("guides-pop").hidden) drawGuides();
});

$("apply-layout").onclick = () => send("apply_layout", {preset: OPT.layout_preset, selected: canvases.words.selected()});

Buddy.on("history", h => {
    for (const b of $$("[data-undo]")) { b.disabled = !h.undo; b.title = !h.undo ? "Nothing to undo" : h.undo === 1 ? "Undo (Ctrl+Z) - 1 step" : `Undo (Ctrl+Z) - ${h.undo} steps`; }
    for (const b of $$("[data-redo]")) { b.disabled = !h.redo; b.title = h.redo ? "Redo (Ctrl+Shift+Z)" : "Nothing to redo"; }
});

// Guides & snapping settings, under the canvas's button.
let guidesAnchor = null;
for (const b of $$("[data-guides]")) {
    b.onclick = e => {
        e.stopPropagation();
        if (!$("guides-pop").hidden && guidesAnchor === b) return closeGuides();
        guidesAnchor = b;
        drawGuides();
        const pop = $("guides-pop");
        pop.hidden = false;
        const r = b.getBoundingClientRect();
        pop.style.top = `${Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 8)}px`;
        pop.style.left = `${Math.max(8, r.right - pop.offsetWidth)}px`;
    };
}
function closeGuides() { $("guides-pop").hidden = true; }
document.addEventListener("mousedown", e => {
    if (!$("guides-pop").hidden && !e.target.closest("#guides-pop, [data-guides], .picker")) closeGuides();
});

function drawGuides() {
    const o = OVERLAY;
    if (!o) return;
    const put = (name, value) => send("overlay", {name, value});
    const select = (name, options) => {
        const s = el("select.field", {onchange: e => put(name, e.target.value)}, options.map(v => el("option", {value: v, text: v})));
        s.value = o[name];
        return s;
    };
    const range = (name, min, max, step, value, format) => {
        const out = el("b.val", {text: format(value)});
        const input = el("input", {type: "range", min, max, step, value});
        input.addEventListener("input", () => { out.textContent = format(Number(input.value)); });
        input.addEventListener("change", () => put(name, Number(input.value)));
        return [input, out];
    };
    const colour = name => {
        const b = el("button.colour", {type: "button", onclick: () => Buddy.pickColor({
            hex: o[name], at: b.getBoundingClientRect(), onPick: v => put(name, v),
        })}, [el("i", {style: `background:${o[name]}`}), el("span", {text: o[name]})]);
        return b;
    };
    const check = (name, label, disabled) => el("label.check", {}, [
        el("input", {type: "checkbox", checked: o[name], disabled, onchange: e => put(name, e.target.checked)}), label]);
    const pct = v => `${Math.round(v * 100)}%`;
    const res = STATE ? STATE.resolution[0] : 1920;
    const spacing = o.grid_spacing || o.default_spacing;
    const [spaceInput, spaceOut] = range("grid_spacing", 1 / 64, 1 / 4, 1 / 640, spacing, v => `${Math.round(v * res)} px`);
    spaceInput.addEventListener("dblclick", () => put("grid_spacing", 0));
    spaceInput.title = "Double-click for the standard spacing for this resolution";
    $("guides-pop").replaceChildren(
        el("div.pop-section", {}, [
            el("b", {text: "Grid"}),
            el("label.lbl", {}, ["Type", select("grid_type", o.grid_types)]),
            o.grid_type === "Standard" ? el("label.lbl", {}, [el("span.row", {}, ["Spacing", el("span.spacer"), spaceOut]), spaceInput]) : null,
            el("div.row", {}, [el("span.small.muted", {text: "Colour"}), colour("grid_color")]),
            el("label.lbl", {}, (([i, v]) => [el("span.row", {}, ["Opacity", el("span.spacer"), v]), i])(range("grid_opacity", 0, 1, 0.01, o.grid_opacity, pct))),
        ]),
        el("div.pop-section", {}, [
            el("b", {text: "Safe zone"}),
            el("label.lbl", {}, ["Platform", select("safe_type", o.safe_types)]),
            el("div.row", {}, [el("span.small.muted", {text: "Colour"}), colour("safe_color")]),
            el("label.lbl", {}, (([i, v]) => [el("span.row", {}, ["Opacity", el("span.spacer"), v]), i])(range("safe_opacity", 0, 1, 0.01, o.safe_opacity, pct))),
        ]),
        el("div.pop-section", {}, [
            el("b", {text: "Snapping"}),
            check("snap", "Snap to the frame centre"),
            check("snap_elements", "Snap to other text", !o.snap),
            check("snap_safe", "Snap to safe-zone edges", !o.snap),
        ]),
    );
}

// ---------------------------------------------------------- animation --

for (const b of $$("[data-scope]")) b.onclick = () => send("apply_animation", {scope: b.dataset.scope});
$("remove-anims").onclick = async () => {
    const yes = await Buddy.confirm({title: "Remove animations?", danger: true, ok: "Remove",
        text: "Removes the animations from every Text+ clip on the timeline. Undo can put them back."});
    if (yes) send("remove_animations");
};

// ----------------------------------------------------------- messages --

Buddy.on("toast", t => Buddy.toast(t.text, 2600));
Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));

const LOG = [];
function logLine(e) {
    LOG.push(e);
    if (LOG.length > 200) LOG.shift();
    const kind = /\[Error|Exception|Warning|Failed|Could not/.test(e.text) ? "error" : "";
    $("log").prepend(el(`li${kind ? "." + kind : ""}`, {}, [el("span.muted", {text: e.time}), " ", el("span", {text: e.text})]));
    while ($("log").children.length > 200) $("log").lastChild.remove();
    $("activity-last").textContent = e.text;
}
Buddy.on("log", entries => { LOG.length = 0; $("log").replaceChildren(); entries.forEach(logLine); });
Buddy.on("log_line", logLine);
