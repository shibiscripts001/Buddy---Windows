/*
 * Color Palette Manager's view. page.py owns every palette and does every
 * edit; this draws what it sends ("library", "extract", "visualize",
 * "tools") and reports what the user did. Swatches arrive as
 * {hex, shown, ink}: edits use hex, the display uses shown (the Vision
 * simulation).
 */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

// ------------------------------------------------------------ strings --

// The tool's own translations (i18n.py), sent for the chosen language.
let STRINGS = {};
const T = text => STRINGS[text] || text;

function translatePage() {
    for (const node of document.querySelectorAll("[data-t]")) node.textContent = T(node.dataset.t);
    for (const node of document.querySelectorAll("[data-tp]")) node.placeholder = T(node.dataset.tp);
    for (const node of document.querySelectorAll("[data-tt]")) node.title = T(node.dataset.tt);
}

Buddy.on("strings", s => {
    STRINGS = s.strings || {};
    document.documentElement.lang = s.language === "English" ? "en" : "";
    translatePage();
    drawGenerators();
});

// --------------------------------------------------------------- page --

let STATE = {tab: "palettes", vision: "None"};

$("vision-icon").append(icon("eye"));
$("search-icon").append(icon("search"));
$("stage-icon").append(icon("image"));
$("ct-swap").append(icon("swap"));
$("ct-swap").dataset.tt = "Swap text/background colours";

Buddy.on("state", s => {
    STATE = s;
    const select = $("vision");
    select.replaceChildren(...s.modes.map(m => el("option", {value: m, text: T(m)})));
    select.value = s.vision;
    document.body.classList.toggle("simulating", s.vision !== "None");
    showTab(s.tab);
});

$("vision").onchange = e => send("vision", {mode: e.target.value});

function showTab(tab) {
    for (const button of $("tabs").querySelectorAll("button")) {
        button.setAttribute("aria-selected", String(button.dataset.tab === tab));
    }
    for (const panel of document.querySelectorAll(".panel")) panel.hidden = panel.id !== `panel-${tab}`;
    STATE.tab = tab;
}

$("tabs").onclick = e => {
    const button = e.target.closest("button[data-tab]");
    if (button && button.dataset.tab !== STATE.tab) {
        showTab(button.dataset.tab);
        send("tab", {tab: button.dataset.tab});
    }
};

for (const node of document.querySelectorAll("[data-action]")) {
    node.addEventListener("click", () => send(node.dataset.action));
}

Buddy.on("toast", t => {
    let action = null;
    if (t.undo) action = {label: T("Undo"), onClick: () => send(t.undo)};
    else if (t.show) action = {label: "Show", onClick: () => { goToPalette(t.show); }};
    Buddy.toast(t.text, action ? 6000 : 2400, action);
});

Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));

function goToPalette(name) {
    showTab("palettes");
    send("tab", {tab: "palettes"});
    if (!(LIB.current === name && openName() === name)) send("toggle_palette", {name});
}

// ------------------------------------------------------------- swatches --

/* A colour you can click (copy), right-click (menu) and drag. */
function swatchEl(sw, opts = {}) {
    const node = el(`button.sw${opts.cls ? "." + opts.cls : ""}`, {
        type: "button",
        title: opts.title || sw.hex,
        draggable: opts.draggable !== false ? "true" : undefined,
        style: `--sw:${sw.shown};--ink:${sw.ink}`,
        onclick: e => (opts.onClick ? opts.onClick(e) : copyHex(sw, e)),
        oncontextmenu: e => {
            if (!opts.menu) return;
            e.preventDefault();
            Buddy.menu({x: e.clientX, y: e.clientY, items: opts.menu()});
        },
        ondragstart: e => {
            e.dataTransfer.effectAllowed = "copyMove";
            e.dataTransfer.setData("text/plain", sw.hex);
            e.dataTransfer.setData("application/x-cp-color", sw.hex);
            if (opts.drag) e.dataTransfer.setData("application/x-cp-swatch", JSON.stringify(opts.drag));
            node.classList.add("dragging");
        },
        ondragend: () => node.classList.remove("dragging"),
    }, opts.label ? el("span.sw-label", {text: sw.hex}) : null);
    return node;
}

/* A copied colour flashes up by the pointer, in the colour itself. */
let tipTimer = 0;
function copyHex(sw, e) {
    send("copy", {hex: sw.hex});
    let tip = $("copy-tip");
    if (!tip) {
        tip = el("div.copy-tip#copy-tip");
        document.body.append(tip);
    }
    tip.textContent = T("Copied {hex}").replace("{hex}", sw.hex);
    tip.style.cssText = `--sw:${sw.shown};--ink:${sw.ink}`;
    const x = e && e.clientX ? e.clientX : innerWidth / 2, y = e && e.clientY ? e.clientY : innerHeight / 2;
    tip.style.left = `${Math.min(x + 12, innerWidth - 150)}px`;
    tip.style.top = `${Math.min(y + 16, innerHeight - 40)}px`;
    tip.classList.add("show");
    clearTimeout(tipTimer);
    tipTimer = setTimeout(() => tip.classList.remove("show"), 1100);
}

function hasType(e, type) { return [...e.dataTransfer.types].includes(type); }

// ------------------------------------------------------------- picker --

// Buddy's shared colour picker, with the screen dropper wired to this page's
// Python side (send "dropper" -> "picked").
let pendingPick = null;
const Picker = {
    open: opts => Buddy.pickColor(Object.assign({
        title: T("Choose colour"), dropperLabel: T("Dropper"),
        dropper: set => { pendingPick = set; send("dropper", {for: "picker"}); },
    }, opts)),
    close: () => Buddy.closePicker(),
    parseHex: text => Buddy.parseHex(text),
};
Buddy.on("picked", p => {
    if (pendingPick) {
        const set = pendingPick;
        pendingPick = null;
        set(p.hex);
    }
});

// ------------------------------------------------------------- prompts --

/* A modal with one text box. check(value) returns an error or "". */
function ask({title, label, value, ok, check, note, extra}) {
    return new Promise(resolve => {
        const field = el("input.field", {value: value || "", spellcheck: "false", autocomplete: "off"});
        const error = el("div.field-error");
        let answer = null;
        const submit = close => {
            const text = field.value.trim();
            const problem = check ? check(text) : (text ? "" : " ");
            if (problem) {
                error.textContent = problem.trim();
                field.classList.add("invalid");
                field.focus();
                return;
            }
            answer = text;
            close();
        };
        const m = Buddy.modal({
            title,
            body: [el("label.lbl", {}, [label, field]), error, note ? el("p.muted.small.note", {text: note}) : null, extra || null],
            buttons: [{label: "Cancel"}, {label: ok || "OK", kind: "accent", onClick: submit}],
            onClose: () => resolve(answer),
        });
        field.addEventListener("input", () => { error.textContent = ""; field.classList.remove("invalid"); });
        field.addEventListener("keydown", e => { if (e.key === "Enter") submit(m.close); });
        requestAnimationFrame(() => field.select());
    });
}

function nameCheck(existing, what, keep) {
    return text => {
        if (!text) return T(`Please enter a name for the new ${what}.`);
        if (text !== keep && existing.includes(text)) return T(`A ${what} with this name already exists.`);
        return "";
    };
}

// ------------------------------------------------------------ palettes --

let LIB = {folders: [], loose: [], current: null, folders_all: []};
let collapsedLoose = false;

function allPaletteNames() {
    return [...LIB.folders.flatMap(f => f.palettes.map(p => p.name)), ...LIB.loose.map(p => p.name)];
}
function openName() {
    for (const row of [...LIB.folders.flatMap(f => f.palettes), ...LIB.loose]) if (row.open) return row.name;
    return null;
}

Buddy.on("library", lib => {
    LIB = lib;
    drawLibrary();
});

$("search").addEventListener("input", e => send("search", {query: e.target.value}));

$("new-palette").onclick = async () => {
    const name = await ask({title: T("Add palette"), label: T("New palette name:"), ok: "Create",
                            check: nameCheck(PALETTE_NAMES, "palette")});
    if (name) send("new_palette", {name});
};
$("new-folder").onclick = async () => {
    const name = await ask({title: T("Add folder"), label: T("New folder name:"), ok: "Create",
                            check: nameCheck(LIB.folders_all, "folder")});
    if (name) send("new_folder", {name});
};

// Every palette's name, not just the ones a search is showing.
let PALETTE_NAMES = [];

