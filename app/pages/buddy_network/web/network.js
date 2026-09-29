/*
 * Buddy Network's view. Python keeps the connection, the rooms, the
 * messages and every decision. This draws what it's sent and reports
 * clicks and typing.
 *
 * Safety: the message list is HTML that render.room_html built with every
 * user-typed thing escaped, so it goes in with innerHTML. Its links are
 * "bn-..." actions that are handed to Python and never followed. Everything
 * else here - names, topics, rooms, buddies, and everything in the panels
 * (account, admin...) - goes in with textContent (el() with {text}). The page's Content-Security-Policy stops any script or
 * remote image from loading, whatever a message contains.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, icon, send} = Buddy;

let state = {mode: "off", online: false, can_send: false, me: null, max_chars: 2000, counter_from: 1500};
let room = null;
let people = [];
let buddies = {buddies: [], incoming: [], outgoing: [], blocked: []};
let buddiesDialog = null;
let browseDialog = null;

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

$("hero-icon").append(icon("chat"));
$("browse").append(icon("search"));
$("new-room").append(icon("plus"));
$("send").append(icon("send"));
$("attach").append(icon("image"));
for (const b of document.querySelectorAll("[data-action]")) b.addEventListener("click", () => send(b.dataset.action));
$("turn-off").onclick = async () => {
    if (await Buddy.confirm({title: "Turn off Buddy Network?", text: "Buddy disconnects and stops checking for messages until you turn it on again. Your account, buddies and saved rooms stay.", ok: "Turn off"})) {
        send("turn_off");
    }
};
$("rules-link").onclick = () => send("rules");
$("appear-offline").onchange = e => send("appear_offline", {on: e.target.checked});
$("chat-menu").onclick = e => { const r = e.currentTarget.getBoundingClientRect(); send("chat_menu", {x: r.right, y: r.bottom + 4}); };

// ------------------------------------------------------------------ state

Buddy.on("state", s => {
    state = s;
    $("off").hidden = s.mode === "chat";
    $("chat").hidden = s.mode !== "chat";
    $("unsupported").hidden = s.mode !== "unsupported";
    if (s.mode === "unsupported") $("unsupported").textContent = s.status.text;
    $("turn-on").disabled = $("import-transfer").disabled = s.mode === "unsupported";

    const status = $("status");
    status.hidden = s.mode !== "chat" || !s.status.text;
    status.dataset.tone = s.status.tone || "";
    $("status-text").textContent = s.status.text;
    status.title = s.status.text;

    if (s.me) {
        $("me-avatar").src = s.me.avatar;
        $("me-name").textContent = s.me.label;
    }
    $("me").hidden = !s.me;
    $("appear-offline").checked = !!s.appear_offline;
    $("appear-offline").disabled = !s.online;
    for (const id of ["buddies-btn", "account-btn", "admin-btn", "browse"]) $(id).disabled = !s.online;
    $("new-room").disabled = !s.can_send;
    $("buddies-btn").replaceChildren("Buddies", s.buddies_waiting ? el("span.count-badge", {text: String(s.buddies_waiting)}) : "");
    $("admin-btn").hidden = !s.staff;
    $("admin-btn").replaceChildren("Admin", s.reports ? el("span.count-badge", {text: String(s.reports)}) : "");
    const composer = $("composer");
    composer.disabled = !s.can_send;
    composer.placeholder = s.placeholder;
    composer.title = s.can_send ? "Enter to send, Shift+Enter for a new line" : "";
    $("send").disabled = !s.can_send;
    $("attach").hidden = !s.images;
    $("gif-btn").hidden = !s.gifs;
    if (!s.gifs) closeGifs(false);
    $("chat-menu").disabled = $("safety-btn").disabled = !s.online;
    updateCounter();
});

// ---------------------------------------------------------------- sidebar

Buddy.on("sidebar", sections => {
    const list = $("side-list");
    const nodes = [];
    for (const section of sections) {
        if (section.heading) nodes.push(el("div.side-heading", {text: section.heading}));
        for (const item of section.items) {
            const classes = ["side-item"];
            if (item.current) classes.push("current");
            if (item.unread && !item.muted) classes.push("unread");
            if (item.muted) classes.push("muted");
            const lead = item.kind === "buddy"
                ? el("span.face", {}, [el("img", {src: item.avatar, alt: ""}), el(`i${item.online ? ".on" : ""}`)])
                : el("span.glyph", {text: "#"});
            const label = el("span.label", {translate: "no"}, [item.label, item.tag ? el("span.tag", {text: `#${item.tag}`}) : null]);
            const button = el(`button.${classes.join(".")}`, {
                type: "button", title: [item.sub, item.muted ? "Muted" : ""].filter(Boolean).join(" · "),
                onclick: () => send("open", {key: item.key}),
                oncontextmenu: e => { e.preventDefault(); send("sidebar_menu", {key: item.key, x: e.clientX, y: e.clientY}); },
            }, [
                lead, label,
                item.pinned ? el("span.pin", {text: "📌", title: "Kept permanently"}) : null,
                item.mention ? el("span.at", {text: "@", title: "Someone mentioned you"}) : null,
                item.unread ? el("span.badge", {text: item.unread >= 99 ? "99+" : String(item.unread)}) : null,
            ]);
            nodes.push(button);
        }
    }
    list.replaceChildren(...nodes);
});

// ------------------------------------------------------------------- room

Buddy.on("room", r => {
    if (!room || !r || r.id !== room.id) closeGifs(false);
    room = r;
    $("chat-menu").hidden = !r;
    $("safety-btn").hidden = !r || r.kind !== "dm";
    const title = $("room-title");
    const topic = $("room-topic");
    const avatar = $("room-avatar");
    if (!r) {
        title.textContent = "";
        topic.replaceChildren();
        avatar.hidden = true;
        $("banner").hidden = true;
        return;
    }
    avatar.hidden = !r.avatar;
    if (r.avatar) avatar.src = r.avatar;
    title.replaceChildren(...(r.kind === "dm" ? [r.title] : [el("span.hash", {text: "#"}), r.title]));
    const bits = [];
    if (r.kind === "dm") bits.push(el("span.topic", {}, [el("span.lock", {text: "🔒 "}), r.topic]));
    else if (r.topic) bits.push(el("span.topic", {text: r.topic, translate: "no"}));   // the room owner's words
    for (const f of r.facts) bits.push(el("span.fact", {text: f}));
    topic.replaceChildren(...bits);
    topic.title = topic.textContent;
    $("safety-btn").textContent = r.verified ? "Safety code ✓" : "Safety code";
    const banner = $("banner");
    banner.hidden = !r.banner;
    banner.className = `banner${r.tone === "warn" ? " warn" : ""}`;
    banner.translate = r.tone === "warn";   // a pinned announcement is the room owner's words
    banner.textContent = r.banner ? (r.tone === "warn" ? `⚠ ${r.banner}` : `📌 ${r.banner}`) : "";
});

// --------------------------------------------------------------- messages

const box = $("messages");

Buddy.on("messages", m => {
    const fromBottom = box.scrollHeight - box.scrollTop - box.clientHeight;
    const atBottom = fromBottom < 8;
    // Python-built and escaped (render.room_html) - see the note at the top.
    box.innerHTML = m.html;
    for (const shot of box.querySelectorAll(".shot")) drawShot(shot);
    if (m.to_bottom || (atBottom && !m.keep_position)) box.scrollTop = box.scrollHeight;
    else box.scrollTop = box.scrollHeight - box.clientHeight - fromBottom;
});

// ----------------------------------------------------------------- images
// A message's image is a placeholder ("shot", sized by Python) until
// attachments.py sends it: always a data: URL it has checked (and, in a
// DM, decrypted) - the page never fetches an image itself.

const shots = new Map();   // id -> {url} or {failed: text}
const SHOTS_KEPT = 150;
let cantShow = "";

function drawShot(shot) {
    const known = shots.get(shot.dataset.image);
    const img = shot.querySelector("img");
    shot.classList.toggle("ready", !!(known && known.url));
    shot.classList.toggle("failed", !!(known && known.failed));
    if (known && known.url) { img.src = known.url; img.hidden = false; shot.title = "Click to see it larger"; }
    else if (known && known.failed) { img.hidden = true; shot.textContent = ""; shot.append(img, el("span", {text: known.failed})); shot.title = ""; }
}

function keepShot(id, value) {
    shots.delete(id);
    shots.set(id, value);
    while (shots.size > SHOTS_KEPT) shots.delete(shots.keys().next().value);
    for (const shot of box.querySelectorAll(".shot")) if (shot.dataset.image === id) drawShot(shot);
}

Buddy.on("image", i => {
    if (i.url) keepShot(i.id, {url: i.url});
    else keepShot(i.id, {failed: i.gone ? (i.text || "Image expired.") : (i.failed || cantShow)});
});
Buddy.on("images", all => {
    cantShow = all.cant_show;
    for (const i of all.ready) shots.set(i.id, {url: i.url});
    for (const i of all.failed) shots.set(i.id, {failed: i.gone ? "Image expired." : cantShow});
    for (const shot of box.querySelectorAll(".shot")) drawShot(shot);
});

function lightbox(id, url) {
    const img = el("img.lightbox-img", {src: url, alt: "Image"});
    Buddy.modal({title: "Image", wide: true, body: img, buttons: [
        {label: "Save…", onClick: () => send("save_image", {id})},
        {label: "Close", kind: "accent"},
    ]});
}
Buddy.on("lightbox", l => lightbox(l.id, l.url));

// Every link in a message is an action for Python - never followed here.
box.addEventListener("click", e => {
    const shot = e.target.closest(".shot");
    if (shot) {
        const known = shots.get(shot.dataset.image);
        if (known && known.url) lightbox(shot.dataset.image, known.url);
        return;
    }
    const a = e.target.closest("a");
    if (!a) return;
    e.preventDefault();
    const href = a.getAttribute("href") || "";
    if (href.startsWith("bn-")) send("anchor", {href, x: e.clientX, y: e.clientY});
});
box.addEventListener("auxclick", e => { if (e.target.closest("a")) e.preventDefault(); });

// ----------------------------------------------------------------- search

Buddy.on("search", s => {
    $("search-bar").hidden = !s.open;
    if (document.activeElement !== $("search")) $("search").value = s.query || "";
    if (s.focus) { $("search").focus(); $("search").select(); }
});
let searchTimer = 0;
$("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => send("search", {query: $("search").value}), 150);
});
$("search").addEventListener("keydown", e => { if (e.key === "Escape") { e.preventDefault(); send("close_search"); } });

// ---------------------------------------------------------------- compose

const composer = $("composer");

function autosize() {
    composer.style.height = "auto";
    const h = Math.min(composer.scrollHeight + 2, 160);
    composer.style.height = `${h}px`;
    composer.style.overflowY = composer.scrollHeight > 160 ? "auto" : "hidden";
}

function updateCounter() {
    const n = composer.value.length;
    const c = $("counter");
    c.textContent = n >= state.counter_from ? `${n.toLocaleString()} / ${state.max_chars.toLocaleString()}` : "";
    c.classList.toggle("over", n > state.max_chars);
}

Buddy.on("compose", c => {
    editingPicture = !!c.picture;
    $("compose-bar").hidden = !c.mode;
    $("compose-label").textContent = c.label || "";
    if (c.text !== null && c.text !== undefined) { composer.value = c.text; autosize(); updateCounter(); }
    if (c.focus) {
        composer.focus();
        composer.selectionStart = composer.selectionEnd = composer.value.length;
    }
});
Buddy.on("compose_insert", text => {
    const start = composer.selectionStart, end = composer.selectionEnd;
    composer.setRangeText(text, start, end, "end");
    composer.focus();
    autosize();
});
// A message the server refused comes back to the box - unless something new is being typed.
Buddy.on("compose_restore", text => { if (!composer.value.trim()) { composer.value = text; autosize(); updateCounter(); } });

let attached = false;

function sendMessage() {
    if (!state.can_send || (!composer.value.trim() && !attached && !editingPicture)) return;
    send("send", {text: composer.value});
}
$("send").onclick = sendMessage;

// The picture waiting to go with the next message (attachments.py).
let editingPicture = false;
Buddy.on("attachment", a => {
    attached = !!a.preview;
    $("attachment").hidden = !a.preview && !a.busy;
    $("attachment").classList.toggle("busy", !!a.busy);   // being shrunk (or fetched from GIPHY)
    $("attachment-preview").hidden = !a.preview;
    if (a.preview) $("attachment-preview").src = a.preview;
    $("attachment-label").textContent = a.busy || a.label || "";
    $("attachment-note").textContent = a.busy ? "" : (a.note || "");
    if (a.focus) composer.focus();
});
// A picture pasted into the box goes through Python, which reads the clipboard itself.
composer.addEventListener("paste", e => {
    const items = [...(e.clipboardData ? e.clipboardData.items : [])];
    if (state.images && items.some(i => i.kind === "file" && i.type.startsWith("image/"))) {
        e.preventDefault();
        send("paste_image");
    }
});
Buddy.on("drop_hover", on => { $("drop-hint").hidden = !(on && state.images && state.mode === "chat"); });

// ------------------------------------------------------------------- GIFs
// GIF search (gif_search.py). Typing searches after a pause (or on Enter):
// the server asks GIPHY, and keeps what it's asked, since the whole network
// shares GIPHY's hourly limit. Every preview arrives from Python as a data:
// URL it has checked - nothing here loads anything from GIPHY. Picking one
// puts it in the composer, like a picture.
const gifPicker = $("gif-picker"), gifGrid = $("gif-grid"), gifSearch = $("gif-search");
const gifThumbs = new Map();   // id -> data URL, for the results showing
let gifTimer = 0, gifAsked = null;   // the search last sent (null: none yet, or ask again)

function openGifs() {
    if (!state.gifs) return;
    gifPicker.hidden = false;
    $("gif-btn").classList.add("on");
    gifSearch.focus();
    gifSearch.select();
    if (gifAsked === null) searchGifs();
}
function closeGifs(focus = true) {
    if (gifPicker.hidden) return;
    gifPicker.hidden = true;
    $("gif-btn").classList.remove("on");
    clearTimeout(gifTimer);
    if (focus) composer.focus();
}
function searchGifs(more = false) {
    clearTimeout(gifTimer);
    const q = gifSearch.value.trim();
    if (!more && q === gifAsked) return;
    gifAsked = q;
    send("gif_search", {q, more});
}
function gifTile(r) {
    const tile = el("button.gif-tile", {
        type: "button", translate: "no", title: [r.title, r.user].filter(Boolean).join(" · "),
        onclick: () => { closeGifs(); send("gif_pick", {id: r.id}); },
    });
    tile.dataset.gif = r.id;
    const url = gifThumbs.get(r.id);
    tile.append(url ? el("img", {src: url, alt: r.title || "GIF"}) : el("span", {text: r.title || "GIF"}));
    return tile;
}
$("gif-btn").onclick = () => (gifPicker.hidden ? openGifs() : closeGifs());
$("gif-close").onclick = () => closeGifs();
$("gif-more").onclick = () => searchGifs(true);
gifSearch.addEventListener("input", () => { clearTimeout(gifTimer); gifTimer = setTimeout(() => searchGifs(), 700); });
gifSearch.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); searchGifs(); }
    else if (e.key === "Escape") { e.preventDefault(); closeGifs(); }
});
document.addEventListener("mousedown", e => {
    if (!gifPicker.hidden && !gifPicker.contains(e.target) && !$("gif-btn").contains(e.target)) closeGifs(false);
});

Buddy.on("gif_results", g => {
    const status = $("gif-status");
    if (g.reset) {   // a new connection: what's showing can't be picked any more
        gifAsked = null;
        gifThumbs.clear();
        gifGrid.replaceChildren();
        status.textContent = "";
        closeGifs(false);
        return;
    }
    status.dataset.tone = g.error ? "danger" : "";
    status.textContent = g.loading ? "Searching…" : (g.error || g.empty || "");
    $("gif-more").hidden = !!(g.loading || g.error) || !g.more;
    if (g.error) gifAsked = null;   // Enter asks again
    if (g.loading || g.error) return;
    if (!g.append) {
        gifThumbs.clear();
        gifGrid.replaceChildren();
        gifGrid.scrollTop = 0;
    }
    gifGrid.append(...g.results.map(gifTile));
});
Buddy.on("gif_thumb", t => {
    gifThumbs.set(t.id, t.url);
    for (const tile of gifGrid.querySelectorAll(".gif-tile")) {
        if (tile.dataset.gif === t.id) tile.replaceChildren(el("img", {src: t.url, alt: tile.title || "GIF"}));
    }
});

// @mentions: after "@", offer names (buddies and whoever's talking here).
const popup = $("mentions");
let matches = [], chosen = 0, partial = null;

function partialBefore(text, cursor) {
    const start = text.lastIndexOf("@", cursor - 1);
    if (start < 0) return null;
    const typed = text.slice(start + 1, cursor);
    if (typed.includes("\n") || typed.includes("#") || typed.length > 24 || (start && !/\s/.test(text[start - 1]))) return null;
    return {start, typed};
}

function drawMentions() {
    popup.hidden = !matches.length;
    popup.replaceChildren(...matches.map((p, i) => el(`li${i === chosen ? ".on" : ""}`, {
        onmousedown: e => { e.preventDefault(); choose(i); },
    }, [el("img", {src: p.avatar, alt: ""}), el("span", {text: p.label, translate: "no"})])));
}

function updateMentions() {
    partial = partialBefore(composer.value, composer.selectionStart);
    const typed = partial ? partial.typed.toLowerCase() : null;
    matches = typed === null ? [] : people.filter(p => p.name.toLowerCase().startsWith(typed))
        .sort((a, b) => a.name.localeCompare(b.name)).slice(0, 8);
    chosen = 0;
    drawMentions();
}

function choose(i) {
    const p = matches[i];
    if (!p || !partial) return;
    composer.setRangeText(`${p.token} `, partial.start, partial.start + 1 + partial.typed.length, "end");
    matches = [];
    drawMentions();
    composer.focus();
}

composer.addEventListener("input", () => { autosize(); updateCounter(); updateMentions(); });
composer.addEventListener("click", () => { matches = []; drawMentions(); });
composer.addEventListener("blur", () => setTimeout(() => { matches = []; drawMentions(); }, 100));
composer.addEventListener("keydown", e => {
    if (matches.length) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            chosen = Math.max(0, Math.min(matches.length - 1, chosen + (e.key === "ArrowDown" ? 1 : -1)));
            drawMentions();
            return;
        }
        if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); choose(chosen); return; }
        if (e.key === "Escape") { e.preventDefault(); matches = []; drawMentions(); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    else if (e.key === "Escape") { e.preventDefault(); send("cancel_compose"); }
});
Buddy.on("people", list => { people = list; });
autosize();

document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f" && state.mode === "chat") {
        e.preventDefault();
        send("open_search");
    }
});

Buddy.on("notice", n => {
    const notice = $("notice");
    notice.textContent = n.text;
    notice.dataset.tone = n.tone || "";
});

// ------------------------------------------------------------------- menu

const menu = $("popup");
function closeMenu() { menu.hidden = true; }
Buddy.on("menu", m => {
    // grid: the emoticon picker - small buttons side by side, each named in its tooltip.
    menu.classList.toggle("grid", !!m.grid);
    menu.replaceChildren(...m.items.map(item => item.sep ? el("hr") : el(`button${item.danger ? ".danger" : ""}${item.on ? ".on" : ""}`, {
        type: "button", role: "menuitem", text: item.label, disabled: !item.enabled,
        title: item.title || null, translate: m.grid ? "no" : null,
        onclick: () => { closeMenu(); send("menu_pick", {id: item.id}); },
    })));
    menu.hidden = false;
    // Keep it on screen.
    const w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = `${Math.max(6, Math.min(m.x - (m.x > innerWidth / 2 ? w : 0), innerWidth - w - 6))}px`;
    menu.style.top = `${Math.max(6, Math.min(m.y, innerHeight - h - 6))}px`;
    const first = menu.querySelector("button:not(:disabled)");
    if (first) first.focus();
});
document.addEventListener("mousedown", e => { if (!menu.hidden && !menu.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !menu.hidden) closeMenu(); });
addEventListener("blur", closeMenu);

// -------------------------------------------------------------- questions

function answer(id, ok, value) { send("answer", {id, ok, value}); }

/* A paragraph per blank-line-separated block, each line its own text
   node - so every sentence Python sends is translated on its own. */
