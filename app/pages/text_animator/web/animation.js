/*
 * Animation's view. Tabs: the kind of animation. Previews: every motion
 * preset page.py sends, playing in a little viewer, grouped by pack; a tile
 * chooses the preset, and Apply puts it on the clips selected in Resolve.
 * What moves in the viewers follows the selection (page.py's "shape").
 * The search and the pack/kind filters only narrow what's drawn here.
 *
 * Favorites is the same panel narrowed to the hearted presets. Editor opens
 * a preset as keys (page.py sends them on "keys"; a saved preset has its own)
 * on a spline editor, beside a viewer that plays exactly what's drawn: the
 * same Bezier curves motion.py lays on the clip. The edit can go on the
 * selected clips as it is, or be saved as a preset of the person's own.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, send} = Buddy;
const SVG = "http://www.w3.org/2000/svg";
const LAST = 40;            // samples 0..40
const PAUSE = 0.6;          // seconds at rest before a preset plays again
const SAVED = "Saved";      // motion.SAVED's pack: the person's own presets

let presets = [], packs = [], kinds = [];
let state = {tab: "previews", chosen: "", way: "both", speed: 1, play: "all", shape: "card",
             favorites: [], connected: false, selection: null, problem: "", working: ""};
let favorites = new Set();
const view = {q: "", pack: "all", kind: "all"};
const tiles = [];           // {p, node, obj, head, offset, hoverAt, visible}
let big = null;             // the chosen preset's viewer

// ------------------------------------------------------------------- tabs

function showTab(tab) {
    for (const b of document.querySelectorAll(".tabs [data-tab]")) {
        b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    }
    for (const panel of document.querySelectorAll(".panel")) {
        const tabs = (panel.dataset.tabs || panel.id.replace("panel-", "")).split(" ");
        panel.hidden = !tabs.includes(tab);
    }
}

function goTab(tab) {
    const was = state.tab;
    state.tab = tab;
    showTab(tab);
    if ((was === "favorites") !== (tab === "favorites")) drawGroups();
    if (tab === "editor") editorShown();
    send("tab", {tab});
}

for (const b of document.querySelectorAll(".tabs [data-tab]")) {
    b.onclick = () => goTab(b.dataset.tab);
}

// ----------------------------------------------------------------- shapes

function svg(tag, attrs = {}, kids = []) {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    for (const c of kids) n.appendChild(c);
    return n;
}

const GLYPHS = {
    heart: '<path d="M12 20.3s-7.4-4.5-9.3-9C1.3 7.8 3.4 4.6 6.8 4.6c2.1 0 3.9 1.2 5.2 3 1.3-1.8 3.1-3 5.2-3 3.4 0 5.5 3.2 4.1 6.7-1.9 4.5-9.3 9-9.3 9z"/>',
    edit: '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z"/><path d="M14 7l3 3"/>',
};

function glyph(name) {
    const node = svg("svg", {viewBox: "0 0 24 24", class: "icon-svg", "aria-hidden": "true"});
    node.innerHTML = GLYPHS[name];
    return node;
}

// Drawn around (0, 0) on the 128x72 stage, about 34x22.
const SHAPES = {
    card: () => [
        svg("rect", {class: "pv-obj", x: -17, y: -11, width: 34, height: 22, rx: 4.5}),
        svg("rect", {class: "pv-line", x: -12, y: -4.5, width: 17, height: 3.4, rx: 1.7, opacity: .6}),
        svg("rect", {class: "pv-line", x: -12, y: 1.6, width: 10, height: 3.4, rx: 1.7, opacity: .35}),
    ],
    title: () => {
        const t = svg("text", {class: "pv-obj pv-word", x: 0, y: 5.5, "font-size": 15, "text-anchor": "middle"});
        t.textContent = "Title";
        return [t];
    },
    photo: () => [
        svg("rect", {class: "pv-obj", x: -16, y: -11, width: 32, height: 22, rx: 3}),
        svg("rect", {class: "pv-line", x: -13.5, y: -8.5, width: 27, height: 17, rx: 1.5, opacity: .9}),
        svg("circle", {class: "pv-obj", cx: 6.5, cy: -3.5, r: 2.6}),
        svg("path", {class: "pv-obj", d: "M-13.5 8.5 L-5 -1 L1 5 L5 1.5 L13.5 8.5 Z"}),
    ],
    clip: () => {
        const holes = [];
        for (let i = 0; i < 5; i++) {
            for (const y of [-9.6, 7.2]) {
                holes.push(svg("rect", {class: "pv-line", x: -14 + i * 6.2, y, width: 3.4, height: 2.4, rx: .6, opacity: .7}));
            }
        }
        return [
            svg("rect", {class: "pv-obj", x: -17, y: -11, width: 34, height: 22, rx: 2.5}),
            ...holes,
            svg("path", {class: "pv-line", d: "M-3 -4.5 L5 0 L-3 4.5 Z", opacity: .9}),
        ];
    },
};

function stage(obj) {
    return svg("svg", {viewBox: "0 0 128 72", "aria-hidden": "true"}, [
        svg("line", {class: "pv-guide", x1: 0, y1: 36, x2: 128, y2: 36}),
        svg("line", {class: "pv-guide", x1: 64, y1: 0, x2: 64, y2: 72}),
        svg("rect", {class: "pv-safe", x: 8, y: 6, width: 112, height: 60, rx: 3}),
        svg("g", {transform: "translate(64 36)"}, [obj]),
    ]);
}

function reshape() {
    const make = SHAPES[state.shape] || SHAPES.card;
    for (const t of [...tiles, big, ed.viewer].filter(Boolean)) t.obj.replaceChildren(...make());
}

// ----------------------------------------------------------------- motion

const channel = arr => f => {
    const i = Math.min(LAST - 1, Math.floor(f)), k = f - i;
    return arr[i] + (arr[i + 1] - arr[i]) * k;
};

function prepare(p) {
    // Play the fitted Fusion curves, including for built-in presets. The raw
    // samples remain available for editing and for the timing bar.
    p.at = p.previewKeys ? atKeys(p.previewKeys)
        : {x: channel(p.x), y: channel(p.y), r: channel(p.r), s: channel(p.s), o: channel(p.o)};
    // How long its first move lasts, for the tile: the In, the Emphasis or the Out.
    const [a, b] = p.segs[0] || [0, 1];
    p.moveSeconds = (b - a) * p.dur;
    p.span = playSpan(p.kind, p.segs.map(([s, e]) => [s * LAST, e * LAST]), !!p.keys).map(f => f / LAST);
    return p;
}

/* The part of a preset that plays, in samples: [first, last]. An In or an
   Out saved from the Editor still holds the whole curve it was drawn from,
   so it plays only the part that goes on clips; built-in ones play whole,
   as they always have. `runs` are where it moves (motion.segments). */
function playSpan(kind, runs, drawn) {
    const first = runs[0], last = runs[runs.length - 1];
    if (kind === "In") return first && first[0] === 0 ? [0, first[1]] : [0, 0];
    if (kind === "Out" && drawn) return last && last[1] === LAST ? [last[0], LAST] : [LAST, LAST];
    return [0, LAST];
}

function pose(t, f) {
    const a = t.p.at;
    t.obj.setAttribute("transform",
        `translate(${a.x(f) * 2} ${a.y(f) * 2}) rotate(${a.r(f)}) scale(${Math.max(0.001, a.s(f))})`);
    t.obj.setAttribute("opacity", Math.max(0, Math.min(1, a.o(f))));
    if (t.head) t.head.style.left = `${(f / LAST) * 100}%`;
}

function chosenSpan(p) {
    const first = p.segs[0], last = p.segs[p.segs.length - 1];
    if (!first || !last) return p.span;
    if (p.kind === "In · Out") {
        if (state.way === "in") return [0, first[1]];
        if (state.way === "out") return [last[0], 1];
    } else if (p.kind === "In") return [0, first[1]];
    else if (p.kind === "Out") return [last[0], 1];
    else if (p.kind === "Emphasis") return [first[0], last[1]];
    return p.span;
}

function frameFor(t, now, always) {
    const [a, b] = always ? chosenSpan(t.p) : (t.p.span || [0, 1]);
    let elapsed;
    if (!always && state.play === "hover") {
        // At rest: mid-hold - or where a lone In ends, where a lone Out starts.
        if (t.hoverAt === null) return (b - a >= 1 ? 0.5 : t.p.kind === "Out" ? a : b) * LAST;
        elapsed = (now - t.hoverAt) / 1000;
    } else {
        elapsed = now / 1000 + t.offset;
    }
    const speed = Number(state.speed) || 1;
    const fps = Number(state.selection?.fps) || 24;
    const length = Math.max(0.01, (b - a) * t.p.dur / speed);
    const local = ((elapsed % (length + PAUSE)) + length + PAUSE) % (length + PAUSE);
    // Resolve shows whole timeline frames. Sampling at the same rate and
    // selected speed exposes the jumps a fast move makes at 24 fps.
    const shown = Math.min(length, Math.floor(local * fps) / fps);
    return Math.min(b, a + shown * speed / t.p.dur) * LAST;
}

function tick(now) {
    if (!document.hidden && !$("panel-previews").hidden) {
        for (const t of tiles) if (t.visible) pose(t, frameFor(t, now, false));
        if (big) pose(big, frameFor(big, now, true));
    }
    if (!document.hidden && !$("panel-editor").hidden) editorTick(now);
    requestAnimationFrame(tick);
}

const seen = new IntersectionObserver(entries => {
    for (const e of entries) {
        const t = tiles.find(x => x.node === e.target);
        if (t) t.visible = e.isIntersecting;
    }
});

// ---------------------------------------------------------- Bezier curves
// The same as motion.py's: a channel is [[t, value, left handle, right
// handle], ...], t in samples, a handle an absolute [t, value] or null.

const bez = (a, b, c, d, u) => { const w = 1 - u; return w * w * w * a + 3 * w * w * u * b + 3 * w * u * u * c + u * u * u * d; };

function controls(k0, k1) {
    return [[k0[0], k0[1]], k0[3] || [k0[0], k0[1]], k1[2] || [k1[0], k1[1]], [k1[0], k1[1]]];
}

function solve(p, t) {
    let lo = 0, hi = 1;
    for (let i = 0; i < 32; i++) {
        const u = (lo + hi) / 2;
        if (bez(p[0][0], p[1][0], p[2][0], p[3][0], u) < t) lo = u; else hi = u;
    }
    return (lo + hi) / 2;
}

function evalKeys(keys, t) {
    if (t <= keys[0][0]) return keys[0][1];
    for (let i = 0; i + 1 < keys.length; i++) {
        const k0 = keys[i], k1 = keys[i + 1];
        if (t <= k1[0]) {
            if (k1[0] - k0[0] <= 1e-9) return k1[1];
            const p = controls(k0, k1);
            return bez(p[0][1], p[1][1], p[2][1], p[3][1], solve(p, t));
        }
    }
    return keys[keys.length - 1][1];
}

