/*
 * Asset Manager's view. Python owns the library, projects, the lists'
 * order and grouping, and plays the media; this draws the list, keeps the
 * selection (and which folders are open), and paints the preview - video
 * frames arrive as images, audio as waveform bars drawn on a canvas.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, icon, send} = Buddy;

let state = {view: "all", filter: {category: "All", search: ""}, categories: [], sorts: {}};
let list = {nodes: [], total: 0, missing: 0, library_total: 0};
let projects = {current: null, items: []};
let preview = null;          // the asset shown, or {multi: n}
let media = {id: null, kind: null, bars: [], progress: 0, position: 0, duration: 0, playing: false, frame: null, failed: null};
const selected = new Set();  // "a:<asset id>" and "f:<folder path>"
const expanded = new Set();  // folder paths
let anchor = null;           // for Shift-click ranges

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const fmtTime = ms => {
    const s = Math.max(0, Math.floor((ms || 0) / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

$("refresh").append(icon("refresh"));
$("play").append(icon("play"));
for (const b of document.querySelectorAll("[data-action]")) b.addEventListener("click", () => send(b.dataset.action));
for (const b of document.querySelectorAll(".views [data-view]")) {
    b.onclick = () => { if (b.dataset.view !== state.view) { clearSelection(); send("view", {view: b.dataset.view}); } };
}

// ---------------------------------------------------------------- toolbar

let searchTimer = 0;
$("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => send("filter", {search: $("search").value}), 150);
});
$("expand-all").onclick = () => { for (const n of list.nodes) if (n.type === "folder") expanded.add(n.path); drawList(); };
$("collapse-all").onclick = () => { expanded.clear(); drawList(); };
$("project-select").onchange = e => { clearSelection(); send("project", {id: e.target.value}); };

function nameDialog(title, value, ok, onDone) {
    const input = el("input.field", {value: value || "", placeholder: "Project name", maxlength: "120"});
    const submit = close => { const v = input.value.trim(); if (v) { close(); onDone(v); } else input.focus(); };
    const dlg = Buddy.modal({title, body: input, buttons: [{label: "Cancel"}, {label: ok, kind: "accent", onClick: submit}]});
    input.addEventListener("keydown", e => { if (e.key === "Enter") submit(dlg.close); });
}
$("new-project").onclick = () => nameDialog("New project", "", "Create", name => send("new_project", {name}));
$("rename-project").onclick = () => {
    const p = projects.items.find(i => i.id === projects.current);
    if (p) nameDialog("Rename project", p.name, "Rename", name => send("rename_project", {name}));
};
$("delete-project").onclick = async () => {
    const p = projects.items.find(i => i.id === projects.current);
    if (p && await Buddy.confirm({title: `Delete "${p.name}"?`, text: "The project goes. Its assets stay in your library.", ok: "Delete", danger: true})) {
        clearSelection();
        send("delete_project");
    }
};
$("add-existing").onclick = () => send("list_available");

function drawToolbar() {
    for (const v of ["all", "folders", "projects"]) $(`toolbar-${v}`).hidden = state.view !== v;
    for (const b of document.querySelectorAll(".views [data-view]")) b.setAttribute("aria-selected", String(b.dataset.view === state.view));
    const cats = $("categories");
    if (!cats.children.length) {
        for (const c of state.categories) {
            cats.append(el("button", {type: "button", text: c === "All" ? "All" : c, dataset: {cat: c},
                onclick: () => send("filter", {category: c})}));
        }
    }
    for (const b of cats.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.cat === state.filter.category));
    if (document.activeElement !== $("search")) $("search").value = state.filter.search;
    const sel = $("project-select");
    sel.replaceChildren(...(projects.items.length
        ? projects.items.map(p => el("option", {value: p.id, text: `${p.name}  (${p.count})`, translate: "no", selected: p.id === projects.current}))
        : [el("option", {value: "", text: "No projects yet"})]));
    sel.disabled = !projects.items.length;
    $("rename-project").disabled = $("delete-project").disabled = $("add-existing").disabled = !projects.current;
}

// ------------------------------------------------------------------- list

function visibleRows() {
    const rows = [];
    for (const n of list.nodes) {
        if (n.type === "folder") {
            rows.push({key: `f:${n.path}`, node: n});
            if (expanded.has(n.path)) for (const c of n.children) rows.push({key: `a:${c.id}`, node: c, child: true});
        } else {
            rows.push({key: `a:${n.id}`, node: n});
        }
    }
    return rows;
}

function selection() {
    const ids = [], folders = [];
    for (const key of selected) (key.startsWith("a:") ? ids : folders).push(key.slice(2));
    return {ids, folders};
}

function selectedCount() {
    let n = 0;
    const {ids, folders} = selection();
    const inFolders = new Set();
    for (const node of list.nodes) {
        if (node.type === "folder" && folders.includes(node.path)) for (const c of node.children) inFolders.add(c.id);
    }
    n = inFolders.size + ids.filter(id => !inFolders.has(id)).length;
    return n;
}

function clearSelection() { selected.clear(); anchor = null; }

function selectionChanged() {
    drawList();
    drawFooter();
    send("select", selection());
}

function clickRow(e, key) {
    const rows = visibleRows();
    if (e.shiftKey && anchor) {
        const a = rows.findIndex(r => r.key === anchor), b = rows.findIndex(r => r.key === key);
        if (a >= 0 && b >= 0) {
            if (!e.ctrlKey && !e.metaKey) selected.clear();
            for (let i = Math.min(a, b); i <= Math.max(a, b); i++) selected.add(rows[i].key);
        }
    } else if (e.ctrlKey || e.metaKey) {
        selected.has(key) ? selected.delete(key) : selected.add(key);
        anchor = key;
    } else {
        selected.clear();
        selected.add(key);
        anchor = key;
    }
    selectionChanged();
}

function sortHeader(label, column, cls) {
    const sort = state.sorts[state.view] || {};
    const active = sort.column === column;
    return el(`th.${cls}.sortable`, {onclick: () => send("sort", {column}), title: `Sort by ${label.toLowerCase()}`},
        [label, active ? el("span.arrow", {text: sort.reverse ? "▼" : "▲"}) : null]);
}

function emptyList() {
    if (state.view === "projects" && !projects.items.length) {
        return el("div.empty", {}, [el("div.strong", {text: "No projects yet"}),
            el("div.small", {text: "A project is a named set of assets from your library – one per client or job."}),
            el("button.btn.accent", {text: "New project", onclick: () => $("new-project").click()})]);
    }
    if (state.view === "projects") {
        return el("div.empty", {}, [el("div.strong", {text: "Nothing in this project yet"}),
            el("div.small", {text: "Add from your library, add files, or drag them here."}),
            el("button.btn.accent", {text: "Add from library…", onclick: () => send("list_available")})]);
    }
    if (!list.library_total) {
        return el("div.empty", {}, [el("div.strong", {text: "Your library is empty"}),
            el("div.small", {text: "Drag in the music, sound effects, logos and footage you reuse – or add them."}),
            el("div.row", {}, [el("button.btn.accent", {text: "Add files…", onclick: () => send("add_files")}),
                               el("button.btn", {text: "Add folder…", onclick: () => send("add_folder")})])]);
    }
    return el("div.empty", {}, [el("div.strong", {text: "Nothing matches"}),
        el("div.small", {text: "Try another type, or a different search."})]);
}

function drawList(reveal = []) {
    const box = $("list");
    const keep = box.scrollTop;
    const fresh = new Set(reveal.map(id => `a:${id}`));
    if (!list.nodes.length) { box.replaceChildren(emptyList()); return; }
    const body = visibleRows().map(({key, node, child}) => {
        if (node.type === "folder") {
            const open = expanded.has(node.path);
            return el(`tr.bucket${selected.has(key) ? ".selected" : ""}`, {title: node.path, onclick: e => clickRow(e, key),
                ondblclick: () => { open ? expanded.delete(node.path) : expanded.add(node.path); drawList(); }}, [
                el("td.name", {colspan: "3"}, el("div.name-cell", {}, [
                    el(`button.chev${open ? ".open" : ""}`, {type: "button", "aria-label": open ? "Collapse" : "Expand",
                        onclick: e => { e.stopPropagation(); open ? expanded.delete(node.path) : expanded.add(node.path); drawList(); }}),
                    icon("folder"),
                    el("span", {}, [el("span", {text: node.name, translate: "no"}), el("span.count", {text: plural(node.count, "file")})]),
                ])),
                el("td.folder", {}, el("bdi", {text: node.path, translate: "no"})),
            ]);
        }
        return el(`tr${child ? ".child" : ""}${node.missing ? ".missing" : ""}${selected.has(key) ? ".selected" : ""}${fresh.has(key) ? ".fresh" : ""}`,
            {title: node.path, onclick: e => clickRow(e, key)}, [
            el("td.name", {}, el("div.name-cell", {}, [el(`span.kind-dot.${node.category}`), el("span", {text: node.name, translate: "no"})])),
            el("td.type", {text: node.category}),
            el("td.added", {text: node.date_added}),
            el("td.folder", {}, el("bdi", {text: node.folder, translate: "no"})),
        ]);
    });
    box.replaceChildren(el("table.table", {}, [
        el("thead", {}, el("tr", {}, [sortHeader("Name", "name", "name"), el("th.type", {text: "Type"}),
                                      sortHeader("Added", "added", "added"), el("th.folder", {text: "Folder"})])),
        el("tbody", {}, body),
    ]));
    box.scrollTop = keep;
    const first = box.querySelector("tr.fresh");
    if (first) first.scrollIntoView({block: "nearest"});
}

function drawFooter() {
    const n = selectedCount();
    $("selection").textContent = n ? `${plural(n, "asset")} selected` : "Click an asset to preview it. Ctrl or Shift for several.";
    $("import").disabled = !n;
    $("import").textContent = n ? `Import ${plural(n, "asset")} to Media Pool` : "Import to Media Pool";
    $("remove").disabled = !n;
    $("remove").textContent = state.view === "projects" ? "Remove from project" : "Remove";
    const s = list;
    $("list-summary").replaceChildren(...(s.total ? [el("span", {text: plural(s.total, "asset")}),
        ...(s.missing ? [" · ", el("span", {text: `${s.missing} missing`})] : [])] : []));
}

$("import").onclick = () => send("import_selected", selection());
async function removeSelected() {
    const n = selectedCount();
    if (!n) return;
    const inProject = state.view === "projects";
    const ok = await Buddy.confirm(inProject
        ? {title: `Take ${plural(n, "asset")} out of this project?`, text: "They stay in your library, and in any other project.", ok: "Remove"}
        : {title: `Remove ${plural(n, "asset")} from the library?`, text: "They also leave every project. The files on disk aren't touched.", ok: "Remove", danger: true});
    if (ok) { send("remove", selection()); clearSelection(); drawFooter(); }
}
$("remove").onclick = removeSelected;

document.addEventListener("keydown", e => {
    const inField = e.target.closest("input, textarea, select, [contenteditable]") || document.querySelector(".modal-backdrop");
    if (inField) return;
    if (e.key === " " && media.id && media.kind && media.kind !== "failed") {
        e.preventDefault();
        send("play", {id: media.id});
    } else if (e.key === "Delete") {
        e.preventDefault();
        removeSelected();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
        e.preventDefault();
        for (const r of visibleRows()) selected.add(r.key);
        selectionChanged();
    } else if (e.key === "Escape" && selected.size) {
        clearSelection();
        selectionChanged();
    }
});

// ---------------------------------------------------------------- preview

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function drawWaveform() {
    const canvas = $("stage").querySelector("canvas");
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    const bars = media.bars || [];
    const margin = w * 0.08, usable = w - 2 * margin, mid = h / 2, half = (h / 2 - margin) * 0.95 * 0.5;
    // While decoding, the bars so far sit at the left of the final 120.
    const total = media.progress > 0 && media.progress < 1 ? Math.max(bars.length, 120) : bars.length;
    if (!total) return;
    const step = usable / Math.max(total, 1);
    const played = media.duration > 0 ? media.position / media.duration : -1;
    const accent = cssVar("--accent-text") || "#888";
    ctx.lineCap = "round";
    ctx.lineWidth = Math.max(1.5, step * 0.6);
    bars.forEach((v, i) => {
        const x = margin + (i + 0.5) * step;
        ctx.strokeStyle = accent;
        ctx.globalAlpha = played >= 0 && (i + 0.5) / total <= played ? 1 : 0.42;
        ctx.beginPath();
        ctx.moveTo(x, mid - v * half);
        ctx.lineTo(x, mid + v * half);
        ctx.stroke();
    });
    ctx.globalAlpha = 1;
}

function seekFromCanvas(e) {
    const canvas = e.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const margin = rect.width * 0.08;
    const fraction = Math.max(0, Math.min(1, (e.clientX - rect.left - margin) / (rect.width - 2 * margin)));
    if (media.duration > 0) { media.position = fraction * media.duration; drawWaveform(); drawTransport(); }
    send("seek", {id: media.id, fraction});
}

function drawStage() {
    const stage = $("stage");
    stage.className = "stage";
    const p = preview;
    if (!p) { stage.replaceChildren(el("div.preview-empty", {text: "Nothing selected"})); return; }
    if (p.multi) { stage.replaceChildren(el("div.multi", {text: String(p.multi)}), el("div.note", {text: "assets selected"})); return; }
    if (p.missing) { stage.classList.add("missing"); stage.replaceChildren(icon("warning"), el("div.note", {text: "The file isn't where it was"})); return; }
    if (p.category === "Image") {
        if (p.image) {
            const img = el("img", {src: p.image, alt: ""});
            img.onerror = () => stage.replaceChildren(icon("image"), el("div.note", {text: "No preview for this format"}));
            stage.replaceChildren(img);
        } else stage.replaceChildren(icon("image"), el("div.note", {text: "No preview for this format"}));
        return;
    }
    if (p.category === "Video") {
        if (media.failed) stage.replaceChildren(icon("film"), el("div.note", {text: media.failed}));
        else if (media.frame) stage.replaceChildren(el("img", {src: media.frame, alt: ""}));
        else stage.replaceChildren(el("span.dots", {}, [el("i"), el("i"), el("i")]));
        return;
    }
    if (p.category === "Audio") {
        if (media.failed) { stage.replaceChildren(icon("music"), el("div.note", {text: "No waveform for this file"})); return; }
        if (!stage.querySelector("canvas")) {
            let dragging = false;
            const canvas = el("canvas", {
                onpointerdown: e => { if (media.kind === "audio") { dragging = true; canvas.setPointerCapture(e.pointerId); seekFromCanvas(e); } },
                onpointermove: e => { if (dragging) seekFromCanvas(e); },
                onpointerup: () => { dragging = false; },
            });
            stage.replaceChildren(canvas);
        }
        drawWaveform();
        return;
    }
    stage.replaceChildren(icon("file"));
}

function drawTransport() {
    const p = preview;
    const playable = p && !p.multi && !p.missing && media.id === p.id && (media.kind === "audio" || media.kind === "video");
    $("transport").hidden = !playable;
    $("volume-row").hidden = !(p && !p.multi && !p.missing && (p.category === "Audio" || p.category === "Video"));
    if (!playable) return;
    $("play").replaceChildren(icon(media.playing ? "pause" : "play"));
    const scrub = $("scrub");
    if (!scrub.matches(":active")) scrub.value = media.duration ? Math.round(1000 * media.position / media.duration) : 0;
    $("time").textContent = `${fmtTime(media.position)} / ${fmtTime(media.duration)}`;
}
$("play").onclick = () => media.id && send("play", {id: media.id});
$("scrub").addEventListener("input", e => {
    const fraction = e.target.value / 1000;
    if (media.duration) { media.position = fraction * media.duration; $("time").textContent = `${fmtTime(media.position)} / ${fmtTime(media.duration)}`; }
    send("seek", {id: media.id, fraction});
});
$("volume").addEventListener("input", e => send("volume", {value: e.target.value / 100}));

function drawInfo() {
    const p = preview;
    const info = $("info"), actions = $("preview-actions");
    if (!p || p.multi) {
        info.replaceChildren(p ? el("div.muted", {text: "Import or remove them together, or pick one to preview."}) : "");
        actions.replaceChildren();
        return;
    }
    info.replaceChildren(
        el("div.title", {text: p.name, translate: "no"}),
        el("div.muted.small", {}, [el("span", {text: p.category}), " · ",
            el("span", {text: p.ext || "no extension", translate: p.ext ? "no" : undefined}), " · ", el("span", {text: `added ${p.date_added}`})]),
        el(`div.status.${p.missing ? "bad" : "ok"}`, {text: p.missing ? "Missing – the file has moved or been deleted" : "File found"}),
        el("div.path", {text: p.path, translate: "no"}),
    );
    actions.replaceChildren(...[
        p.missing ? el("button.btn.accent", {type: "button", text: "Find it…", onclick: () => send("locate", {id: p.id})}) : null,
        el("button.btn", {type: "button", text: "Open folder", disabled: p.missing, onclick: () => send("open_folder", {id: p.id})}),
        !p.missing ? el("button.btn.ghost", {type: "button", text: "Moved it?", title: "Point this asset at a different file", onclick: () => send("locate", {id: p.id})}) : null,
    ].filter(Boolean));
}

function drawPreview() { drawStage(); drawTransport(); drawInfo(); }
new ResizeObserver(() => { if (preview && preview.category === "Audio") drawWaveform(); }).observe($("stage"));

// --------------------------------------------------------------- the rest

function availableDialog(d) {
    const chosen = new Set();
    const search = el("input.field.picker-search", {type: "search", placeholder: "Search the library", autocomplete: "off"});
    const box = el("div.picker");
    const draw = () => {
        const q = search.value.trim().toLowerCase();
        const rows = d.rows.filter(r => !q || r.name.toLowerCase().includes(q));
        box.replaceChildren(...(rows.length ? rows.map(r => el(`label${r.missing ? ".missing" : ""}`, {title: r.path}, [
            el("input", {type: "checkbox", checked: chosen.has(r.id), onchange: e => { e.target.checked ? chosen.add(r.id) : chosen.delete(r.id); }}),
            el("span.pname", {text: r.name, translate: "no"}),
            el("span.ptype", {text: r.category}),
        ])) : [el("div.empty", {}, el("div.small", {text: d.rows.length ? "Nothing matches" : "Everything in your library is in this project already."}))]));
    };
    search.addEventListener("input", draw);
    draw();
    Buddy.modal({
        title: `Add to "${d.project}" from your library`, wide: true, body: [search, box],
        buttons: [{label: "Cancel"}, {label: "Add selected", kind: "accent", onClick: close => {
            if (chosen.size) send("link_existing", {ids: [...chosen]});
            close();
        }}],
    });
}

Buddy.on("state", s => { state = s; drawToolbar(); drawFooter(); });
Buddy.on("projects", p => { projects = p; drawToolbar(); });
Buddy.on("list", l => {
    list = l;
    // Drop selections that aren't in the new list.
    const keys = new Set();
    for (const n of l.nodes) {
        if (n.type === "folder") { keys.add(`f:${n.path}`); for (const c of n.children) keys.add(`a:${c.id}`); }
        else keys.add(`a:${n.id}`);
    }
    for (const k of [...selected]) if (!keys.has(k)) selected.delete(k);
    // Just added: open the groups they're in, so they don't land out of sight.
    const reveal = new Set(l.reveal || []);
    for (const n of l.nodes) if (n.type === "folder" && n.children.some(c => reveal.has(c.id))) expanded.add(n.path);
    drawList([...reveal]);
    drawFooter();
});
Buddy.on("preview", p => {
    preview = p;
    media = {id: p && p.id, kind: null, bars: [], progress: 0, position: 0, duration: 0, playing: false, frame: null, failed: null};
    $("stage").replaceChildren();
    drawPreview();
});
Buddy.on("frame", f => { if (f.id === media.id) { media.frame = f.src; const img = $("stage").querySelector("img"); if (img) img.src = f.src; else drawStage(); } });
Buddy.on("waveform", w => { if (w.id === media.id) { media.bars = w.bars; media.progress = w.progress; drawStage(); } });
Buddy.on("media", m => {
    if (m.id !== media.id) return;
    if (m.kind === "failed") media.failed = m.message; else media.kind = m.kind;
    drawPreview();
});
Buddy.on("playhead", t => {
    media.position = t.position; media.duration = t.duration;
    if (preview && preview.category === "Audio") drawWaveform();
    drawTransport();
});
Buddy.on("playing", on => { media.playing = on; drawTransport(); });
Buddy.on("available", availableDialog);
Buddy.on("drop_hover", on => { $("drop-veil").hidden = !on; });
Buddy.on("log", entries => {
    $("activity").hidden = !entries.length;
    $("log").replaceChildren(...entries.slice().reverse().map(e =>
        el(`li.k-${e.kind}`, {}, [el("span.time", {text: e.time}),
            ...(e.parts ? e.parts.flatMap((p, i) => i ? [" ", el("span", {text: p})] : [el("span", {text: p})]) : [e.text])])));
});
Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));
Buddy.on("toast", t => Buddy.toast(t.text, 3000));

drawPreview();