function textBlock(text) {
    return String(text || "").split(/\n\s*\n/).map(para => el("p.modal-text", {},
        para.split("\n").flatMap((line, i) => i ? [el("br"), line] : [line])));
}

Buddy.on("ask", q => {
    const done = {value: false};
    const reply = (ok, value) => { if (!done.value) { done.value = true; answer(q.id, ok, value); } };
    const onClose = () => reply(false);
    if (q.kind === "choice") {
        Buddy.modal({
            title: q.title, body: textBlock(q.text), onClose,
            buttons: [{label: q.cancel || "Cancel"}, ...q.buttons.map(b => ({
                label: b.label, kind: b.kind, onClick: close => { reply(true, b.id); close(); },
            }))],
        });
    } else if (q.kind === "prompt") {
        const input = q.multiline
            ? el("textarea.field", {rows: "4", maxlength: String(q.maxlength), placeholder: q.placeholder || ""})
            : el("input.field", {type: q.password ? "password" : "text", maxlength: String(q.maxlength),
                                 placeholder: q.placeholder || "", autocomplete: "off"});
        input.value = q.value || "";
        const dlg = Buddy.modal({
            title: q.title, body: [...textBlock(q.text), input], onClose,
            buttons: [{label: "Cancel"}, {label: q.ok || "Save", kind: q.danger ? "danger" : "accent",
                                          onClick: close => { reply(true, input.value); close(); }}],
        });
        if (!q.multiline) input.addEventListener("keydown", e => { if (e.key === "Enter") { reply(true, input.value); dlg.close(); } });
    } else if (q.kind === "name") {
        const input = el("input.field", {maxlength: "24", placeholder: "2–24 letters, numbers, spaces and . _ - '"});
        input.value = q.current || "";
        const error = el("div.field-error", {text: q.error || ""});
        const save = close => {
            if (input.value.trim().replace(/\s+/g, " ").length < 2) { error.textContent = "Names are at least 2 characters."; return; }
            reply(true, input.value); close();
        };
        const dlg = Buddy.modal({
            title: "Your Buddy Network name", onClose,
            body: [el("p.modal-text", {}, ["Choose the name other people see. It's always shown with your tag, like ",
                                           el("b", {text: `Name #${q.tag}`}), ", so it doesn't have to be unique."]),
                   el("p.modal-text", {}, el("b", {text: "Don't use your real name."})), input, error],
            buttons: [{label: "Cancel"}, {label: "Save", kind: "accent", onClick: save}],
        });
        input.addEventListener("keydown", e => { if (e.key === "Enter") save(dlg.close); });
    } else if (q.kind === "room") {
        const name = el("input.field", {maxlength: "32", placeholder: "e.g. Colour Grading"});
        const topic = el("input.field", {maxlength: "120", placeholder: "What the room is for"});
        name.value = q.name || ""; topic.value = q.topic || "";
        const error = el("div.field-error", {text: q.error || ""});
        const create = close => {
            if (name.value.trim().replace(/\s+/g, " ").length < 2) { error.textContent = "Room names are at least 2 characters."; return; }
            reply(true, {name: name.value, topic: topic.value}); close();
        };
        const dlg = Buddy.modal({
            title: "New room", onClose,
            body: [el("label.lbl", {}, ["Room name", name]), el("label.lbl", {}, ["Topic (optional)", topic]),
                   el("p.note", {text: "Anyone can find your room and read what's said in it. You can have up to 3 rooms, and a room closes after 30 days with no messages – unless you mark it to keep permanently from the room's menu."}),
                   error],
            buttons: [{label: "Cancel"}, {label: "Create room", kind: "accent", onClick: create}],
        });
        for (const f of [name, topic]) f.addEventListener("keydown", e => { if (e.key === "Enter") create(dlg.close); });
    } else if (q.kind === "link") {
        const general = ["Only open links from people you trust.",
                         "Never type a password, API key or payment details into a site you reached from a chat link.",
                         "Don't download or run files from links in chat."];
        const dlg = Buddy.modal({
            title: "Open this link?", wide: true, onClose,
            body: [el("div.muted", {text: "This link goes to:"}), el("div.link-host", {text: q.host, translate: "no"}),
                   el("div.link-url", {text: q.url, translate: "no"}),
                   el("ul.link-warn", {}, [...q.warnings.map(w => el("li.bad", {text: w})), ...general.map(g => el("li", {text: g}))])],
            buttons: [
                {label: "Copy link", onClick: close => { reply(true, "copy"); close(); }},
                {label: "Open", kind: "", onClick: close => { reply(true, "open"); close(); }},
                {label: "Cancel", kind: "accent"},
            ],
        });
        // Cancel is the safe default: focused, and Enter presses it.
        const cancel = dlg.node.querySelector(".modal-buttons .btn:last-child");
        requestAnimationFrame(() => cancel && cancel.focus());
    } else if (q.kind === "rules") {
        // RULES_HTML is Buddy's own fixed text (dialogs.py), not anyone's message.
        const rules = el("div.rules", {html: q.html});
        if (q.accept) {
            const agree = el("input", {type: "checkbox"});
            const buttons = [{label: "Cancel"}, {label: "Turn on Buddy Network", kind: "accent", id: "rules-ok",
                              onClick: close => { if (agree.checked) { reply(true, true); close(); } }}];
            const dlg = Buddy.modal({title: "Buddy Network rules", wide: true, onClose, buttons,
                body: [rules, el("label.check", {}, [agree, " I'm 13 or older and I'll follow these rules"])]});
            const ok = dlg.node.querySelector("#rules-ok");
            ok.disabled = true;
            agree.onchange = () => { ok.disabled = !agree.checked; };
        } else {
            Buddy.modal({title: "Buddy Network rules", wide: true, onClose, body: rules, buttons: [{label: "Close", kind: "accent"}]});
        }
    } else if (q.kind === "select") {
        let value = q.value;
        const opts = el("div.options", {}, q.options.map(o => el("label", {}, [
            el("input", {type: "radio", name: `sel${q.id}`, checked: o.id === q.value, onchange: () => { value = o.id; }}),
            o.label,
        ])));
        Buddy.modal({title: q.title, body: [...textBlock(q.text), opts], onClose,
            buttons: [{label: "Cancel"}, {label: "Save", kind: "accent", onClick: close => { reply(true, value); close(); }}]});
    }
});

