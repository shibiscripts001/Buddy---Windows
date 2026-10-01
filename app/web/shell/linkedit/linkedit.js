/* Adding or editing a link bar link (core/link_bar_web.py LinkDialog): its
   name and its address. Python checks the address; a bad one comes back
   as "error", shown under the field. */
"use strict";

const {send} = Buddy;
const $ = id => document.getElementById(id);

const save = () => send("save", {name: $("name").value, url: $("url").value});

$("ok").onclick = save;
$("cancel").onclick = () => send("cancel");
$("browse").onclick = () => send("browse", {url: $("url").value});
$("url").oninput = () => { $("url").classList.remove("invalid"); $("error").textContent = ""; };
document.addEventListener("keydown", e => {
    if (e.key === "Escape") send("cancel");
    else if (e.key === "Enter" && e.target.matches("input")) save();
});

Buddy.on("link", link => {
    $("title").textContent = link.title;
    $("ok").textContent = link.ok;
    $("name").value = link.name;
    $("url").value = link.url;
    // A new link starts at its address; a dropped or edited one already
    // has it, so naming it is what's left.
    requestAnimationFrame(() => {
        const field = link.url ? $("name") : $("url");
        field.focus();
        field.select();
    });
});

Buddy.on("picked", picked => {
    $("url").value = picked.url;
    $("url").oninput();
});

Buddy.on("error", error => {
    $("error").textContent = error.text;
    $("url").classList.add("invalid");
    $("url").focus();
});