function drawLibrary() {
    const box = $("library");
    const nodes = [];
    for (const folder of LIB.folders) nodes.push(folderEl(folder));
    if (LIB.loose.length || LIB.folders.length) {
        if (LIB.folders.length) {
            nodes.push(dropHead(el("div.loose-head", {}, [
                el("span.section-title.small-title", {text: "Not in a folder"}),
                el("span.muted.small", {text: String(LIB.loose.length)}),
            ]), null));
        }
        for (const row of LIB.loose) nodes.push(paletteEl(row));
    }
    if (!nodes.length || (!LIB.loose.length && LIB.folders.every(f => !f.palettes.length) && LIB.query)) {
        nodes.push(el("div.empty", {}, [
            el("div.strong", {text: LIB.query ? "Nothing matches that search" : "No palettes yet"}),
            el("div", {text: LIB.query ? "Search looks at palette names, tags and hex codes." : "Make one, import a file on Tools, or pull one out of an image on Extract."}),
        ]));
    }
    box.replaceChildren(...nodes);
    $("lib-count").textContent = LIB.query ? "" : `${LIB.total} ${LIB.total === 1 ? "palette" : "palettes"}`;
}

/* A header that takes a palette dragged onto it (folder, or null = none). */
function dropHead(node, folder) {
    node.addEventListener("dragover", e => {
        if (!hasType(e, "application/x-cp-palette")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        node.classList.add("drop-on");
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop-on"));
    node.addEventListener("drop", e => {
        node.classList.remove("drop-on");
        const name = e.dataTransfer.getData("application/x-cp-palette");
        if (name) { e.preventDefault(); send("move_palette", {name, folder}); }
    });
    return node;
}

function folderEl(folder) {
    const caret = el("span.caret", {text: folder.open ? "▾" : "▸"});
    const more = el("button.btn.ghost.icon.more", {type: "button", title: "Folder options", onclick: e => {
        e.stopPropagation();
        const r = e.currentTarget.getBoundingClientRect();
        Buddy.menu({x: r.left, y: r.bottom + 4, items: folderMenu(folder)});
    }}, icon("more"));
    const head = dropHead(el("div.folder-head", {
        tabindex: "0", role: "button", "aria-expanded": String(folder.open),
        onclick: () => send("toggle_folder", {name: folder.name}),
        onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); send("toggle_folder", {name: folder.name}); } },
        oncontextmenu: e => { e.preventDefault(); Buddy.menu({x: e.clientX, y: e.clientY, items: folderMenu(folder)}); },
    }, [caret, el("span.folder-icon", {}, icon("folder")), el("span.folder-name", {text: folder.name}),
        el("span.chip", {text: String(folder.count)}), el("div.spacer"), more]), folder.name);
    const body = folder.open ? el("div.folder-body", {}, folder.palettes.length
        ? folder.palettes.map(paletteEl)
        : [el("div.muted.small.folder-empty", {text: T("Folder is empty. Drag palettes here to add.")})]) : null;
    return el("div.folder", {}, [head, body]);
}

function folderMenu(folder) {
    return [
        {label: "New palette in this folder…", onClick: async () => {
            const name = await ask({title: T("Add palette"), label: T("New palette name:"), ok: "Create",
                                    check: nameCheck(PALETTE_NAMES, "palette")});
            if (name) send("new_palette", {name, folder: folder.name});
        }},
        {label: T("Rename") + "…", onClick: async () => {
            const name = await ask({title: T("Rename folder"), label: T("Rename folder '{name}' to:").replace("{name}", folder.name),
                                    value: folder.name, ok: T("Rename"), check: nameCheck(LIB.folders_all, "folder", folder.name)});
            if (name && name !== folder.name) send("rename_folder", {name: folder.name, new: name});
        }},
        {sep: true},
        {label: T("Delete") + "…", danger: true, onClick: async () => {
            const yes = await Buddy.confirm({title: T("Delete folder"), danger: true, ok: T("Delete"),
                text: T("Delete folder '{name}'?\nPalettes inside will move to Unorganized.").replace("{name}", folder.name)});
            if (yes) send("delete_folder", {name: folder.name});
        }},
    ];
}

function paletteEl(row) {
    const head = el("div.pal-head", {
        tabindex: "0", role: "button", draggable: "true", "aria-expanded": String(row.open),
        onclick: e => { if (!e.target.closest("button")) send("toggle_palette", {name: row.name}); },
        onkeydown: e => { if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) { e.preventDefault(); send("toggle_palette", {name: row.name}); } },
        oncontextmenu: e => { e.preventDefault(); Buddy.menu({x: e.clientX, y: e.clientY, items: paletteMenu(row)}); },
        ondragstart: e => {
            if (e.target !== e.currentTarget) return;
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("application/x-cp-palette", row.name);
            document.body.classList.add("dragging-palette");
        },
        ondragend: () => document.body.classList.remove("dragging-palette"),
    }, [
        el("span.caret", {text: row.open ? "▾" : "▸"}),
        el("span.pal-name", {text: row.name}),
        ...row.tags.map(t => el("span.chip.tag", {text: t})),
        row.open ? el("div.spacer") : el("div.strip.mini", {}, row.colors.map(c => el("i", {style: `background:${c.shown}`}))),
        el("span.muted.small.count", {text: row.colors.length === 1 ? "1 colour" : `${row.colors.length} colours`}),
        el("button.btn.ghost.icon.more", {type: "button", title: "Palette options", onclick: e => {
            e.stopPropagation();
            const r = e.currentTarget.getBoundingClientRect();
            Buddy.menu({x: r.left, y: r.bottom + 4, items: paletteMenu(row)});
        }}, icon("more")),
    ]);
    return el(`div.pal${row.open ? ".open" : ""}`, {dataset: {name: row.name}}, [head, row.open ? paletteBody(row) : null]);
}

function paletteBody(row) {
    const grid = el("div.sw-grid");
    row.colors.forEach((c, i) => {
        grid.append(swatchEl(c, {
            cls: "big", label: true, drag: {name: row.name, index: i},
            title: `${c.hex} – click to copy, drag to reorder, right-click for more`,
            menu: () => [
                {label: T("Copy hex ({hex})").replace("{hex}", c.hex), onClick: () => copyHex(c)},
                {label: T("Change colour") + "…", onClick: () => editColor(row, i, grid.children[i])},
                {sep: true},
                {label: T("Remove"), danger: true, onClick: () => send("remove_color", {name: row.name, index: i})},
            ],
        }));
    });
    const add = el("button.sw.big.add-tile", {type: "button", title: T("Add colour"), onclick: e => {
        Picker.open({hex: row.colors.length ? row.colors[row.colors.length - 1].hex : "#FFFFFF", title: T("Add colour"),
                     okLabel: T("Add colour"), at: e.currentTarget.getBoundingClientRect(),
                     onPick: hex => send("add_color", {name: row.name, hex})});
    }}, icon("plus"));
    grid.append(add);
    reorderTarget(grid, row);

    const field = el("input.field.hex-in", {placeholder: "#RRGGBB", spellcheck: "false", maxlength: "7", autocomplete: "off"});
    const addTyped = () => {
        const hex = Picker.parseHex(field.value);
        if (!hex) { field.classList.add("invalid"); field.focus(); return; }
        send("add_color", {name: row.name, hex});
        field.value = "";
    };
    field.addEventListener("keydown", e => { if (e.key === "Enter") addTyped(); });
    field.addEventListener("input", () => {
        field.classList.remove("invalid");
        const hex = Picker.parseHex(field.value);
        field.style.setProperty("--typed", hex || "transparent");
    });
    const tools = el("div.row.pal-tools", {}, [
        el("div.hex-wrap", {}, [el("i.typed"), field]),
        el("button.btn", {type: "button", text: T("Add colour"), onclick: addTyped}),
        el("button.btn", {type: "button", title: "Pick a colour from anywhere on screen",
                          onclick: () => send("dropper", {for: "add", name: row.name})}, [icon("dropper"), el("span", {text: T("Screen dropper")})]),
        el("div.spacer"),
        el("button.btn.ghost", {type: "button", title: T("Open in mini window"), onclick: () => send("mini", {name: row.name})},
           [icon("popout"), el("span", {text: "Mini window"})]),
    ]);
    const empty = row.colors.length ? null : el("div.muted.small.pal-empty", {text: "No colours yet – type a hex code, use the + tile, or pick one off the screen."});
    return el("div.pal-body", {}, [grid, empty, tools]);
}

/* Drag swatches to reorder them; a colour dragged in from another palette
   is added to this one. */