Buddy.on("alert", a => Buddy.modal({title: a.title, body: textBlock(a.text), buttons: [{label: "OK", kind: "accent"}]}));
Buddy.on("toast", t => Buddy.toast(t.text, 2500));

// ---------------------------------------------------------------- buddies

function personRow(p, actions) {
    return el("div.person", {title: p.id}, [
        el("img", {src: p.avatar, alt: ""}),
        el("span.pname", {translate: "no"}, [p.name, el("small", {text: `#${p.tag}`})]),
        ...actions.map(([label, kind, cls]) => el(`button.btn${cls ? "." + cls : ""}`, {
            type: "button", text: label,
            onclick: () => kind === "message" ? send("open", {key: `user:${p.id}`}) : send("social", {kind, user: p.id}),
        })),
    ]);
}

function drawBuddies() {
    if (!buddiesDialog) return;
    const {body} = buddiesDialog;
    const section = (title, list, actions, empty) => el("div", {}, [
        el("h3", {text: title}),
        ...(list.length ? list.map(p => personRow(p, actions)) : [el("div.none", {text: empty})]),
    ]);
    body.lists.replaceChildren(
        ...(buddies.incoming.length ? [section("Asking to be your buddy", buddies.incoming,
            [["Accept", "buddy_accept", "accent"], ["Decline", "buddy_decline", "ghost"], ["Block", "block", "ghost"]], "")] : []),
        section("Your buddies", buddies.buddies, [["Message", "message", ""], ["Remove", "buddy_remove", "ghost"], ["Block", "block", "ghost"]],
            "No buddies yet – add one with their ID, or click someone's name in a chat."),
        ...(buddies.outgoing.length ? [section("Waiting for them to accept", buddies.outgoing, [["Cancel request", "buddy_cancel", "ghost"]], "")] : []),
        ...(buddies.blocked.length ? [section("Blocked", buddies.blocked, [["Unblock", "unblock", "ghost"]], "")] : []),
    );
}

