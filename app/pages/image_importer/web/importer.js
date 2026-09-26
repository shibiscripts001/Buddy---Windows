/*
 * Image Importer's view. Python keeps the list and does every file and
 * Resolve step; this draws the thumbnails and reports clicks, Ctrl+V and
 * the bin name. Which thumbnails are ticked for removal lives here.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, icon, send} = Buddy;

let items = [];
let downloads = [];
const selected = new Set();

$("drop-icon").append(icon("image"));
for (const b of document.querySelectorAll("[data-action]")) b.addEventListener("click", () => send(b.dataset.action));

// Ctrl+V anywhere but a text box pastes into the list (Python reads the
// clipboard - the page can't).
document.addEventListener("keydown", e => {
    const inField = e.target.closest("input, textarea, [contenteditable]");
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "v" && !inField) {
        e.preventDefault();
        send("paste");
    } else if ((e.key === "Delete" || e.key === "Backspace") && !inField && selected.size) {
        e.preventDefault();
        removeSelected();
    }
});

let binTimer = 0;
$("bin-name").addEventListener("input", () => {
    clearTimeout(binTimer);
    binTimer = setTimeout(() => { send("bin_name", {value: $("bin-name").value}); drawFooter(); }, 200);
    drawFooter();
});

function removeSelected() {
    send("remove", {ids: [...selected]});
    selected.clear();
}
$("remove-selected").onclick = removeSelected;
$("clear").onclick = async () => {
    if (await Buddy.confirm({title: "Clear the list?", text: "The images come off the list. Their files stay in the save folder.", ok: "Clear"})) {
        selected.clear();
        send("clear");
    }
};

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function thumb(item) {
    const frame = el("div.frame");
    if (item.exists) {
        const img = el("img", {src: item.url, alt: "", loading: "lazy", draggable: "false"});
        // Formats Chromium can't draw (TIFF) get a plain file icon.
        img.onerror = () => frame.replaceChildren(icon("image"));
        frame.append(img);
    } else {
        frame.append(icon("warning"));
    }
    const node = el(`div.thumb${item.exists ? "" : ".missing"}${selected.has(item.id) ? ".selected" : ""}`, {
        title: item.path, tabindex: "0",
        onclick: e => {
            if (e.target.closest(".x")) return;
            selected.has(item.id) ? selected.delete(item.id) : selected.add(item.id);
            draw();
        },
    }, [
        frame,
        el("div.name", {text: item.name}),
        el("div.meta", {text: item.exists ? `${item.source} · ${item.added_at}` : "File not found"}),
        el("button.x", {type: "button", title: "Take off the list", "aria-label": `Remove ${item.name}`, text: "×",
            onclick: () => { selected.delete(item.id); send("remove", {ids: [item.id]}); }}),
    ]);
    return node;
}

function pending(d) {
    return el("div.thumb.pending", {title: d.url}, [
        el("div.frame", {}, el("span.dots", {}, [el("i"), el("i"), el("i")])),
        el("div.name", {text: "Downloading…"}),
        el("div.meta", {text: d.url}),
    ]);
}

function drawFooter() {
    const present = items.filter(i => i.exists).length;
    const bin = $("bin-name").value.trim();
    const go = $("import");
    go.disabled = !present || !bin;
    go.textContent = present ? `Import ${plural(present, "image")}${bin ? ` into "${bin}"` : ""}` : "Import";
    go.title = !bin && present ? "Type a bin name first" : "";
}

function draw() {
    for (const id of [...selected]) if (!items.some(i => i.id === id)) selected.delete(id);
    const any = items.length + downloads.length > 0;
    $("dropzone").hidden = any;
    $("gallery-head").hidden = !any;
    $("grid").replaceChildren(...items.map(thumb), ...downloads.map(pending));
    $("count").textContent = items.length ? plural(items.length, "image") : "Downloading";
    $("selected-note").textContent = selected.size ? `${selected.size} selected – Delete removes` : "";
    $("remove-selected").hidden = !selected.size;
    $("clear").hidden = !items.length;
    drawFooter();
}

Buddy.on("settings", s => {
    const folder = $("save-folder");
    folder.replaceChildren(el("bdi", {text: s.save_folder}));
    folder.title = s.save_folder;
    if (document.activeElement !== $("bin-name")) $("bin-name").value = s.bin_name;
    drawFooter();
});
Buddy.on("items", list => { items = list; draw(); });
Buddy.on("downloads", list => { downloads = list; draw(); });
Buddy.on("drop_hover", on => { $("drop-veil").hidden = !on; });

Buddy.on("log", entries => {
    $("activity").hidden = !entries.length;
    $("log").replaceChildren(...entries.slice().reverse().map(e =>
        el(`li.k-${e.kind}`, {}, [el("span.time", {text: e.time}), e.text])));
});
Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));
Buddy.on("toast", t => Buddy.toast(t.text, 3000));

draw();