const atKeys = keys => ({x: t => evalKeys(keys.x, t), y: t => evalKeys(keys.y, t), r: t => evalKeys(keys.r, t),
                         s: t => evalKeys(keys.s, t), o: t => evalKeys(keys.o, t)});

/* One more key at t, the curve's shape unchanged (motion.split_at). Its index. */
function splitAt(keys, t) {
    for (let i = 0; i + 1 < keys.length; i++) {
        const k0 = keys[i], k1 = keys[i + 1];
        if (Math.abs(k0[0] - t) < 1e-6) return i;
        if (Math.abs(k1[0] - t) < 1e-6) return i + 1;
        if (k0[0] < t && t < k1[0]) {
            const p = controls(k0, k1), u = solve(p, t);
            const lerp = (a, b) => [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
            const q0 = lerp(p[0], p[1]), q1 = lerp(p[1], p[2]), q2 = lerp(p[2], p[3]);
            const r0 = lerp(q0, q1), r1 = lerp(q1, q2), s = lerp(r0, r1);
            k0[3] = k0[3] ? q0 : null;
            k1[2] = k1[2] ? q2 : null;
            keys.splice(i + 1, 0, [t, s[1], r0, r1]);
            return i + 1;
        }
    }
    return -1;
}

// ------------------------------------------------------------------ tiles

const seconds = s => `${s.toFixed(1)}s`;

function tile(p, index) {
    const obj = svg("g");
    const head = el("div.pv-head");
    const choose = () => send("choose", {id: p.id});
    const hearted = favorites.has(p.id);
    const node = el("div.card.pv-tile", {
        role: "button", tabindex: "0", "aria-pressed": String(p.id === state.chosen),
        title: `${p.label} · ${p.pack}`, onclick: choose,
        onkeydown: e => {
            if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) { e.preventDefault(); choose(); }
        },
    }, [
        el("div.pv-stage", {}, [stage(obj)]),
        el("div.pv-track", {}, [
            ...p.segs.map(([a, b]) => el("div.pv-seg", {style: `left:${a * 100}%;width:${(b - a) * 100}%`})),
            head,
        ]),
        el("div.pv-meta", {}, [
            el("span.pv-name", {text: p.label, translate: "no"}),
            el("span.pv-sub.muted.small", {}, [el("span", {text: p.kind}), el("span.pv-dur", {text: seconds(p.moveSeconds / (Number(state.speed) || 1))})]),
        ]),
        el("div.pv-acts", {}, [
            el("button.pv-act", {type: "button", title: "Open in the Editor",
                                 onclick: e => { e.stopPropagation(); openInEditor(p.id); }}, [glyph("edit")]),
            el("button.pv-act.heart", {
                type: "button", "aria-pressed": String(hearted), dataset: {id: p.id},
                title: hearted ? "Take it out of Favorites" : "Add to Favorites",
                onclick: e => { e.stopPropagation(); send("favorite", {id: p.id, on: !favorites.has(p.id)}); },
            }, [glyph("heart")]),
        ]),
    ]);
    const t = {p, node, obj, head, offset: index * 0.37, hoverAt: null, visible: true};
    node.addEventListener("mouseenter", () => { t.hoverAt = performance.now(); });
    node.addEventListener("mouseleave", () => { t.hoverAt = null; });
    tiles.push(t);
    return node;
}

function segmented(host, items, key) {
    host.replaceChildren(...items.map(([id, label, n]) => el("button", {
        type: "button", "aria-pressed": String(view[key] === id),
        onclick: () => { view[key] = id; drawGroups(); },
    }, [label, n == null ? null : el("span.n", {text: String(n)})])));
}

const hearts = () => state.tab === "favorites";

const matches = p => (hearts() || view.pack === "all" || p.pack === view.pack)
    && (view.kind === "all" || p.kind === view.kind)
    && (!view.q || p.label.toLowerCase().includes(view.q));

function drawGroups() {
    const pool = hearts() ? presets.filter(p => favorites.has(p.id)) : presets;
    const count = f => pool.filter(f).length;
    $("packs").hidden = hearts();
    segmented($("packs"), [["all", "All", presets.length],
        ...packs.map(k => [k.id, k.id, count(p => p.pack === k.id)])], "pack");
    // Only the kinds there are: an In is only ever made in the Editor.
    segmented($("kinds"), [["all", "Any kind"], ...kinds.filter(k => presets.some(p => p.kind === k)).map(k => [k, k])], "kind");

    for (const t of tiles) seen.unobserve(t.node);
    tiles.length = 0;
    let index = 0;
    const groups = packs.map(pack => {
        const list = pool.filter(p => p.pack === pack.id && matches(p));
        if (!list.length) return null;
        return el("section.pv-group", {style: `--pack: var(--pack-${pack.id.toLowerCase()})`}, [
            el("div.pv-group-head", {}, [
                el("span.pv-dot"),
                el("h2.section-title", {text: pack.id}),
                el("span.chip", {text: String(list.length)}),
                el("span.pv-credit.muted.small", {text: pack.credit}),
            ]),
            el("div.pv-grid", {}, list.map(p => tile(p, index++))),
        ]);
    }).filter(Boolean);
    let empty;
    if (hearts() && !favorites.size) {
        empty = el("div.empty", {}, [
            el("span.strong", {text: "No favorites yet"}),
            el("span", {text: "Heart a preset on Previews and it's kept here."}),
        ]);
    } else {
        empty = el("div.empty", {}, [
            el("span.strong", {text: "No presets match"}),
            el("span", {text: "Try another name, pack or kind."}),
        ]);
    }
    $("groups").replaceChildren(...(groups.length ? groups : [empty]));
    for (const t of tiles) seen.observe(t.node);
    reshape();
}

$("q").addEventListener("input", e => { view.q = e.target.value.trim().toLowerCase(); drawGroups(); });

// ------------------------------------------------------------ apply card

for (const b of $("way").querySelectorAll("button")) b.onclick = () => send("way", {way: b.dataset.way});
for (const b of $("speed").querySelectorAll("button")) b.onclick = () => send("speed", {speed: Number(b.dataset.speed)});
for (const b of $("play").querySelectorAll("button")) b.onclick = () => send("play", {play: b.dataset.play});
$("apply").onclick = () => send("apply");
$("remove").onclick = () => send("remove");
$("update").onclick = () => send("update");

const WHERE = {
    both: "The In starts on each clip's first frame and the Out ends on its last.",
    in: "The In starts on each clip's first frame.",
    out: "The Out ends on each clip's last frame.",
    "Emphasis": "Plays where the playhead is over a clip, or in the middle of the clip.",
    "Out": "Ends on each clip's last frame.",
    "In": "The In starts on each clip's first frame.",
};

function drawChosen() {
    const p = presets.find(x => x.id === state.chosen) || presets[0];
    if (!p) return;
    if (!big || big.p !== p) {
        const obj = svg("g");
        $("chosen-stage").replaceChildren(stage(obj));
        big = {p, obj, head: null, offset: 0, hoverAt: null};
        $("apply").closest(".pv-apply").style.setProperty("--pack", `var(--pack-${p.pack.toLowerCase()})`);
        reshape();
    }
    $("chosen-name").textContent = p.label;
    $("chosen-sub").textContent = `${p.pack} · ${p.kind} · ${Number(state.selection?.fps) || 24} fps preview`;
    const inOut = p.kind === "In · Out";
    $("way-row").hidden = !inOut;
    $("where").textContent = WHERE[inOut ? state.way : p.kind] || "";
    for (const t of tiles) t.node.setAttribute("aria-pressed", String(t.p.id === p.id));
}

function selectionChips() {
    const s = state.selection;
    if (!state.connected) {
        return [el("span.muted.small", {text: "Not connected to Resolve"}),
                el("button.btn.ghost", {type: "button", text: "Connect", onclick: () => send("refresh")})];
    }
    if (state.problem) return [el("span.warn", {text: state.problem})];
    if (!s) return [el("span.muted.small", {text: "Reading the selection…"})];
    if (!s.total) return [el("span.muted.small", {text: "Nothing selected in the timeline"})];
    return s.parts.map(part => el("span.chip", {text: part.text}));
}

function applyLabel(working, can) {
    return working ? "Applying…" : !can ? "Apply to selected" : can === 1 ? "Apply to 1 clip" : `Apply to ${can} clips`;
}

function drawSelection() {
    const s = state.selection;
    $("selection").replaceChildren(...selectionChips());
    $("ed-selection").replaceChildren(...selectionChips());

    const can = s && s.animatable;
    const apply = $("apply"), remove = $("remove");
    apply.disabled = !!state.working || !can;
    remove.disabled = !!state.working || !(s && s.animated);
    apply.textContent = applyLabel(state.working === "apply" && state.tab !== "editor", can);
    remove.textContent = state.working === "remove" ? "Removing…" : "Remove";
    $("ed-apply").disabled = !!state.working || !can || !ed.keys;
    $("ed-apply").textContent = applyLabel(state.working === "apply" && state.tab === "editor", can);
    // Re-framed in the Inspector since the preset went on: the move doesn't
    // follow until Update framing takes the new framing in.
    const reframed = (s && s.reframed) || 0;
    $("reframed").hidden = !reframed;
    $("reframed-text").textContent = reframed === 1 ? "Framing changed on 1 clip"
        : `Framing changed on ${reframed} clips`;
    $("update").disabled = !!state.working;
    $("update").textContent = state.working === "update" ? "Updating…" : "Update framing";
    remove.title = s && s.animated ? "Take Buddy's preset off the selected clips" : "None of the selected clips has a Buddy preset";
}

function pressed(host, attr, value) {
    for (const b of host.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset[attr] === String(value)));
}

// ================================================================= Editor

const CHANNELS = [["x", "Position X"], ["y", "Position Y"], ["r", "Rotation"], ["s", "Scale"], ["o", "Opacity"]];
const REST = {x: 0, y: 0, r: 0, s: 1, o: 1};
// motion.py's _EPS and _GAP: how far from rest counts as moving, and how
// short a pass through rest still counts as one move.
const EPS = {x: 0.05, y: 0.05, r: 0.05, s: 0.005, o: 0.005};
const GAP = 3;
// The least a channel's graph spans, so a tiny move isn't blown up to fill it.
const MIN_SPAN = {x: 16, y: 12, r: 30, s: 0.5, o: 1};
// A channel's value as it reads: x and y as a share of the frame (motion.py:
// x/64 of its width, y/36 of its height), scale and opacity as percentages.
const SHOW = {
    x: v => `${Math.round(v / 64 * 100)}%`, y: v => `${Math.round(v / 36 * 100)}%`,
    r: v => `${Math.round(v)}°`, s: v => `${Math.round(v * 100)}%`, o: v => `${Math.round(v * 100)}%`,
};
// The kinds the Editor makes; an Emphasis it was opened on stays one.
const EDIT_KINDS = ["In", "Out", "In · Out"];
// The graph's margins, px. The time bar - where the playhead is moved - runs
// along the top; the curves are drawn below it.
const PAD = {l: 48, r: 14, t: 34, b: 12};
const BAR = {top: 4, height: 22};