Buddy.on("buddies", b => {
    buddies = b;
    if (b.clear_add && buddiesDialog) buddiesDialog.body.input.value = "";
    drawBuddies();
});
Buddy.on("buddies_error", text => { if (buddiesDialog) buddiesDialog.body.error.textContent = text; });
Buddy.on("buddies_open", open => {
    if (!open) { if (buddiesDialog) buddiesDialog.close(); return; }
    if (buddiesDialog) return;
    const input = el("input.field", {placeholder: "Paste their Buddy Network ID"});
    const add = () => send("add_buddy", {text: input.value});
    input.addEventListener("keydown", e => { if (e.key === "Enter") add(); });
    const error = el("div.field-error");
    const lists = el("div.people");
    const dlg = Buddy.modal({
        title: "Buddies", wide: true,
        body: [el("div.add-row", {}, [input, el("button.btn.accent", {type: "button", text: "Send request", onclick: add})]),
               el("p.note", {text: "Or click someone's name in a chat. They can find their ID under Account."}),
               error, lists,
               el("p.note", {text: "Blocked people can't message you or ask to be your buddy, and their messages are hidden everywhere. They aren't told."})],
        buttons: [{label: "Close"}],
        onClose: () => { buddiesDialog = null; },
    });
    buddiesDialog = {close: dlg.close, body: {input, error, lists}};
    drawBuddies();
});

