/* Organize sidebar (core/nav_organizer.py). Python keeps the layout being
   edited; this draws it and reports drags, ticks and divider edits. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
const edit = (action, extra) => send("edit", Object.assign({action}, extra || {}));

let rows = [];
let selected = 0;

async function askLabel(title, value) {
    const input = el("input.field", {maxlength: "40", placeholder: "Leave blank for a plain line"});
    input.value = value || "";
    let answer = null;
    return new Promise(resolve => {
        const done = close => { answer = input.value; close(); };
        const m = Buddy.modal({
            title, body: [el("label.lbl", {}, ["Divider name (leave blank for a plain line):", input])],
            buttons: [{label: "Cancel"}, {label: "OK", kind: "accent", onClick: done}],
            onClose: () => resolve(answer),
        });
        input.addEventListener("keydown", e => { if (e.key === "Enter") done(m.close); });
        requestAnimationFrame(() => input.select());
    });
}

$("add").onclick = async () => {
    const label = await askLabel("Add divider", "");
    if (label !== null) edit("add", {index: selected, label});
};
$("show-all").onclick = () => edit("show_all");
$("reset").onclick = () => edit("reset");
$("cancel").onclick = () => send("cancel");
$("save").onclick = () => send("save");
document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !document.querySelector(".modal-backdrop")) send("cancel");
});

function rowEl(r, i) {
    const li = el(`li.org-row${r.type === "divider" ? ".divider" : ""}`, {draggable: "true", tabindex: "0",
        "aria-selected": String(i === selected), onclick: () => { selected = i; draw(); }});
    li.addEventListener("dragstart", e => { e.dataTransfer.setData("text/x-org-row", String(i)); li.classList.add("dragging"); });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    li.addEventListener("dragover", e => { if ([...e.dataTransfer.types].includes("text/x-org-row")) { e.preventDefault(); li.classList.add("drop-on"); } });
    li.addEventListener("dragleave", () => li.classList.remove("drop-on"));
    li.addEventListener("drop", e => {
        li.classList.remove("drop-on");
        const from = Number(e.dataTransfer.getData("text/x-org-row"));
        if (Number.isInteger(from) && from !== i) { e.preventDefault(); selected = i; edit("move", {from, to: i}); }
    });
    li.addEventListener("keydown", e => {
        if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
            e.preventDefault();
            const to = i + (e.key === "ArrowUp" ? -1 : 1);
            if (to >= 0 && to < rows.length) { selected = to; edit("move", {from: i, to}); }
        }
    });
    const grip = el("span.org-grip", {text: "⋮⋮", title: "Drag to move (or Alt + Up/Down)"});
    if (r.type === "divider") {
        li.append(grip, el(`span.org-divider${r.label ? ".named" : ""}`, {text: r.label || "Plain line"}),
            el("button.btn.ghost.small", {type: "button", text: "Rename…", onclick: async e => {
                e.stopPropagation();
                const label = await askLabel("Rename divider", r.label);
                if (label !== null) edit("rename", {index: i, label});
            }}),
            el("button.btn.ghost.small", {type: "button", text: "Remove", onclick: e => { e.stopPropagation(); edit("remove", {index: i}); }}));
        li.addEventListener("dblclick", async () => {
            const label = await askLabel("Rename divider", r.label);
            if (label !== null) edit("rename", {index: i, label});
        });
    } else {
        const box = el("input", {type: "checkbox", checked: r.visible, "aria-label": `Show ${r.label} in the sidebar`,
                                 onclick: e => e.stopPropagation(), onchange: e => edit("visible", {index: i, visible: e.target.checked})});
        li.append(grip, el("label.check.org-tool", {}, [box, el("span", {text: r.label}),
            r.placeholder ? el("span.muted.small", {text: "(not yet integrated)"}) : null]));
    }
    return li;
}

function draw() {
    $("list").replaceChildren(...rows.map(rowEl));
}

Buddy.on("organizer", o => {
    rows = o.rows;
    selected = Math.min(selected, Math.max(0, rows.length - 1));
    $("note").textContent = o.note;
    $("reset").disabled = !o.can_reset;
    draw();
});