const ed = {
    id: "",             // the preset it started from
    saved: false,       // ...one of the person's own: Save changes and Delete
    label: "", kind: "In · Out", dur: 1,
    keys: null,         // {x, y, r, s, o}, or null while they're on their way
    waiting: "",        // the id asked for on "keys"
    on: ["s"],          // the channels shown on the graph, in colour, to edit
    ch: "s",            // ...the one being edited: its handles shown, its units up the side
    sels: {},           // channel -> its selected keys' indexes: keys of several shown channels move together
                        // (ed.sel is the edited channel's)
    span: [0, LAST],    // the part that plays for the kind (playSpan)
    ease: null,         // a key's ease copied from the menu (copyEase), to paste
    menuAt: null,       // [left, top] the key menu was dragged to by its grip, or null: beside the keys
    view: "clip",       // "clip": with one clip selected, laid out at its length; "preset": the preset's own time
    layout: null,       // clipLayout(), or null on the preset's own time
    dirty: false,
    playing: true, started: 0,
    head: 0,            // the playhead, in seconds along the graph's time (the clip's or the preset's)
    undo: [], redo: [],
    range: [0, 1],      // the edited channel's values, bottom to top
    ranges: {},         // ...every channel's: each shown one fills the height
    snap: null,         // the time a dragged key snapped to, a key of another channel: drawn as a guide
    viewer: null,
    drag: null,
};
const keyCache = {};    // id -> keys page.py sent

const clone = v => JSON.parse(JSON.stringify(v));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// The edited channel's selected keys, read and set as one list.
Object.defineProperty(ed, "sel", {get: () => ed.sels[ed.ch] || [], set: v => { ed.sels[ed.ch] = v; }});
// The channels with keys selected, and how many keys in all.
const selChannels = () => CHANNELS.map(([c]) => c).filter(c => (ed.sels[c] || []).length);
const selCount = () => selChannels().reduce((n, c) => n + ed.sels[c].length, 0);
const clearSel = () => { ed.sels = {}; };
// The one key selected, of the edited channel - its handles shown - or -1.
const single = () => selCount() === 1 && ed.sel.length === 1 ? ed.sel[0] : -1;

function editorShown() {
    if (!ed.id || !presets.some(p => p.id === ed.id)) loadPreset(state.chosen || (presets[0] && presets[0].id), true);
    else drawEditor();
}

function openInEditor(id) {
    goTab("editor");
    if (id !== ed.id) loadPreset(id);
}

async function loadPreset(id, force) {
    const p = presets.find(x => x.id === id);
    if (!p) return;
    if (ed.dirty && !force && !(await Buddy.confirm({
        title: "Discard this edit?", text: "The changes to the curves haven't been saved.", ok: "Discard", danger: true}))) {
        $("ed-base").value = ed.id;
        return;
    }
    Object.assign(ed, {id, saved: p.pack === SAVED, label: p.pack === SAVED ? p.label : `${p.label} edit`,
                       kind: p.kind, dur: p.dur, sels: {}, dirty: false, undo: [], redo: []});
    if (p.keys) return setKeys(p.keys);
    if (keyCache[id]) return setKeys(keyCache[id]);
    ed.keys = null;
    ed.waiting = id;
    send("keys", {id});
    drawEditor();
}

function setKeys(keys) {
    ed.keys = clone(keys);
    ed.waiting = "";
    // Start on the channel that moves the most.
    const swing = c => {
        const v = ed.keys[c].map(k => k[1]);
        return (Math.max(...v) - Math.min(...v)) / MIN_SPAN[c];
    };
    // Several shown stay shown, from one preset to the next; one shown is the
    // one that moves the most.
    const pool = ed.on.length > 1 ? ed.on : CHANNELS.map(([c]) => c);
    ed.ch = pool.reduce((a, b) => swing(b) > swing(a) ? b : a, pool.includes("s") ? "s" : pool[0]);
    if (ed.on.length <= 1) ed.on = [ed.ch];
    fitRange();
    restart();
    drawEditor();
}

function restart() {
    ed.started = performance.now();
    ed.playing = true;
}

function snapshot() {
    ed.undo.push(JSON.stringify({keys: ed.keys, kind: ed.kind, dur: ed.dur}));
    if (ed.undo.length > 200) ed.undo.shift();
    ed.redo = [];
}

function changed() {
    ed.dirty = true;
    drawEditor();
}

function step(from, to) {
    if (!from.length) return;
    to.push(JSON.stringify({keys: ed.keys, kind: ed.kind, dur: ed.dur}));
    Object.assign(ed, JSON.parse(from.pop()));
    clearSel();
    changed();
}

/* Every channel's range, each fitted to its own keys and handles: the shown
   ones are drawn over each other, each filling the graph's height. */
function fitRange() {
    for (const [c] of CHANNELS) {
        const vals = [REST[c]];
        for (const k of ed.keys[c]) {
            vals.push(k[1]);
            if (k[2]) vals.push(k[2][1]);
            if (k[3]) vals.push(k[3][1]);
        }
        let lo = Math.min(...vals), hi = Math.max(...vals);
        const span = Math.max(hi - lo, MIN_SPAN[c]);
        const mid = (lo + hi) / 2;
        ed.ranges[c] = [mid - span / 2 * 1.15, mid + span / 2 * 1.15];
    }
    ed.range = ed.ranges[ed.ch];
}

/* Edits c: its handles shown, its units up the side. What is selected stays. */
function activate(c) {
    if (c === ed.ch) return;
    ed.ch = c;
    ed.range = ed.ranges[c];
}

/* Where the keys move, in whole samples - motion.segments for a preset drawn
   as keys (_keyed_runs, then short rests merged), so the preview plays the
   same part that goes on clips. */
function runsOf(keys) {
    const times = [...new Set(CHANNELS.flatMap(([c]) => keys[c].map(k => +k[0].toFixed(6))))].sort((a, b) => a - b);
    const runs = [];
    for (let i = 0; i + 1 < times.length; i++) {
        const t0 = times[i], t1 = times[i + 1];
        let moves = false;
        for (let f = 0; f <= 6 && !moves; f++) {
            const t = t0 + (t1 - t0) * f / 6;
            moves = CHANNELS.some(([c]) => Math.abs(evalKeys(keys[c], t) - REST[c]) > EPS[c]);
        }
        if (!moves) continue;
        const end = runs[runs.length - 1];
        if (end && Math.abs(end[1] - t0) < 1e-6) end[1] = t1;
        else runs.push([t0, t1]);
    }
    const merged = [];
    for (const [a, b] of runs.map(([a, b]) => [Math.floor(a), Math.min(LAST, Math.ceil(b))])) {
        const end = merged[merged.length - 1];
        if (end && a - end[1] <= GAP) end[1] = b;
        else merged.push([a, b]);
    }
    return merged;
}

/* The preset laid on the one selected clip, as motion.plan() lays it: the In
   from the clip's first frame, the Out ending on its last, an Emphasis in the
   middle, each at the chosen speed - squeezed to fit a clip too short for
   them - and the rest held in between. {total: seconds, parts: [{t0, t1: the
   move's samples, s0, s1: its seconds on the clip}]}, or null: no clip, or
   nothing of this kind to put on it. */
function clipLayout() {
    const clip = state.selection && state.selection.clip;
    if (ed.view !== "clip" || !clip || !(clip.frames > 1) || !ed.keys) return null;
    const fps = Number(state.selection.fps) || 24, speed = Number(state.speed) || 1;
    const total = (clip.frames - 1) / fps;          // its first frame to its last
    const found = {};
    for (const [a, b] of runsOf(ed.keys)) {         // motion.moves
        if (a === 0 && !found.in && (ed.kind === "In · Out" || ed.kind === "In")) found.in = [a, b];
        else if (b === LAST && (ed.kind === "In · Out" || ed.kind === "Out")) found.out = [a, b];
        else if (ed.kind === "Emphasis") {
            found.emphasis = found.emphasis ? [Math.min(found.emphasis[0], a), Math.max(found.emphasis[1], b)] : [a, b];
        }
    }
    const way = state.way || "both";
    const wanted = ed.kind === "In · Out" ? ["in", "out"].filter(m => found[m] && (way === "both" || way === m))
        : ["in", "out", "emphasis"].filter(m => found[m]);
    if (!wanted.length) return null;
    const seconds = m => (found[m][1] - found[m][0]) / LAST * ed.dur / speed;
    const sum = wanted.reduce((acc, m) => acc + seconds(m), 0);
    const squeeze = sum > total ? total / sum : 1;
    const parts = wanted.map(m => {
        const length = seconds(m) * squeeze;
        const first = m === "in" ? 0 : m === "out" ? total - length : (total - length) / 2;
        return {t0: found[m][0], t1: found[m][1], s0: first, s1: first + length};
    }).sort((p, q) => p.s0 - q.s0);
    return {total, parts};
}

/* A preset sample's seconds on the clip, or null where the clip doesn't play
   it (a move the kind leaves out, a stretch of rest squeezed away). */
function onClip(L, t) {
    for (const p of L.parts) {
        if (t >= p.t0 - 1e-9 && t <= p.t1 + 1e-9) {
            return p.s0 + (t - p.t0) / Math.max(1e-9, p.t1 - p.t0) * (p.s1 - p.s0);
        }
    }
    return null;
}

/* The preset sample the clip shows at s seconds: along a move, or - between
   them - held where the last one ended (before the first, where it starts). */
function fromClip(L, s) {
    const parts = L.parts;
    if (s <= parts[0].s0) return parts[0].t0;
    for (let i = 0; i < parts.length; i++) {
        const p = parts[i];
        if (s <= p.s1) return p.s1 - p.s0 > 1e-9 ? p.t0 + (s - p.s0) / (p.s1 - p.s0) * (p.t1 - p.t0) : p.t0;
        if (!parts[i + 1] || s < parts[i + 1].s0) return p.t1;
    }
    return parts[parts.length - 1].t1;
}

function respan() {
    ed.span = ed.keys ? playSpan(ed.kind, runsOf(ed.keys), true) : [0, LAST];
    ed.layout = clipLayout();
    const none = ed.span[1] - ed.span[0] <= 0;
    $("ed-kind-note").textContent = !ed.keys || !none ? ""
        : ed.kind === "In" ? "Nothing moves at the start of this curve, so there's no In to play."
        : "Nothing moves at the end of this curve, so there's no Out to play.";
}

// ----------------------------------------------------------- the head card