function reorderTarget(grid, row) {
    let marker = null;
    const spot = e => {
        const tiles = [...grid.querySelectorAll(".sw:not(.add-tile)")];
        for (let i = 0; i < tiles.length; i++) {
            const r = tiles[i].getBoundingClientRect();
            if (e.clientY < r.top - 4) return i;
            if (e.clientY <= r.bottom + 4 && e.clientX < r.left + r.width / 2) return i;
        }
        return tiles.length;
    };
    const clear = () => { if (marker) marker.classList.remove("before", "after"); marker = null; };
    grid.addEventListener("dragover", e => {
        if (!hasType(e, "application/x-cp-color")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = hasType(e, "application/x-cp-swatch") ? "move" : "copy";
        const at = spot(e), tiles = grid.querySelectorAll(".sw:not(.add-tile)");
        clear();
        marker = tiles[at] || tiles[tiles.length - 1] || null;
        if (marker) marker.classList.add(tiles[at] ? "before" : "after");
    });
    grid.addEventListener("dragleave", e => { if (!grid.contains(e.relatedTarget)) clear(); });
    grid.addEventListener("drop", e => {
        clear();
        e.preventDefault();
        const from = e.dataTransfer.getData("application/x-cp-swatch");
        const source = from ? JSON.parse(from) : null;
        if (source && source.name === row.name) send("move_color", {name: row.name, from: source.index, to: spot(e)});
        else send("add_color", {name: row.name, hex: e.dataTransfer.getData("application/x-cp-color")});
    });
}

function editColor(row, index, anchor) {
    const c = row.colors[index];
    Picker.open({hex: c.hex, title: T("Change colour"), at: anchor ? anchor.getBoundingClientRect() : {x: innerWidth / 2 - 130, y: 120},
                 onPick: hex => { if (hex !== c.hex) send("change_color", {name: row.name, index, hex}); }});
}

function paletteMenu(row) {
    const moveItems = [
        {heading: "Move to folder"},
        ...LIB.folders_all.map(f => ({label: f, disabled: row.folder === f, onClick: () => send("move_palette", {name: row.name, folder: f})})),
        {label: "Not in a folder", disabled: !row.folder, onClick: () => send("move_palette", {name: row.name, folder: null})},
    ];
    return [
        {label: T("Rename…").replace("...", "…"), onClick: async () => {
            const name = await ask({title: T("Rename palette"), label: T("Rename '{name}' to:").replace("{name}", row.name),
                                    value: row.name, ok: T("Rename"), check: nameCheck(PALETTE_NAMES, "palette", row.name)});
            if (name && name !== row.name) send("rename_palette", {name: row.name, new: name});
        }},
        {label: T("Edit tags…").replace("...", "…"), onClick: async () => {
            const tags = await ask({title: T("Edit custom tags"), value: row.tags.join(", "), ok: "Save",
                                    label: T("Custom tags for '{name}' (comma-separated):").replace("{name}", row.name),
                                    check: () => "", note: "Tags are searchable – e.g. warm, client, night exterior."});
            if (tags !== null) send("set_tags", {name: row.name, tags});
        }},
        {label: LIB.folders_all.length ? "Move to folder…" : "Move to folder (make a folder first)", disabled: !LIB.folders_all.length,
         onClick: () => setTimeout(() => {
             const r = document.querySelector(`.pal[data-name="${CSS.escape(row.name)}"] .more`).getBoundingClientRect();
             Buddy.menu({x: r.left, y: r.bottom + 4, items: moveItems});
         })},
        {sep: true},
        {label: T("Update version…").replace("...", "…"), onClick: async () => {
            const label = await ask({title: T("Update version"), label: T("Version label for '{name}':").replace("{name}", row.name),
                                     value: "v1.1 – new grade", ok: "Save",
                                     note: "Saves the palette's colours as they are now, so you can go back to them later."});
            if (label) send("save_version", {name: row.name, label});
        }},
        {label: row.versions ? `Version history (${row.versions})…` : "Version history (none saved)", disabled: !row.versions,
         onClick: () => send("history", {name: row.name})},
        {sep: true},
        {label: T("Open in mini window"), onClick: () => send("mini", {name: row.name})},
        {label: "Export…", onClick: () => {
            showTab("tools");
            send("tab", {tab: "tools"});
            send("export_options", {palette: row.name});
        }},
        {sep: true},
        {label: T("Delete") + "…", danger: true, onClick: async () => {
            const yes = await Buddy.confirm({title: T("Delete palette"), danger: true, ok: T("Delete"),
                text: T("Are you sure you want to delete '{name}'?").replace("{name}", row.name)});
            if (yes) send("delete_palette", {name: row.name});
        }},
    ];
}

Buddy.on("history", h => {
    const list = el("div.history", {}, h.versions.map(v => el("div.version", {}, [
        el("div.version-text", {}, [
            el("b", {text: `${v.number}. ${v.label}`}),
            el("span.muted.small", {text: v.timestamp ? v.timestamp.replace("T", " ") : ""}),
        ]),
        el("div.strip", {}, v.colors.map(c => el("i", {style: `background:${c.shown}`, title: c.hex}))),
        el("button.btn", {type: "button", text: "Restore", onclick: () => { m.close(); send("restore_version", {name: h.name, index: v.index}); }}),
    ])));
    const m = Buddy.modal({
        title: `Versions of ${h.name}`, wide: true,
        body: [el("div.version.now", {}, [
            el("div.version-text", {}, [el("b", {text: "Now"}), el("span.muted.small", {text: "Restoring replaces these colours"})]),
            el("div.strip", {}, h.colors.map(c => el("i", {style: `background:${c.shown}`, title: c.hex}))),
        ]), list],
        buttons: [{label: T("Close")}],
    });
});

// ---------------------------------------------------------- generators --
// The list, and the open generator (generators.py via page.py). Each one's
// view is built the first time it opens and updated in place after that,
// so what you're typing or dragging isn't lost when Python answers.

const GENERATORS = [
    {id: "harmony", title: "Colour harmony generator", text: "Colours that go together, from one base colour: analogous, complementary, triads, shades and more."},
    {id: "theme", title: "Light / dark theme generator", text: "Turns a palette into matching light and dark theme versions."},
    {id: "gradient", title: "Gradient ramp generator", text: "Smooth ramps between colours – linear or radial, with easing – and a 16-bit PNG export."},
    {id: "grayscale", title: "Grayscale / neutral generator", text: "Tinted greys and neutrals built from a base colour."},
    {id: "mood", title: "Neon / pastel generator", text: "Pushes colours to vivid neon or soft pastel versions of themselves."},
    {id: "glass", title: "Glass generator", text: "Primary, highlight and shadow tones for a frosted-glass look."},
];

function drawGenerators() {
    $("gen-grid").replaceChildren(...GENERATORS.map(g => el("button.card.gen", {
        type: "button", onclick: () => send("generator", {id: g.id}),
    }, [
        el("span.gen-icon", {}, icon(g.id === "gradient" ? "palette" : "spark")),
        el("span.gen-title", {text: T(g.title)}),
        el("span.muted.gen-text", {text: g.text}),
        el("span.gen-open", {}, [T("Open"), icon("right")]),
    ])));
}
drawGenerators();

const genViews = {};    // id -> {root, update}
let GEN = null;         // the open generator's latest view
let genBusy = false, genPending = null;

const gen = (action, extra) => send("gen", Object.assign({id: GEN.id, action}, extra || {}));

/* For controls that fire as you drag (angle, curve, weight): one request at
   a time, and only the latest waiting - the preview is a picture Python draws. */
function genLive(action, extra) {
    if (genBusy) { genPending = [action, extra]; return; }
    genBusy = true;
    gen(action, extra);
}

Buddy.on("generator", g => {
    $("gen-home").hidden = !!g.open;
    $("gen-host").hidden = !g.open;
    genBusy = false;
    if (!g.open) { GEN = null; return; }
    GEN = g.view;
    let v = genViews[g.open];
    if (!v) {
        v = genViews[g.open] = buildGenerator(g.view);
        $("gen-host").append(v.root);
    }
    for (const [id, other] of Object.entries(genViews)) other.root.hidden = id !== g.open;
    v.update(g.view);
    if (genPending) { const [a, x] = genPending; genPending = null; genLive(a, x); }
});

function buildGenerator(d) {
    const parts = {base: baseGenerator, colors: colorsGenerator, gradient: gradientGenerator}[d.kind](d);
    const title = el("h2.gen-view-title", {text: d.title});
    const root = el("div.gen-view", {}, [
        el("div.row.gen-view-head", {}, [
            el("button.btn.ghost", {type: "button", onclick: () => send("generator", {id: null})}, [icon("left"), T("Generators")]),
            title,
        ]),
        el("div.gen-body", {}, [
            el("div.card.gen-preview-card", {}, parts.preview),
            el("div.gen-controls", {}, parts.controls),
        ]),
        parts.foot ? el("div.row.gen-foot", {}, parts.foot) : null,
    ]);
    return {root, update: v => { title.textContent = v.title; parts.update(v); }};
}

// ------------------------------------------------------ shared pieces --

function sectionLabel(text) { return el("div.gen-label", {text: T(text)}); }

function dragData(e) {
    try { return JSON.parse(e.dataTransfer.getData("application/x-cp-swatch") || "null"); } catch (_err) { return null; }
}

function dropZone(node, {accepts, onDrop}) {
    node.addEventListener("dragover", e => {
        if (!accepts(e)) return;
        e.preventDefault();
        node.classList.add("drop-on");
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop-on"));
    node.addEventListener("drop", e => {
        node.classList.remove("drop-on");
        if (!accepts(e)) return;
        e.preventDefault();
        onDrop(e, dragData(e), e.dataTransfer.getData("application/x-cp-color"));
    });
}

const isColor = e => hasType(e, "application/x-cp-color");

/* The generated colours: click copies, right-click copies or adds to the
   open palette, drag one onto another to swap, drop any colour to replace. */
function previewGrid(cls) {
    const grid = el(`div.gen-preview.${cls || "cols4"}`);
    return {
        node: grid,
        draw(list, labels) {
            grid.replaceChildren(...list.map((sw, i) => {
                const node = swatchEl(sw, {
                    cls: "big", drag: {gen: GEN.id, preview: i},
                    menu: () => [
                        {label: T("Copy hex ({hex})").replace("{hex}", sw.hex), onClick: () => copyHex(sw)},
                        {label: `${T("Add to")} ${GEN.target}`, onClick: () => gen("add", {indexes: [i]})},
                    ],
                });
                dropZone(node, {accepts: isColor, onDrop: (_e, data, hex) => {
                    if (data && data.gen === GEN.id && data.preview !== undefined) gen("swap", {from: data.preview, to: i});
                    else if (hex) gen("replace", {index: i, hex});
                }});
                return el("div.gen-cell", {}, [labels ? el("div.muted.small", {text: T(labels[i])}) : null, node,
                                               el("div.gen-hex", {text: sw.hex})]);
            }));
        },
    };
}

/* The colours a generator works from: click to change one, right-click to
   remove it, drag to reorder, "+" (or a dropped colour) to add. */
function colorsGrid() {
    const grid = el("div.gen-colors");
    return {
        node: grid,
        draw(v) {
            const cells = v.colors.map((sw, i) => {
                const node = swatchEl(sw, {
                    cls: "gen-in", drag: {gen: GEN.id, input: i},
                    onClick: e => Picker.open({hex: sw.hex, at: e.currentTarget, title: T("Choose colour"),
                                               onPick: hex => gen("edit", {index: i, hex})}),
                    menu: () => [{label: T("Remove"), danger: true, disabled: v.colors.length <= v.min,
                                  onClick: () => gen("remove", {index: i})}],
                });
                dropZone(node, {accepts: isColor, onDrop: (_e, data, hex) => {
                    if (data && data.gen === GEN.id && data.input !== undefined) gen("reorder", {from: data.input, to: i});
                    else if (hex) gen("edit", {index: i, hex});
                }});
                return node;
            });
            if (v.colors.length < v.max) {
                const plus = el("button.btn.accent.gen-plus", {type: "button", title: T("Choose new colour"),
                    onclick: e => Picker.open({hex: "#00FF88", at: e.currentTarget, title: T("Choose new colour"),
                                               onPick: hex => gen("add_color", {hex})})}, icon("plus"));
                dropZone(plus, {accepts: isColor, onDrop: (_e, data, hex) => {
                    if (data && data.gen === GEN.id && data.input !== undefined) gen("reorder", {from: data.input, to: v.colors.length});
                    else if (hex) gen("add_color", {hex});
                }});
                cells.push(plus);
            }
            while (cells.length < v.max) cells.push(el("span.gen-slot"));
            grid.replaceChildren(...cells);
        },
    };
}

function colorsHeader() {
    return el("div.row.gen-colors-head", {}, [
        sectionLabel("Colours:"), el("div.spacer"),
        el("button.btn.ghost", {type: "button", text: T("Clear all"), onclick: () => gen("clear")}),
        el("button.btn", {type: "button", text: T("Add palette colours"), title: T("Add the colours of the palette below"),
                          onclick: () => gen("add_palette", {name: GEN.source})}),
    ]);
}

/* One of your palettes to drag colours from. */
function sourceStrip() {
    const select = el("select.field", {"aria-label": T("Saved palette:"), onchange: () => gen("source", {name: select.value})});
    const strip = el("div.gen-source");
    return {
        node: el("div.gen-group", {}, [sectionLabel("Saved palette:"), select, strip,
                                       el("div.muted.small", {text: "Drag a colour onto the colours or the preview."})]),
        draw(v) {
            fillSelect(select, v.palettes, v.source);
            strip.replaceChildren(...(v.source_colors.length
                ? v.source_colors.map(sw => swatchEl(sw, {cls: "mid", onClick: e => copyHex(sw, e)}))
                : [el("span.muted.small", {text: "This palette is empty."})]));
        },
    };
}

function undoRow() {
    const undo = el("button.btn", {type: "button", onclick: () => gen("undo")}, [icon("undo"), T("Undo")]);
    const redo = el("button.btn", {type: "button", text: T("Redo"), onclick: () => gen("redo")});
    return {node: el("div.row", {}, [undo, redo]), draw(v) { undo.disabled = !v.can_undo; redo.disabled = !v.can_redo; }};
}

function intensitySelect() {
    const select = el("select.field.gen-amount", {"aria-label": T("Regen amount:"), onchange: () => gen("intensity", {value: select.value})});
    return {node: el("label.row.gen-amount-row", {}, [T("Regen amount:"), select]),
            draw(v) { fillSelect(select, v.intensities, v.intensity); }};
}

function segmented(onPick) {
    const node = el("div.segmented.wrap");
    return {
        node,
        draw(options, value, label) {
            if (node.children.length !== options.length) {
                node.replaceChildren(...options.map(o => el("button", {type: "button", text: label ? label(o) : o, "data-value": o,
                                                                       onclick: () => onPick(o)})));
            }
            for (const b of node.children) b.setAttribute("aria-pressed", String(b.dataset.value === value));
        },
    };
}

function createButton(extra) {
    const btn = el("button.btn.accent", {type: "button", onclick: async () => {
        if (!GEN.default_name && !GEN.preview.length) return;
        const name = await ask({title: GEN.prompt_title, label: T("Enter name for new palette:"), value: GEN.default_name,
                                ok: "Create", check: nameCheck(PALETTE_NAMES, "palette")});
        if (name) gen("create", {name});
    }});
    return {node: btn, draw(v) { btn.textContent = v.create_label; btn.disabled = !v.preview.length; if (extra) extra(v); }};
}

// ------------------------------------------------- harmony & neutrals --

function baseGenerator(d) {
    const baseSw = el("button.sw.gen-in", {type: "button", title: T("Choose base colour"),
        onclick: e => Picker.open({hex: GEN.base.hex, at: e.currentTarget, title: T("Choose base colour"),
                                   onPick: hex => gen("base", {hex})})});
    dropZone(baseSw, {accepts: isColor, onDrop: (_e, _d, hex) => hex && gen("base", {hex})});
    const hexField = el("input.field.mono.gen-hexin", {spellcheck: "false", autocomplete: "off", maxlength: "7", "aria-label": T("Base colour:")});
    hexField.addEventListener("input", () => {
        const hex = Picker.parseHex(hexField.value);
        hexField.classList.toggle("invalid", !hex);
        if (hex && hex !== GEN.base.hex) gen("base", {hex});
    });
    hexField.addEventListener("blur", () => { hexField.value = GEN.base.hex; hexField.classList.remove("invalid"); });
    const addBase = el("button.btn.gen-target", {type: "button", onclick: () => gen("add", {base: true})});
    const rules = d.rules ? segmented(value => gen("rule", {value})) : null;
    const wheels = d.wheels ? segmented(value => gen("wheel", {value})) : null;
    const countLabel = el("span");
    const count = el("input", {type: "range", step: "1", "aria-label": T("Colours:")});
    count.addEventListener("input", () => { countLabel.textContent = `${T("Colours:")} ${count.value}`; gen("count", {value: Number(count.value)}); });
    count.addEventListener("dblclick", () => { count.value = GEN.count_default; count.dispatchEvent(new Event("input")); });
    const amount = intensitySelect();
    const history = undoRow();
    const preview = previewGrid("cols4");
    const create = createButton();
    const controls = [
        el("div.gen-group", {}, [sectionLabel("Base colour:"), el("div.row.wrap", {}, [baseSw, hexField, addBase])]),
        rules ? el("div.gen-group", {}, [sectionLabel("Harmony:"), rules.node]) : null,
        wheels ? el("div.gen-group", {}, [sectionLabel("Wheel:"), wheels.node]) : null,
        el("div.gen-group", {}, [el("div.gen-label", {}, countLabel), count,
                                 el("div.muted.small", {text: "Double-click the slider to reset it."})]),
        el("div.row.wrap", {}, [amount.node, el("button.btn.accent", {type: "button", text: T("Regenerate"), onclick: () => gen("regenerate")})]),
        el("div.row.wrap", {}, [el("button.btn", {type: "button", onclick: () => gen("randomize")}, [icon("refresh"), T("Randomize")]), history.node]),
    ];
    return {
        preview: [sectionLabel("Preview:"), preview.node], controls, foot: [create.node],
        update(v) {
            baseSw.style.cssText = `--sw:${v.base.shown};--ink:${v.base.ink}`;
            if (document.activeElement !== hexField) hexField.value = v.base.hex;
            addBase.replaceChildren(el("span", {text: `${T("Add colour")} → ${v.target}`}));
            addBase.title = `${T("Add colour")} → ${v.target}`;
            if (rules) rules.draw(v.rules, v.rule, T);
            if (wheels) wheels.draw(v.wheels, v.wheel);
            count.min = v.count_min; count.max = v.count_max; count.value = v.count;
            countLabel.textContent = `${T("Colours:")} ${v.count}`;
            amount.draw(v); history.draw(v); create.draw(v);
            preview.draw(v.preview);
        },
    };
}

// ------------------------------------------ theme, neon/pastel, glass --

function colorsGenerator(d) {
    const glass = d.id === "glass";
    const grid = colorsGrid();
    const amount = intensitySelect();
    const history = undoRow();
    const source = sourceStrip();
    const modes = d.modes ? segmented(value => gen("mode", {value})) : null;
    const preview = previewGrid("cols3");
    const hint = el("p.muted.gen-hint");
    const shape = glass ? el("div.glass-shape") : null;
    const addAll = d.id === "theme" ? el("button.btn.gen-target", {type: "button", onclick: () => gen("add", {indexes: GEN.preview.map((_s, i) => i)})}) : null;
    const create = createButton(v => { if (addAll) { addAll.replaceChildren(el("span", {text: `${T("Add entire theme to palette")} → ${v.target}`})); addAll.title = `${T("Add entire theme to palette")} → ${v.target}`; addAll.disabled = !v.preview.length; } });
    const action = glass
        ? el("button.btn.accent", {type: "button", text: T("Generate"), onclick: () => gen("generate")})
        : modes.node;
    return {
        preview: [sectionLabel("Preview:"), shape, preview.node, hint],
        controls: [
            el("div.gen-group", {}, [colorsHeader(), grid.node]),
            el("div.row.wrap", {}, [amount.node, action]),
            history.node,
            source.node,
        ],
        foot: [addAll, create.node],
        update(v) {
            grid.draw(v); amount.draw(v); history.draw(v); source.draw(v); create.draw(v);
            if (modes) modes.draw(v.modes, v.mode, T);
            preview.draw(v.preview, glass ? v.labels : null);
            hint.textContent = T(v.hint);
            hint.hidden = v.preview.length > 0;
            if (shape) drawGlass(shape, v.shape);
        },
    };
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, children) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    for (const c of children || []) node.append(c);
    return node;
}

/* The frosted pill, as the old window painted it: three coloured blobs
   behind, a tinted fill, a sheen, a glint and a rim. The real colours,
   never Vision-simulated. The dark backdrop is part of the picture. */
function drawGlass(box, s) {
    const stop = (offset, color, opacity) => svg("stop", {offset, "stop-color": color, "stop-opacity": opacity});
    const radial = (id, cx, cy, r, color, from) => svg("radialGradient", {id, cx, cy, r, gradientUnits: "userSpaceOnUse"},
                                                        [stop(0, color, from), stop(1, color, 0)]);
    const pill = {x: 14, y: 14, width: 232, height: 76, rx: 38};
    box.replaceChildren(svg("svg", {viewBox: "0 0 260 104", width: 260, height: 104, role: "img", "aria-label": "Glass preview"}, [
        svg("defs", {}, [
            radial("gb1", 39, 26, 221, s.blob1, 0.92), radial("gb2", 221, 31.2, 221, s.blob2, 0.92),
            radial("gb3", 130, 104, 234, s.highlight, 0.92), radial("glint", 65.04, 27.68, 41.76, "#FFFFFF", 0.86),
            svg("linearGradient", {id: "sheen", x1: 0, y1: 14, x2: 0, y2: 90, gradientUnits: "userSpaceOnUse"},
                [stop(0, s.highlight, 0.745), stop(0.32, s.highlight, 0)]),
            svg("linearGradient", {id: "rim", x1: 0, y1: 14, x2: 0, y2: 90, gradientUnits: "userSpaceOnUse"},
                [stop(0, s.highlight, 0.9), stop(1, s.shadow, 0.706)]),
            svg("clipPath", {id: "pill"}, [svg("rect", pill)]),
        ]),
        svg("rect", {width: 260, height: 104, rx: 12, fill: "#141414"}),
        svg("rect", {width: 260, height: 104, rx: 12, fill: "url(#gb1)"}),
        svg("rect", {width: 260, height: 104, rx: 12, fill: "url(#gb2)"}),
        svg("rect", {width: 260, height: 104, rx: 12, fill: "url(#gb3)"}),
        svg("rect", Object.assign({}, pill, {y: 19, fill: s.shadow, "fill-opacity": 0.39})),
        svg("g", {"clip-path": "url(#pill)"}, [
            svg("rect", Object.assign({}, pill, {fill: s.highlight, "fill-opacity": 0.157})),
            svg("rect", Object.assign({}, pill, {fill: s.primary, "fill-opacity": 0.118})),
            svg("rect", Object.assign({}, pill, {fill: "url(#sheen)"})),
            svg("rect", Object.assign({}, pill, {fill: "url(#glint)"})),
        ]),
        svg("rect", Object.assign({}, pill, {fill: "none", stroke: "url(#rim)", "stroke-width": 1.8})),
    ]));
}

// ------------------------------------------------------------ gradient --

function gradientGenerator(d) {
    const img = el("img.gen-gradient", {alt: T("Preview:")});
    const aspects = segmented(value => gen("aspect", {value}));
    const style = el("select.field", {"aria-label": T("Style:"), onchange: () => gen("style", {value: style.value})});
    const grid = colorsGrid();
    const modes = segmented(value => gen("mode", {value}));
    const weightLabel = el("span.gen-weight");
    const weight = el("input", {type: "range", min: "-100", max: "100", step: "1", "aria-label": T("Weight:")});
    weight.addEventListener("input", () => genLive("weight", {value: Number(weight.value)}));
    weight.addEventListener("dblclick", () => { weight.value = 0; genLive("weight", {value: 0}); });
    const angleLabel = el("span.gen-weight");
    const dial = angleDial(a => genLive("angle", {value: a}));
    const curve = easingCurve(c => genLive("easing", {curve: c}));
    const presets = segmented(value => gen("preset", {value}));
    const source = sourceStrip();
    const exportBtn = el("button.btn.accent", {type: "button", text: T("Export PNG…"), onclick: () => gen("export")});
    const pngInfo = el("span.muted.small");
    const settingsBtn = el("button.btn.ghost.icon", {type: "button", title: T("PNG export settings"), onclick: () => pngSettings(GEN)}, icon("gear"));
    return {
        preview: [el("div.row.gen-aspect", {}, [sectionLabel("Preview:"), el("div.spacer"), aspects.node]),
                  el("div.gen-gradient-box", {}, img)],
        controls: [
            el("label.lbl", {}, [T("Style:"), style]),
            el("div.gen-group", {}, [colorsHeader(), grid.node]),
            el("div.gen-group", {}, [sectionLabel("Colour mode:"), modes.node]),
            el("div.gen-group", {}, [el("div.row", {}, [sectionLabel("Weight:"), el("div.spacer"), weightLabel]), weight]),
            el("div.row.gen-dials", {}, [
                el("div.gen-group", {}, [el("div.row", {}, [sectionLabel("Angle:"), angleLabel]), dial.node]),
                el("div.gen-group", {}, [sectionLabel("Easing curve:"), curve.node, presets.node]),
            ]),
            source.node,
            el("div.row", {}, [exportBtn, settingsBtn, pngInfo]),
        ],
        update(v) {
            img.src = v.image;
            img.className = `gen-gradient ${v.aspect === "9:16" ? "tall" : "wide"}`;
            aspects.draw(v.aspects, v.aspect);
            fillSelect(style, v.styles, v.style);
            [...style.options].forEach(o => { o.textContent = T(o.value); });
            grid.draw(v);
            modes.draw(v.modes, v.mode);
            if (document.activeElement !== weight) weight.value = v.weight;
            weightLabel.textContent = v.weight_label;
            angleLabel.textContent = `${Math.round(v.angle)}°`;
            dial.draw(v.angle, v.style === "Linear");
            curve.draw(v.easing);
            presets.draw(v.presets, v.preset, T);
            source.draw(v);
            pngInfo.textContent = `${v.png.width} × ${v.png.height}, ${v.png.bit_depth}-bit`;
        },
    };
}

function pointerDrag(node, onMove) {
    node.addEventListener("pointerdown", e => {
        if (e.button !== 0) return;
        if (onMove(e, true) === false) return;
        try { node.setPointerCapture(e.pointerId); } catch (_err) { /* a synthetic event */ }
        const move = ev => onMove(ev, false);
        const up = () => { node.removeEventListener("pointermove", move); node.removeEventListener("pointerup", up); };
        node.addEventListener("pointermove", move);
        node.addEventListener("pointerup", up);
    });
}

/* 0 deg points right, 90 down, as the old dial. */
function angleDial(onChange) {
    const needle = svg("line", {x1: 60, y1: 60, class: "dial-needle"});
    const knob = svg("circle", {r: 5, class: "dial-knob"});
    const ticks = [...Array(8)].map((_x, i) => {
        const a = i * Math.PI / 4;
        return svg("line", {x1: 60 + Math.cos(a) * 46, y1: 60 + Math.sin(a) * 46, x2: 60 + Math.cos(a) * 52, y2: 60 + Math.sin(a) * 52, class: "dial-tick"});
    });
    const node = svg("svg", {viewBox: "0 0 120 120", width: 120, height: 120, class: "dial", role: "slider", "aria-label": T("Angle:"),
                                                                             tabindex: "0", "aria-valuemin": "0", "aria-valuemax": "360"},
                     [svg("circle", {cx: 60, cy: 60, r: 52, class: "dial-face"}), ...ticks, needle, knob, svg("circle", {cx: 60, cy: 60, r: 3, class: "dial-hub"})]);
    let enabled = true, current = 0;
    pointerDrag(node, e => {
        if (!enabled) return false;
        const r = node.getBoundingClientRect();
        const a = (Math.atan2(e.clientY - r.top - r.height / 2, e.clientX - r.left - r.width / 2) * 180 / Math.PI + 360) % 360;
        show(a);
        onChange(Math.round(a * 10) / 10);
    });
    // Arrows turn it a degree (Shift: 15, the ticks' half-step), wrapping round.
    node.addEventListener("keydown", e => {
        if (!enabled) return;
        const step = e.shiftKey ? 15 : 1;
        const turn = {ArrowRight: step, ArrowUp: step, ArrowLeft: -step, ArrowDown: -step}[e.key];
        if (turn === undefined && e.key !== "Home") return;
        e.preventDefault();
        const a = e.key === "Home" ? 0 : ((Math.round(current) + turn) % 360 + 360) % 360;
        show(a);
        onChange(a);
    });
    function show(angle) {
        current = angle;
        node.setAttribute("aria-valuenow", String(Math.round(angle)));
        const a = angle * Math.PI / 180, x = 60 + Math.cos(a) * 52, y = 60 + Math.sin(a) * 52;
        needle.setAttribute("x2", x); needle.setAttribute("y2", y);
        knob.setAttribute("cx", x); knob.setAttribute("cy", y);
    }
    return {node, draw(angle, on) {
        enabled = on;
        node.classList.toggle("off", !on);
        node.setAttribute("tabindex", on ? "0" : "-1");
        node.setAttribute("aria-disabled", String(!on));
        show(angle);
    }};
}

/* A cubic-bezier easing curve with two handles: x 0..1, y -0.6..1.6. */
function easingCurve(onChange) {
    const W = 180, H = 150, M = 16;
    const px = x => M + x * (W - 2 * M), py = y => M + (1.6 - y) / 2.2 * (H - 2 * M);
    const vx = X => Math.max(0, Math.min(1, (X - M) / (W - 2 * M))), vy = Y => Math.max(-0.6, Math.min(1.6, 1.6 - (Y - M) / (H - 2 * M) * 2.2));
    const path = svg("path", {class: "curve-line"});
    const g1 = svg("line", {class: "curve-guide", x1: px(0), y1: py(0)}), g2 = svg("line", {class: "curve-guide", x1: px(1), y1: py(1)});
    const handle = n => svg("circle", {r: 6, class: "curve-handle", tabindex: "0", role: "slider",
                                        "aria-label": `${T("Easing curve:").replace(/:$/, "")} – handle ${n}`});
    const h1 = handle(1), h2 = handle(2);
    const dots = [];
    for (let i = 0; i <= 4; i++) for (let j = 0; j <= 3; j++) dots.push(svg("circle", {cx: M + i * (W - 2 * M) / 4, cy: M + j * (H - 2 * M) / 3, r: 1.2, class: "curve-dot"}));
    const node = svg("svg", {viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "curve"},
                     [svg("rect", {width: W, height: H, rx: 6, class: "curve-bg"}), ...dots, g1, g2, path, h1, h2]);
    let c = [0.33, 0.33, 0.67, 0.67], grab = -1;
    const local = e => { const r = node.getBoundingClientRect(); return [(e.clientX - r.left) * W / r.width, (e.clientY - r.top) * H / r.height]; };
    pointerDrag(node, (e, start) => {
        const [X, Y] = local(e);
        if (start) {
            const d1 = Math.abs(X - px(c[0])) + Math.abs(Y - py(c[1])), d2 = Math.abs(X - px(c[2])) + Math.abs(Y - py(c[3]));
            grab = Math.min(d1, d2) > 14 ? -1 : (d1 <= d2 ? 0 : 2);
            if (grab < 0) return false;
            return;
        }
        c = c.slice();
        c[grab] = Math.round(vx(X) * 100) / 100;
        c[grab + 1] = Math.round(vy(Y) * 100) / 100;
        draw(c);
        onChange(c);
    });
    // Each handle takes the arrows too: 0.01 a press, 0.1 with Shift.
    [[h1, 0], [h2, 2]].forEach(([h, i]) => h.addEventListener("keydown", e => {
        const step = e.shiftKey ? 0.1 : 0.01;
        const move = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step]}[e.key];
        if (!move) return;
        e.preventDefault();
        c = c.slice();
        c[i] = Math.round(Math.max(0, Math.min(1, c[i] + move[0])) * 100) / 100;
        c[i + 1] = Math.round(Math.max(-0.6, Math.min(1.6, c[i + 1] + move[1])) * 100) / 100;
        draw(c);
        onChange(c);
    }));
    function draw(curve) {
        c = curve;
        const [x1, y1, x2, y2] = c;
        const b = (t, p1, p2, p3) => 3 * (1 - t) * (1 - t) * t * p1 + 3 * (1 - t) * t * t * p2 + t * t * t * p3;
        let d = "";
        for (let i = 0; i <= 40; i++) { const t = i / 40; d += `${i ? "L" : "M"}${px(b(t, x1, x2, 1)).toFixed(1)} ${py(b(t, y1, y2, 1)).toFixed(1)}`; }
        path.setAttribute("d", d);
        g1.setAttribute("x2", px(x1)); g1.setAttribute("y2", py(y1));
        g2.setAttribute("x2", px(x2)); g2.setAttribute("y2", py(y2));
        h1.setAttribute("cx", px(x1)); h1.setAttribute("cy", py(y1));
        h2.setAttribute("cx", px(x2)); h2.setAttribute("cy", py(y2));
        h1.setAttribute("aria-valuetext", `x ${x1}, y ${y1}`);
        h2.setAttribute("aria-valuetext", `x ${x2}, y ${y2}`);
    }
    return {node, draw(curve) { if (grab < 0) draw(curve); }};
}

