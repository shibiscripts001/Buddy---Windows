/*
 * Ask Buddy's view. Draws what pages/manual_chat/page.py sends and reports
 * clicks and keystrokes back - no chat state lives here beyond what is on
 * screen. Answer HTML arrives already rendered and escaped by Python
 * (conversation.md_to_html); everything else is set as text.
 */
"use strict";

const $ = id => document.getElementById(id);
const {el, icon, send} = Buddy;

const messages = $("messages");
const transcript = $("transcript");
const input = $("input");
const sendBtn = $("send");
let sending = false;

$("prev").append(icon("left"));
$("next").append(icon("right"));
$("export").append(icon("download"), "Export");
sendBtn.append(icon("send"), "Send");
$("attach").append(icon("image"));
$("attach").onclick = () => send("attach_image");

// ------------------------------------------------------------ transcript

// Whether the reader is following the conversation (at the bottom) or has
// scrolled back to read. Following, the newest message stays in view as
// replies land and the cards below the transcript come and go; reading
// back, nothing moves under them.
let following = true;

function nearBottom() {
    return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 60;
}

function scrollToBottom(smooth) {
    transcript.style.scrollBehavior = smooth ? "smooth" : "auto";
    transcript.scrollTop = transcript.scrollHeight;
    following = true;
}

transcript.addEventListener("scroll", () => { following = nearBottom(); }, {passive: true});
new ResizeObserver(() => { if (following) scrollToBottom(false); }).observe(transcript);
new ResizeObserver(() => { if (following) scrollToBottom(false); }).observe(messages);

function traceNode(trace) {
    if (!trace.length) return null;
    const label = trace.length === 1 ? "Used 1 tool" : `Used ${trace.length} tools`;
    return el("details.trace", {}, [
        el("summary", {text: label}),
        el("div.chips", {}, trace.map(t => el("span.chip", {text: t}))),
    ]);
}

function copyButton(block) {
    if (!block.copyable) return null;
    const btn = el("button.btn.ghost", {title: "Copy this reply", onclick: () => {
        send("copy", {i: block.i});
        btn.replaceChildren(icon("check"), "Copied");
        setTimeout(() => btn.replaceChildren(icon("copy"), "Copy"), 1500);
    }}, [icon("copy"), "Copy"]);
    return el("div.actions", {}, [btn]);
}

function picturesNode(images) {
    if (!images || !images.length) return null;
    // Small data: URLs Python made from the pictures sent (pictures.py).
    return el("div.sent-pictures", {}, images.map(src => el("img", {src, alt: "Picture sent with the question"})));
}

function messageNode(block) {
    return el(`article.msg.${block.role}`, {dataset: {i: block.i}}, [
        el("div.who", {text: block.who}),
        el("div.bubble", {}, [
            picturesNode(block.images),
            el("div.body", {html: block.html}),
            traceNode(block.trace),
        ]),
        copyButton(block),
    ]);
}

Buddy.on("transcript", data => {
    messages.replaceChildren(...data.blocks.map(messageNode));
    const box = $("suggestions");
    box.replaceChildren(...data.suggestions.map(text =>
        el("button.btn", {text, onclick: () => ask(text)})));
    requestAnimationFrame(() => scrollToBottom(false));
});

Buddy.on("append", block => {
    const stick = following || block.role === "user";
    $("suggestions").replaceChildren();
    messages.append(messageNode(block));
    if (stick) requestAnimationFrame(() => scrollToBottom(true));
});

Buddy.on("thinking", data => {
    $("thinking").hidden = !data.on;
    $("thinking-text").textContent = data.message || "Thinking…";
    if (data.on && following) requestAnimationFrame(() => scrollToBottom(true));
});

// -------------------------------------------------------------- controls