function drawBase() {
    const select = $("ed-base");
    // Only when the presets change: rebuilt on every state, an open list closed.
    const sig = presets.map(p => `${p.id}\t${p.label}`).join("\n");
    if (select.dataset.sig === sig) {
        select.value = ed.id;
        return;
    }
    select.dataset.sig = sig;
    select.replaceChildren(...packs.map(pack => {
        const list = presets.filter(p => p.pack === pack.id);
        if (!list.length) return null;
        const group = el("optgroup", {label: pack.id});
        group.append(...list.map(p => el("option", {value: p.id, text: p.label, translate: "no"})));
        return group;
    }).filter(Boolean));
    select.value = ed.id;
}

$("ed-base").addEventListener("change", e => loadPreset(e.target.value));
$("ed-name").addEventListener("input", e => { ed.label = e.target.value; ed.dirty = true; drawButtons(); });
$("ed-dur").addEventListener("change", e => {
    const dur = clamp(Number(e.target.value) || ed.dur, 0.2, 10);
    if (dur !== ed.dur) { snapshot(); ed.dur = dur; changed(); }
    e.target.value = ed.dur;
});

function drawKinds() {
    const list = EDIT_KINDS.includes(ed.kind) ? EDIT_KINDS : [...EDIT_KINDS, ed.kind];
    $("ed-kind").replaceChildren(...list.map(k => el("button", {
        type: "button", "aria-pressed": String(ed.kind === k),
        onclick: () => {
            if (ed.kind === k) return;
            snapshot();
            ed.kind = k;
            restart();          // from the start of the part that now plays
            changed();
        },
    }, [k])));
}

function payload() {
    return {label: ($("ed-name").value || "").trim() || "Untitled", kind: ed.kind, dur: ed.dur, keys: ed.keys};
}

$("ed-saveas").onclick = () => send("save", payload());
$("ed-update").onclick = () => send("save", {...payload(), id: ed.id});
$("ed-apply").onclick = () => send("apply_draft", payload());
$("ed-revert").onclick = () => loadPreset(ed.id);
$("ed-delete").onclick = async () => {
    const p = presets.find(x => x.id === ed.id);
    if (!p || !(await Buddy.confirm({title: `Delete ${p.label}?`, text: "It goes from Previews and Favorites. Clips it's already on keep their move.",
                                     ok: "Delete", danger: true}))) return;
    ed.dirty = false;
    send("delete", {id: ed.id});
};

function drawButtons() {
    $("ed-delete").hidden = !ed.saved;
    $("ed-update").hidden = !ed.saved;
    $("ed-update").disabled = !ed.dirty || !ed.keys;
    $("ed-saveas").disabled = !ed.keys;
    $("ed-revert").disabled = !ed.dirty;
    $("ed-undo").disabled = !ed.undo.length;
    $("ed-redo").disabled = !ed.redo.length;
    const keys = ed.keys && ed.keys[ed.ch];
}

// ---------------------------------------------------------------- viewer

function drawViewer() {
    if (!ed.viewer) {
        const obj = svg("g");
        $("ed-stage").replaceChildren(stage(obj));
        ed.viewer = {p: {at: null, dur: 1}, obj, head: null};
        reshape();
    }
    $("ed-play").replaceChildren(Buddy.icon(ed.playing ? "pause" : "play"));
}

function editorTick(now) {
    if (!ed.keys) return;
    if (ed.playing) {
        // Laid on a clip: its whole length, as it'll play there. On the
        // preset's own time: only the part the kind puts on clips.
        const speed = Number(state.speed) || 1;
        const fps = Number(state.selection?.fps) || 24;
        const [from, length, rate] = ed.layout ? [0, ed.layout.total, 1]
            : [ed.span[0] / LAST * ed.dur, (ed.span[1] - ed.span[0]) / LAST * ed.dur / speed, speed];
        const local = length > 0 ? ((now - ed.started) / 1000) % (length + PAUSE) : 0;
        const shown = Math.min(length, Math.floor(local * fps) / fps);
        ed.head = from + shown * rate;
    }
    ed.head = clamp(ed.head, 0, axisLength());
    ed.viewer.p.at = atKeys(ed.keys);
    pose(ed.viewer, sampleAt(ed.head));
    $("ed-time").textContent = `${ed.head.toFixed(2)}s / ${axisLength().toFixed(2)}s`;
    // The playhead's frame, as the time bar counts them, of the last one.
    const fps = Number(state.selection?.fps) || 24;
    const frame = `Frame ${Math.round(ed.head * fps)} / ${Math.round(axisLength() * fps)}`;
    if ($("ed-frame").textContent !== frame) $("ed-frame").textContent = frame;
    placeHead();
}

$("ed-play").onclick = () => {
    ed.playing = !ed.playing;
    if (ed.playing) {
        let into = ed.head;                         // seconds into the loop
        if (!ed.layout) {
            const a = ed.span[0] / LAST * ed.dur, b = ed.span[1] / LAST * ed.dur;
            into = ed.head >= a && ed.head <= b ? (ed.head - a) / (Number(state.speed) || 1) : 0;
        }
        ed.started = performance.now() - into * 1000;
    }
    drawViewer();
};

// ----------------------------------------------------------------- graph

const graph = $("ed-graph");
let W = 600, H = 340;

// The time across the graph: the preset's own seconds, or the selected
// clip's with the preset laid on it (clipLayout). X takes a preset sample -
// null where the clip doesn't show it - and T gives one back.
const axisLength = () => ed.layout ? ed.layout.total : ed.dur;
const XS = s => PAD.l + s / axisLength() * (W - PAD.l - PAD.r);
const SX = x => (x - PAD.l) / (W - PAD.l - PAD.r) * axisLength();
const secondsOf = t => ed.layout ? onClip(ed.layout, t) : t / LAST * ed.dur;
const sampleAt = sec => ed.layout ? fromClip(ed.layout, sec) : sec / ed.dur * LAST;
const X = t => { const sec = secondsOf(t); return sec === null ? null : XS(sec); };
const T = x => sampleAt(clamp(SX(x), 0, axisLength()));
// Value: the edited channel's, or c's - each shown channel on its own range.
const rangeOf = c => c === ed.ch ? ed.range : ed.ranges[c];
const Y = (v, c = ed.ch) => { const [lo, hi] = rangeOf(c); return PAD.t + (hi - v) / (hi - lo) * (H - PAD.t - PAD.b); };
const V = (y, c = ed.ch) => { const [lo, hi] = rangeOf(c); return hi - (y - PAD.t) / (H - PAD.t - PAD.b) * (hi - lo); };

