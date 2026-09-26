/*
 * Media Relink's view. Python owns the clip lists and every decision; this
 * draws them and keeps which rows are ticked (sent with Relink selected).
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, send} = Buddy;

const INTRO = {
    fix: ["Fix offline media",
          "Scan the project for clips whose files have gone missing, then search a folder to find them. " +
          "Clips that already work are never touched, even if a file with the same name turns up."],
    relocate: ["Move to a new location",
               "Point clips at a copy of their media somewhere else – a new drive, or off a server onto a local SSD - " +
               "even clips that work now. Scan, search the new location, tick the clips to move, then relink."],
};

let state = {mode: "fix", connected: false, busy: false};
const lists = {fix: null, relocate: null};
const selected = {fix: new Set(), relocate: new Set()};
const generation = {fix: -1, relocate: -1};
const onlyOffline = $("only-offline");

const plural = (n, word, many) => `${n} ${n === 1 ? word : (many || word + "s")}`;
const needsAttention = r => r.status !== "online" || r.new_path;

for (const b of document.querySelectorAll("#mode [data-mode]")) {
    b.onclick = () => { state.mode = b.dataset.mode; draw(); send("mode", {mode: state.mode}); };
}
for (const b of document.querySelectorAll("[data-action]")) b.addEventListener("click", () => send(b.dataset.action));
$("cancel-search").onclick = () => send("cancel_search");
onlyOffline.onchange = () => draw();

$("select-all").onclick = () => {
    const list = lists[state.mode];
    if (!list) return;
    const set = selected[state.mode];
    const every = list.rows.length && list.rows.every(r => set.has(r.id));
    set.clear();
    if (!every) for (const r of list.rows) set.add(r.id);
    draw();
};
$("relink-selected").onclick = () => send("relink_selected", {ids: [...selected[state.mode]]});
$("relink-all").onclick = () => send("relink_all");

// ------------------------------------------------------------------ draw

function statusChip(r) {
    return el(`span.status-chip.st-${r.status}`, {title: `Recorded path: ${r.old_path}`}, [el("i"), r.label]);
}

// The file name is the same on both sides (that's how a match is found),
// so the table shows the folders; the full paths are in the tooltip.
const folderOf = path => path.replace(/[\\/][^\\/]*$/, "") || path;

function pathLine(cls, path, arrow) {
    return el(`div.path${cls}`, {title: path}, el("bdi", {}, [arrow ? el("span.path-arrow", {text: "→"}) : null, folderOf(path)]));
}

function pathCell(r) {
    if (r.new_path) return el("td.file", {}, [pathLine(".old", r.old_path), pathLine(".new", r.new_path, true)]);
    return el("td.file", {}, pathLine("", r.old_path));
}

function actionCell(r) {
    if (r.status === "ambiguous") {
        return el("td.act", {}, el("button.btn.accent", {type: "button", text: "Pick…", onclick: () => pickDialog(r)}));
    }
    if (r.status === "online" && state.mode === "fix") return el("td.act");
    const label = r.candidates.length > 1 ? "Change…" : "Browse…";
    return el("td.act", {}, el("button.btn.ghost", {
        type: "button", text: label, title: "Choose the file for this clip yourself",
        onclick: () => r.candidates.length > 1 ? pickDialog(r) : send("browse", {id: r.id}),
    }));
}

function emptyState(title, detail, button) {
    return el("div.empty", {}, [el("div.strong", {text: title}), detail ? el("div.small", {text: detail}) : null, button || null]);
}

function drawTable(list) {
    const box = $("results");
    if (!state.connected && !list.scanned) {
        box.replaceChildren(emptyState("Not connected to Resolve", "Open Resolve with a project, then press Connect.",
            el("button.btn.accent", {text: "Connect", onclick: () => send("connect")})));
        return;
    }
    if (!list.scanned) {
        box.replaceChildren(emptyState("Scan the project to list its clips",
            state.mode === "fix" ? "Buddy checks every clip's file and lists the ones that are missing." :
                                   "Buddy lists every clip in the Media Pool with the file it points at.",
            el("button.btn.accent", {text: "Scan project", onclick: () => send("scan")})));
        return;
    }
    const filter = state.mode === "fix" && onlyOffline.checked;
    const rows = filter ? list.rows.filter(needsAttention) : list.rows;
    if (!list.rows.length) {
        box.replaceChildren(emptyState("No clips with files", "The Media Pool has no clips that link to a file."));
        return;
    }
    if (!rows.length) {
        box.replaceChildren(emptyState("Nothing is offline",
            `All ${plural(list.rows.length, "clip")} link to files that exist.`,
            el("button.btn", {text: "Show all clips", onclick: () => { onlyOffline.checked = false; draw(); }})));
        return;
    }
    const set = selected[state.mode];
    const allTicked = rows.every(r => set.has(r.id));
    const head = el("tr", {}, [
        el("th.sel", {}, el("input", {type: "checkbox", checked: allTicked, title: "Select all shown",
            onchange: e => { for (const r of rows) e.target.checked ? set.add(r.id) : set.delete(r.id); draw(); }})),
        el("th.name", {text: "Clip"}), el("th.bin", {text: "Bin"}), el("th.status", {text: "Status"}),
        el("th.file", {text: "File"}), el("th.act"),
    ]);
    const body = rows.map(r => el(`tr${set.has(r.id) ? ".selected" : ""}`, {
        ondblclick: () => r.status === "ambiguous" ? pickDialog(r) : (r.status !== "online" || state.mode !== "fix") && send("browse", {id: r.id}),
    }, [
        el("td.sel", {}, el("input", {type: "checkbox", checked: set.has(r.id),
            onchange: e => { e.target.checked ? set.add(r.id) : set.delete(r.id); draw(); }})),
        el("td.name", {text: r.name, title: r.name}),
        el("td.bin", {text: r.bin, title: r.bin}),
        el("td.status", {}, statusChip(r)),
        pathCell(r),
        actionCell(r),
    ]));
    const scroll = box.scrollTop;
    box.replaceChildren(el("table.table", {}, [el("thead", {}, head), el("tbody", {}, body)]));
    box.scrollTop = scroll;
}

function drawCounts(list) {
    const c = list.counts;
    const chip = (n, label, cls) => el(`span.count${cls ? "." + cls : ""}`, {}, [el("b", {text: n.toLocaleString()}), label]);
    $("counts").replaceChildren(...(list.scanned ? [
        chip(c.total, c.total === 1 ? "clip" : "clips"),
        state.mode === "fix" || c.offline ? chip(c.offline, "offline") : null,
        c.matched ? chip(c.matched, c.matched === 1 ? "match" : "matches") : null,
        c.ambiguous ? chip(c.ambiguous, "to pick") : null,
    ].filter(Boolean) : []));
}

function draw() {
    const mode = state.mode;
    for (const b of document.querySelectorAll("#mode [data-mode]")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
    const [title, text] = INTRO[mode];
    $("intro-title").textContent = title;
    $("intro-text").textContent = text;
    $("only-offline-box").hidden = mode !== "fix";
    $("relink-all").hidden = mode !== "fix";

    const list = lists[mode] || {rows: [], counts: {total: 0}, scanned: false, summary: ""};
    drawCounts(list);
    drawTable(list);
    $("summary").textContent = list.summary || "";

    const scan = $("scan");
    scan.classList.toggle("accent", !list.scanned);
    scan.textContent = list.scanned ? "Scan again" : "Scan project";
    const search = $("search");
    search.classList.toggle("accent", list.scanned && !list.counts.matched && !list.counts.ambiguous);

    const set = selected[mode];
    const picked = list.rows.filter(r => set.has(r.id));
    const ready = picked.filter(r => r.new_path && r.status !== "online");
    const matches = list.rows.filter(r => r.status === "match_found" && r.new_path).length;
    $("select-all").hidden = !list.rows.length;
    $("select-all").textContent = list.rows.length && list.rows.every(r => set.has(r.id)) ? "Select none" : "Select all";
    $("selection").textContent = picked.length
        ? `${picked.length} selected${ready.length !== picked.length ? ` · ${ready.length} with a file to go to` : ""}`
        : (mode === "relocate" && matches ? "Tick the clips to move" : "");
    const rs = $("relink-selected");
    rs.textContent = ready.length ? `Relink ${plural(ready.length, "selected clip")}` : "Relink selected";
    rs.classList.toggle("accent", mode === "relocate");
    const ra = $("relink-all");
    ra.textContent = matches ? `Relink all ${plural(matches, "match", "matches")}` : "Relink all matches";

    const busy = state.busy;
    scan.disabled = search.disabled = busy;
    search.disabled = busy || !list.rows.length;
    rs.disabled = busy || !ready.length;
    ra.disabled = busy || !matches;
    $("select-all").disabled = busy;
}

// ---------------------------------------------------------------- picking

function pickDialog(r) {
    let choice = r.new_path || r.candidates[0];
    const list = el("div.pick-list", {}, r.candidates.map(path => el("label", {}, [
        el("input", {type: "radio", name: "pick", checked: path === choice, onchange: () => { choice = path; }}),
        el("span", {text: path}),
    ])));
    Buddy.modal({
        title: `Which file is "${r.name}"?`,
        body: [el("p.modal-text.muted", {text: `${plural(r.candidates.length, "file")} in that folder share its name.`}), list],
        wide: true,
        buttons: [
            {label: "Browse for another…", onClick: close => { close(); send("browse", {id: r.id}); }},
            {label: "Cancel"},
            {label: "Use this file", kind: "accent", onClick: close => { close(); send("pick", {id: r.id, path: choice}); }},
        ],
    });
}

// ---------------------------------------------------------------- events

Buddy.on("state", s => { state = s; draw(); });

Buddy.on("rows", d => {
    if (d.generation !== generation[d.mode]) {
        generation[d.mode] = d.generation;
        selected[d.mode].clear();
    }
    const ids = new Set(d.rows.map(r => r.id));
    for (const id of [...selected[d.mode]]) if (!ids.has(id)) selected[d.mode].delete(id);
    lists[d.mode] = d;
    if (d.mode === state.mode) draw();
});

Buddy.on("search", s => {
    $("search-card").hidden = !s;
    if (!s) return;
    $("search-title").textContent = s.stopping ? "Stopping…" : `Searching ${s.folder}`;
    $("search-count").textContent = s.seen ? `Looked at ${s.seen.toLocaleString()} files so far` : "Starting…";
    $("cancel-search").disabled = s.stopping;
});

Buddy.on("log", entries => {
    $("activity").hidden = !entries.length;
    $("log").replaceChildren(...entries.slice().reverse().map(e =>
        el(`li.k-${e.kind}`, {}, [el("span.time", {text: e.time}), e.text])));
});

Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));
Buddy.on("toast", t => Buddy.toast(t.text, 3000));

draw();
