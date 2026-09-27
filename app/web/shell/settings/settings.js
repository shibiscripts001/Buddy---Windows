/* The Settings window (core/settings_dialog.py). It draws the fields Python
   sends (core/settings_form.py documents them) and reports each change;
   Python checks it, saves it and sends the fields back. Everything shown
   goes in as text - only a hint's "html" (Buddy's own, for links) doesn't. */
"use strict";

const {el, send} = Buddy;
const $ = id => document.getElementById(id);
const form = $("form");

$("close").onclick = () => send("action", {section: "shell", action: "close"});
$("reset").onclick = () => send("action", {section: "shell", action: "reset_theme"});

let statusTimer = 0;
Buddy.on("status", s => {
    $("status").textContent = s.text;
    $("status").className = `form-status ${s.tone || ""}`;
    clearTimeout(statusTimer);
    if (s.text) statusTimer = setTimeout(() => { $("status").textContent = ""; }, 6000);
});

const set = (section, key, value) => send("set", {section, key, value});
const act = (section, action) => send("action", {section, action});

function labelled(f, control, extra) {
    return el(`div.set-row${f.indent ? ".indent" : ""}`, {"data-key": f.key || ""}, [
        el("label.set-label", {}, [el("span", {text: f.label}), control]),
        ...(extra || []),
        f.hint ? el("div.set-hint", {text: f.hint}) : null,
        f.error ? el("div.field-error", {text: f.error}) : null,
    ]);
}

const FIELDS = {
    heading: f => el("h2.set-heading", {text: f.text}),
    line: () => el("hr.set-line"),
    info: f => el("div.set-row", {}, [el("div.set-label", {}, [el("span", {text: f.label}), el("div.set-info", {text: f.text, translate: f.raw ? "no" : null})])]),
    hint: f => {
        const node = el(`div.set-hint${f.indent ? ".indent" : ""}${f.tone ? "." + f.tone : ""}`);
        if (f.html) node.innerHTML = f.html;   // Buddy's own fixed HTML (links), never user text
        else node.textContent = f.text;
        return node;
    },
    check: (f, s) => {
        const box = el("input", {type: "checkbox", checked: f.value, disabled: f.disabled, "data-key": f.key,
                                 onchange: e => set(s, f.key, e.target.checked)});
        return el(`div.set-row${f.indent ? ".indent" : ""}`, {title: f.tooltip || ""}, [
            el("label.check", {}, [box, el("span", {text: f.label})]),
            f.hint ? el("div.set-hint.under-check", {text: f.hint}) : null,
            f.error ? el("div.field-error", {text: f.error}) : null,
        ]);
    },
    select: (f, s) => {
        const box = el("select.field", {"data-key": f.key, title: f.tooltip || "", onchange: e => set(s, f.key, decode(f, e.target.value))},
                       f.options.map(o => el("option", {value: String(o.value), text: o.label, translate: f.raw ? "no" : null})));
        box.value = String(f.value);
        return labelled(f, box);
    },
    text: (f, s) => {
        const input = el("input.field", {type: f.password ? "password" : "text", placeholder: f.placeholder || "",
                                         spellcheck: "false", autocomplete: "off", "data-key": f.key});
        input.value = f.value;
        if (f.live) input.addEventListener("input", () => set(s, f.key, input.value));
        else input.addEventListener("change", () => set(s, f.key, input.value));
        let list = null;
        if (f.suggest && f.suggest.length) {
            list = el("datalist", {id: `list-${s}-${f.key}`}, f.suggest.map(v => el("option", {value: v})));
            input.setAttribute("list", list.id);
        }
        const control = f.browse
            ? el("div.set-inline", {}, [input, el("button.btn", {type: "button", text: "Browse…", onclick: () => act(s, f.browse)})])
            : input;
        return labelled(f, control, list ? [list] : []);
    },
    textarea: (f, s) => {
        const box = el("textarea.field.set-textarea", {rows: String(f.rows || 8), placeholder: f.placeholder || "",
                                                       spellcheck: "true", "data-key": f.key,
                                                       onchange: e => set(s, f.key, e.target.value)});
        box.value = f.value;
        return labelled(f, box);
    },
    number: (f, s) => {
        const input = el("input.field.set-num", {inputmode: "decimal", placeholder: f.placeholder || "", spellcheck: "false",
                                                  autocomplete: "off", "data-key": f.key,
                                                  onchange: e => set(s, f.key, e.target.value)});
        input.value = f.value === null || f.value === undefined ? "" : String(f.value);
        return labelled(f, el("div.set-inline", {}, [input, f.suffix ? el("span.set-hint", {text: f.suffix}) : null]));
    },
    slider: (f, s) => {
        const readout = el("span.set-readout");
        const range = el("input", {type: "range", min: String(f.min), max: String(f.max), step: String(f.step), "data-key": f.key});
        range.value = String(f.value);
        const show = () => { readout.textContent = f.readouts[range.value] ?? range.value; };
        show();
        range.addEventListener("input", show);
        range.addEventListener("change", () => set(s, f.key, Number(range.value)));
        range.addEventListener("dblclick", () => { range.value = String(f.default); show(); set(s, f.key, f.default); });
        const row = labelled(f, el("div.set-inline", {}, [range, readout]));
        row.title = "Double-click the slider to reset it";
        return row;
    },
    color: (f, s) => {
        const btn = el("button.btn.set-color", {type: "button", disabled: !f.enabled, "data-key": f.key,
                                                onclick: e => Buddy.pickColor({hex: f.value, title: f.label, at: e.currentTarget,
                                                                               onPick: hex => set(s, f.key, hex)})},
                       [el("i", {style: `background:${f.value}`}), el("span", {text: f.value})]);
        return labelled(f, btn);
    },
    buttons: (f, s) => el("div.set-row.set-buttons", {}, f.items.map(b => el(`button.btn${b.kind ? "." + b.kind : ""}`, {
        type: "button", text: b.label, title: b.tooltip || "", onclick: () => act(s, b.action),
    }))),
};