function numberField(value) {
    const f = el("input.field.gen-num", {inputmode: "numeric", spellcheck: "false", autocomplete: "off"});
    f.value = String(value);
    return f;
}

function pngSettings(v) {
    const width = numberField(v.png.width), height = numberField(v.png.height);
    const depth = el("select.field", {}, [el("option", {value: "8", text: "8-bit"}), el("option", {value: "16", text: "16-bit"})]);
    depth.value = String(v.png.bit_depth);
    const error = el("div.field-error");
    const preset = name => {
        let [w, h] = v.resolutions[name];
        if (Number(height.value) > Number(width.value)) [w, h] = [h, w];   // keeps the orientation
        width.value = w; height.value = h;
    };
    Buddy.modal({
        title: T("PNG export settings"),
        body: [
            el("div.gen-group", {}, [sectionLabel("Resolution preset:"),
                el("div.row", {}, Object.keys(v.resolutions).map(n => el("button.btn", {type: "button", text: n, onclick: () => preset(n)})))]),
            el("div.row.gen-size", {}, [el("label.lbl", {}, [T("Width:"), width]), el("label.lbl", {}, [T("Height:"), height]),
                el("button.btn.ghost", {type: "button", title: T("Swap width and height (switch between vertical/horizontal)"),
                    onclick: () => { [width.value, height.value] = [height.value, width.value]; }}, [icon("swap"), T("Swap W/H")])]),
            el("label.lbl", {}, [T("Bit depth:"), depth]),
            error,
        ],
        buttons: [{label: T("Cancel")}, {label: T("OK"), kind: "accent", onClick: close => {
            const w = Number(width.value), h = Number(height.value);
            if (!Number.isInteger(w) || !Number.isInteger(h) || w < 16 || h < 16 || w > 8192 || h > 8192) {
                error.textContent = "Width and height are whole numbers from 16 to 8192.";
                return;
            }
            gen("png", {width: w, height: h, bit_depth: Number(depth.value)});
            close();
        }}],
    });
}

