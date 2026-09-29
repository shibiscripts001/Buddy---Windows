/* Dailies review UI; Python owns playback, drafts, and Resolve writes. */
"use strict";

const {el, send} = Buddy;
const $ = id => document.getElementById(id);
const fields = [...document.querySelectorAll("[data-field]")];
const tags = [...document.querySelectorAll("[data-tag]")];
// "state" is small and comes often; the tape rows come only when they
// change, and the catalog only when the clip picker asks for it.
let state = {sources: [], current: null, pending: 0, source: "@all", clip_count: 0};
let tape = {clips: [], loading: false};
let catalog = null;
let catalogWaiters = [];
let current = null;
let draftFields = {};
let draftTag = null;
let draftColor = null;      // null: leave the clip's colour as it is; "": clear it
let logEdited = false;
let draftTimer = 0;
let failed = "";
let stageGeometry = "";
let stageFrame = 0;
let spaceShortcutEnabled = null;
const typingSelector = "textarea, select, [contenteditable='true'], input:not([type='range']):not([type='checkbox'])";

function syncSpaceShortcut() {
    const focused = document.activeElement;
    const editing = focused?.matches?.(typingSelector);
    const enabled = !editing && !document.querySelector(".modal-backdrop, .dropdown-pop");
    if (enabled !== spaceShortcutEnabled) {
        spaceShortcutEnabled = enabled;
        send("space_shortcut", {enabled});
    }
}
document.addEventListener("focusin", syncSpaceShortcut);
document.addEventListener("focusout", () => requestAnimationFrame(syncSpaceShortcut));
syncSpaceShortcut();

function syncStageGeometry() {
    stageFrame = 0;
    const stage = $("stage").getBoundingClientRect();
    const panel = document.querySelector(".player-panel").getBoundingClientRect();
    // The native video surface does not inherit CSS overflow clipping.
    // Keep it inside the scrolling workspace, clear of the source/footer bars.
    const workspace = document.querySelector(".workspace").getBoundingClientRect();
    const left = Math.max(0, stage.left, panel.left, workspace.left);
    const top = Math.max(0, stage.top, panel.top, workspace.top);
    const right = Math.min(innerWidth, stage.right, panel.right, workspace.right);
    const bottom = Math.min(innerHeight, stage.bottom, panel.bottom, workspace.bottom);
    const covered = document.querySelector(".modal-backdrop, .dropdown-pop");
    const box = {x: Math.round(left), y: Math.round(top),
        width: covered ? 0 : Math.max(0, Math.round(right - left)),
        height: covered ? 0 : Math.max(0, Math.round(bottom - top)),
        stage: {x: Math.round(stage.left), y: Math.round(stage.top),
            width: Math.round(stage.width), height: Math.round(stage.height)}};
    const key = JSON.stringify(box);
    if (key !== stageGeometry) { stageGeometry = key; send("stage_geometry", box); }
}
function scheduleStageGeometry() {
    if (!stageFrame) stageFrame = requestAnimationFrame(syncStageGeometry);
}
new ResizeObserver(scheduleStageGeometry).observe($("stage"));
new ResizeObserver(scheduleStageGeometry).observe(document.querySelector(".player-panel"));
new ResizeObserver(scheduleStageGeometry).observe(document.querySelector(".workspace"));
new MutationObserver(() => { scheduleStageGeometry(); syncSpaceShortcut(); }).observe(document.body, {childList: true});
window.addEventListener("resize", scheduleStageGeometry);
window.addEventListener("scroll", scheduleStageGeometry, true);
scheduleStageGeometry();

