/* Announcements (core/announcements_window.py). Everything is set with
   textContent: an announcement is plain text. */
"use strict";

const {el, send} = Buddy;
const $ = id => document.getElementById(id);

$("close").onclick = () => send("close");
document.addEventListener("keydown", e => { if (e.key === "Escape") send("close"); });

Buddy.on("announcements", a => {
    $("list").replaceChildren(...(a.items.length ? a.items.map(item => el("article.news", {}, [
        el("h2", {}, [item.title, item.new ? el("span.new", {text: "• new"}) : null]),
        el("div.set-hint", {text: item.when}),
        el("p", {text: item.text}),
    ])) : [el("p.muted", {text: "No announcements right now."})]));
    $("close").focus();
});
