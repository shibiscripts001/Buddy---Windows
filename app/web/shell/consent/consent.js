/* The write-consent gate (core/write_consent.py). Python says how the typing
   is going, and checks the sentence again itself when Enable is pressed. */
"use strict";

const {el, send} = Buddy;
const $ = id => document.getElementById(id);

$("typed").addEventListener("input", () => send("type", {text: $("typed").value}));
$("typed").addEventListener("keydown", e => { if (e.key === "Enter" && !$("enable").disabled) enable(); });
$("enable").onclick = enable;
$("cancel").onclick = () => send("cancel");
document.addEventListener("keydown", e => { if (e.key === "Escape") send("cancel"); });

function enable() { send("enable", {text: $("typed").value}); }

Buddy.on("consent", c => {
    $("title").textContent = c.title;
    $("body").replaceChildren(...c.body.map(t => el("li", {text: t})));
    $("sentence").textContent = c.sentence;
    requestAnimationFrame(() => $("typed").focus());
});

Buddy.on("typed", t => {
    $("enable").disabled = !t.ok;
    $("status").textContent = t.status;
});