// ------------------------------------------------------------- extract --

let EXTRACT = null;

Buddy.on("extract", x => {
    EXTRACT = x;
    PALETTE_NAMES = x.palettes;
    $("stage-empty").hidden = x.has_image;
    $("stage-img").hidden = !x.has_image;
    $("stage-bar").hidden = !x.has_image;
    if (x.has_image) $("stage-img").src = x.preview;
    else $("stage-img").removeAttribute("src");
    $("stage-name").textContent = x.name;
    $("stage-busy").hidden = !x.fetching;
    $("busy-text").textContent = x.fetching ? `Downloading ${x.fetching}` : "";
    $("no-imaging").textContent = x.imaging ? "" : T("Image extraction requires PIL/Pillow.");

    const o = x.options;
    setRange("count", o.count);
    setRange("weight", o.weight, "%");
    for (const b of $("weighing").children) b.setAttribute("aria-pressed", String(b.dataset.value === o.weighing));
    $("weighing-hint").textContent = o.weighing === "Balanced"
        ? "Picks colours that differ from each other, so small accents show up."
        : "The most common colours in the image.";
    $("false-color").checked = o.false_color;
    $("legend").hidden = !o.false_color;
    $("legend").replaceChildren(...x.legend.map(z => el("li", {title: z.info}, [
        el("i", {style: `background:${z.color}`}), el("b", {text: z.name}), el("span.muted", {text: z.range}),
    ])));

    $("export-strip").disabled = !x.colors.length;
    $("export-fc").disabled = !x.has_image;
    $("create-from-image").disabled = !x.colors.length;
    $("extracted").replaceChildren(...(x.colors.length ? x.colors.map(c => swatchEl(c, {
        cls: "huge", label: true,
        menu: () => [
            {label: T("Copy hex ({hex})").replace("{hex}", c.hex), onClick: () => copyHex(c)},
            {sep: true},
            {heading: T("Add to palette")},
            ...[x.current, ...x.palettes.filter(p => p !== x.current)].map(p => ({
                label: p, onClick: () => send("add_extracted", {hex: c.hex, name: p}),
            })),
        ],
    })) : [el("div.muted.small.none", {text: x.has_image ? "No colours found." : "Colours from the image show here."})]));
});