// ----------------------------------------------------------------- browse

Buddy.on("found_rooms", f => {
    if (!browseDialog) {
        const query = el("input.field", {type: "search", placeholder: "Search by name or topic", autocomplete: "off"});
        const permanent = el("input", {type: "checkbox"});
        const results = el("div.found");
        const status = el("div.muted.small", {text: "Searching…"});
        let timer = 0;
        const search = () => { status.textContent = "Searching…"; send("find_rooms", {query: query.value, permanent_only: permanent.checked}); };
        query.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 300); });
        permanent.onchange = search;
        const dlg = Buddy.modal({
            title: "Browse rooms", wide: true,
            body: [query, el("label.check.small", {}, [permanent, " Permanent rooms only"]), results, status],
            buttons: [{label: "Close"}],
            onClose: () => { browseDialog = null; send("open_found", {close: true}); },
        });
        browseDialog = {close: dlg.close, query, permanent, results, status};
    }
    const b = browseDialog;
    if (f.error) { b.status.textContent = f.error; return; }
    if (f.searching) return;
    if (f.query !== b.query.value || f.permanent_only !== b.permanent.checked) return;   // overtaken by typing
    b.results.replaceChildren(...f.rooms.map(r => el("button", {
        type: "button", ondblclick: () => open(r), onclick: () => open(r),
    }, [
        el("span.fname", {text: `${r.permanent ? "📌 " : ""}# ${r.name}`, translate: "no"}),
        r.topic ? el("span.ftopic", {text: r.topic, translate: "no"}) : null,
        el("span.fmeta", {}, [el("span", {text: r.here ? `${r.here} here now` : "Nobody here right now"}), " · ",
                              el("span", {text: `made by ${r.owner}`})]),
    ])));
    b.status.textContent = f.rooms.length === 1 ? "1 room, most active first."
        : f.rooms.length ? `${f.rooms.length.toLocaleString()} rooms, most active first.`
        : (f.query.trim() || f.permanent_only) ? "No rooms match – try another word, or make the room yourself."
        : "Nobody has made a room yet – be the first with New room.";
    function open(r) { send("open_found", {id: r.id}); const d = browseDialog; browseDialog = null; d.close(); }
});

