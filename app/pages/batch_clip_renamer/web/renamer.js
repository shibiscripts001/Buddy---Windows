/*
 * Batch Clip Renamer's view. The form's fields live here (they are what
 * the user types into) and are reported to Python as they change; the
 * preview, counts and every rename decision come back from page.py.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, icon, send} = Buddy;

let mode = "sequential";
let state = null;
const fields = {base: $("base"), start: $("start"), find: $("find"), replace: $("replace")};
const matchCase = $("match-case");
const onlyChanges = $("only-changes");

$("refresh").append(icon("refresh"), "Refresh");

// ----------------------------------------------------------------- form

function setMode(next) {
    mode = next;
    for (const tab of document.querySelectorAll(".tabs [data-mode]")) {
        tab.setAttribute("aria-selected", String(tab.dataset.mode === mode));
    }
    $("fields-sequential").hidden = mode !== "sequential";
    $("fields-replace").hidden = mode !== "replace";
}

function setScope(scope) {
    for (const b of document.querySelectorAll("#scope [data-scope]")) {
        b.setAttribute("aria-pressed", String(b.dataset.scope === scope));
    }
}

let timer = 0;
function reportInputs(now) {
    clearTimeout(timer);
    const go = () => send("inputs", {
        mode,
        base: fields.base.value,
        start: fields.start.value,
        find: fields.find.value,
        replace: fields.replace.value,
        match_case: matchCase.checked,
    });
    if (now) go(); else timer = setTimeout(go, 90);
}

for (const tab of document.querySelectorAll(".tabs [data-mode]")) {
    tab.onclick = () => { setMode(tab.dataset.mode); reportInputs(true); focusFirst(); };
}
for (const b of document.querySelectorAll("#scope [data-scope]")) {
    b.onclick = () => { setScope(b.dataset.scope); send("scope", {scope: b.dataset.scope}); };
}
for (const input of Object.values(fields)) {
    input.addEventListener("input", () => reportInputs(false));
    input.addEventListener("keydown", e => {
        if (e.key === "Enter" && !$("rename").disabled) { reportInputs(true); send("rename"); }
    });
}
matchCase.onchange = () => reportInputs(true);
onlyChanges.onchange = () => state && drawPreview(state);

function focusFirst() {
    (mode === "sequential" ? fields.base : fields.find).focus();
}

Buddy.on("inputs", data => {
    fields.base.value = data.base;
    fields.start.value = data.start;
    fields.find.value = data.find;
    fields.replace.value = data.replace;
    matchCase.checked = !!data.match_case;
    setMode(data.mode);
    setScope(data.scope);
    focusFirst();
});

// -------------------------------------------------------------- preview

function emptyState(title, detail, button) {
    return el("div.empty", {}, [
        el("div.strong", {text: title}),
        detail ? el("div.small", {text: detail}) : null,
        button || null,
    ]);
}

function drawPreview(s) {
    const box = $("preview");
    if (!s.connected) {
        box.replaceChildren(emptyState(
            "Not connected to Resolve",
            "Open Resolve with a project, then press Connect.",
            el("button.btn.accent", {text: "Connect", onclick: () => send("connect")}),
        ));
        return;
    }
    if (s.error) {
        box.replaceChildren(emptyState(s.error, s.scope === "selected"
            ? "Select clips in Resolve's Media Pool – this updates by itself."
            : "Open a bin with clips in Resolve's Media Pool – this updates by itself."));
        return;
    }
    const rows = onlyChanges.checked && s.changes ? s.rows.filter(r => r.changed) : s.rows;
    const body = rows.map(r => el(`tr.${r.changed ? "changed" : "same"}`, {}, [
        el("td.name", {text: r.old, translate: "no"}),
        el("td.arrow", {text: "→"}),
        el("td.name.new", {}, [el("span", {text: r.new, translate: "no"}),
                               r.skipped ? el("span.chip.tag", {text: `unchanged - ${r.skipped}`}) : null]),
    ]));
    box.replaceChildren(el("table.table", {}, [
        el("thead", {}, el("tr", {}, [el("th", {text: "Current name"}), el("th.arrow"), el("th", {text: "New name"})])),
        el("tbody", {}, body),
    ]));
}

Buddy.on("state", s => {
    state = s;
    setScope(s.scope);
    const where = s.connected && !s.error && s.where ? s.where : "Preview";
    // A bin's name is the user's; "Preview" and "Selected clips" are ours.
    $("where").translate = !(s.scope === "bin" && where !== "Preview" && where !== "Current bin");
    $("where").textContent = where;
    $("summary").replaceChildren(...(!s.connected || s.error ? [] : [
        el("span", {text: s.total === 1 ? "1 clip" : `${s.total} clips`}), " · ",
        el("span", {text: s.changes ? `${s.changes} will be renamed` : "nothing to rename yet"}),
    ]));
    $("only-changes-box").hidden = !s.connected || !!s.error;
    drawPreview(s);

    // A problem with a field - an unusable Start #, say - is shown on the
    // field. An empty required field just keeps Rename off, quietly.
    for (const input of Object.values(fields)) input.classList.remove("invalid");
    const p = s.problem;
    if (p && p.message && fields[p.field]) fields[p.field].classList.add("invalid");
    $("problem").textContent = p && p.message ? p.message : "";

    $("dupes").textContent = s.duplicates.length
        ? `More than one clip would be named ${s.duplicates.slice(0, 3).map(n => `"${n}"`).join(", ")}` +
          `${s.duplicates.length > 3 ? ` and ${s.duplicates.length - 3} more` : ""}. Resolve allows it, but check that's what you meant.`
        : "";

    const rename = $("rename");
    rename.disabled = !s.connected || !!s.error || !!p || !s.changes;
    rename.textContent = !s.changes ? "Rename" : s.changes === 1 ? "Rename 1 clip" : `Rename ${s.changes} clips`;

    const undo = $("undo");
    undo.hidden = !s.undo;
    if (s.undo) {
        undo.replaceChildren(icon("undo"), `Undo last rename (${s.undo.count})`);
        undo.title = s.undo.count === 1 ? `Put back the 1 name changed in ${s.undo.where}`
                                        : `Put back the ${s.undo.count} names changed in ${s.undo.where}`;
    }
});

// ------------------------------------------------------------- activity

Buddy.on("log", entries => {
    $("activity").hidden = !entries.length;
    $("log").replaceChildren(...entries.slice().reverse().map(e =>
        el(`li.${e.kind}`, {}, [el("span.time", {text: e.time}), e.text])));
    if (entries.length) $("activity").open = true;
});

Buddy.on("toast", t => Buddy.toast(t.text, 3500));

$("refresh").onclick = () => send("refresh");
$("rename").onclick = () => { reportInputs(true); send("rename"); };
$("undo").onclick = () => send("undo");

setMode(mode);
