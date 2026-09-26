/* "Rebuild from PDF..." (bundle_dialog.py). Python runs the build; this shows
   where it's got to. A PDF dropped from Explorer is caught on the Python
   side (the page can't see a dropped file's path). */
"use strict";

const {el, send} = Buddy;
const $ = id => document.getElementById(id);

$("browse").onclick = () => send("browse");
$("build").onclick = () => send("start");
$("close").onclick = () => send("close");
document.addEventListener("keydown", e => { if (e.key === "Escape") send("close"); });

Buddy.on("drop_hover", on => $("drop").classList.toggle("hover", on));

Buddy.on("build", b => {
    $("drop-label").textContent = b.pdf ? b.pdf.name : "Drop the Reference Manual PDF here";
    $("drop").classList.toggle("off", b.running);
    $("browse").disabled = b.running;
    $("pdf").hidden = !b.pdf;
    $("pdf").textContent = b.pdf ? b.pdf.detail : "";
    $("checks").replaceChildren(...b.checks.map(c => el(`div.check-row.${c.tone}`, {}, [
        el("span", {text: c.text}), c.code ? el("code", {text: c.code}) : null,
    ])));
    $("saves").textContent = b.saves_to;
    $("bar").hidden = b.progress === null;
    if (b.progress !== null) {
        $("bar-fill").style.width = `${b.progress}%`;
        $("bar-text").textContent = `${b.progress}%`;
    }
    $("status").textContent = b.status;
    $("build").disabled = !b.can_build;
    $("close").textContent = b.running ? "Stop" : "Close";
    $("close").disabled = b.closing;
});