function niceStep(span, count) {
    const raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    return [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
}

// A whole number of frames between the time bar's marks, about count of them.
function frameStep(frames, count) {
    const raw = frames / count, mag = Math.pow(10, Math.max(0, Math.floor(Math.log10(Math.max(raw, 1)))));
    return [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
}

function pathOf(keys, y) {
    if (ed.layout) return clipPath(keys, y);
    let d = `M${X(keys[0][0])} ${y(keys[0][1])}`;
    for (let i = 0; i + 1 < keys.length; i++) {
        const p = controls(keys[i], keys[i + 1]);
        d += ` C${X(p[1][0])} ${y(p[1][1])} ${X(p[2][0])} ${y(p[2][1])} ${X(p[3][0])} ${y(p[3][1])}`;
    }
    return d;
}

/* A channel laid on the clip: each move traced along its seconds, the
   holds between them straight, as Fusion draws a spline with no keys there. */
function clipPath(keys, y) {
    const L = ed.layout, points = [];
    const first = L.parts[0], last = L.parts[L.parts.length - 1];
    if (first.s0 > 0) points.push([0, evalKeys(keys, first.t0)]);
    for (const p of L.parts) {
        for (let i = 0; i <= 48; i++) {
            points.push([p.s0 + (p.s1 - p.s0) * i / 48, evalKeys(keys, p.t0 + (p.t1 - p.t0) * i / 48)]);
        }
    }
    if (last.s1 < L.total) points.push([L.total, evalKeys(keys, last.t1)]);
    return "M" + points.map(([sec, v]) => `${XS(sec)} ${y(v)}`).join(" L");
}

const still = c => ed.keys[c].every(k => Math.abs(k[1] - REST[c]) < 1e-6 && (!k[2] || Math.abs(k[2][1] - REST[c]) < 1e-6)
                                       && (!k[3] || Math.abs(k[3][1] - REST[c]) < 1e-6));

function placeHead() {
    const x = XS(ed.head);
    const line = graph.querySelector(".ed-head-line"), knob = graph.querySelector(".ed-head-knob");
    if (line) { line.setAttribute("x1", x); line.setAttribute("x2", x); }
    if (knob) knob.setAttribute("transform", `translate(${x} 0)`);
}

function drawGraph() {
    W = graph.clientWidth || W;
    H = graph.clientHeight || H;
    graph.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const kids = [];
    if (!ed.keys) {
        const t = svg("text", {class: "ed-axis", x: W / 2, y: H / 2, "text-anchor": "middle"});
        t.textContent = "Reading the curves…";
        graph.replaceChildren(t);
        $("ed-keymenu").hidden = true;
        return;
    }
    graph.style.setProperty("--ch", `var(--ch-${ed.ch})`);
    // The time bar: seconds, and the playhead's knob. Dragging here - and
    // only here - moves the playhead.
    kids.push(svg("rect", {class: "ed-bar", x: PAD.l, y: BAR.top, width: W - PAD.l - PAD.r, height: BAR.height,
                           rx: 4, "data-bar": "1"}));
    // Keys the clip doesn't show (a part the kind leaves out) aren't drawn, or selectable.
    for (const c of Object.keys(ed.sels)) {
        const keep = ed.on.includes(c) ? ed.sels[c].filter(i => ed.keys[c][i] && X(ed.keys[c][i][0]) !== null) : [];
        if (keep.length) ed.sels[c] = keep;
        else delete ed.sels[c];
    }
    if (!ed.sel.length && selChannels().length) { activate(selChannels()[0]); drawChannels(); }
    // Time in frames, at the timeline's frame rate: on a clip, its first frame to its last.
    const fps = Number(state.selection?.fps) || 24;
    const frames = axisLength() * fps;
    const fStep = frameStep(frames, 8);
    for (let f = 0; f <= frames + 1e-6; f += fStep) {
        const x = XS(f / fps);
        kids.push(svg("line", {class: "ed-grid", x1: x, y1: PAD.t, x2: x, y2: H - PAD.b}));
        kids.push(svg("line", {class: "ed-tick", x1: x, y1: BAR.top + BAR.height - 5, x2: x, y2: BAR.top + BAR.height}));
        const label = svg("text", {class: "ed-axis ed-bar-label", x: x + 3, y: BAR.top + 14});
        label.textContent = String(f);
        kids.push(label);
    }
    // Value: the channel's own units up the side.
    const vStep = niceStep(ed.range[1] - ed.range[0], 6);
    for (let v = Math.ceil(ed.range[0] / vStep) * vStep; v <= ed.range[1]; v += vStep) {
        const y = Y(v);
        kids.push(svg("line", {class: "ed-grid", x1: PAD.l, y1: y, x2: W - PAD.r, y2: y}));
        const label = svg("text", {class: `ed-axis${ed.on.length > 1 ? " ed-value" : ""}`, x: PAD.l - 6, y: y + 3, "text-anchor": "end"});
        label.textContent = SHOW[ed.ch](Math.abs(v) < 1e-9 ? 0 : v);
        kids.push(label);
    }
    kids.push(svg("line", {class: "ed-rest", x1: PAD.l, y1: Y(REST[ed.ch]), x2: W - PAD.r, y2: Y(REST[ed.ch])}));
    // The channels not shown that move: muted and faint behind, in their colours, each fitted to the height.
    for (const [c] of CHANNELS) {
        if (ed.on.includes(c) || still(c)) continue;
        const vals = ed.keys[c].flatMap(k => [k[1], k[2] && k[2][1], k[3] && k[3][1]]).filter(v => v != null);
        const lo = Math.min(...vals), hi = Math.max(...vals), span = Math.max(hi - lo, 1e-6);
        const y = v => PAD.t + 6 + (hi - v) / span * (H - PAD.t - PAD.b - 12);
        kids.push(svg("path", {class: "ed-ghost", d: pathOf(ed.keys[c], y), style: `--ch: var(--ch-${c})`}));
    }
    // The other channels shown, in their colours, on their own ranges: a
    // press on one of their keys or curves edits that channel.
    for (const c of ed.on) {
        if (c === ed.ch) continue;
        const y = v => Y(v, c), style = `--ch: var(--ch-${c})`, d = pathOf(ed.keys[c], y);
        kids.push(svg("path", {class: "ed-curve other", d, style}));
        kids.push(svg("path", {class: "ed-hit", d, "data-ch": c}));
        ed.keys[c].forEach((key, i) => X(key[0]) !== null && kids.push(svg("circle", {
            class: `ed-key other${(ed.sels[c] || []).includes(i) ? " sel" : ""}`, cx: X(key[0]), cy: y(key[1]), r: 4.5,
            "data-key": i, "data-ch": c, style,
        })));
    }
    const keys = ed.keys[ed.ch];
    kids.push(svg("path", {class: "ed-curve", d: pathOf(keys, Y)}));
    if (ed.snap !== null && X(ed.snap) !== null) {
        kids.push(svg("line", {class: "ed-snap", x1: X(ed.snap), y1: PAD.t, x2: X(ed.snap), y2: H - PAD.b}));
    }
    // What the kind leaves out - an In's Out, an Out's In - dimmed.
    const [a, b] = ed.layout ? [0, LAST] : ed.span;      // on a clip, what it leaves out isn't drawn at all
    for (const [from, to] of [[0, a], [b, LAST]]) {
        if (to - from <= 0) continue;
        kids.push(svg("rect", {class: "ed-unused", x: X(from), y: PAD.t, width: X(to) - X(from), height: H - PAD.t - PAD.b}));
    }
    kids.push(svg("line", {class: "ed-head-line", x1: XS(ed.head), y1: BAR.top, x2: XS(ed.head), y2: H - PAD.b}));
    kids.push(svg("g", {class: "ed-head-knob", transform: `translate(${XS(ed.head)} 0)`}, [
        svg("path", {d: `M-6 ${BAR.top + 1} h12 v9 l-6 6 l-6 -6 z`}),
    ]));
    const one = single(), k = keys[one];
    if (k) {
        for (const [side, h] of [["lh", k[2]], ["rh", k[3]]]) {
            if (!h || X(h[0]) === null) continue;
            kids.push(svg("line", {class: "ed-arm", x1: X(k[0]), y1: Y(k[1]), x2: X(h[0]), y2: Y(h[1])}));
            kids.push(svg("circle", {class: "ed-hand", cx: X(h[0]), cy: Y(h[1]), r: 4.5, "data-hand": side, "data-i": one}));
        }
    }
    keys.forEach((key, i) => X(key[0]) !== null && kids.push(svg("circle", {
        class: `ed-key${ed.sel.includes(i) ? " sel" : ""}`, cx: X(key[0]), cy: Y(key[1]), r: 5.5, "data-key": i, "data-ch": ed.ch,
    })));
    const box = ed.drag && ed.drag.kind === "box" && ed.drag.rect;
    if (box) kids.push(svg("rect", {class: "ed-box", ...box}));
    graph.replaceChildren(...kids);
    drawKeyMenu();
}

/* Clip or Preset: shown while exactly one clip that takes a preset is selected. */
function drawView() {
    const clip = state.selection && state.selection.clip;
    const host = $("ed-view");
    host.hidden = !(clip && clip.frames > 1);
    if (host.hidden) return;
    const fps = Number(state.selection.fps) || 24;
    const items = [
        ["clip", `Clip ${(clip.frames / fps).toFixed(1)}s`, `Laid out at ${clip.name}'s length, as it'll play there`],
        ["preset", `Preset ${ed.dur.toFixed(1)}s`, "The preset's own timing"],
    ];
    host.replaceChildren(...items.map(([view, label, title]) => el("button", {
        type: "button", "aria-pressed": String(ed.view === view), title,
        onclick: () => {
            if (ed.view === view) return;
            ed.view = view;
            clearSel();
            ed.head = 0;
            restart();
            drawEditor();
        },
    }, [label])));
}

/* The channel buttons: each shows its channel or hides it, any number at
   once, the one turned on edited. Ctrl-click shows that one alone; a
   right-click picks its colour. */
function drawChannels() {
    $("ed-channels").replaceChildren(...CHANNELS.map(([c, label]) => el(
        `button${ed.keys && still(c) ? ".ed-still" : ""}${ed.on.length > 1 && ed.ch === c ? ".ed-editing" : ""}`, {
            type: "button", "aria-pressed": String(ed.on.includes(c)), style: `--ch: var(--ch-${c})`,
            title: ed.keys && still(c)
                ? `${label} doesn't move – drag its line to make it. Click to show or hide it, Ctrl-click to show it alone, right-click for its colour`
                : `${label}. Click to show or hide it, Ctrl-click to show it alone, right-click for its colour`,
            onclick: e => showChannel(c, e.ctrlKey || e.metaKey),
            oncontextmenu: e => { e.preventDefault(); colourMenu(c, label, e.clientX, e.clientY); },
        }, [el("span.ed-dot"), label])));
}

function showChannel(c, alone) {
    if (!ed.keys) return;
    if (alone) {
        ed.on = [c];
        ed.sels = {[c]: ed.sels[c] || []};          // a hidden channel's keys aren't selected
    } else if (!ed.on.includes(c)) {
        ed.on = CHANNELS.map(([k]) => k).filter(k => k === c || ed.on.includes(k));
    } else {
        if (ed.on.length === 1) return;             // one stays shown
        ed.on = ed.on.filter(k => k !== c);
        delete ed.sels[c];
        if (ed.ch === c) activate(selChannels()[0] || ed.on[0]);
        return drawEditor();
    }
    activate(c);                                    // turned on: it's the one edited
    drawEditor();
}

// The colours on offer for a channel, besides its own and one picked freely.
const SWATCHES = [["Red", "#ff5a5f"], ["Orange", "#ff8a3d"], ["Amber", "#ffb21e"], ["Lime", "#a3e635"],
                  ["Green", "#3ddc84"], ["Teal", "#2dd4bf"], ["Sky", "#38bdf8"], ["Blue", "#3ea6ff"],
                  ["Purple", "#d07bff"], ["Pink", "#ff6fb5"]];

function colourMenu(c, label, x, y) {
    const now = (state.colors || {})[c];
    Buddy.menu({x, y, items: [
        {heading: `${label} colour`},
        ...SWATCHES.map(([name, hex]) => ({label: name, swatch: hex, checked: now === hex, onClick: () => setColour(c, hex)})),
        {sep: true},
        {label: "Custom colour…", onClick: () => pickColour(c)},
        {label: "Default colour", disabled: !now, onClick: () => setColour(c, null)},
    ]});
}

// The system's colour picker, through an unseen colour field: a picker left
// open from before is taken away first.
function pickColour(c) {
    for (const old of document.querySelectorAll("input.ed-picker")) old.remove();
    const input = el("input.ed-picker", {type: "color", value: (state.colors || {})[c] ||
        getComputedStyle($("panel-editor")).getPropertyValue(`--ch-${c}`).trim() || "#ffffff"});
    input.style.cssText = "position:fixed;opacity:0;pointer-events:none;left:50%;top:50%;width:1px;height:1px";
    input.oninput = () => paintColour(c, input.value);
    input.onchange = () => { setColour(c, input.value); input.remove(); };
    document.body.append(input);
    input.click();
}

function paintColour(c, hex) {
    const panel = $("panel-editor");
    if (hex) panel.style.setProperty(`--ch-${c}`, hex);
    else panel.style.removeProperty(`--ch-${c}`);
}

function setColour(c, hex) {
    state.colors = {...(state.colors || {}), [c]: hex};
    if (!hex) delete state.colors[c];
    paintColours();
    send("color", {channel: c, color: hex});
}

/* The colours picked for channels, over the defaults (animation.css). */
function paintColours() {
    for (const [c] of CHANNELS) paintColour(c, (state.colors || {})[c]);
}

function drawEditor() {
    if ($("panel-editor").hidden) return;
    drawBase();
    drawKinds();
    if (document.activeElement !== $("ed-name")) $("ed-name").value = ed.label;
    if (document.activeElement !== $("ed-dur")) $("ed-dur").value = ed.dur;
    respan();
    drawViewer();
    drawView();
    drawChannels();
    drawGraph();
    drawButtons();
    drawSelection();
}

new ResizeObserver(() => { if (!$("panel-editor").hidden) drawGraph(); }).observe($("ed-graph-wrap"));

// ------------------------------------------------------------- dragging

function point(e) {
    const r = graph.getBoundingClientRect();
    return [(e.clientX - r.left) * W / r.width, (e.clientY - r.top) * H / r.height];
}

// A double-click is told apart here, not by "dblclick": the graph is drawn
// afresh on every press, so the two clicks never land on the same element.
let lastPress = {at: 0, x: -99, y: -99};

function scrub(x) {
    ed.playing = false;
    ed.head = clamp(SX(x), 0, axisLength());
    drawViewer();
    placeHead();
}

graph.addEventListener("pointerdown", e => {
    if (!ed.keys || e.button !== 0) return;
    graph.focus();
    const hit = e.target.dataset || {};
    const [px, py] = point(e);
    graph.setPointerCapture(e.pointerId);
    if (py < PAD.t - 4) {                               // the time bar
        ed.drag = {kind: "head"};
        scrub(px);
        return;
    }
    // A key or the curve of another channel shown: that one's edited now.
    if (hit.ch && hit.ch !== ed.ch) {
        activate(hit.ch);
        drawChannels();
    }
    const double = e.timeStamp - lastPress.at < 400 && Math.hypot(px - lastPress.x, py - lastPress.y) < 6;
    lastPress = {at: double ? 0 : e.timeStamp, x: px, y: py};
    if (double && hit.key === undefined && !hit.hand) {
        ed.drag = null;
        addKey(T(px));
        return;
    }
    // Ctrl (Cmd on a Mac) or Shift: a click adds a key or takes it out, a box adds -
    // keys of any channel shown, to move together. Shift on a key also drags
    // it straight away, in time only: a Shift-click that never moves takes an
    // already selected key out when it lets go.
    const adding = e.shiftKey || e.ctrlKey || e.metaKey;
    const keysDrag = extra => ({kind: "keys", x0: px, y0: py, orig: clone(ed.keys), moved: false, grab: Number(hit.key), ...extra});
    if (hit.key !== undefined) {
        const i = Number(hit.key);
        if (e.shiftKey && !e.ctrlKey && !e.metaKey) {
            const was = ed.sel.includes(i);
            if (!was) ed.sel = [...ed.sel, i].sort((a, b) => a - b);
            ed.drag = keysDrag({untick: was ? i : null});
        } else if (adding) {
            ed.sel = ed.sel.includes(i) ? ed.sel.filter(j => j !== i) : [...ed.sel, i].sort((a, b) => a - b);
            // Its last key taken out: the channel edited is one still with keys selected.
            if (!ed.sel.length && selChannels().length) { activate(selChannels()[0]); drawChannels(); }
            ed.drag = null;
        } else {
            if (!ed.sel.includes(i)) { clearSel(); ed.sel = [i]; }
            ed.drag = keysDrag({});
        }
    } else if (hit.hand) {
        ed.drag = {kind: "hand", i: Number(hit.i), side: hit.hand, moved: false};
    } else {                                            // a box around the keys to select
        ed.drag = {kind: "box", x0: px, y0: py, add: adding, before: clone(ed.sels), rect: null};
        if (!adding) clearSel();
    }
    drawGraph();
    drawButtons();
});

graph.addEventListener("pointermove", e => {
    const d = ed.drag;
    if (!d) return;
    const [x, y] = point(e);
    if (d.kind === "head") return scrub(x);
    if (d.kind === "box") {
        const x0 = Math.min(d.x0, x), x1 = Math.max(d.x0, x), y0 = Math.min(d.y0, y), y1 = Math.max(d.y0, y);
        d.rect = {x: x0, y: y0, width: x1 - x0, height: y1 - y0};
        const within = c => ed.keys[c].map((k, i) => [X(k[0]), Y(k[1], c), i])
            .filter(([kx, ky]) => kx !== null && kx >= x0 && kx <= x1 && ky >= y0 && ky <= y1).map(([, , i]) => i);
        // The keys in the box, of every channel shown.
        ed.sels = {};
        for (const c of ed.on) {
            const keys = [...new Set([...(d.add ? d.before[c] || [] : []), ...within(c)])].sort((a, b) => a - b);
            if (keys.length) ed.sels[c] = keys;
        }
        // The edited channel is one with keys in it, if any are.
        if (!ed.sel.length && selChannels().length) { activate(selChannels()[0]); drawChannels(); }
        drawGraph();
        drawButtons();
        return;
    }
    // A click's wobble isn't a drag: keys move once the pointer has come 3 px.
    if (!d.moved && d.kind === "keys" && Math.hypot(x - d.x0, y - d.y0) < 3) return;
    if (!d.moved) { snapshot(); d.moved = true; ed.dirty = true; }
    if (d.kind === "keys") moveKeys(d, x, y, e.shiftKey, e.ctrlKey || e.metaKey);
    else moveHandle(d, x, y, e.altKey);
    drawGraph();
});

/* The selected keys, together, by how far the pointer has come - from where
   they were when the drag began, so nothing drifts. None passes a key that
   isn't moving, the first and last stay on their frames. Shift keeps them
   all at their values: they slide along in time only. Keys of several
   channels move by the same time, and up or down by the same distance on
   the graph - each in its own channel's units. The key dragged snaps to the
   time of another shown channel's key, one not moving, it comes within 6 px
   of - unless Ctrl is held. */
function moveKeys(d, x, y, lockValue, noSnap) {
    const chans = selChannels();
    let pinnedEnd = false, lo = -Infinity, hi = Infinity;
    for (const c of chans) {
        const orig = d.orig[c], n = orig.length, picked = new Set(ed.sels[c]);
        if (picked.has(0) || picked.has(n - 1)) pinnedEnd = true;
        for (const i of picked) {
            if (i > 0 && !picked.has(i - 1)) lo = Math.max(lo, orig[i - 1][0] + 0.05 - orig[i][0]);
            if (i < n - 1 && !picked.has(i + 1)) hi = Math.min(hi, orig[i + 1][0] - 0.05 - orig[i][0]);
        }
    }
    let dt = pinnedEnd ? 0 : T(x) - T(d.x0);
    dt = clamp(dt, Math.min(lo, 0), Math.max(hi, 0));
    ed.snap = null;
    const grabbed = d.orig[ed.ch][d.grab];
    const at = grabbed ? X(grabbed[0] + dt) : null;
    if (!noSnap && !pinnedEnd && at !== null) {
        let best = null;
        for (const c of ed.on) {
            if (c === ed.ch) continue;
            const moving = new Set(ed.sels[c] || []);
            ed.keys[c].forEach((k, i) => {
                if (moving.has(i)) return;
                const kx = X(k[0]), off = kx === null ? Infinity : Math.abs(kx - at);
                const shift = k[0] - grabbed[0];
                if (off < 6 && shift >= Math.min(lo, 0) && shift <= Math.max(hi, 0) && (!best || off < best[0])) best = [off, k[0], shift];
            });
        }
        if (best) { dt = best[2]; ed.snap = best[1]; }
    }
    for (const c of chans) {
        const keys = clone(d.orig[c]);
        const dv = lockValue ? 0 : V(y, c) - V(d.y0, c);
        for (const i of ed.sels[c]) {
            const k = keys[i];
            k[0] += dt;
            k[1] += dv;
            for (const h of [k[2], k[3]]) if (h) { h[0] += dt; h[1] += dv; }
        }
        // Every handle stays on its own side, inside its neighbours.
        keys.forEach((k, i) => {
            const prev = keys[i - 1], next = keys[i + 1];
            if (k[2]) k[2][0] = clamp(k[2][0], prev ? prev[0] : k[0], k[0]);
            if (k[3]) k[3][0] = clamp(k[3][0], k[0], next ? next[0] : k[0]);
        });
        ed.keys[c] = keys;
    }
}

function moveHandle(d, x, y, broken) {
    const keys = ed.keys[ed.ch], k = keys[d.i];
    const prev = keys[d.i - 1], next = keys[d.i + 1];
    const own = d.side === "lh" ? 2 : 3, other = 5 - own;
    const lo = own === 2 ? (prev ? prev[0] : k[0]) : k[0];
    const hi = own === 2 ? k[0] : (next ? next[0] : k[0]);
    const h = [clamp(T(x), lo, hi), V(y)];
    k[own] = h;
    // Its partner turns with it - smooth through the key - unless Alt breaks
    // them. Lengths compared as the graph shows them.
    if (!broken && k[other]) {
        const sx = LAST, sy = ed.range[1] - ed.range[0];
        const dx = (h[0] - k[0]) / sx, dy = (h[1] - k[1]) / sy, len = Math.hypot(dx, dy);
        const o = k[other], olen = Math.hypot((o[0] - k[0]) / sx, (o[1] - k[1]) / sy);
        if (len > 1e-9) {
            const olo = other === 2 ? (prev ? prev[0] : k[0]) : k[0];
            const ohi = other === 2 ? k[0] : (next ? next[0] : k[0]);
            k[other] = [clamp(k[0] - dx / len * olen * sx, olo, ohi), k[1] - dy / len * olen * sy];
        }
    }
}

function endDrag() {
    const d = ed.drag;
    ed.drag = null;
    ed.snap = null;
    if (!d) return;
    // A Shift-click on a selected key, never dragged: it comes out.
    if (!d.moved && d.untick != null) {
        ed.sel = ed.sel.filter(j => j !== d.untick);
        if (!ed.sel.length && selChannels().length) { activate(selChannels()[0]); drawChannels(); }
        drawButtons();
    }
    // A click without a drag still ends one: the menu, hidden while the
    // pointer was down, comes back for what's selected now.
    if (d.kind === "box" || !d.moved) return drawGraph();
    // Dragged past the graph's edge: refit it to show the whole curve.
    const moved = d.kind === "keys" ? selChannels().map(c => [c, ed.sels[c]]) : [[ed.ch, [d.i]]];
    const outside = moved.some(([c, sel]) => sel.some(i => {
        const k = ed.keys[c][i], [lo, hi] = rangeOf(c);
        return [k[1], k[2] && k[2][1], k[3] && k[3][1]].some(v => v != null && (v < lo || v > hi));
    }));
    if (outside) fitRange();
    changed();
}
graph.addEventListener("pointerup", endDrag);
graph.addEventListener("pointercancel", endDrag);

function addKey(t) {
    t = clamp(t, 0, LAST);
    if (t <= 0.05 || t >= LAST - 0.05) return;
    snapshot();
    const at = splitAt(ed.keys[ed.ch], t);
    clearSel();
    ed.sel = [at];
    changed();
}

/* The selected keys, channel by channel: [keys, their selected indexes]. */
const eachSelected = () => selChannels().map(c => [ed.keys[c], ed.sels[c]]);

/* Takes out the selected keys, of every channel - never a curve's end keys. */
function removeKeys() {
    if (!ed.keys) return;
    const gone = eachSelected().map(([keys, sel]) => [keys, sel.filter(i => i > 0 && i < keys.length - 1)]);
    if (!gone.some(([, sel]) => sel.length)) return;
    snapshot();
    for (const [keys, sel] of gone) for (const i of [...sel].sort((a, b) => b - a)) keys.splice(i, 1);
    clearSel();
    changed();
}

function ease(kind) {
    if (!ed.keys || !selCount()) return;
    snapshot();
    for (const [keys, sel] of eachSelected()) for (const i of sel) {
        const k = keys[i], prev = keys[i - 1], next = keys[i + 1];
        const left = prev ? (k[0] - prev[0]) / 3 : 0, right = next ? (next[0] - k[0]) / 3 : 0;
        if (kind === "linear") {
            k[2] = prev ? [k[0] - left, k[1] + (prev[1] - k[1]) / 3] : null;
            k[3] = next ? [k[0] + right, k[1] + (next[1] - k[1]) / 3] : null;
        } else {
            const slope = kind === "smooth" && prev && next ? (next[1] - prev[1]) / (next[0] - prev[0]) : 0;
            k[2] = prev ? [k[0] - left, k[1] - slope * left] : null;
            k[3] = next ? [k[0] + right, k[1] + slope * right] : null;
        }
    }
    changed();
}

// --------------------------------------------------------------- key menu
// A small bar beside the selected keys: one key gets its own ease, moving to
// the playhead and copying its ease; several also loop and reverse.

const MENU_GLYPHS = {
    smooth: '<path d="M3 18C9 18 15 6 21 6"/><path d="M6.5 16l11-8" opacity=".55"/><circle cx="12" cy="12" r="1.8"/>',
    flat: '<path d="M3 18c5 0 5-8 9-8s4 8 9 8"/><path d="M5.5 10h13" opacity=".55"/><circle cx="12" cy="10" r="1.8"/>',
    linear: '<path d="M3 18l9-10 9 10"/><circle cx="12" cy="8" r="1.8"/>',
    toHead: '<path d="M16 4v16"/><path d="M3 12h9M9 9l3 3-3 3"/>',
    endToHead: '<path d="M8 4v16"/><path d="M21 12h-9M15 9l-3 3 3 3"/>',
    paste: '<rect x="5.5" y="5" width="13" height="16" rx="2"/><path d="M9 5V3.5h6V5"/><path d="M8.5 15c2.2 0 2.2-4 3.5-4s1.3 4 3.5 4"/>',
    unease: '<path d="M3 18l9-10 9 10"/><path d="M4 4l16 16"/>',
    grip: '<circle cx="9" cy="6" r="1.3"/><circle cx="15" cy="6" r="1.3"/><circle cx="9" cy="12" r="1.3"/>' +
          '<circle cx="15" cy="12" r="1.3"/><circle cx="9" cy="18" r="1.3"/><circle cx="15" cy="18" r="1.3"/>',
    loopEven: '<path d="M17 3l3 3-3 3"/><path d="M4 11V9.5A3.5 3.5 0 0 1 7.5 6H20"/><path d="M7 21l-3-3 3-3"/>' +
              '<path d="M20 13v1.5a3.5 3.5 0 0 1-3.5 3.5H4"/><path d="M9.5 10.5h5M9.5 13.5h5"/>',
    loop: '<path d="M17 3l3 3-3 3"/><path d="M4 11V9.5A3.5 3.5 0 0 1 7.5 6H20"/><path d="M7 21l-3-3 3-3"/><path d="M20 13v1.5a3.5 3.5 0 0 1-3.5 3.5H4"/>',
};

function menuIcon(name) {
    if (!MENU_GLYPHS[name]) return Buddy.icon(name);
    const node = svg("svg", {viewBox: "0 0 24 24", class: "icon-svg", "aria-hidden": "true"});
    node.innerHTML = MENU_GLYPHS[name];
    return node;
}

/* The selected keys' first and last index, and whether either end key of the
   curve (which never leaves its frame) is among them. */
function picked() {
    const keys = ed.keys[ed.ch];
    const lo = Math.min(...ed.sel), hi = Math.max(...ed.sel);
    return {keys, lo, hi, ends: lo === 0 || hi === keys.length - 1};
}

const pinned = "The first and last keys stay on their frames";

function drawKeyMenu() {
    const menu = $("ed-keymenu");
    if (!ed.keys || !ed.sel.length || (ed.drag && ed.drag.kind !== "head")) {
        menu.hidden = true;
        return;
    }
    const {keys, lo, hi} = picked();
    const one = selCount() === 1, cross = selChannels().length > 1;
    const ends = eachSelected().some(([k, sel]) => sel.includes(0) || sel.includes(k.length - 1));
    const inner = eachSelected().some(([k, sel]) => sel.some(i => i > 0 && i < k.length - 1));
    const oneChannel = "Loop, Loop evenly and Reverse work on one channel's keys at a time";
    const button = (icon, title, act, disabled) => el("button.ed-km-btn", {
        type: "button", title, "aria-label": title, disabled: !!disabled,
        onclick: () => act(),
    }, [menuIcon(icon)]);
    const gap = () => el("span.ed-km-sep");
    const shapes = [         // not "ease": that would hide ease(), which these call
        button("smooth", "Smooth", () => ease("smooth")),
        button("flat", "Flat", () => ease("flat")),
        button("linear", "Linear", () => ease("linear")),
    ];
    const parts = one ? [
        ...shapes, gap(),
        button("toHead", ends ? pinned : "Move to playhead", () => toHead("start"), ends),
        gap(),
        button("copy", "Copy ease", copyEase),
        button("paste", ed.ease ? "Paste ease" : "Copy a key's ease first", pasteEase, !ed.ease),
        button("unease", "Delete ease", deleteEase),
        gap(),
        button("trash", inner ? "Delete key" : pinned, removeKeys, !inner),
    ] : [
        ...shapes, gap(),
        button("toHead", ends ? pinned : "Move start to playhead", () => toHead("start"), ends),
        button("endToHead", ends ? pinned : "Move end to playhead", () => toHead("end"), ends),
        gap(),
        button("paste", ed.ease ? "Paste ease" : "Copy a key's ease first", pasteEase, !ed.ease),
        button("unease", "Delete ease", deleteEase),
        gap(),
        ...(cross ? [
            button("loop", oneChannel, loopKeys, true),
            button("loopEven", oneChannel, loopEvenly, true),
            button("swap", oneChannel, reverseKeys, true),
        ] : [
            button("loop", loopTimes() ? "Loop: repeat these keys to fill the space after them" : "No room after these keys to repeat them",
                   loopKeys, !loopTimes()),
            button("loopEven", evenPasses() ? "Loop evenly: repeat these keys a whole number of times, evened out to fill the space to the next key exactly"
                                            : "No key after these to loop up to", loopEvenly, !evenPasses()),
            button("swap", "Reverse order", reverseKeys, keys[hi][0] - keys[lo][0] <= 0),
        ]),
        gap(),
        button("trash", inner ? "Delete keys" : pinned, removeKeys, !inner),
    ];
    const grip = el("span.ed-km-grip", {title: "Drag to move this menu – double-click to put it back by the keys"},
                    [menuIcon("grip")]);
    menu.replaceChildren(grip, ...parts);
    menu.hidden = false;
    placeKeyMenu(menu);
}

/* Beside the selection, never over it: above, below, to the right or left of
   the selected keys (and the handles shown), else a corner of the graph -
   the first of those that covers none of them, and of those the one over the
   fewest other keys. Pushed back inside the graph, "above" can land on the
   very keys it's for, so every spot is checked where it really ends up. */
function placeKeyMenu(menu) {
    const w = menu.offsetWidth, h = menu.offsetHeight, near = 10, gap = 14;
    if (ed.menuAt) {                    // where it was dragged to, kept until a double-click on the grip
        menu.style.left = `${clamp(ed.menuAt[0], 0, W - w)}px`;
        menu.style.top = `${clamp(ed.menuAt[1], 0, H - h)}px`;
        return;
    }
    // The selected keys (and the handles shown) of every channel, and the
    // other keys of the channels shown, where they're drawn.
    const shown = p => p[0] !== null;
    const mine = [], others = [];
    for (const c of ed.on) {
        const at = p => [X(p[0]), Y(p[1], c)], sel = ed.sels[c] || [];
        ed.keys[c].forEach((k, i) => {
            if (!sel.includes(i)) return others.push(at(k));
            for (const p of c === ed.ch && single() === i ? [k, k[2], k[3]] : [k]) if (p) mine.push(at(p));
        });
    }
    for (const list of [mine, others]) list.splice(0, list.length, ...list.filter(shown));
    const xs = mine.map(p => p[0]), ys = mine.map(p => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const cx = (x0 + x1) / 2 - w / 2, cy = (y0 + y1) / 2 - h / 2;
    const spots = [
        [cx, y0 - h - gap], [cx, y1 + gap], [x1 + gap, cy], [x0 - w - gap, cy],
        [x1 + gap, y0 - h - gap], [x0 - w - gap, y0 - h - gap], [x1 + gap, y1 + gap], [x0 - w - gap, y1 + gap],
        [4, PAD.t], [W - w - 4, PAD.t], [4, H - h - 4], [W - w - 4, H - h - 4],
    ].map(([l, t]) => [clamp(l, 4, W - w - 4), clamp(t, PAD.t, H - h - 4)]);
    const under = ([l, t], points) => points.filter(([x, y]) =>
        x > l - near && x < l + w + near && y > t - near && y < t + h + near).length;
    const score = (spot, order) => under(spot, mine) * 1000 + under(spot, others) + order * 0.01;
    const best = spots.reduce((a, s, i) => score(s, i) < score(a.s, a.i) ? {s, i} : a, {s: spots[0], i: 0}).s;
    menu.style.left = `${best[0]}px`;
    menu.style.top = `${best[1]}px`;
}

// The grip: drag the menu anywhere over the graph and it stays there for
// every selection after; a double-click puts it back beside the keys. On the
// menu itself, not the grip - its buttons are drawn afresh, the menu isn't.
const keyMenu = $("ed-keymenu");
let menuDrag = null, gripPressed = 0;

keyMenu.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !e.target.closest(".ed-km-grip")) return;
    e.preventDefault();
    // A double-click told apart here: the drag's pointer capture sends the
    // second click to the menu, not the grip, so "dblclick" can't name it.
    if (e.timeStamp - gripPressed < 400) {
        gripPressed = 0;
        ed.menuAt = null;
        return drawKeyMenu();
    }
    gripPressed = e.timeStamp;
    menuDrag = {dx: e.clientX - keyMenu.offsetLeft, dy: e.clientY - keyMenu.offsetTop};
    keyMenu.setPointerCapture(e.pointerId);
    keyMenu.classList.add("moving");
});
keyMenu.addEventListener("pointermove", e => {
    if (!menuDrag) return;
    ed.menuAt = [clamp(e.clientX - menuDrag.dx, 0, W - keyMenu.offsetWidth),
                 clamp(e.clientY - menuDrag.dy, 0, H - keyMenu.offsetHeight)];
    keyMenu.style.left = `${ed.menuAt[0]}px`;
    keyMenu.style.top = `${ed.menuAt[1]}px`;
});
const dropMenu = () => { menuDrag = null; keyMenu.classList.remove("moving"); };
keyMenu.addEventListener("pointerup", dropMenu);
keyMenu.addEventListener("pointercancel", dropMenu);

