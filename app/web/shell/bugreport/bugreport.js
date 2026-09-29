/* Report a bug (core/bug_report.py): the words, the screenshots and what
   goes with them. Python keeps the screenshots and does the sending; this
   draws them and says what was clicked. Everything is set with
   textContent. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
let state = {sent: false, sending: null, text_max: 4000};

const busy = () => state.sent || state.sending !== null;

function counted() {
    const n = $("text").value.length;
    $("count").textContent = n > 3000 ? `${n.toLocaleString()} / ${state.text_max.toLocaleString()}` : "";
}
$("text").addEventListener("input", counted);
$("add").onclick = () => send("add");
$("send").onclick = () => send("send", {text: $("text").value});
$("close").onclick = () => send("close");
document.addEventListener("keydown", e => {
    if (e.key === "Escape") send("close");
    if (e.key === "Enter" && e.ctrlKey && !busy()) send("send", {text: $("text").value});
});
// A picture on the clipboard goes through Python, which reads it itself;
// text pasted into the box is left to the box.
document.addEventListener("paste", e => {
    const items = [...(e.clipboardData ? e.clipboardData.items : [])];
    if (!busy() && items.some(i => i.kind === "file" && i.type.startsWith("image/"))) {
        e.preventDefault();
        send("paste");
    }
});
Buddy.on("drop_hover", on => { $("drop").hidden = !(on && !busy()); });

Buddy.on("bug", b => {
    state = b;
    $("shots").replaceChildren(...b.shots.map((s, index) => el("figure.bug-shot", {}, [
        el("img", {src: s.preview, alt: `Screenshot ${index + 1}`}),
        el("figcaption.set-hint", {text: s.label, translate: "no"}),
        busy() ? null : el("button.btn.ghost.icon.bug-remove", {
            type: "button", title: "Remove", "aria-label": "Remove", onclick: () => send("remove", {index}),
        }, [icon("trash")]),
    ])));
    $("shots").hidden = !b.shots.length;
    $("shots-hint").textContent = `Paste (Ctrl+V) or drop them here too – up to ${b.max}.`;
    $("add").disabled = busy() || !b.can_add;
    $("preparing").hidden = !b.preparing;
    $("preparing").textContent = b.preparing === 1 ? "Preparing a screenshot…" : `Preparing ${b.preparing} screenshots…`;
    $("details").replaceChildren(...b.details.flatMap(d => [el("dt", {text: d.label}),
                                                            el("dd", {text: d.value, translate: "no"})]));
    $("who").textContent = b.who;
    $("text").disabled = busy();
    $("send").hidden = b.sent;
    $("send").disabled = busy();
    $("close").textContent = b.sent ? "Close" : "Cancel";
    const status = $("status");
    status.className = `set-hint bug-status${b.error ? " danger" : b.sent ? " success" : ""}`;
    status.textContent = b.error || (b.sent ? "Thanks – your report was sent."
                                     : b.sending !== null ? `Sending… ${b.sending}%` : "");
    if (b.sent) $("close").focus();
    else if (b.focus) $("text").focus();
    counted();
});
requestAnimationFrame(() => $("text").focus());