function setRange(id, value, suffix = "") {
    const input = $(id);
    if (document.activeElement !== input) input.value = value;
    $(`${id}-val`).textContent = `${input.value}${suffix}`;
}

// Sent as the slider moves, a frame at most - extraction runs on a small
// copy of the image, so it keeps up.
let optTimer = 0;
function sendOptions(opts) {
    cancelAnimationFrame(optTimer);
    optTimer = requestAnimationFrame(() => send("extract_options", opts));
}
for (const [id, suffix] of [["count", ""], ["weight", "%"]]) {
    const input = $(id);
    input.addEventListener("input", () => {
        $(`${id}-val`).textContent = `${input.value}${suffix}`;
        sendOptions({[id]: Number(input.value)});
    });
    // Double-click puts a slider back to its default, as in the standalone.
    input.addEventListener("dblclick", () => {
        input.value = input.dataset.default;
        input.dispatchEvent(new Event("input"));
    });
}
$("weighing").onclick = e => {
    const b = e.target.closest("button[data-value]");
    if (b) send("extract_options", {weighing: b.dataset.value});
};
$("false-color").onchange = e => send("extract_options", {false_color: e.target.checked});

$("create-from-image").onclick = async () => {
    const name = await ask({title: T("New palette from image"), label: T("Enter name for new palette:"),
                            value: EXTRACT.suggested, ok: "Create", check: nameCheck(PALETTE_NAMES, "palette")});
    if (name) send("create_from_image", {name});
};

