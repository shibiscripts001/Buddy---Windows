/* Adding or editing a link bar link (core/link_bar_web.py LinkDialog): its
   name, its address and its icon ("Auto": the bar picks one from the
   address). Python checks the address; a bad one comes back as "error",
   shown under the field. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

let chosen = "";
const save = () => send("save", {name: $("name").value, url: $("url").value, icon: chosen});

function choose(name) {
    chosen = name;
    for (const b of $("icons").children) b.setAttribute("aria-checked", String(b.dataset.icon === name));
}

function drawIcons(names) {
    const auto = el("button.btn.small.link-icon.link-icon-auto", {type: "button", role: "radio", "data-icon": "",
                                                                   text: "Auto", title: "Chosen from the address"});
    $("icons").replaceChildren(auto, ...names.map(name => {
        const label = name[0].toUpperCase() + name.slice(1);
        const b = el("button.btn.icon.link-icon", {type: "button", role: "radio", "data-icon": name,
                                                   title: label, "aria-label": label});
        b.append(icon(name));
        return b;
    }));
    for (const b of $("icons").children) b.onclick = () => choose(b.dataset.icon);
}

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
    drawIcons(link.icons || []);
    choose(link.icon || "");
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
