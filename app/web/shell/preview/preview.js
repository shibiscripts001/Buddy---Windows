/* A still picture of a link bar web link (core/link_peek_web.py
   PagePreview): Python loads the page out of sight and sends a photo of
   it, which is all this shows. */
"use strict";

const {icon, send} = Buddy;
const $ = id => document.getElementById(id);

$("refresh").append(icon("refresh"));
$("refresh").onclick = () => send("refresh");
$("browser").onclick = () => send("browser");
document.addEventListener("keydown", e => { if (e.key === "Escape") send("close"); });
$("shot").addEventListener("dragstart", e => e.preventDefault());

Buddy.on("preview", p => {
    $("title").textContent = p.name || p.title || p.host;
    $("url").textContent = p.url;
    $("url").title = p.url;
    $("refresh").disabled = p.state === "loading";
    $("frame").dataset.state = p.state;
    $("shot").hidden = !p.image;
    if (p.image) $("shot").src = p.image;
    $("note").textContent = p.state === "loading" ? `Loading a preview of ${p.host}…`
        : p.state === "failed" ? p.error : "";
});
