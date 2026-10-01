/* Command Center (page.py): Buddy's actions, each with its hot keys. Python
   keeps the bindings and runs them; this draws them, records a key combo
   when a key box is clicked, and says what was changed. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

const MARKER_HEX = {Blue: "#2F7BF5", Cyan: "#16C6D8", Green: "#3DB94A", Yellow: "#F2CC2E", Red: "#E5393B",
    Pink: "#F46BAF", Purple: "#9453D6", Fuchsia: "#CF3A95", Rose: "#F2A0B0", Lavender: "#B3A4E4", Sky: "#8CC8F5",
    Mint: "#7EDCB2", Lemon: "#EFEA7C", Sand: "#D5B27C", Cocoa: "#7B5534", Cream: "#F1E6CC"};
let clipHex = {};
let recording = null;   // the binding id whose key box is waiting for keys

$("enabled").onchange = e => send("enable", {on: e.target.checked});

const MODIFIER_CODES = new Set(["ControlLeft", "ControlRight", "AltLeft", "AltRight", "ShiftLeft", "ShiftRight",
                                "MetaLeft", "MetaRight", "OSLeft", "OSRight"]);

document.addEventListener("keydown", e => {
    if (!recording) return;
    e.preventDefault();
    e.stopPropagation();
    if (MODIFIER_CODES.has(e.code)) return;            // wait for the key itself
    const id = recording;
    stopRecording();
    const bare = !e.ctrlKey && !e.altKey && !e.shiftKey && !e.metaKey;
    if (bare && e.code === "Escape") return;
    if (bare && (e.code === "Backspace" || e.code === "Delete")) return send("clear_keys", {id});
    send("record", {id, code: e.code, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey, meta: e.metaKey});
}, true);
document.addEventListener("mousedown", e => { if (recording && !e.target.closest(".cc-keys")) stopRecording(); });
addEventListener("blur", () => stopRecording());

function stopRecording() {
    recording = null;
    for (const b of document.querySelectorAll(".cc-keys.recording")) {
        b.classList.remove("recording");
        b.replaceChildren(...keyFace(b.dataset.keys));
    }
}

function keyFace(keys) {
    if (!keys) return [el("span.cc-nokeys", {text: "Set hot key"})];
    return keys.split("+").flatMap((k, i) => i ? [el("span.cc-plus", {text: "+"}), el("kbd", {text: k})] : [el("kbd", {text: k})]);
}

function swatchOf(o, value) {
    const hex = o.swatch === "marker" ? MARKER_HEX[value] : o.swatch === "clip" ? clipHex[value] : null;
    return hex ? el("i.cc-swatch", {style: `background:${hex}`}) : null;
}

function optionControl(b, o) {
    const value = b.options[o.key];
    if (o.kind === "check") {
        const box = el("input", {type: "checkbox", checked: !!value, onchange: e => send("option", {id: b.id, key: o.key, value: e.target.checked})});
        return el("label.check.cc-opt", {}, [box, ` ${o.label}`]);
    }
    if (o.kind === "folder") {
        return el("span.cc-opt.cc-folder", {}, [
            el("span.muted.small", {text: o.label}),
            el("button.btn", {type: "button", text: value ? "Change…" : "Choose…", onclick: () => send("browse", {id: b.id, key: o.key})}),
            value ? el("span.cc-path", {text: value, title: value, translate: "no"}) : el("span.muted.small", {text: "(only copied)"}),
            value ? el("button.btn.ghost.icon", {type: "button", title: "Don't save it anywhere", text: "×",
                                                 onclick: () => send("option", {id: b.id, key: o.key, value: ""})}) : null,
        ]);
    }
    // select, preset
    const choices = o.choices || [];
    if (o.kind === "preset" && !choices.length) return el("span.muted.small.cc-opt", {text: "Animation's presets appear here once it has loaded."});
    const select = el("select.field", {"aria-label": o.label, onchange: e => send("option", {id: b.id, key: o.key, value: e.target.value})},
        choices.map(c => el("option", {value: c.id, text: c.label, translate: o.kind === "preset" ? "no" : null})));
    select.value = value;
    return el("label.cc-opt", {}, [el("span.muted.small", {text: o.label}), swatchOf(o, value), select]);
}

function bindingRow(action, b) {
    const keys = el("button.cc-keys", {type: "button", title: b.keys ? "Click, then press new keys (Backspace clears)" : "Click, then press the keys you want",
        "data-keys": b.keys || "", onclick: () => {
            stopRecording();
            recording = b.id;
            keys.classList.add("recording");
            keys.replaceChildren(el("span", {text: "Press keys…"}));
        }}, keyFace(b.keys));
    const run = el("button.btn.cc-run", {type: "button", disabled: b.running, onclick: () => send("run", {id: b.id})},
        [icon("play"), el("span", {text: b.running ? "Running…" : "Run"})]);
    const remove = el("button.btn.ghost.icon.cc-remove", {type: "button", title: "Remove this hot key", onclick: () => send("remove", {id: b.id})}, icon("trash"));
    const notes = [b.error ? el("div.cc-error", {text: b.error}) : null, b.note ? el("div.cc-note", {text: b.note}) : null];
    return el("div.cc-binding", {}, [
        el("div.cc-row", {}, [el("div.cc-opts", {}, action.options.map(o => optionControl(b, o))), keys, run, remove]),
        ...notes,
    ]);
}

Buddy.on("state", s => {
    clipHex = s.clip_colors || {};
    $("enabled").checked = s.enabled;
    $("enabled").disabled = !s.supported;
    $("unsupported").hidden = s.supported;
    if (recording) return;   // don't redraw under a key box that's waiting
    $("groups").replaceChildren(...s.groups.map(g => el("section.cc-group", {}, [
        el("h2.section-title", {text: g.name}),
        ...g.actions.map(a => el("div.card.cc-action", {}, [
            el("div.cc-head", {}, [el("h3.cc-title", {text: a.label}), el("p.muted.small.cc-about", {text: a.about})]),
            ...a.bindings.map(b => bindingRow(a, b)),
            el("button.btn.ghost.cc-add", {type: "button", onclick: () => send("add", {action: a.id})},
               [icon("plus"), el("span", {text: a.bindings.length ? "Another hot key" : "Add a hot key"})]),
        ])),
    ])));
    $("recent").replaceChildren(...(s.recent.length ? s.recent.map(r => el(`div.cc-recent-row${r.ok ? "" : ".bad"}`, {}, [
        el("span.cc-when", {text: r.when}),
        el("span.cc-what", {text: r.what}),
        el("span.cc-said", {text: r.text}),
    ])) : [el("div.muted.small", {text: "Nothing yet – what your hot keys do shows here."})]));
});

Buddy.on("toast", t => Buddy.toast(t.text, 2600));