function sendDraft() {
    clearTimeout(draftTimer);
    draftTimer = 0;
    if (!current) return;
    send("edit", {id: current.id, log: $("log").value, log_edited: logEdited, fields: draftFields,
                  tag: draftTag, color: draftColor});
    $("save-status").textContent = "Saved as you type";
}
function queueDraft() {
    $("save-status").textContent = "Saving…";
    clearTimeout(draftTimer);
    draftTimer = setTimeout(sendDraft, 220);
}
function flushDraft() {
    if (draftTimer) sendDraft();
    draftTimer = 0;
}
$("log").addEventListener("input", () => { logEdited = true; queueDraft(); showQuickHint(); });
for (const input of fields) input.addEventListener("input", () => {
    draftFields[input.dataset.field] = input.value;
    queueDraft();
});
for (const button of tags) button.onclick = () => {
    if (!current) return;
    draftTag = button.dataset.tag;
    drawTags();
    sendDraft();
};

// Resolve's Clip Color picker: no colour, then its sixteen, in its order.
const colorButtons = ["", ...Object.keys(CLIP_COLORS)].map(name => {
    const button = el("button", {type: "button", title: name || "No color", "aria-label": name || "No color"});
    button.dataset.color = name;
    if (name) button.style.setProperty("--swatch", CLIP_COLORS[name]);
    else button.textContent = "×";
    button.onclick = () => {
        if (!current) return;
        draftColor = name;
        drawColors();
        sendDraft();
    };
    return button;
});
$("colors").replaceChildren(...colorButtons);
function drawColors() {
    const color = draftColor === null ? String(current?.metadata?.["Clip Color"] || "") : draftColor;
    for (const button of colorButtons) {
        button.setAttribute("aria-pressed", String(button.dataset.color === color));
        button.disabled = !current;
    }
}