// A web image dragged in from a browser: the page sees its link (files
// from Explorer are taken on the Python side - file_drops).
const stage = $("stage");
function dropLink(e) {
    const uri = (e.dataTransfer.getData("text/uri-list") || "").split(/\r?\n/).find(l => l && !l.startsWith("#"));
    const text = uri || e.dataTransfer.getData("text/plain") || "";
    const html = e.dataTransfer.getData("text/html") || "";
    const img = /<img[^>]+src="([^"]+)"/i.exec(html);
    return (img && /^(https?:|data:image)/.test(img[1]) ? img[1] : text).trim();
}
document.addEventListener("dragover", e => {
    if (hasType(e, "Files") || hasType(e, "application/x-cp-color") || hasType(e, "application/x-cp-palette")) return;
    if (STATE.tab === "extract" && (hasType(e, "text/uri-list") || hasType(e, "text/plain"))) {
        e.preventDefault();
        $("drop-veil").hidden = false;
    }
});
document.addEventListener("dragleave", e => { if (!e.relatedTarget) $("drop-veil").hidden = true; });
document.addEventListener("drop", e => {
    $("drop-veil").hidden = true;
    if (hasType(e, "application/x-cp-color") || hasType(e, "application/x-cp-palette")) return;
    e.preventDefault();
    if (STATE.tab !== "extract") return;
    const link = dropLink(e);
    if (/^(https?:|data:image)/.test(link)) send("drop_link", {link});
});
Buddy.on("drop_hover", on => { $("drop-veil").hidden = !on; });
stage.addEventListener("contextmenu", e => {
    e.preventDefault();
    Buddy.menu({x: e.clientX, y: e.clientY, items: [
        {label: T("Paste image"), onClick: () => send("paste_image")},
        {label: T("Browse image…").replace("...", "…"), onClick: () => send("browse_image")},
    ]});
});

// Ctrl+V pastes an image on Extract (the page can't read the clipboard
// itself; Python does).
document.addEventListener("keydown", e => {
    const inField = e.target.closest("input, textarea, select");
    if (STATE.tab === "extract" && !inField && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") {
        e.preventDefault();
        send("paste_image");
    }
});

// ----------------------------------------------------------- visualize --

let VIS = null;

Buddy.on("visualize", v => {
    VIS = v;
    fillSelect($("vis-palette"), v.palettes, v.palette);
    $("show-hex").checked = v.show_hex;
    $("show-pct").checked = v.show_pct;
    $("reset-slots").disabled = !v.can_reset;
    $("export-vis").disabled = v.empty;

    const map = $("treemap");
    map.classList.toggle("no-hex", !v.show_hex);
    map.classList.toggle("no-pct", !v.show_pct);
    map.replaceChildren(...v.treemap.map(slotEl));
    if (v.empty) map.append(el("div.treemap-empty", {}, [
        el("div.strong", {text: "This palette has no colours yet"}),
        el("div.muted", {text: "Grey blocks stand in until it does – or drop colours on them to try some out."}),
    ]));

    $("tray").replaceChildren(...(v.tray.length ? v.tray.map(c => swatchEl(c, {cls: "mid"})) : [el("span.muted.small", {text: "No colours in this palette."})]));
    drawMockups(v.mockup);
});

function fillSelect(select, names, value) {
    if ([...select.options].map(o => o.value).join("\n") !== names.join("\n")) {
        select.replaceChildren(...names.map(n => el("option", {value: n, text: n})));
    }
    select.value = value;
}

