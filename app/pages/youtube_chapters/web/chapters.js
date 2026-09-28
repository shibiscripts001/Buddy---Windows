/*
 * YouTube Chapters' view. page.py reads the timeline and builds the
 * chapters (chapters.py); this draws them and reports edits and clicks.
 */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

$("refresh").append(icon("refresh"), el("span", {text: "Refresh"}));

for (const node of document.querySelectorAll("[data-action]")) {
    if (node.dataset.action !== "copy" && node.dataset.action !== "save") {
        node.addEventListener("click", () => send(node.dataset.action));
    }
}

let STATE = null;
let editTimer = 0;
let pendingEdit = false;

Buddy.on("state", s => {
    STATE = s;
    const t = $("timeline");
    t.classList.toggle("bad", !s.timeline);
    t.replaceChildren(s.timeline
        ? el("span", {}, [el("b", {text: s.timeline, translate: "no"}),
            ` · ${Math.round(s.fps * 1000) / 1000} fps · `,
            el("span", {text: s.total === 1 ? "1 marker" : `${s.total} markers`}),
            s.busy ? " · Waiting for Resolve…" : ""])
        : el("span", {text: s.problem || "No timeline open"}));
    t.hidden = !s.connected;   // offline is said once, in Buddy's header

    const chip = (name, count, hex) => el(`button.filter${s.filter === name ? ".on" : ""}${count ? "" : ".zero"}`, {
        type: "button", "aria-pressed": String(s.filter === name),
        title: name === "All" ? "Every marker" : "Only markers of this colour",
        onclick: () => send("filter", {color: name}),
    }, [hex ? el("i.dot", {style: `background:${hex}`}) : null, el("span", {text: name}), el("b", {text: String(count)})]);
    const used = s.colors.filter(c => c.count || c.name === s.filter);
    $("filters").replaceChildren(chip("All", s.total), ...used.map(c => chip(c.name, c.count, c.hex)));
});

function colorHex() {
    return Object.fromEntries((STATE ? STATE.colors : []).map(c => [c.name, c.hex]));
}

Buddy.on("chapters", c => {
    const hex = colorHex();
    const row = (ch, skipped) => el(`div.ch${skipped ? ".skipped" : ""}`, {}, [
        el("span.time", {text: ch.time}),
        el("i.dot", {style: ch.color && hex[ch.color] ? `background:${hex[ch.color]}` : "visibility:hidden", title: ch.color || ""}),
        el("span.name", {text: ch.name, translate: "no"}),
        ch.note ? el("span.note", {text: ch.note}) : null,
    ]);
    const nodes = c.chapters.map(ch => row(ch, false));
    if (c.skipped.length) {
        nodes.push(el("div.skip-head", {text: `Left out (${c.skipped.length})`}));
        nodes.push(...c.skipped.map(ch => row(ch, true)));
    }
    if (!c.chapters.length) {
        const noTimeline = STATE && !STATE.timeline;
        nodes.unshift(el("div.empty", {}, STATE && !STATE.connected
            ? [el("div.strong", {text: "Not connected to Resolve"}), el("div", {text: "Open Resolve with a project, then press Connect."}),
               el("button.btn", {type: "button", text: "Connect", onclick: () => send("refresh")})]
            : noTimeline
            ? [el("div.strong", {text: "No timeline to read"}), el("div", {text: STATE.problem || "Open a timeline in Resolve."}),
               el("button.btn", {type: "button", text: "Try again", onclick: () => send("refresh")})]
            : [el("div.strong", {text: STATE && STATE.filter !== "All" ? "No markers of this colour" : "No markers yet"}),
               el("div", {text: "In Resolve, press M to add a marker at the playhead, then double-click it to give it a name."})]));
    }
    $("list").replaceChildren(...nodes);
    $("count").textContent = c.chapters.length ? (c.chapters.length === 1 ? "1 chapter" : `${c.chapters.length} chapters`) : "";

    $("warnings").replaceChildren(...c.warnings.map(w => el("div.warn-line", {}, [icon("warning"), el("span", {text: w})])));
    const text = $("text");
    if (text.value === c.text) pendingEdit = false;
    if (!(document.activeElement === text && (c.edited || pendingEdit))) text.value = c.text;
    meta(c);
    $("copy").disabled = $("save").disabled = !text.value.trim();
    const folder = $("folder");
    if (document.activeElement !== folder) folder.value = c.folder;
    $("open-folder").disabled = !c.folder;
    const name = $("file-name");
    if (document.activeElement !== name) name.value = c.file_name;
});

function meta(m) {
    $("edited").hidden = !m.edited;
    $("reset").hidden = !m.edited;
    $("stale").hidden = !m.stale;
}
Buddy.on("chapters_meta", meta);

// Edits go to Python as you type. Copy and Save carry the current text so
// mouse and keyboard activation both use the latest edit.
function flushEdit() {
    clearTimeout(editTimer);
    send("edit", {text: $("text").value});
}
$("text").addEventListener("input", e => {
    clearTimeout(editTimer);
    pendingEdit = true;
    $("copy").disabled = $("save").disabled = !e.target.value.trim();
    editTimer = setTimeout(flushEdit, 150);
});
for (const id of ["copy", "save"]) $(id).addEventListener("click", () => {
    clearTimeout(editTimer);
    send(id, {text: $("text").value});
});
$("folder").addEventListener("change", e => send("folder", {value: e.target.value}));
$("file-name").addEventListener("input", e => send("file_name", {value: e.target.value}));

Buddy.on("toast", t => Buddy.toast(t.text, t.open_folder ? 6000 : 3000,
    t.open_folder ? {label: "Open folder", onClick: () => send("open_folder")} : null));
Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));

Buddy.on("log", entries => {
    $("activity").hidden = !entries.length;
    $("log").replaceChildren(...entries.slice().reverse().map(e => el(`li.${e.kind}`, {}, [
        el("span.muted", {text: e.time}), " ", el("span", {text: e.text}),
    ])));
});