// ----------------------------------------------------------------- panels
// Account, Safety code, Avatars, Transfer, Saved chats, Ban and Admin
// (page.py _push_panels, panels.py). Python sends the whole stack each
// time; each panel is built once and then updated in place, so typing in
// one of its fields isn't lost when an answer arrives.

const openPanels = new Map();   // kind -> {close, update, quiet}

const panelAct = (kind, action, extra) => send("panel_action", {kind, action, ...(extra || {})});

function button(label, onclick, cls) {
    return el(`button.btn${cls ? "." + cls : ""}`, {type: "button", text: label, onclick});
}

function section(title, kids) {
    return el("section.psec", {}, [el("h3", {text: title}), ...kids]);
}

function listOrNote(list, empty, row) {
    if (list === null) return [el("div.none", {text: "Loading…"})];
    return list.length ? list.map(row) : [el("div.none", {text: empty})];
}

const PANELS = {
    account(d) {
        const act = action => () => panelAct("account", action);
        const avatar = el("img.acct-avatar", {alt: ""});
        const name = el("div.acct-name", {translate: "no"});
        const id = el("input.field.mono", {readonly: true, "aria-label": "Your ID"});
        const code = el("input.field.mono", {readonly: true, "aria-label": "Recovery code"});
        const show = button("Show", act("show_code"));
        const warning = el("p.note"), encryption = el("p.note"), saved = el("span.note.grow");
        return {
            title: "Your Buddy Network account", wide: true,
            body: [
                el("div.acct-head", {}, [avatar, el("div.acct-who", {}, [name, el("div.prow", {}, [
                    button("Change name", act("name")), button("Avatar…", act("avatars"))])])]),
                section("Your ID", [el("p.note", {text: "Share it so people can add you as a buddy."}),
                                    el("div.prow", {}, [id, button("Copy ID", act("copy_id"))])]),
                section("Recovery code", [warning, el("div.prow", {}, [code, show, button("Copy", act("copy_code"))]),
                                          el("div", {}, button("Use a recovery code instead…", act("use_code"), "ghost"))]),
                section("Encryption", [encryption]),
                section("Move to another PC", [
                    el("p.note", {text: "One file carries this account, its encryption keys and your saved direct messages to another PC – Windows or Mac – so it can read your earlier messages too."}),
                    el("div.prow", {}, [button("Save a transfer file…", act("save_transfer")),
                                        button("Import a transfer file…", act("import_transfer"))])]),
                section("Direct messages saved on this PC", [el("div.prow", {}, [saved, button("Saved chats…", act("saved"))])]),
                section("Delete account", [
                    el("p.note", {text: "Deletes your name, ID, buddies, direct messages and the rooms you made, straight away. Your messages in other people's rooms stay, shown as \"Deleted user\", until they're 30 days old – delete any you want gone first."}),
                    el("div", {}, button("Delete my account…", act("delete"), "danger"))]),
            ],
            update(d) {
                avatar.src = d.avatar;
                name.textContent = d.title;
                id.value = d.id;
                code.value = d.code == null ? "•".repeat(20) : d.code;
                show.disabled = d.code != null;
                warning.textContent = d.recovery_warning;
                encryption.textContent = d.encryption;
                saved.textContent = d.saved_chats;
            },
        };
    },

    safety(d) {
        const lines = el("div.safety-code"), status = el("p.safety-status");
        return {
            title: d.title,
            body: [el("p.note", {text: d.about}), lines, status,
                   el("p.note", {text: "If the codes are the same, only the two of you can read these messages. If they're different, don't share anything private here – and tell an admin."})],
            buttons: [{label: "Accept new keys", id: "safety-accept", onClick: () => panelAct("safety", "accept")},
                      {label: "They match", kind: "accent", id: "safety-match", onClick: () => panelAct("safety", "verified")},
                      {label: "Close"}],
            update(d, node) {
                lines.replaceChildren(...(d.lines.length ? d.lines.map(l => el("div", {text: l})) : [el("div.none", {text: "No keys yet"})]));
                status.textContent = d.status;
                status.className = `safety-status ${d.tone}`;
                node.querySelector("#safety-accept").hidden = !d.can_accept;
                node.querySelector("#safety-match").hidden = !d.can_verify;
            },
        };
    },

    avatars(d) {
        const act = (action, extra) => panelAct("avatars", action, extra);
        const preview = el("img.av-preview", {alt: "Avatar preview"});
        const status = el("p.note");
        const use = button("Use this", () => act("use"), "accent");
        const keep = button("Save to favourites", () => act("keep"));
        const original = button("My original", () => act("original"));
        original.title = "The avatar made from your ID";
        const label = el("h3.av-label"), strip = el("div.av-strip"), error = el("div.field-error");
        const remove = button("Remove from favourites", () => {
            const chosen = strip.querySelector("[aria-pressed=true]");
            if (chosen) act("remove", {index: Number(chosen.dataset.index)});
        }, "ghost");
        return {
            title: "Your avatar",
            body: [el("p.note", {text: "Avatars are made by Buddy from a random code – nothing is uploaded. Roll new ones until you like one, then use it. Favourites stay with your account on every PC."}),
                   el("div.av-top", {}, [preview, el("div.av-side", {}, [status, button("New avatar", () => act("roll")), use, keep, original])]),
                   label, strip, el("div.prow", {}, [el("span.note.grow", {text: "Click one to see it, then Use this."}), remove]), error],
            buttons: [{label: "Close"}],
            update(d) {
                preview.src = d.preview;
                status.textContent = d.status;
                use.disabled = d.using;
                keep.disabled = !d.can_keep;
                original.disabled = d.is_original;
                label.textContent = d.saved_label;
                strip.replaceChildren(...(d.saved.length ? d.saved.map(s => el("button.av-fav", {
                    type: "button", "aria-pressed": String(s.selected), "data-index": String(s.index),
                    title: s.in_use ? "In use" : "Click to see it", onclick: () => act("pick", {index: s.index}),
                }, el("img", {src: s.url, alt: ""}))) : [el("div.none", {text: "None yet – Save to favourites keeps the one on show."})]));
                remove.disabled = !d.saved.some(s => s.selected);
                error.textContent = d.error;
            },
        };
    },

    transfer(d) {
        const password = el("input.field", {type: "password", placeholder: "Password (optional)", autocomplete: "new-password"});
        const confirm = el("input.field", {type: "password", placeholder: "The same password again (if you set one)", autocomplete: "new-password"});
        const chats = el("input", {type: "checkbox", checked: d.has_chats});
        const chatsText = el("span");
        const error = el("div.field-error");
        const save = () => panelAct("transfer", "save", {password: password.value, confirm: confirm.value, chats: chats.checked});
        for (const f of [password, confirm]) f.addEventListener("keydown", e => { if (e.key === "Enter") save(); });
        return {
            title: "Save a transfer file",
            body: [el("p.note", {text: "The file holds this account and its encryption keys, locked with the password you choose here – or not locked at all, if you leave it empty. Anyone with the file (and its password, if it has one) can be you and read your direct messages, so delete it once you've moved."}),
                   password, confirm,
                   el("p.note", {text: "If you set a password, there's no way to open the file without it – Buddy can't reset it."}),
                   el("label.check", {}, [chats, chatsText]), error],
            buttons: [{label: "Cancel"}, {label: "Save…", kind: "accent", onClick: save}],
            update(d) {
                chatsText.textContent = ` ${d.chats_label}`;
                chats.disabled = !d.has_chats;
                if (!d.has_chats) chats.checked = false;
                error.textContent = d.error;
            },
        };
    },

    saved(d) {
        const list = el("div.plist"), note = el("p.saved-note");
        return {
            title: "Direct messages saved on this PC", wide: true,
            body: [el("p.note", {text: "Kept while \"Keep a copy of my direct messages on this PC\" is on (Settings), locked to your Windows account. They stay after the server deletes them (after 30 days)."}),
                   list, note],
            buttons: [{label: "Close"}],
            update(d) {
                list.replaceChildren(...listOrNote(d.chats, "Nothing saved on this PC.", c => el("div.prow.pitem", {}, [
                    el("div.grow", {}, [el("div.strong", {text: c.name, translate: "no"}), el("div.note", {text: c.detail})]),
                    button("Export…", () => panelAct("saved", "export", {index: c.index})),
                    button("Delete", () => panelAct("saved", "delete", {index: c.index}), "ghost"),
                ])));
                note.textContent = d.note;
                note.className = `saved-note ${d.tone}`;
            },
        };
    },

    ban(d) {
        const length = el("select.field", {"aria-label": "How long"}, d.lengths.map((label, i) => el("option", {value: String(i), text: label})));
        const reason = el("input.field", {maxlength: "200", placeholder: "Reason (they see this)"});
        const network = el("input", {type: "checkbox"});
        return {
            title: "Ban",
            body: [el("div.strong", {text: d.who, translate: "no"}), el("label.lbl", {}, ["For", length]), reason,
                   el("label.check", {}, [network, " Also stop new identities from their network"]),
                   el("p.note", {text: "They're signed out at once and can't sign back in until the ban ends. The network option only works while they're online: the server never stores addresses, so it keeps a scrambled (hashed) copy of theirs, only for the length of the ban. It can also stop other people on the same network."})],
            buttons: [{label: "Cancel"}, {label: "Ban", kind: "danger", onClick: () => panelAct("ban", "ban", {
                length: length.selectedIndex, reason: reason.value, network: network.checked})}],
            update() {},
        };
    },

    admin(d) {
        const act = (action, extra) => panelAct("admin", action, extra);
        const tabs = el("div.tabs", {role: "tablist"});
        const panes = {};
        const lists = {};
        const hint = text => el("p.note", {text});
        for (const id of ["reports", "bans", "admins", "log", "app", "bugs"]) {
            lists[id] = el("div.plist.tall");
            panes[id] = el("div.admin-pane", {hidden: true});
        }
        panes.reports.append(hint("Reported messages, oldest first, as they were when reported. You only ever see what someone reported – never anyone's DMs."), lists.reports);
        panes.bans.append(lists.bans, hint("To ban someone, click their name in a chat, or use Ban author on a report."));
        const newId = el("input.field.grow", {placeholder: "Their Buddy Network ID", maxlength: "40"});
        const newRole = el("select.field");
        const staffHint = hint("");
        panes.admins.append(lists.admins, el("div.prow", {}, [newId, newRole,
            button("Give role", () => act("give_role", {user: newId.value, role: newRole.value}))]), staffHint);
        panes.log.append(lists.log, hint("Every admin action, newest first, kept for 90 days."));
        const title = el("input.field", {maxlength: "80", placeholder: "Title, e.g. \"Buddy 2.1 is out\""});
        const text = el("textarea.field", {rows: "4", placeholder: "What people should know (up to 1,000 characters)"});
        const count = el("span.note.grow");
        const counted = () => { count.textContent = `${text.value.length.toLocaleString()} / ${(1000).toLocaleString()}`; };
        text.addEventListener("input", counted);
        counted();
        panes.app.append(hint("Announcements for every Buddy, chat users or not, behind the glowing dot next to \"Buddy\". Buddys check once a day, so a new one can take up to a day to show. Plain text only; the 10 newest are shown."),
                         lists.app, title, text,
                         el("div.prow", {}, [count, button("Post to every Buddy", () => act("post", {title: title.value, text: text.value}), "accent")]));
        panes.bugs.append(hint("Bug reports from the bug button in Buddy's header – from anyone, signed in to Buddy Network or not. Oldest first, kept for 90 days unless you delete them."), lists.bugs);
        const error = el("div.field-error");
        let tab = d.tab, sent = d.sent;
        const showTab = id => {
            tab = id;
            for (const b of tabs.children) b.setAttribute("aria-selected", String(b.dataset.tab === id));
            for (const [key, pane] of Object.entries(panes)) pane.hidden = key !== id;
        };
        return {
            title: "Admin", wide: true,
            body: [tabs, ...Object.values(panes), error],
            buttons: [{label: "Close"}],
            update(d) {
                if (tabs.children.length !== d.tabs.length) {
                    tabs.replaceChildren(...d.tabs.map(t => el("button", {type: "button", role: "tab", text: t.label, "data-tab": t.id,
                        onclick: () => { showTab(t.id); act("tab", {tab: t.id}); }})));
                    newRole.replaceChildren(...d.roles.map(r => el("option", {value: r.id, text: r.label})));
                }
                showTab(d.tabs.some(t => t.id === tab) ? tab : d.tab);
                staffHint.textContent = d.staff_hint;
                lists.reports.replaceChildren(...listOrNote(d.reports, "No reports waiting.", r => el("div.pitem.report", {}, [
                    el("div.strong", {text: r.head, translate: "no"}),
                    el("blockquote", {text: r.text, translate: "no"}),
                    r.claimed ? el("div.note", {text: "An encrypted direct message: this text came from the reporter's Buddy – it can't be checked against what was really sent."}) : null,
                    el("div.note", {text: r.by}),
                    el("div.prow", {}, [
                        r.image ? button("View image", () => act("view_image", {id: r.id})) : null,
                        button("Delete message", () => act("delete_message", {id: r.id}), "danger"),
                        button("Keep it (close report)", () => act("keep", {id: r.id})),
                        r.can_ban ? button("Ban author…", () => act("ban_author", {id: r.id}), "ghost") : null,
                    ]),
                ])));
                lists.bans.replaceChildren(...listOrNote(d.bans, "Nobody is banned.", b => el("div.prow.pitem", {}, [
                    el("div.grow", {}, [el("div.strong", {}, [el("span", {text: b.head, translate: "no"}), " – ", el("span", {text: b.lasts})]), b.reason ? el("div.note", {text: b.reason, translate: "no"}) : null]),
                    button("Unban", () => act("unban", {id: b.id})),
                ])));
                lists.admins.replaceChildren(...listOrNote(d.staff, "No staff yet.", a => el("div.prow.pitem", {}, [
                    el("div.grow.strong", {text: a.head, translate: "no"}), el("span.chip", {text: a.role}),
                    a.can_remove ? button("Make an ordinary user", () => act("remove_role", {id: a.id}), "ghost") : null,
                ])));
                lists.log.replaceChildren(...listOrNote(d.log, "Nothing yet.", e => el("div.pitem", {}, [
                    el("div.mono.small", {text: e.head, translate: "no"}), e.detail ? el("div.note", {text: e.detail, translate: "no"}) : null,
                ])));
                lists.app.replaceChildren(...listOrNote(d.app, "None posted.", a => el("div.prow.pitem", {}, [
                    el("div.grow", {translate: "no"}, [el("div.strong", {text: a.head}), el("div.note.pre", {text: a.text})]),
                    button("Remove", () => act("delete_announcement", {id: a.id}), "ghost"),
                ])));
                lists.bugs.replaceChildren(...listOrNote(d.bugs, "No bug reports.", b => el("div.pitem.report", {}, [
                    el("div.strong", {}, [el("span", {text: b.when}), " – ",
                        b.who ? el("span", {text: b.who, translate: "no"}) : el("span", {text: "Not signed in to Buddy Network"})]),
                    b.text ? el("blockquote", {text: b.text, translate: "no"}) : null,
                    b.details.length ? el("div.note.bug-meta", {}, b.details.map(x => el("div", {}, [
                        el("span", {text: x.label}), ": ", el("span", {text: x.value, translate: "no"})]))) : null,
                    el("div.prow", {}, [
                        ...b.shots.map(s => button(`Screenshot ${s.number}`, () => act("view_bug_image", {id: b.id, image: s.id}))),
                        button("Delete", () => act("delete_bug", {id: b.id}), "danger"),
                    ]),
                ])));
                error.textContent = d.error;
                if (d.sent !== sent) {   // a role given or an announcement posted: clear what was typed
                    sent = d.sent;
                    newId.value = ""; title.value = ""; text.value = ""; counted();
                }
            },
        };
    },
};

Buddy.on("panels", list => {
    const wanted = new Set(list.map(p => p.kind));
    for (const [kind, panel] of openPanels) {
        if (!wanted.has(kind)) { panel.quiet = true; panel.close(); }
    }
    for (const data of list) {
        let panel = openPanels.get(data.kind);
        if (!panel) {
            const make = PANELS[data.kind];
            if (!make) continue;
            const built = make(data);
            panel = {quiet: false, update: built.update};
            const dlg = Buddy.modal({
                title: built.title, wide: built.wide, body: built.body,
                buttons: built.buttons || [{label: "Close"}],
                onClose: () => { openPanels.delete(data.kind); if (!panel.quiet) send("panel_close", {kind: data.kind}); },
            });
            dlg.node.classList.add(`panel-${data.kind}`);
            panel.close = dlg.close;
            panel.node = dlg.node;
            openPanels.set(data.kind, panel);
        }
        panel.update(data, panel.node);
    }
});
