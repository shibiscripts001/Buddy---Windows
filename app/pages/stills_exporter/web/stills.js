/*
 * Stills Exporter's view. page.py makes every Resolve call and keeps the
 * grabbed-stills list; this draws them and reports clicks.
 */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

$("refresh").append(icon("refresh"), el("span", {text: "Refresh"}));
for (const node of document.querySelectorAll("[data-action]")) {
    node.addEventListener("click", () => send(node.dataset.action));
}

let STATE = null, GRABBED = [], OPTIONS = null;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

Buddy.on("state", s => {
    STATE = s;
    const t = $("timeline");
    t.classList.toggle("bad", !s.timeline);
    t.replaceChildren(s.timeline
        ? el("span", {}, [el("b", {text: s.timeline}), ` · ${plural(s.colors.reduce((a, c) => a + c.count, 0), "marker")}`])
        : el("span", {text: s.problem || "No timeline open"}));
    t.hidden = !s.connected;   // offline is said once, in Buddy's header

    $("colours").replaceChildren(...s.colors.map(c => el(`button.colour${c.name === s.color ? ".on" : ""}`, {
        type: "button", "aria-pressed": String(c.name === s.color), title: `${c.name} – ${plural(c.count, "marker")}`,
        onclick: () => send("options", {color: c.name}),
    }, [el("i.dot", {style: `background:${c.hex}`}), el("span", {text: c.name}), c.count ? el("b", {text: String(c.count)}) : null])));

    const hex = (s.colors.find(c => c.name === s.color) || {}).hex;
    $("add-marker").disabled = !s.timeline;
    $("markers-head").textContent = s.timeline ? `${plural(s.markers.length, s.color + " marker")} on this timeline` : "";
    $("markers").replaceChildren(...s.markers.map(m => el("li", {}, [
        el("i.dot", {style: `background:${hex}`}), el("span.tc", {text: m.timecode}), el("span.name", {text: m.name || ""}),
    ])));
    const grab = $("grab");
    grab.disabled = !s.markers.length;
    // The button always says what it does; why it can't yet is a note, not
    // the button's label.
    grab.textContent = s.markers.length ? `Grab ${plural(s.markers.length, "still")} at the ${s.color} markers` : "Grab stills";
    $("grab-note").hidden = !(s.timeline && !s.markers.length);
    $("grab-note").textContent = `No ${s.color} markers on this timeline yet – add them in step 1.`;
});

$("add-marker").onclick = () => {
    send("add_marker", {name: $("marker-name").value});
    $("marker-name").value = "";
};
$("marker-name").addEventListener("keydown", e => { if (e.key === "Enter") $("add-marker").click(); });

Buddy.on("grabbed", list => {
    GRABBED = list;
    const hexOf = name => ((STATE && STATE.colors.find(c => c.name === name)) || {}).hex || "transparent";
    $("grabbed-head").textContent = list.length ? `${plural(list.length, "still")} grabbed this session` : "Nothing grabbed yet this session.";
    $("clear").hidden = !list.length;
    $("grabbed").replaceChildren(...list.map(g => el("li", {}, [
        el("i.dot", {style: `background:${hexOf(g.color)}`}), el("span.tc", {text: g.timecode}), el("span.name", {text: g.name || g.color}),
        el("button.btn.ghost.icon.x", {type: "button", title: "Take it off this list (the gallery keeps it)", text: "×",
                                       onclick: () => send("remove_grabbed", {id: g.id})}),
    ])));
    drawExport();
});

$("clear").onclick = () => send("clear");

Buddy.on("options", o => {
    OPTIONS = o;
    $("format").replaceChildren(...o.formats.map(f => el("button", {
        type: "button", text: f, "aria-pressed": String(f === o.format), onclick: () => send("options", {format: f}),
    })));
    if (document.activeElement !== $("prefix")) $("prefix").value = o.prefix;
    const folder = $("folder");
    // Trimmed from the left, so the end of a long path shows.
    folder.replaceChildren(el("bdi", {text: o.folder || "No folder chosen"}));
    folder.title = o.folder;
    folder.classList.toggle("unset", !o.folder);
    $("delete-after").checked = o.delete_after;
    $("open-folder").disabled = !o.folder;
    drawExport();
});

function drawExport() {
    if (!OPTIONS) return;
    const b = $("export");
    b.disabled = !GRABBED.length || !OPTIONS.folder;
    b.textContent = GRABBED.length ? `Export ${plural(GRABBED.length, "still")}` : "Export stills";
    b.title = !OPTIONS.folder ? "Choose a folder first" : !GRABBED.length ? "Grab some stills first" : "";
}

$("prefix").addEventListener("change", e => send("options", {prefix: e.target.value}));
$("delete-after").onchange = e => send("options", {delete_after: e.target.checked});

$("export").onclick = async () => {
    if (OPTIONS.delete_after) {
        const yes = await Buddy.confirm({
            title: "Delete from the gallery?", danger: true, ok: "Export and delete",
            text: `Export ${plural(GRABBED.length, "still")}, then delete them from Resolve's gallery?\n\nThe exported image files are kept – only the gallery copies go, and that can't be undone from here.`,
        });
        if (!yes) return;
        send("export", {confirmed: true});
    } else {
        send("export", {});
    }
};

Buddy.on("toast", t => Buddy.toast(t.text, t.open_folder ? 6000 : 3000,
    t.open_folder ? {label: "Open folder", onClick: () => send("open_folder")} : null));
Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));

Buddy.on("log", entries => {
    $("activity").hidden = !entries.length;
    $("log").replaceChildren(...entries.slice().reverse().map(e => el(`li.${e.kind}`, {}, [
        el("span.muted", {text: e.time}), " ", el("span", {text: e.text}),
    ])));
});