/* Shifts the selected keys in time by dt: never past a key that isn't
   selected, never the curve's end keys. True when they moved all the way. */
function shiftKeys(dt) {
    const all = eachSelected();
    if (all.some(([keys, sel]) => sel.includes(0) || sel.includes(keys.length - 1))) return false;
    let lo = -Infinity, hi = Infinity;
    for (const [keys, sel] of all) {
        const chosen = new Set(sel);
        for (const i of chosen) {
            if (!chosen.has(i - 1)) lo = Math.max(lo, keys[i - 1][0] + 0.05 - keys[i][0]);
            if (!chosen.has(i + 1)) hi = Math.min(hi, keys[i + 1][0] - 0.05 - keys[i][0]);
        }
    }
    const moved = clamp(dt, Math.min(lo, 0), Math.max(hi, 0));
    if (Math.abs(moved) < 1e-9) return Math.abs(dt) < 1e-9;
    snapshot();
    for (const [keys, sel] of all) {
        for (const i of sel) {
            const k = keys[i];
            k[0] += moved;
            for (const h of [k[2], k[3]]) if (h) h[0] += moved;
        }
        tidyHandles(keys);
    }
    changed();
    return Math.abs(moved - dt) < 1e-6;
}

/* The selected keys - of every channel - moved so the first (or the last)
   of them lands on the playhead. */
