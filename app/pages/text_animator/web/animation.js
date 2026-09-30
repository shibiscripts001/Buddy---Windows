/*
 * Animation's view. Tabs: the kind of animation. Previews: every motion
 * preset page.py sends, playing in a little viewer, grouped by pack; a tile
 * chooses the preset, and Apply puts it on the clips selected in Resolve.
 * What moves in the viewers follows the selection (page.py's "shape").
 * The search and the pack/kind filters only narrow what's drawn here.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, send} = Buddy;
const SVG = "http://www.w3.org/2000/svg";
const LAST = 40;            // samples 0..40
const PAUSE = 0.6;          // seconds at rest before a preset plays again

let presets = [], packs = [], kinds = [];
let state = {tab: "previews", chosen: "", way: "both", speed: 1, play: "all", shape: "card",
             connected: false, selection: null, problem: "", working: ""};
const view = {q: "", pack: "all", kind: "all"};
const tiles = [];           // {p, node, obj, head, offset, hoverAt, visible}
let big = null;             // the chosen preset's viewer

// ------------------------------------------------------------------- tabs

function showTab(tab) {
    for (const b of document.querySelectorAll(".tabs [data-tab]")) {
        b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    }
    for (const panel of document.querySelectorAll(".panel")) {
        panel.hidden = panel.id !== `panel-${tab}`;
    }
}

for (const b of document.querySelectorAll(".tabs [data-tab]")) {
    b.onclick = () => { showTab(b.dataset.tab); send("tab", {tab: b.dataset.tab}); };
}

// ----------------------------------------------------------------- shapes

function svg(tag, attrs = {}, kids = []) {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    for (const c of kids) n.appendChild(c);
    return n;
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
    for (const t of [...tiles, big].filter(Boolean)) t.obj.replaceChildren(...make());
}

// ----------------------------------------------------------------- motion

const channel = arr => f => {
    const i = Math.min(LAST - 1, Math.floor(f)), k = f - i;
    return arr[i] + (arr[i + 1] - arr[i]) * k;
};

function prepare(p) {
    p.at = {x: channel(p.x), y: channel(p.y), r: channel(p.r), s: channel(p.s), o: channel(p.o)};
    // How long its first move lasts, for the tile: the In, the Emphasis or the Out.
    const [a, b] = p.segs[0] || [0, 1];
    p.moveSeconds = (b - a) * p.dur;
    return p;
}

function pose(t, f) {
    const a = t.p.at;
    t.obj.setAttribute("transform",
        `translate(${a.x(f) * 2} ${a.y(f) * 2}) rotate(${a.r(f)}) scale(${Math.max(0.001, a.s(f))})`);
    t.obj.setAttribute("opacity", Math.max(0, Math.min(1, a.o(f))));
    if (t.head) t.head.style.left = `${(f / LAST) * 100}%`;
}

function frameFor(t, now, always) {
    let elapsed;
    if (!always && state.play === "hover") {
        if (t.hoverAt === null) return LAST / 2;        // at rest, mid-hold
        elapsed = (now - t.hoverAt) / 1000;
    } else {
        elapsed = now / 1000 + t.offset;
    }
    const u = (elapsed % (t.p.dur + PAUSE)) / t.p.dur;
    return Math.min(1, u) * LAST;
}

function tick(now) {
    if (!document.hidden && !$("panel-previews").hidden) {
        for (const t of tiles) if (t.visible) pose(t, frameFor(t, now, false));
        if (big) pose(big, frameFor(big, now, true));
    }
    requestAnimationFrame(tick);
}

const seen = new IntersectionObserver(entries => {
    for (const e of entries) {
        const t = tiles.find(x => x.node === e.target);
        if (t) t.visible = e.isIntersecting;
    }
});

// ------------------------------------------------------------------ tiles

const seconds = s => `${s.toFixed(1)}s`;

function tile(p, index) {
    const obj = svg("g");
    const head = el("div.pv-head");
    const node = el("button.card.pv-tile", {
        type: "button", "aria-pressed": String(p.id === state.chosen),
        title: `${p.label} · ${p.pack}`, onclick: () => send("choose", {id: p.id}),
    }, [
        el("div.pv-stage", {}, [stage(obj)]),
        el("div.pv-track", {}, [
            ...p.segs.map(([a, b]) => el("div.pv-seg", {style: `left:${a * 100}%;width:${(b - a) * 100}%`})),
            head,
        ]),
        el("div.pv-meta", {}, [
            el("span.pv-name", {text: p.label, translate: "no"}),
            el("span.pv-sub.muted.small", {}, [el("span", {text: p.kind}), el("span.pv-dur", {text: seconds(p.moveSeconds)})]),
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

const matches = p => (view.pack === "all" || p.pack === view.pack)
    && (view.kind === "all" || p.kind === view.kind)
    && (!view.q || p.label.toLowerCase().includes(view.q));

function drawGroups() {
    const count = f => presets.filter(f).length;
    segmented($("packs"), [["all", "All", presets.length],
        ...packs.map(k => [k.id, k.id, count(p => p.pack === k.id)])], "pack");
    segmented($("kinds"), [["all", "Any kind"], ...kinds.map(k => [k, k])], "kind");

    for (const t of tiles) seen.unobserve(t.node);
    tiles.length = 0;
    let index = 0;
    const groups = packs.map(pack => {
        const list = presets.filter(p => p.pack === pack.id && matches(p));
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
    $("groups").replaceChildren(...(groups.length ? groups : [el("div.empty", {}, [
        el("span.strong", {text: "No presets match"}),
        el("span", {text: "Try another name, pack or kind."}),
    ])]));
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
    $("chosen-sub").textContent = `${p.pack} · ${p.kind}`;
    const inOut = p.kind === "In · Out";
    $("way-row").hidden = !inOut;
    $("where").textContent = WHERE[inOut ? state.way : p.kind] || "";
    for (const t of tiles) t.node.setAttribute("aria-pressed", String(t.p.id === p.id));
}

function drawSelection() {
    const s = state.selection, host = $("selection");
    let chips;
    if (!state.connected) {
        chips = [el("span.muted.small", {text: "Not connected to Resolve"}),
                 el("button.btn.ghost", {type: "button", text: "Connect", onclick: () => send("refresh")})];
    } else if (state.problem) {
        chips = [el("span.warn", {text: state.problem})];
    } else if (!s) {
        chips = [el("span.muted.small", {text: "Reading the selection…"})];
    } else if (!s.total) {
        chips = [el("span.muted.small", {text: "Nothing selected in the timeline"})];
    } else {
        chips = s.parts.map(part => el("span.chip", {text: part.text}));
    }
    host.replaceChildren(...chips);

    const can = s && s.animatable;
    const apply = $("apply"), remove = $("remove");
    apply.disabled = !!state.working || !can;
    remove.disabled = !!state.working || !(s && s.animated);
    apply.textContent = state.working === "apply" ? "Applying…"
        : !can ? "Apply to selected" : can === 1 ? "Apply to 1 clip" : `Apply to ${can} clips`;
    remove.textContent = state.working === "remove" ? "Removing…" : "Remove";
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

// ------------------------------------------------------------------ events

Buddy.on("presets", data => {
    packs = data.packs;
    kinds = data.kinds;
    presets = data.presets.map(prepare);
    big = null;
    drawGroups();
    drawChosen();
});

Buddy.on("state", data => {
    const shapeChanged = data.shape !== state.shape;
    state = data;
    showTab(state.tab);
    pressed($("way"), "way", state.way);
    pressed($("speed"), "speed", state.speed);
    pressed($("play"), "play", state.play);
    drawChosen();
    drawSelection();
    if (shapeChanged) reshape();
});

Buddy.on("toast", data => Buddy.toast(data.text));
Buddy.on("alert", data => Buddy.modal({
    title: data.title,
    body: el("p.modal-text", {text: data.text}),
    buttons: [{label: "Got it", kind: "accent"}],
}));

requestAnimationFrame(tick);
