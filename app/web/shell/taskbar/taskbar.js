/* The desktop layout's taskbar: Programs, the pinned and open tools, and
   the tray. The shell (core/desk_web.py, core/shell_window.py) decides
   everything; this draws it and says what was clicked. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
const left = node => Math.round(node.getBoundingClientRect().left);

$("programs").prepend(icon("programs"));
$("cascade").append(icon("cascade"));
$("tile").append(icon("tile"));
$("settings").append(icon("gear"));
$("bug").append(icon("bug"));
$("bug").onclick = () => send("bug");
$("programs").onclick = () => send("programs", {left: left($("programs"))});
$("cascade").onclick = () => send("arrange", {how: "cascade"});
$("tile").onclick = () => send("arrange", {how: "tile"});
$("settings").onclick = () => send("settings");
$("orb").onclick = () => send("orb");
$("conn").onclick = () => send("reconnect");

function tick() {
    $("clock").textContent = new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}
tick();
setInterval(tick, 15000);

new ResizeObserver(() => send("size", {height: $("bar").getBoundingClientRect().height})).observe($("bar"));

// The Web tab playing sound (with a pause button), or paused (a play
// button) - as beside its name in the sidebar (rail.js), button first.
function taskBadge(item) {
    if (item.badge !== "sound" && item.badge !== "paused") return [];
    const paused = item.badge === "paused";
    const label = paused ? "Play" : "Pause";
    const media = el("span.task-media", {role: "button", title: label, "aria-label": label,
                                         onclick: e => { e.stopPropagation(); send("media", {id: item.id}); }},
                     [icon(paused ? "play" : "pause")]);
    return paused ? [media] : [media, el("span.task-badge", {title: "Playing sound"}, [icon("volume")])];
}

Buddy.on("taskbar", t => {
    $("tasks").replaceChildren(...t.items.map(item => {
        const button = el("button.task", {
            type: "button", title: item.label, "data-state": item.state,
            "aria-pressed": String(item.active), "data-pinned": item.pinned ? "true" : undefined,
            onclick: () => send("task", {id: item.id}),
        }, [el("i.task-swatch", {style: `background:${item.color}`}), el("span.task-label", {text: item.label}),
            ...taskBadge(item)]);
        button.oncontextmenu = e => { e.preventDefault(); send("task_menu", {id: item.id, left: left(button)}); };
        return button;
    }));
    const conn = $("conn");
    conn.replaceChildren(el("i.task-dot"), el("span", {text: t.connected ? "Resolve" : "Not connected"}));
    conn.className = `btn ghost task-conn ${t.connected ? "ok" : "bad"}`;
    conn.title = t.connected ? "Connected to DaVinci Resolve – click to reconnect"
                             : "Not connected to Resolve – click to connect";
    $("orb").hidden = !t.orb;
});