function showQuickHint() {
    const text = $("log").value;
    const hits = [
        ["Scene", /(?:^|\W)s\s*0*(\d+[a-z]?)(?!\w)/gi],
        ["Shot", /(?:^|\W)sh\s*0*(\d+[a-z]?)(?!\w)/gi],
        ["Take", /(?:^|\W)t\s*0*(\d+[a-z]?)(?!\w)/gi],
    ].flatMap(([label, re]) => [...text.matchAll(re)].slice(-1).map(m => `${label} ${m[1].toUpperCase()}`));
    $("quick-hint").textContent = hits.length
        ? `Recognized: ${hits.join(" · ")}. Your original text stays in Comments.`
        : "s1 → Scene 1, sh2 → Shot 2, t3 → Take 3. Your original text is kept in Comments.";
}
function drawTags() {
    const tag = draftTag === null ? String(current?.metadata?.Tag || "0") : String(draftTag);
    for (const button of tags) button.setAttribute("aria-pressed", String(button.dataset.tag === tag));
}
function pendingLabel(row, pending) {
    const label = row.querySelector(".pending");
    if (pending && !label) row.append(el("span.pending.small", {text: "Notes to apply"}));
    if (!pending && label) label.remove();
}
function drawTape() {
    const list = $("tape");
    list.replaceChildren(...(tape.clips.length ? tape.clips.map((clip, index) => {
        const row = el("button.tape-row", {
            type: "button", title: `${clip.name} · ${clip.bin}`,
            onclick: () => { flushDraft(); send("pick_clip", {id: clip.id}); },
        }, [
            el("span.name", {text: `${index + 1}. ${clip.name}`, translate: "no"}),
            el("span.detail", {text: `${clip.type} · ${clip.bin}`, translate: "no"}),
        ]);
        row.dataset.id = clip.id;
        if (CLIP_COLORS[clip.color]) row.style.setProperty("--clip-color", CLIP_COLORS[clip.color]);
        pendingLabel(row, clip.pending);
        return row;
    }) : [el("div.tape-empty", {text: tape.loading ? "Reading Media Pool…" : "No clips in this source and media filter."})]));
    $("tape-count").textContent = `${tape.clips.length} clip${tape.clips.length === 1 ? "" : "s"}`;
    markSelection(true);
}
// Moving the highlight, rather than rebuilding every row: a click used to
// redraw the whole tape - hundreds of rows - just to move one highlight.
let markedId = null;
function markSelection(force = false) {
    if (!force && markedId === state.current) return;
    markedId = state.current;
    for (const row of $("tape").querySelectorAll(".tape-row")) {
        row.setAttribute("aria-current", String(row.dataset.id === state.current));
    }
    $("tape").querySelector('[aria-current="true"]')?.scrollIntoView({block: "nearest"});
}
function drawSources() {
    const source = $("source");
    source.replaceChildren(
        new Option("All media", "@all"),
        new Option(`Current Bin${state.current_bin ? ` · ${state.current_bin}` : ""}`, "@bin"),
        ...state.sources.map(s => new Option(`${s.name} (${s.count})`, s.id)),
    );
    source.value = state.source;
    const custom = !state.source.startsWith("@");
    for (const button of document.querySelectorAll(".custom-only")) button.hidden = !custom;
    $("type-filter").value = state.filter || "All";
}
function drawState() {
    $("project").textContent = state.project ? `Project: ${state.project}` : "";
    $("status").textContent = state.problem || (state.loading ? "Reading Media Pool…" :
        state.saving ? "Writing clip metadata…" : "Drafts are kept in Buddy until playback finishes or you apply them.");
    $("pending").textContent = `${state.pending || 0} clip${state.pending === 1 ? "" : "s"} with notes to apply`;
    $("apply").disabled = !state.pending || state.saving || state.loading;
    $("source").disabled = !state.project || state.loading;
    $("type-filter").disabled = !state.project || state.loading;
    $("new-source").disabled = !state.project || state.loading;
    $("play").disabled = !state.current || state.loading;
    $("remove-from-source").hidden = state.source.startsWith("@") || !state.current;
    $("play").classList.toggle("playing", !!state.auto_play);
    $("previous").disabled = !state.current;
    $("next").disabled = !state.current;
    drawSources();
    markSelection();
}
// The stage never swaps its picture for a message while a clip opens: a clip
// whose still isn't here yet keeps the last picture, faded, until it is.
function drawStage() {
    const stage = $("stage");
    const shown = stage.querySelector("img");
    if (!current) { stage.replaceChildren(el("span.muted", {text: "Choose a clip to review."})); return; }
    if (failed) { stage.replaceChildren(el("span.error", {text: failed})); return; }
    if (currentFrame) {
        const id = current.id;
        let img = shown;
        if (!img) {
            img = el("img", {src: currentFrame, alt: current.name});
            stage.replaceChildren(img);
        } else if (img.getAttribute("src") !== currentFrame) {
            img.src = currentFrame;
        }
        img.alt = current.name;
        img.classList.remove("stale");
        // Python keeps the last video frame over the stage until this still
        // is really on screen - decoded, not just handed its src.
        img.decode().catch(() => {}).then(() => requestAnimationFrame(() => {
            if (current?.id === id) send("still_shown", {id});
        }));
        return;
    }
    if (current.type === "Audio") { stage.replaceChildren(el("span.audio-symbol", {text: "♫"}), el("span", {text: "Audio clip"})); return; }
    if (shown && current.type === "Video") { shown.classList.add("stale"); return; }
    stage.replaceChildren();
}
let currentFrame = "";
function drawCurrent(data) {
    if (current && data.clip && data.clip.id === current.id) {
        current = data.clip;
        currentFrame = data.frame || "";
        drawColors();
        if (data.draft?.applied) {
            for (const input of fields) {
                if (!(input.dataset.field in draftFields) && input !== document.activeElement) {
                    input.value = current.metadata?.[input.dataset.field] || "";
                }
            }
        }
        drawStage();
        return;
    }
    flushDraft();
    current = data.clip;
    currentFrame = data.frame || "";
    failed = "";
    $("clip-name").textContent = current?.name || "Choose a clip";
    $("clip-bin").textContent = current?.bin || "";
    const draft = data.draft || {};
    draftFields = {...(draft.fields || {})};
    draftTag = draft.tag === undefined ? null : draft.tag;
    draftColor = draft.color === undefined ? null : draft.color;
    logEdited = !!draft.log_edited;
    $("log").value = draft.log || "";
    $("log").disabled = !current;
    for (const input of fields) {
        input.value = draftFields[input.dataset.field] ?? current?.metadata?.[input.dataset.field] ?? "";
        input.disabled = !current;
    }
    for (const button of tags) button.disabled = !current;
    showQuickHint();
    drawTags();
    drawColors();
    drawStage();
}