function toHead(which) {
    const times = eachSelected().flatMap(([keys, sel]) => sel.map(i => keys[i][0]));
    const from = which === "end" ? Math.max(...times) : Math.min(...times);
    if (!shiftKeys(sampleAt(ed.head) - from)) Buddy.toast("They stop at the next key.");
}

/* Every handle on its own side of its key, inside its neighbours. */
function tidyHandles(keys) {
    keys.forEach((k, i) => {
        const prev = keys[i - 1], next = keys[i + 1];
        k[2] = prev && k[2] ? [clamp(k[2][0], prev[0], k[0]), k[2][1]] : null;
        k[3] = next && k[3] ? [clamp(k[3][0], k[0], next[0]), k[3][1]] : null;
    });
}

/* A key's ease as shares of the stretch to each neighbour - how far along
   in time its handle reaches, and how far towards the neighbour's value -
   so it fits any key it's pasted on, in any channel. */
function copyEase() {
    const keys = ed.keys[ed.ch], i = ed.sel[0], k = keys[i];
    const share = (h, n) => {
        if (!h || !n || Math.abs(n[0] - k[0]) < 1e-9) return null;
        const dv = n[1] - k[1];
        return [Math.abs((h[0] - k[0]) / (n[0] - k[0])), Math.abs(dv) > 1e-9 ? (h[1] - k[1]) / dv : 0];
    };
    ed.ease = {l: share(k[2], keys[i - 1]), r: share(k[3], keys[i + 1])};
    Buddy.toast("Copied the ease.");
    drawKeyMenu();
}