Buddy.on("controls", c => {
    const wasSending = sending;
    sending = c.sending;
    $("prev").disabled = !c.can_prev;
    $("next").disabled = !c.can_next;
    $("switcher").hidden = c.total < 2;
    $("position").textContent = `Chat ${c.index + 1} of ${c.total}`;
    $("prev").title = c.can_prev ? `Back to conversation ${c.index} of ${c.total}` : "No earlier conversation";
    $("next").title = c.can_next ? `Forward to conversation ${c.index + 2} of ${c.total}` : "No later conversation";

    const newBtn = $("new");
    newBtn.disabled = !c.can_new;
    newBtn.replaceChildren(icon("plus"), "New chat");
    // "of N" makes the cap visible, so history quietly stopping at the
    // limit doesn't look like messages going missing.
    if (c.turns) newBtn.append(el("span.badge", {text: `${c.turns}/${c.limit}`}));
    newBtn.title = c.turns
        ? `Start a fresh conversation. This one has ${c.turns} of ${c.limit} remembered turns.`
        : "Start a fresh conversation";

    sendBtn.disabled = sending || !canSend();
    if (wasSending && !sending) input.focus();
});

Buddy.on("status", s => { $("status").textContent = s.text || ""; });
Buddy.on("toast", t => Buddy.toast(t.text));

$("prev").onclick = () => send("prev_chat");
$("next").onclick = () => send("next_chat");
$("new").onclick = () => { send("new_chat"); input.focus(); };
$("export").onclick = () => send("export");

// ----------------------------------------------------------------- offer

Buddy.on("offer", offer => {
    const box = $("offer");
    box.hidden = !offer;
    if (!offer) return box.replaceChildren();
    box.replaceChildren(
        icon("tool"),
        el("div.reason", {}, [el("div.strong", {text: "Buddy suggests a tool"}),
                              el("div.muted.small", {text: offer.reason || ""})]),
        el("button.btn.accent", {text: offer.label, onclick: () => send("open_tool", {tool_id: offer.tool_id})}),
    );
});

// -------------------------------------------------------------- proposal

Buddy.on("proposal", p => {
    const box = $("proposal");
    box.hidden = !p;
    if (!p) return box.replaceChildren();
    const locked = p.state !== "ready";
    box.replaceChildren(
        el("div.title", {}, [p.destructive ? icon("warning") : icon("spark"), el("span", {text: p.title})]),
        el("div.items", {}, [
            p.reason ? el("div.muted", {text: p.reason, style: "margin-bottom:6px"}) : null,
            ...p.warnings.map(w => el("div.warning", {text: w})),
            ...p.details.map(d => el("div", {text: d})),
        ]),
        el("div.row", {}, [
            el("div.note.muted.small", {text: p.note}),
            el("button.btn", {text: "Discard", disabled: p.state === "busy",
                              onclick: () => send("discard_proposal")}),
            el("button.btn.danger", {text: p.state === "busy" ? "Applying…" : p.apply_label,
                                     disabled: locked, onclick: () => send("apply_proposal")}),
        ]),
    );
});


// ------------------------------------------------------------- pictures

let pictureCount = 0;
const canSend = () => !!input.value.trim() || pictureCount > 0;

Buddy.on("pictures", p => {
    pictureCount = p.items.length;
    $("attach").hidden = !p.available;
    $("pictures").hidden = !pictureCount;
    $("picture-strip").replaceChildren(...p.items.map((item, index) => el("div.picture", {title: item.label}, [
        el("img", {src: item.preview, alt: "Picture to send"}),
        el("button.remove", {type: "button", title: "Don't send this picture", text: "✕",
                             onclick: () => send("remove_picture", {index})}),
    ])));
    $("picture-note").textContent = p.note || "";
    sendBtn.disabled = sending || !canSend();
});

// A pasted picture goes through Python, which reads the clipboard itself.
input.addEventListener("paste", e => {
    const items = [...(e.clipboardData ? e.clipboardData.items : [])];
    if (!$("attach").hidden && items.some(i => i.kind === "file" && i.type.startsWith("image/"))) {
        e.preventDefault();
        send("paste_image");
    }
});

// -------------------------------------------------------------- composer

function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
    input.style.overflowY = input.scrollHeight > 200 ? "auto" : "hidden";
    sendBtn.disabled = sending || !canSend();
}

function ask(text) {
    text = (text || "").trim();
    if ((!text && !pictureCount) || sending) return;
    send("send", {text});
    input.value = "";
    autosize();
    input.focus();
}

input.addEventListener("input", autosize);
input.addEventListener("keydown", e => {
    // Enter sends, Shift+Enter is a new line. While an answer is on its way
    // the box stays editable, so the next question can be drafted.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        ask(input.value);
    }
});
sendBtn.onclick = () => ask(input.value);

autosize();
input.focus();