// The catalog is every clip in the Media Pool, so it is fetched only here.
function withCatalog(fn) {
    catalogWaiters.push(fn);
    if (catalogWaiters.length === 1) send("catalog");
}
function picker(title, existing, done) {
    withCatalog(() => openPicker(title, existing, done));
}
function openPicker(title, existing, done) {
    const chosen = new Set(existing || []);
    const search = el("input.field", {type: "search", placeholder: "Search Media Pool clips", autocomplete: "off"});
    const list = el("div.clip-picker");
    const draw = () => {
        const query = search.value.trim().toLowerCase();
        const rows = catalog.filter(c => !query || `${c.name} ${c.bin} ${c.type}`.toLowerCase().includes(query));
        list.replaceChildren(...(rows.length ? rows.map(c => el("label", {}, [
            el("input", {type: "checkbox", checked: chosen.has(c.id), onchange: e => {
                e.target.checked ? chosen.add(c.id) : chosen.delete(c.id);
            }}),
            el("span", {}, [el("b", {text: c.name, translate: "no"}), el("small", {text: ` ${c.type} · ${c.bin}`, translate: "no"})]),
        ])) : [el("p.muted", {text: "No clips match."})]));
    };
    search.addEventListener("input", draw);
    draw();
    Buddy.modal({title, wide: true, body: [search, list], buttons: [
        {label: "Cancel"}, {label: "Add selected", kind: "accent", onClick: close => { done([...chosen]); close(); }},
    ]});
}

$("refresh").onclick = () => { flushDraft(); send("refresh"); };
$("source").onchange = e => { flushDraft(); send("select_source", {id: e.target.value}); };
$("type-filter").onchange = e => { flushDraft(); send("filter", {type: e.target.value}); };
$("play").onclick = () => { flushDraft(); send("play"); };
$("next").onclick = () => { flushDraft(); send("next"); };
$("previous").onclick = () => { flushDraft(); send("previous"); };
$("apply").onclick = () => { flushDraft(); send("commit"); };
$("remove-from-source").onclick = () => { flushDraft(); send("remove_clip", {id: state.current}); };
for (const button of document.querySelectorAll("[data-viewer]")) {
    button.onclick = () => send("viewer", {viewer: button.dataset.viewer});
}
Strip.onChange(() => {
    $("time").textContent = Strip.time();
    $("strip-info").textContent = Strip.info();
});
$("transcribe-on").onchange = e => send("transcribe", {on: e.target.checked});
$("transcript-to-log").onclick = () => send("copy_transcript");
$("volume").oninput = e => send("volume", {value: e.target.value / 100});

$("new-source").onclick = () => {
    if (!state.clip_count) return Buddy.toast("Refresh the Media Pool first.");
    const name = el("input.field", {placeholder: "Source name", maxlength: "80"});
    const scope = el("select.field", {}, [new Option("All media", "all"), new Option("Current Bin", "bin"), new Option("Choose clips…", "selected")]);
    const body = el("div.source-form", {}, [el("label.lbl", {}, ["Name", name]), el("label.lbl", {}, ["Clips", scope])]);
    Buddy.modal({title: "New source tape", body, buttons: [
        {label: "Cancel"}, {label: "Create", kind: "accent", onClick: close => {
            if (!name.value.trim()) return name.focus();
            const value = name.value.trim(), kind = scope.value;
            close();
            if (kind === "selected") picker(`Choose clips for ${value}`, [], ids => send("create_source", {name: value, scope: "selected", ids}));
            else send("create_source", {name: value, scope: kind});
        }},
    ]});
};
$("rename-source").onclick = () => {
    const source = state.sources.find(s => s.id === state.source);
    if (!source) return;
    const input = el("input.field", {value: source.name, maxlength: "80"});
    Buddy.modal({title: "Rename source", body: input, buttons: [
        {label: "Cancel"}, {label: "Rename", kind: "accent", onClick: close => {
            if (!input.value.trim()) return input.focus();
            send("rename_source", {id: source.id, name: input.value.trim()}); close();
        }},
    ]});
};
$("delete-source").onclick = async () => {
    const source = state.sources.find(s => s.id === state.source);
    if (source && await Buddy.confirm({title: `Delete source “${source.name}”?`, text: "The source tape is removed. Review drafts stay with their clips.", ok: "Delete", danger: true})) {
        flushDraft(); send("delete_source", {id: source.id});
    }
};
$("add-clips").onclick = () => picker("Add clips to source", [], ids => send("add_clips", {ids}));