function pasteEase() {
    if (!ed.ease) return;
    snapshot();
    for (const [keys, sel] of eachSelected()) {
        for (const i of sel) {
            const k = keys[i], prev = keys[i - 1], next = keys[i + 1];
            const put = (s, n) => s && n ? [k[0] + (n[0] - k[0]) * s[0], k[1] + (n[1] - k[1]) * s[1]] : null;
            k[2] = put(ed.ease.l, prev);
            k[3] = put(ed.ease.r, next);
        }
        tidyHandles(keys);
    }
    changed();
}

function deleteEase() {
    snapshot();
    for (const [keys, sel] of eachSelected()) for (const i of sel) keys[i][2] = keys[i][3] = null;
    changed();
}

/* How many times the selected stretch (first to last selected key) fits in
   the room after it - up to the next key, or the curve's end. */
function loopTimes() {
    const {keys, lo, hi} = picked();
    const length = keys[hi][0] - keys[lo][0];
    if (length < 0.1 || hi === keys.length - 1) return 0;
    const room = keys[hi + 1][0] - 0.05 - keys[hi][0];
    return Math.max(0, Math.floor(room / length + 1e-9));
}

/* The selected stretch, repeated after itself as many times as it fits. Each
   copy starts where the last one ended, so the loop runs on unbroken. */
function loopKeys() {
    const times = loopTimes();
    if (!times) return;
    const {keys, lo, hi} = picked();
    const pattern = clone(keys.slice(lo, hi + 1));
    const length = pattern[pattern.length - 1][0] - pattern[0][0];
    const at = (h, off) => h ? [h[0] + off, h[1]] : null;
    snapshot();
    const copies = [];
    let joint = keys[hi];                       // where one pass ends and the next begins
    for (let j = 1; j <= times; j++) {
        const off = length * j;
        joint[3] = at(pattern[0][3], off);      // leaves the joint the way the stretch began
        for (let m = 1; m < pattern.length; m++) {
            const k = pattern[m];
            const copy = [k[0] + off, k[1], at(k[2], off), at(k[3], off)];
            copies.push(copy);
            joint = copy;
        }
    }
    joint[3] = at(pattern[pattern.length - 1][3], length * times);
    keys.splice(hi + 1, 0, ...copies);
    tidyHandles(keys);
    changed();
}

/* Loop evenly: how many passes of the selected stretch - itself the first -
   come nearest to filling it and the room after it, up to the next key.
   At least two; 0 with no key after the selection to loop up to. */
function evenPasses() {
    const {keys, lo, hi} = picked();
    const length = keys[hi][0] - keys[lo][0];
    if (length < 0.1 || hi === keys.length - 1) return 0;
    return Math.max(2, Math.round((keys[hi + 1][0] - keys[lo][0]) / length));
}

/* The selected stretch repeated a whole number of times, every pass - the
   first too - stretched or squeezed alike so the last ends exactly on the
   next key: no pass cut short, no leftover gap. Each starts where the one
   before ended; the last flows into the next key, arriving the way the
   stretch arrives at its own end. */
function loopEvenly() {
    const passes = evenPasses();
    if (!passes) return;
    const {keys, lo, hi} = picked();
    const t0 = keys[lo][0], next = keys[hi + 1];
    const period = (next[0] - t0) / passes, scale = period / (keys[hi][0] - t0);
    const fit = h => h ? [t0 + (h[0] - t0) * scale, h[1]] : null;
    const pattern = clone(keys.slice(lo, hi + 1)).map(k => [t0 + (k[0] - t0) * scale, k[1], fit(k[2]), fit(k[3])]);
    const at = (h, off) => h ? [h[0] + off, h[1]] : null;
    snapshot();
    const out = clone(pattern);
    for (let j = 1; j < passes; j++) {
        const off = period * j;
        out[out.length - 1][3] = at(pattern[0][3], off);       // leaves the joint the way the stretch began
        for (let m = 1; m < pattern.length; m++) {
            const k = pattern[m];
            out.push([k[0] + off, k[1], at(k[2], off), at(k[3], off)]);
        }
    }
    const end = out.pop();                                      // lands on the next key, which stays
    next[2] = end[2] ? [end[2][0], next[1] + (end[2][1] - end[1])] : null;
    keys.splice(lo, hi - lo + 1, ...out);
    ed.sel = ed.sel.filter(i => i <= hi);
    tidyHandles(keys);
    changed();
}

/* The selected stretch, played backwards: each key mirrored in time across
   it, its two handles swapped. */
function reverseKeys() {
    const {keys, lo, hi} = picked();
    const t0 = keys[lo][0], t1 = keys[hi][0];
    const mirror = h => h ? [t0 + t1 - h[0], h[1]] : null;
    snapshot();
    const part = keys.slice(lo, hi + 1).map(k => [t0 + t1 - k[0], k[1], mirror(k[3]), mirror(k[2])]).reverse();
    keys.splice(lo, hi - lo + 1, ...part);
    ed.sel = ed.sel.map(i => lo + hi - i).sort((a, b) => a - b);
    tidyHandles(keys);
    changed();
}

$("ed-fit").onclick = () => { if (ed.keys) { fitRange(); drawGraph(); } };
$("ed-undo").replaceChildren(Buddy.icon("undo"));
$("ed-redo").replaceChildren(Buddy.icon("undo"));
$("ed-redo").firstChild.style.transform = "scaleX(-1)";
$("ed-undo").onclick = () => step(ed.undo, ed.redo);
$("ed-redo").onclick = () => step(ed.redo, ed.undo);

document.addEventListener("keydown", e => {
    if ($("panel-editor").hidden || typing(e)) return;
    // Delete or Backspace takes the selected keys out wherever the focus is -
    // not only on the graph: a press on Smooth or the stage moved it off.
    if ((e.key === "Delete" || e.key === "Backspace") && ed.keys && selCount()) {
        e.preventDefault();
        return removeKeys();
    }
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); step(...(e.shiftKey ? [ed.redo, ed.undo] : [ed.undo, ed.redo])); }
    else if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); step(ed.redo, ed.undo); }
    else if (mod && e.key.toLowerCase() === "a" && e.target === graph && ed.keys) {
        e.preventDefault();
        ed.sels = Object.fromEntries(ed.on.map(c => [c, ed.keys[c].map((_k, i) => i)]));       // every shown channel's
        drawGraph();
        drawButtons();
    }
    // Space plays or pauses wherever the focus is on the tab, never also
    // pressing the button that has it.
    else if (e.key === " " && !typing(e)) { e.preventDefault(); if (!e.repeat) $("ed-play").click(); }
});
document.addEventListener("keyup", e => {
    if (e.key === " " && !$("panel-editor").hidden && !typing(e)) e.preventDefault();
});
// Where a key is text being typed: a field, a list, or anything in a dialog.
function typing(e) {
    return !!e.target.closest?.("textarea, select, .modal, [contenteditable], " +
        "input:not([type=range]):not([type=checkbox]):not([type=radio]):not([type=button])");
}

// ------------------------------------------------------------------ events

Buddy.on("presets", data => {
    packs = data.packs;
    kinds = data.kinds;
    presets = data.presets.map(prepare);
    big = null;
    drawGroups();
    drawChosen();
    if (!$("panel-editor").hidden) editorShown();
});

Buddy.on("keys", data => {
    keyCache[data.id] = data.keys;
    if (ed.waiting === data.id) setKeys(data.keys);
});

Buddy.on("saved", data => {
    const p = presets.find(x => x.id === data.id);
    if (!p) return;
    Object.assign(ed, {id: p.id, saved: true, label: p.label, dirty: false});
    drawEditor();
});

Buddy.on("state", data => {
    const shapeChanged = data.shape !== state.shape;
    const before = JSON.stringify([state.tab === "favorites", [...favorites]]);
    state = data;
    favorites = new Set(state.favorites || []);
    showTab(state.tab);
    pressed($("way"), "way", state.way);
    pressed($("speed"), "speed", state.speed);
    pressed($("play"), "play", state.play);
    paintColours();
    if (JSON.stringify([state.tab === "favorites", [...favorites]]) !== before) drawGroups();
    for (const b of document.querySelectorAll(".pv-act.heart")) {
        const on = favorites.has(b.dataset.id);
        b.setAttribute("aria-pressed", String(on));
        b.title = on ? "Take it out of Favorites" : "Add to Favorites";
    }
    for (const t of tiles) t.node.querySelector(".pv-dur").textContent = seconds(t.p.moveSeconds / (Number(state.speed) || 1));
    drawChosen();
    drawSelection();
    if (state.tab === "editor") editorShown();
    if (shapeChanged) reshape();
});

Buddy.on("toast", data => Buddy.toast(data.text));
Buddy.on("alert", data => Buddy.modal({
    title: data.title,
    body: el("p.modal-text", {text: data.text}),
    buttons: [{label: "Got it", kind: "accent"}],
}));

requestAnimationFrame(tick);
