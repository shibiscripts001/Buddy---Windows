/* Peek: a link bar folder's contents (core/link_peek_web.py FolderPeek,
   core/folder_peek.py). Click to select (Ctrl and Shift for more),
   double-click to go into a folder or open a file, and drag the selection
   out - the drag itself is started in Python, as real files. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
const DRAG_START = 5;

let folder = {entries: [], crumbs: []};
let shown = [];                 // the entries the filter lets through, in order
let selected = new Set();       // their paths
let anchor = -1;                // where a Shift-click range starts
let press = null;               // {x, y, index, plain} while the button's down on a row
let dragging = false;

$("up").append(icon("left"));
$("refresh").append(icon("refresh"));
$("reveal").append(icon("popout"));
$("up").onclick = () => folder.up && send("go", {path: folder.up});
$("refresh").onclick = () => send("go", {path: folder.path});
$("reveal").onclick = () => send("reveal");
$("filter").oninput = () => draw();

// No browser drags of our own: Python carries the files.
document.addEventListener("dragstart", e => e.preventDefault());

function draw() {
    const words = $("filter").value.trim().toLowerCase();
    shown = folder.entries.filter(e => !words || e.name.toLowerCase().includes(words));
    const paths = new Set(shown.map(e => e.path));
    selected = new Set([...selected].filter(p => paths.has(p)));
    $("list").replaceChildren(...shown.map(row));
    if (folder.error) $("list").append(el("div.peek-empty", {text: folder.error}));
    else if (!shown.length) $("list").append(el("div.peek-empty", {text: words ? "Nothing matches." : "This folder is empty."}));
    if (folder.more) $("list").append(el("div.peek-empty", {text: `…and ${folder.more} more`}));
    count();
}

function row(entry, index) {
    // translate="no": the names are the user's files.
    return el("div.peek-item", {role: "option", "data-index": String(index), translate: "no", title: entry.name,
                                "aria-selected": String(selected.has(entry.path))}, [
        entry.thumb ? el("img.peek-thumb", {src: entry.thumb, alt: "", loading: "lazy", draggable: "false"})
                    : icon(entry.kind),
        el("span.peek-name", {text: entry.name}),
        entry.size ? el("span.peek-size", {text: entry.size}) : null,
    ]);
}

function count() {
    const n = shown.length;
    $("count").textContent = selected.size ? `${selected.size} of ${n} selected` : (n === 1 ? "1 item" : `${n} items`);
}

function mark() {
    for (const node of $("list").querySelectorAll(".peek-item")) {
        node.setAttribute("aria-selected", String(selected.has(shown[+node.dataset.index].path)));
    }
    count();
}

function pick(index, e) {
    const path = shown[index].path;
    if (e.shiftKey && anchor >= 0) {
        if (!e.ctrlKey) selected.clear();
        const [a, b] = [Math.min(anchor, index), Math.max(anchor, index)];
        for (let i = a; i <= b; i++) selected.add(shown[i].path);
    } else if (e.ctrlKey || e.metaKey) {
        if (selected.has(path)) selected.delete(path); else selected.add(path);
        anchor = index;
    } else {
        selected = new Set([path]);
        anchor = index;
    }
    mark();
}

const rowOf = e => e.target.closest(".peek-item");
const chosen = () => shown.filter(e => selected.has(e.path)).map(e => e.path);
const modified = e => e.shiftKey || e.ctrlKey || e.metaKey;

$("list").addEventListener("pointerdown", e => {
    const node = rowOf(e);
    if (!node || e.button !== 0 || dragging) return;
    const index = +node.dataset.index;
    // Pressing on something not yet selected selects it, so it can be
    // dragged straight away; a plain press on part of a selection keeps
    // all of it for a drag, and narrows to that one if it's only a click.
    if (!selected.has(shown[index].path) || modified(e)) pick(index, e);
    press = {x: e.clientX, y: e.clientY, index, plain: !modified(e)};
});

$("list").addEventListener("pointermove", e => {
    if (!press || dragging || !(e.buttons & 1)) return;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG_START) return;
    dragging = true;
    press = null;
    send("drag", {paths: chosen()});
});

$("list").addEventListener("pointerup", () => {
    if (press && !dragging && press.plain && selected.size > 1) {
        selected = new Set([shown[press.index].path]);
        anchor = press.index;
        mark();
    }
    press = null;
});

$("list").addEventListener("dblclick", e => {
    const node = rowOf(e);
    if (node) send("open", {paths: [shown[+node.dataset.index].path]});
});

// A click on the empty part of the list clears the selection.
$("list").addEventListener("click", e => {
    if (!rowOf(e) && !modified(e)) { selected.clear(); mark(); }
});

document.addEventListener("keydown", e => {
    const typing = e.target === $("filter");
    if (e.key === "Escape") {
        if (typing && $("filter").value) { $("filter").value = ""; draw(); }
        else send("close");
    } else if (e.key === "Enter" && selected.size) {
        send("open", {paths: chosen()});
    } else if (!typing && (e.key === "Backspace" || (e.altKey && e.key === "ArrowUp"))) {
        e.preventDefault();
        if (folder.up) send("go", {path: folder.up});
    } else if (!typing && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
        e.preventDefault();
        selected = new Set(shown.map(x => x.path));
        mark();
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && shown.length) {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        const at = anchor < 0 ? (step > 0 ? 0 : shown.length - 1) : Math.min(shown.length - 1, Math.max(0, anchor + step));
        pick(at, {});
        $("list").querySelector(`[data-index="${at}"]`).scrollIntoView({block: "nearest"});
        $("list").focus();
    }
});

Buddy.on("folder", state => {
    const moved = state.path !== folder.path;
    folder = state;
    if (moved) {
        selected.clear();
        anchor = -1;
        $("filter").value = "";
        $("list").scrollTop = 0;
    }
    $("up").disabled = !state.up;
    $("crumbs").replaceChildren(...state.crumbs.flatMap((c, i) => {
        const last = i === state.crumbs.length - 1;
        const part = last ? el("span.peek-crumb.here", {text: c.name, title: c.path})
                          : el("button.peek-crumb", {type: "button", text: c.name, title: c.path});
        if (!last) part.onclick = () => send("go", {path: c.path});
        return i ? [el("span.peek-sep", {text: "›"}), part] : [part];
    }));
    draw();
});

Buddy.on("drag_done", () => { dragging = false; press = null; });
