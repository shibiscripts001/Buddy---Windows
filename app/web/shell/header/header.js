/* The shell header's view: title, the news orb, the Resolve connection, the
   bug report, dual view and Settings. The shell (core/shell_window.py) decides everything. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

$("split").append(icon("split"));
$("settings").append(icon("gear"));
$("bug").append(icon("bug"));
$("bug").onclick = () => send("bug");
for (const b of document.querySelectorAll("[data-action]")) b.onclick = () => send(b.dataset.action);
$("settings").onclick = () => send("settings");
$("orb").onclick = () => send("orb");
$("update").onclick = () => send("update");
$("split").onclick = () => send("split", {on: $("split").getAttribute("aria-pressed") !== "true"});
$("side").onchange = e => send("side", {id: e.target.value});

function reportSize() { send("size", {height: $("header").getBoundingClientRect().height}); }
new ResizeObserver(reportSize).observe($("header"));

// The view is only as tall as the header: while a dropdown is open, the
// shell grows it down over the panes to make room for the list.
Buddy.dropdownRoom = 420;
let shrinkTimer = 0;
document.addEventListener("buddy:dropdown", e => {
    clearTimeout(shrinkTimer);
    if (e.detail.open) send("overlay", {height: Math.ceil(e.detail.bottom + 24)});
    else shrinkTimer = setTimeout(() => send("overlay", {height: 0}), 180);   // after the closing animation
});

Buddy.on("header", h => {
    // The one place Buddy says whether it can reach Resolve; pages only say
    // what they need from it (a timeline, a bin) once it can.
    $("status").textContent = h.connected ? "Connected to Resolve" : "Not connected to Resolve";
    $("connect").textContent = h.connected ? "Reconnect" : "Connect";
    $("connect").title = h.connected ? "Connect to Resolve again – after restarting it, or opening another project"
                                     : "Connect to DaVinci Resolve";
    $("status").className = `status ${h.connected ? "ok" : "bad"}`;
    $("orb").hidden = !h.orb;
    // A newer Buddy (core/updater.py): to install, or installed and waiting for a restart.
    const u = h.update;
    $("update").hidden = !u;
    if (u) {
        $("update").textContent = u.kind === "restart" ? "Restart to update" : "Update";
        $("update").title = u.kind === "restart" ? `Buddy ${u.version} is installed – restart Buddy to start using it`
                                                 : `Buddy ${u.version} is ready to install`;
    }
    // Buddy is the window in use (Off-world's cursor blinks only then).
    document.documentElement.classList.toggle("buddy-active", !!h.active);
    $("split").setAttribute("aria-pressed", String(h.split));
    const side = $("side");
    side.hidden = !h.split;
    side.replaceChildren(...h.choices.map(c => el("option", {value: c.id, text: c.label})));
    if (h.side) side.value = h.side;
});