/* A select's value comes back as the type Python sent (numbers stay numbers). */
function decode(f, text) {
    const match = f.options.find(o => String(o.value) === text);
    return match ? match.value : text;
}

Buddy.on("settings", data => {
    // Keep the field being typed in: its focus, caret and (for a field that
    // saves when it's left) what's been typed so far.
    const active = document.activeElement;
    const key = active && active.dataset ? active.dataset.key : null;
    const section = key ? active.closest("[data-section]")?.dataset.section : null;
    const typed = active && ((active.tagName === "INPUT" && active.type !== "checkbox" && active.type !== "range")
                             || active.tagName === "TEXTAREA") ? active.value : null;
    const scrolledTo = active && active.tagName === "TEXTAREA" ? active.scrollTop : 0;
    const caret = typed !== null ? [active.selectionStart, active.selectionEnd] : null;
    const scroll = form.scrollTop;

    form.replaceChildren(...data.sections.map(sec => el("section.set-section", {"data-section": sec.id}, [
        sec.title ? el("h1.set-title", {text: sec.title}) : null,
        ...sec.fields.map(f => (FIELDS[f.kind] || (() => null))(f, sec.id)),
    ])));
    form.scrollTop = scroll;
    if (key && section) {
        const again = form.querySelector(`[data-section="${section}"] [data-key="${CSS.escape(key)}"]:is(input,select,button,textarea)`);
        if (again) {
            if (typed !== null && (again.tagName === "INPUT" || again.tagName === "TEXTAREA")) {
                again.value = typed;
                try { again.setSelectionRange(caret[0], caret[1]); } catch (_err) { /* not a text field */ }
                again.scrollTop = scrolledTo;
            }
            again.focus();
        }
    }
});