document.addEventListener("keydown", e => {
    if (e.target.closest(`${typingSelector}, .modal-backdrop, .dropdown-pop`)) return;
    if (e.key === " ") { e.preventDefault(); if (!e.repeat) { flushDraft(); send("space_play"); } }
    if (e.key === "ArrowRight") { e.preventDefault(); flushDraft(); send("next"); }
    if (e.key === "ArrowLeft") { e.preventDefault(); flushDraft(); send("previous"); }
});

Buddy.on("state", s => {
    if (state.project_id !== s.project_id) current = null;
    state = s;
    drawState();
    for (const button of document.querySelectorAll("[data-viewer]")) {
        button.setAttribute("aria-pressed", String(button.dataset.viewer === state.viewer));
    }
    Strip.setViewer(state.viewer);
    Strip.setCurrent(state.current);
    Strip.setPlaying(!!state.auto_play);
});
Buddy.on("tape", t => { tape = t; drawTape(); Strip.setTape(tape.clips); });
Buddy.on("catalog", clips => {
    catalog = clips;
    const waiting = catalogWaiters;
    catalogWaiters = [];
    for (const fn of waiting) fn();
});
Buddy.on("current", data => drawCurrent(data));
Buddy.on("frame", data => { if (current && data.id === current.id) { currentFrame = data.src; drawStage(); } });
Buddy.on("playing", _playing => {});
Buddy.on("position", data => {
    Strip.setPosition(data.position, data.duration);
    Transcript.time(data.position / 1000);
});
Buddy.on("transcript", data => Transcript.set(data));
Buddy.on("transcript_segment", data => Transcript.add(data));
Buddy.on("transcript_text", data => {
    if (!current || data.id !== current.id) return;
    const log = $("log");
    log.value = log.value.trim() ? `${log.value.trimEnd()}\n\n${data.text}` : data.text;
    logEdited = true;
    showQuickHint();
    sendDraft();
});
Buddy.on("failed", data => { if (current && data.id === current.id) { failed = data.text; drawStage(); } });
Buddy.on("draft_saved", data => {
    $("save-status").textContent = "Saved as you type";
    state.pending = data.pending;
    const clip = tape.clips.find(c => c.id === data.id);
    if (clip) clip.pending = true;
    const row = [...$("tape").querySelectorAll(".tape-row")].find(r => r.dataset.id === data.id);
    if (row) pendingLabel(row, true);
    $("pending").textContent = `${state.pending} clip${state.pending === 1 ? "" : "s"} with notes to apply`;
    $("apply").disabled = !state.pending;
});
Buddy.on("finished", data => {
    const summary = `Finished ${data.count} clips${data.skipped ? ` (${data.skipped} could not be previewed)` : ""}.`;
    if (data.applying) { flushDraft(); send("commit"); }
    Buddy.toast(`${summary} ${data.applying ? "Applying pending notes to Resolve." : "Review your notes, then apply them to Resolve."}`, 5000);
    if (!data.applying) $("apply").focus();
});
Buddy.on("toast", data => Buddy.toast(data.text, 3500));
Buddy.on("alert", data => Buddy.modal({title: data.title, body: el("p.modal-text", {text: data.text}), buttons: [{label: "OK", kind: "accent"}]}));
