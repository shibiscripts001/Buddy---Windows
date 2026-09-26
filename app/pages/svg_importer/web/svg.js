/*
 * SVG Importer's view. Python holds the file lists and options and runs
 * every conversion; this draws them and reports clicks.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, icon, send} = Buddy;

let state = {kind: "svg", busy: null};
let files = {svg: [], lottie: []};
let options = null;
let details = [];

const LABEL = {svg: "SVG", lottie: "Lottie"};
const EXT = {svg: ".svg", lottie: ".json"};
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

$("comp-refresh").append(icon("refresh"));
for (const b of document.querySelectorAll("[data-action]")) b.addEventListener("click", () => send(b.dataset.action));
for (const b of document.querySelectorAll("#kinds [data-kind]")) {
    b.onclick = () => { state.kind = b.dataset.kind; draw(); send("kind", {kind: state.kind}); };
}
for (const group of document.querySelectorAll("[data-option]")) {
    for (const b of group.querySelectorAll("[data-value]")) {
        b.onclick = () => send("option", {kind: state.kind, name: group.dataset.option, value: b.dataset.value});
    }
}
$("consolidate").onchange = e => send("option", {kind: "svg", name: "consolidate", value: e.target.checked});
$("show-debug").onchange = () => drawDetails();

function drawFiles() {
    const list = files[state.kind] || [];
    $("files-title").textContent = list.length ? `${plural(list.length, LABEL[state.kind] + " file")}` : "Files";
    $("file-list").replaceChildren(...list.map(f => el("li", {title: f.path}, [
        icon(state.kind === "svg" ? "image" : "play"),
        el("span.name", {text: f.name}),
        el("span.where", {}, el("bdi", {text: f.path.slice(0, f.path.length - f.name.length)})),
        el("button.x", {type: "button", text: "×", title: "Take off the list", disabled: !!state.busy,
            onclick: () => send("remove_file", {path: f.path})}),
    ])));
    const hint = $("drop-hint");
    hint.replaceChildren(...(list.length ? [] : [el("div", {}, [
        el("div", {}, [el("b", {text: `Drag ${EXT[state.kind]} files here`}), " or use Add files"]),
        el("div.small", {text: state.kind === "svg"
            ? "Several files come in together, laid out side by side – one paste."
            : "Exported from After Effects with the Bodymovin / Lottie plugin."}),
    ])]));
    $("clear-files").hidden = !list.length;
}

function drawOptions() {
    if (!options) return;
    const o = options[state.kind];
    for (const group of document.querySelectorAll("[data-option]")) {
        for (const b of group.querySelectorAll("[data-value]")) {
            b.setAttribute("aria-pressed", String(o[group.dataset.option] === b.dataset.value));
        }
    }
    $("consolidate-box").hidden = state.kind !== "svg";
    $("consolidate").checked = !!options.svg.consolidate;
    $("lottie-note").hidden = state.kind !== "lottie";
}

function drawDetails() {
    const box = $("details");
    box.hidden = !details.length;
    const log = $("details-log");
    log.classList.toggle("hide-debug", !$("show-debug").checked);
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    log.replaceChildren(...details.map(d => el(`li.k-${d.kind}`, {text: d.text})));
    if (atBottom) log.scrollTop = log.scrollHeight;
    const notes = details.filter(d => d.kind === "note").length;
    const errors = details.filter(d => d.kind === "error").length;
    $("details-note").textContent = [errors ? plural(errors, "error") : "", notes ? plural(notes, "note") : ""].filter(Boolean).join(" · ");
}

function draw() {
    for (const b of document.querySelectorAll("#kinds [data-kind]")) {
        b.setAttribute("aria-pressed", String(b.dataset.kind === state.kind));
        b.disabled = !!state.busy && b.dataset.kind !== state.kind;
    }
    drawFiles();
    drawOptions();
    const n = (files[state.kind] || []).length;
    const convert = $("convert");
    const converting = state.busy === "convert";
    convert.disabled = !!state.busy || !n;
    convert.textContent = converting ? "Converting…" : n > 1 ? `Convert ${n} files & copy for Fusion` : "Convert & copy for Fusion";
    $("go-note").textContent = n > 1 ? "They're pasted as one group each, side by side." : "";
    for (const id of ["dump", "comp-refresh"]) $(id).disabled = !!state.busy;
    for (const b of document.querySelectorAll("[data-action='add_files'], [data-action='add_folder'], [data-action='clear_files']")) {
        b.disabled = !!state.busy;
    }
}

Buddy.on("state", s => { state = s; draw(); });
Buddy.on("files", f => { files = f; draw(); });
Buddy.on("options", o => { options = o; drawOptions(); });
Buddy.on("details", d => { details = d; drawDetails(); });
Buddy.on("drop_hover", on => { $("drop-veil").hidden = !on; });

Buddy.on("comp", c => {
    const box = $("comp");
    box.dataset.status = c.status;
    $("comp-text").textContent =
        c.status === "ok" ? `Fusion comp ${c.width} × ${c.height}${c.fps ? ` · ${Number(c.fps.toFixed(3))} fps` : ""}` :
        c.status === "none" ? "No Fusion comp open" :
        "Fusion comp not read yet";   // offline included: Buddy's header says that
    box.title = c.status === "none" && c.error ? c.error : "Size and frame rate used for the conversion – read again every time you convert";
});

Buddy.on("result", r => {
    const box = $("result");
    box.hidden = !r;
    if (!r) return;
    box.className = `card result ${r.ok ? "ok" : "bad"}`;
    if (r.ok) {
        const what = r.count === 1 ? `"${r.names[0]}"` : plural(r.count, "file");
        box.replaceChildren(
            el("div.badge", {}, icon("check")),
            el("div.text", {}, [
                el("div.strong", {text: `Copied ${what} for Fusion`}),
                el("div.muted", {}, ["On the Fusion page, click in the Flow view and press ", el("kbd", {text: "Ctrl"}), " + ", el("kbd", {text: "V"}), "."]),
            ]),
            el("button.btn", {type: "button", text: "Copy again", onclick: () => send("copy_again")}),
        );
    } else {
        box.replaceChildren(
            el("div.badge", {}, icon("warning")),
            el("div.text", {}, [
                el("div.strong", {text: "Couldn't convert"}),
                el("div.muted", {text: r.error}),
            ]),
            el("button.btn.ghost", {type: "button", text: "Show details", onclick: () => { $("details").open = true; $("details").scrollIntoView({behavior: "smooth"}); }}),
        );
    }
});

Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));
Buddy.on("toast", t => Buddy.toast(t.text, 3000));

draw();