$("vis-palette").onchange = e => send("vis_palette", {name: e.target.value});
$("show-hex").onchange = e => send("vis_options", {show_hex: e.target.checked});
$("show-pct").onchange = e => send("vis_options", {show_pct: e.target.checked});

function slotEl(s) {
    const [x, y, w, h] = s.box;
    const node = el("button.slot", {
        type: "button", title: `${s.hex} · ${s.pct} – click for options, or drop a colour here`,
        style: `left:${x}%;top:${y}%;width:${w}%;height:${h}%;--sw:${s.shown};--ink:${s.ink}`,
        onclick: e => Buddy.menu({x: e.clientX, y: e.clientY, items: slotMenu(s)}),
        oncontextmenu: e => { e.preventDefault(); Buddy.menu({x: e.clientX, y: e.clientY, items: slotMenu(s)}); },
    }, el("span.slot-label", {}, [el("b.slot-hex", {text: s.hex}), el("span.slot-pct", {text: s.pct})]));
    slotDrop(node, s.slot);
    return node;
}

function slotDrop(node, slot) {
    node.addEventListener("dragover", e => {
        if (!hasType(e, "application/x-cp-color")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        node.classList.add("drop-on");
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop-on"));
    node.addEventListener("drop", e => {
        node.classList.remove("drop-on");
        const hex = e.dataTransfer.getData("application/x-cp-color");
        if (hex) { e.preventDefault(); e.stopPropagation(); send("set_slot", {slot, hex}); }
    });
}

function slotMenu(s) {
    return [
        {label: T("Copy hex ({hex})").replace("{hex}", s.hex), swatch: s.shown, onClick: () => copyHex(s)},
        {label: T("Change colour") + "…", onClick: () => Picker.open({
            hex: s.hex, title: T("Change colour"), at: {x: innerWidth / 2 - 130, y: 140},
            onPick: hex => send("set_slot", {slot: s.slot, hex}),
        })},
        {sep: true},
        {heading: T("Swap position with…").replace(/(\.\.\.|…)$/, "")},
        ...VIS.treemap.filter(o => o.slot !== s.slot).map(o => ({
            label: `${o.hex} (${o.pct})`, swatch: o.shown, onClick: () => send("swap_slots", {a: s.slot, b: o.slot}),
        })),
    ];
}

/* The standalone's three mockup cards, side by side and live. Any part can
   be recoloured (click it), or take a colour dropped on it. */
function drawMockups(m) {
    const part = (tag, sw, slot, extra, children) => {
        const node = el(tag, Object.assign({
            style: `--sw:${sw.shown};--ink:${sw.ink}`, title: `Slot ${slot + 1} · ${sw.hex} – click to change`,
            onclick: e => {
                e.stopPropagation();
                Picker.open({hex: sw.hex, title: T("Change colour"), at: e.currentTarget.getBoundingClientRect(),
                             onPick: hex => send("set_slot", {slot, hex})});
            },
        }, extra || {}), children);
        slotDrop(node, slot);
        return node;
    };
    const card = (title, content) => el("div.mock", {}, [el("div.mock-title.muted.small", {text: T(title)}), content]);
    $("mockups").replaceChildren(
        card("Header card", part("div.mock-header", m.dominant, 0, {}, [
            el("b", {text: T("Sample header")}),
            el("span", {text: T("Secondary text showing contrast against the dominant background.")}),
            part("span.mock-btn", m.accent, 2, {}, [T("Action button")]),
        ])),
        card("Stat tile", part("div.mock-stat", m.secondary, 1, {}, [
            el("div", {}, [el("b.mock-num", {text: "128"}), el("span", {text: T("Active users")})]),
            part("span.mock-badge", m.accent, 2, {}, ["+12%"]),
        ])),
        card("Palette tags", el("div.mock-tags", {}, m.tags.map(t => part("span.mock-tag", t, t.slot, {}, [t.hex])))),
    );
}

// --------------------------------------------------------------- tools --

let TOOLS = null;

Buddy.on("tools", t => {
    TOOLS = t;
    fillSelect($("ex-palette"), t.palettes, t.palette);
    if ($("ex-format").options.length !== t.formats.length) {
        $("ex-format").replaceChildren(...t.formats.map(f => el("option", {value: f, text: f})));
    }
    $("ex-format").value = t.format;
    $("ex-strip").replaceChildren(...t.colors.map(c => el("i", {style: `background:${c.shown}`, title: c.hex})));
    $("ex-code-box").hidden = t.preview === null;
    $("ex-code").textContent = t.preview || "";
    $("ex-save").disabled = !t.colors.length;
    $("ex-note").textContent = !t.colors.length ? T("This palette has no colours.")
        : t.binary ? "A binary file – there's nothing to preview." : "";

    const c = t.contrast;
    for (const [id, sw] of [["ct-text", c.text], ["ct-bg", c.bg]]) {
        const node = $(id);
        node.querySelector("i").style.background = sw.hex;
        node.querySelector("b").textContent = sw.hex;
    }
    const sample = $("ct-sample");
    sample.style.background = c.bg.hex;
    sample.style.color = c.text.hex;
    $("ct-ratio").textContent = `${c.ratio} : 1`;
    $("ct-checks").replaceChildren(...c.checks.map(k => el(`li.${k.pass ? "pass" : "fail"}`, {}, [
        el("span", {text: T(k.name)}),
        el("b.badge", {text: (k.pass ? T("PASS ({threshold})") : T("FAIL ({threshold})")).replace("{threshold}", k.threshold)}),
    ])));
    $("ct-apca").textContent = T("APCA Lc: {score} (informational, not part of WCAG 2.1)").replace("{score}", c.apca);
    fillSelect($("ct-palette"), t.palettes, t.ct_palette);
    drawContrastTray();
});

$("ex-palette").onchange = e => send("export_options", {palette: e.target.value});
$("ex-format").onchange = e => send("export_options", {format: e.target.value});
$("ex-copy").onclick = () => {
    send("copy_text", {text: TOOLS.preview || ""});
    Buddy.toast("Copied");
};

$("ct-palette").onchange = e => send("contrast_palette", {name: e.target.value});
function drawContrastTray() {
    const colors = TOOLS.ct_colors;
    $("ct-tray").replaceChildren(...(colors.length ? colors.map(c => swatchEl(c, {
        cls: "mid",
        title: `${c.hex} – click to use it, or drag it onto Text or Background`,
        onClick: e => Buddy.menu({x: e.clientX, y: e.clientY, items: [
            {label: `Use ${c.hex} as text`, swatch: c.shown, onClick: () => send("contrast_set", {which: "text", hex: c.hex})},
            {label: `Use ${c.hex} as background`, swatch: c.shown, onClick: () => send("contrast_set", {which: "bg", hex: c.hex})},
        ]}),
    })) : [el("span.muted.small", {text: "No colours in this palette."})]));
}

for (const [id, which, title] of [["ct-text", "text", "Choose Text Color"], ["ct-bg", "bg", "Choose Background Color"]]) {
    const node = $(id);
    node.onclick = () => Picker.open({
        hex: TOOLS.contrast[which].hex, title, at: node.getBoundingClientRect(),
        onPick: hex => send("contrast_set", {which, hex}),
    });
    node.addEventListener("dragover", e => {
        if (!hasType(e, "application/x-cp-color")) return;
        e.preventDefault();
        node.classList.add("drop-on");
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop-on"));
    node.addEventListener("drop", e => {
        node.classList.remove("drop-on");
        e.preventDefault();
        e.stopPropagation();
        send("contrast_set", {which, hex: e.dataTransfer.getData("application/x-cp-color")});
    });
}

Buddy.on("import_name", imp => {
    const existing = Object.keys(imp.existing);
    // Taking an existing name needs a second press of Import, after a warning.
    let warned = null;
    const check = text => {
        if (!text) return T("Please enter a name for the imported palette.");
        if (existing.includes(text) && warned !== text) {
            warned = text;
            return T("A palette named '{name}' already exists ({count} colours). Overwrite it?")
                .replace("{name}", text).replace("{count}", imp.existing[text]) + " Press Import again to replace it.";
        }
        return "";
    };
    ask({title: T("Import palette"), label: T("Palette name:"), value: imp.suggested, ok: "Import", check,
         note: `${imp.colors.length} colour${imp.colors.length === 1 ? "" : "s"} from ${imp.file}`,
         extra: el("div.strip", {}, imp.colors.map(c => el("i", {style: `background:${c.shown}`, title: c.hex}))),
    }).then(name => send("import_commit", name ? {name, overwrite: existing.includes(name)} : {name: ""}));
});
