/* alert() / confirm() (core/web_page.py MessageDialog). The text is set with
   textContent - it can hold anything an error message says. */
"use strict";

const {send} = Buddy;
const $ = id => document.getElementById(id);

$("ok").onclick = () => send("answer", {ok: true});
$("cancel").onclick = () => send("answer", {ok: false});
document.addEventListener("keydown", e => {
    if (e.key === "Escape") send("answer", {ok: false});
});

Buddy.on("message", m => {
    $("title").textContent = m.title;
    $("text").textContent = m.text;
    $("ok").textContent = m.ok;
    $("ok").className = `btn ${m.danger ? "danger" : "accent"}`;
    $("cancel").hidden = !m.cancel;
    $("cancel").textContent = m.cancel || "";
    requestAnimationFrame(() => (m.cancel && m.danger ? $("cancel") : $("ok")).focus());
});
