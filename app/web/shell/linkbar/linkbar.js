/* The link bar: the user's own links (core/link_bar.py), along the bottom
   of the window - or the top under the desktop layout. The shell
   (core/link_bar_web.py, core/shell_window.py) keeps them; this draws them
   and says what was clicked, right-clicked or dragged where. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

$("add").prepend(icon("plus"));
$("add").onclick = () => send("add");

// Right-click on the bar itself (not a link): Add link, Hide.
$("bar").oncontextmenu = e => {
    e.preventDefault();
    if (!e.target.closest(".link")) send("menu", {index: -1});
};

// More links than fit: the wheel scrolls them sideways.
$("links").addEventListener("wheel", e => {
    if (!e.deltaY) return;
    e.preventDefault();
    $("links").scrollLeft += e.deltaY;
}, {passive: false});

new ResizeObserver(() => send("size", {height: $("bar").getBoundingClientRect().height})).observe($("bar"));

// A link or folder dragged over the bar (core/web_page.py file drops).
Buddy.on("drop_hover", on => $("bar").classList.toggle("drop-over", !!on));

/* Dragging a link moves it: it follows the pointer, a line shows where it
   would land, and letting go there sends the move. A press that barely
   moves is a click. */
const DRAG_START = 5;
let drag = null;

function others(dragged) {
    return [...$("links").children].filter(node => node !== dragged);
}

// Where the dragged link would go: past every other link whose middle is
// left of the pointer.
function landing(dragged, x) {
    return others(dragged).filter(node => {
        const r = node.getBoundingClientRect();
        return r.left + r.width / 2 < x;
    }).length;
}

function clearLanding() {
    [...$("links").children].forEach(node => node.classList.remove("drop-before", "drop-after"));
}

function showLanding(d, to) {
    clearLanding();
    if (to === d.index) return;
    const rest = others(d.node);
    if (to < rest.length) rest[to].classList.add("drop-before");
    else if (rest.length) rest[rest.length - 1].classList.add("drop-after");
}

function endDrag(commit, x) {
    const d = drag;
    drag = null;
    const to = commit && d.moved ? landing(d.node, x) : d.index;
    d.node.classList.remove("dragging");
    d.node.style.transform = "";
    clearLanding();
    if (to !== d.index) send("move", {from: d.index, to});
}

function wire(button, index) {
    button.onclick = () => {
        if (button.dataset.dragged) { delete button.dataset.dragged; return; }
        send("open", {index});
    };
    button.oncontextmenu = e => { e.preventDefault(); send("menu", {index}); };
    button.onpointerdown = e => {
        if (e.button !== 0) return;
        drag = {node: button, index, startX: e.clientX, moved: false};
        button.setPointerCapture(e.pointerId);
    };
    button.onpointermove = e => {
        if (!drag || drag.node !== button) return;
        const dx = e.clientX - drag.startX;
        if (!drag.moved && Math.abs(dx) < DRAG_START) return;
        drag.moved = true;
        button.classList.add("dragging");
        button.style.transform = `translateX(${dx}px)`;
        showLanding(drag, landing(button, e.clientX));
    };
    button.onpointerup = e => {
        if (!drag || drag.node !== button) return;
        if (drag.moved) button.dataset.dragged = "1";   // not a click as well
        endDrag(true, e.clientX);
    };
    button.onpointercancel = () => { if (drag && drag.node === button) endDrag(false, 0); };
}

Buddy.on("linkbar", state => {
    $("bar").dataset.place = state.place;
    $("bar").classList.toggle("no-links", !state.links.length);
    $("links").replaceChildren(...state.links.map((link, index) => {
        // translate="no": the name and address are the user's.
        const button = el("button.link", {type: "button", title: link.url, "data-kind": link.kind, translate: "no"}, [
            icon(link.icon || (link.kind === "path" ? "folder" : "globe")),
            el("span.link-name", {text: link.name}),
        ]);
        wire(button, index);
        return button;
    }));
    if (!state.links.length) {
        $("links").append(el("span.link-empty", {text: "Add links to web pages, files or folders – or drag one here."}));
    }
});
