/* The Settings window (core/settings_dialog.py): a rail of groups, the
   group's pages beside it, and the page open. It draws the fields Python
   sends (core/settings_form.py documents them) and reports each change;
   Python checks it, saves it and sends the fields back. Search looks
   through every page's fields here, in English and in the language shown.
   Everything shown goes in as text - only a hint's "html" (Buddy's own,
   for links) doesn't. */
"use strict";

const {el, send, icon} = Buddy;
const $ = id => document.getElementById(id);
const form = $("form");

let DATA = {groups: [], pages: []};
let current = null;                 // the page open
const lastInGroup = {};             // group -> the page last open in it
let modelFilter = "all";            // the Model library's filter
let flash = null;                   // {page, keys} - fields to light up once drawn

$("close").onclick = () => send("action", {section: "shell", action: "close"});

let statusTimer = 0;
Buddy.on("status", s => {
    $("status").textContent = s.text;
    $("status").className = `form-status ${s.tone || ""}`;
    clearTimeout(statusTimer);
    if (s.text) statusTimer = setTimeout(() => { $("status").textContent = ""; }, 6000);
});

const set = (section, key, value) => send("set", {section, key, value});
const act = (section, action) => send("action", {section, action});
const pageById = id => DATA.pages.find(p => p.id === id);

function labelled(f, control, extra) {
    return el(`div.set-row${f.indent ? ".indent" : ""}`, {"data-key": f.key || ""}, [
        el("label.set-label", {}, [el("span", {text: f.label}), control]),
        ...(extra || []),
        f.hint ? el("div.set-hint", {text: f.hint}) : null,
        f.error ? el("div.field-error", {text: f.error}) : null,
    ]);
}

function chip(c) {
    return c && c.text ? el(`span.chip.set-chip${c.tone ? "." + c.tone : ""}`, {text: c.text, title: c.tip || ""}) : null;
}

function rowButton(a, owner) {
    return el(`button.btn.small${a.kind ? "." + a.kind : ""}`, {
        type: "button", text: a.label, title: a.tip || "", disabled: !!a.disabled,
        onclick: () => a.page ? openPage(a.page) : act(owner, a.action),
    });
}

/* A row's parts with dots between - each its own text, so each is translated. */
const dotted = parts => parts.flatMap((p, i) => i ? [" · ", el("span", {text: p})] : [el("span", {text: p})]);

const WHERE = [["all", "All"], ["local", "On this PC"], ["cloud", "Cloud"], ["missing", "Not downloaded"]];

const FIELDS = {
    line: () => null,
    heading: () => null,
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
        return el(`div.set-row${f.indent ? ".indent" : ""}`, {title: f.tooltip || "", "data-key": f.key}, [
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
        type: "button", text: b.label, title: b.tooltip || "", disabled: !!b.disabled, "data-key": b.action,
        onclick: () => act(s, b.action),
    }))),
    status: f => el("div.set-row.set-status", {"data-key": f.label}, [
        el("div.set-status-line", {}, [
            el("span.set-status-label", {text: f.label}),
            el(`span.chip.set-chip${f.tone ? "." + f.tone : ""}`, {text: f.text, translate: f.raw ? "no" : null}),
            f.page ? el("button.btn.small.ghost", {type: "button", text: "Open", onclick: () => openPage(f.page)}) : null,
        ]),
        f.hint ? el("div.set-hint", {text: f.hint}) : null,
    ]),
    link: f => el("div.set-row", {"data-key": f.page}, [
        el("button.btn.set-link", {type: "button", onclick: () => openPage(f.page)}, [el("span", {text: f.label}), icon("right")]),
        f.hint ? el("div.set-hint", {text: f.hint}) : null,
    ]),
    progress: (f, s) => el("div.set-row.set-progress", {}, [
        el("div.set-status-line", {}, [
            el("span.set-hint", {text: f.text}),
            f.value !== null && f.value !== undefined ? el("span.set-readout", {text: `${f.value}%`}) : null,
            f.stop ? el("button.btn.small", {type: "button", text: "Stop", onclick: () => act(s, f.stop)}) : null,
        ]),
        el(`div.set-bar${f.value === null || f.value === undefined ? ".indeterminate" : ""}`, {},
           [el("i", {style: `width:${f.value || 0}%`})]),
    ]),
    storage: f => {
        const sum = f.parts.reduce((n, p) => n + p.bytes, 0) || 1;
        return el("div.set-row.set-storage", {}, [
            el("div.set-status-line", {}, [el("span.set-status-label", {text: f.total}),
                                           el("span.set-hint.set-path", {text: f.path, translate: "no", title: f.path})]),
            el("div.set-stack", {}, f.parts.map(p => el(`i.tone-${p.tone}`, {style: `width:${100 * p.bytes / sum}%`, title: p.label}))),
            el("div.set-legend", {}, f.parts.map(p => el(`span.tone-${p.tone}`, {}, [
                el("span", {text: p.label}), " ", el("span", {text: p.size, translate: "no"})]))),
        ]);
    },
    models: (f, s) => {
        const rows = f.filters && modelFilter !== "all" ? f.rows.filter(r => r.where === modelFilter) : f.rows;
        const filters = f.filters ? el("div.set-filters", {role: "group"}, WHERE.map(([id, label]) => el("button.btn.small", {
            type: "button", text: label, "aria-pressed": String(modelFilter === id),
            onclick: () => { modelFilter = id; draw(); },
        }))) : null;
        return el("div.set-row.set-models", {}, [
            filters,
            el("div.set-model-list", {}, rows.length ? rows.map(r => el("div.set-model", {"data-key": r.id || r.label}, [
                el("div.set-model-text", {}, [
                    el("div.set-model-name", {text: r.label, translate: r.raw ? "no" : null}),
                    r.sub && r.sub.length ? el("div.set-hint", {}, dotted(r.sub)) : null,
                    r.note ? el("div.set-hint.set-path", {text: r.note, title: r.note, translate: "no"}) : null,
                ]),
                el("span.set-model-size", {text: r.size || ""}),
                el("div.set-model-chip", {}, [chip(r.chip)]),
                el("div.set-model-actions", {}, (r.actions || []).map(a => rowButton(a, r.owner || s))),
            ])) : [el("div.set-hint.set-empty", {text: "Nothing here."})]),
        ]);
    },
};

/* A select's value comes back as the type Python sent (numbers stay numbers). */
function decode(f, text) {
    const match = f.options.find(o => String(o.value) === text);
    return match ? match.value : text;
}

/* A page's fields as cards: each heading starts one, a line ends one. */
function cards(fields, section) {
    const out = [];
    let card = null;
    const open = title => {
        card = el("div.set-card", {}, title ? [el("h2.set-heading", {text: title})] : []);
        out.push(card);
    };
    for (const f of fields) {
        if (f.kind === "heading") { open(f.text); continue; }
        if (f.kind === "line") { card = null; continue; }
        const node = (FIELDS[f.kind] || (() => null))(f, section);
        if (!node) continue;
        if (!card) open("");
        card.append(node);
    }
    return out;
}

// ------------------------------------------------------------ search --

const SPACELESS = /[぀-ヿ一-鿿가-힯]/;     // Japanese, Chinese, Korean
const fold = s => String(s || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
const both = s => s ? `${fold(s)} ${fold(Buddy.t(s))}` : "";

function fieldTexts(f) {
    const out = [f.label, f.text, f.hint, f.tooltip, f.total];
    if (f.kind === "hint" && f.html) out[1] = "";
    for (const o of f.options || []) out.push(o.label);
    for (const b of f.items || []) out.push(b.label, b.tooltip);
    for (const p of f.parts || []) out.push(p.label);
    return out.filter(Boolean);
}

const rowTexts = r => [r.label, ...(r.sub || []), r.chip && r.chip.text, r.note].filter(Boolean);

/* Every field the query's words are all in - with its card's heading and
   page's title counted as part of it, so "theme" finds Appearance's. */
function search(query) {
    // Each word from the start of a word ("dual" isn't in "individually") -
    // except in a language written without spaces, where anywhere will do.
    const words = fold(query).split(/\s+/).filter(Boolean).map(w => SPACELESS.test(w)
        ? w : new RegExp(`(?:^|[^\\p{L}\\p{N}])${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`, "u"));
    const hits = (texts, around) => {
        const hay = [...texts, ...around].map(both).join(" ");
        return words.every(w => typeof w === "string" ? hay.includes(w) : w.test(hay));
    };
    const results = [];
    for (const page of DATA.pages) {
        const group = DATA.groups.find(g => g.id === page.group);
        let heading = "";
        for (const f of page.fields) {
            if (f.kind === "heading") { heading = f.text; continue; }
            if (f.kind === "line") continue;
            const around = [heading, page.title];
            let found = null;
            if (f.kind === "models") {
                const rows = f.rows.filter(r => hits(rowTexts(r), around));
                if (rows.length) found = {...f, rows, filters: false};
            } else if (hits(fieldTexts(f), around)) {
                found = f;
            }
            if (!found) continue;
            let entry = results.find(r => r.page === page && r.heading === heading);
            if (!entry) results.push(entry = {page, group, heading, fields: []});
            entry.fields.push(found);
        }
    }
    return results;
}

function drawResults(query) {
    const results = search(query);
    const count = results.reduce((n, r) => n + r.fields.length, 0);
    $("title").textContent = "Search results";
    $("subtitle").textContent = count === 1 ? "1 setting" : count ? `${count} settings` : "";
    $("group-name").textContent = "";
    const byPage = [];
    for (const r of results) {
        const seen = byPage.find(b => b.page === r.page);
        if (seen) seen.count += r.fields.length;
        else byPage.push({page: r.page, group: r.group, count: r.fields.length});
    }
    $("pages").replaceChildren(...byPage.map(b => el("button.set-page", {type: "button", role: "listitem",
                                                                           onclick: () => openPage(b.page.id, true)}, [
        el("span.set-page-name", {text: b.page.title}),
        el("span.set-count", {text: String(b.count)}),
    ])));
    if (!results.length) {
        form.replaceChildren(el("div.set-empty", {}, [el("p", {text: "No settings match your search."}),
                                                     el("p.set-hint", {text: "Try fewer words, or another name for it."})]));
        return;
    }
    form.replaceChildren(...results.map(r => el("div.set-card.set-result", {"data-section": r.page.owner}, [
        el("button.set-crumb", {type: "button", onclick: () => openPage(r.page.id, true)}, [
            el("span", {text: r.group ? r.group.label : ""}), icon("right"),
            el("span", {text: r.page.title}),
            ...(r.heading ? [icon("right"), el("span", {text: r.heading})] : []),
        ]),
        ...r.fields.map(f => (FIELDS[f.kind] || (() => null))(f, r.page.owner)),
    ])));
}

// -------------------------------------------------------------- draw --

function openPage(id, fromSearch) {
    const page = pageById(id);
    if (!page) return;
    if (fromSearch) {
        const results = search($("search").value).filter(r => r.page === page);
        flash = {page: id, keys: results.flatMap(r => r.fields.flatMap(f => f.kind === "models"
            ? f.rows.map(row => row.id || row.label) : [f.key || f.page || f.label || ""]))};
    }
    $("search").value = "";
    current = id;
    lastInGroup[page.group] = id;
    form.scrollTop = 0;
    draw();
}

function drawRail() {
    const page = pageById(current);
    const searching = !!$("search").value.trim();
    const groups = DATA.groups.filter(g => DATA.pages.some(p => p.group === g.id));
    const button = g => el("button.set-rail-item", {
        type: "button", "aria-current": !searching && page && page.group === g.id ? "page" : null, title: g.label,
        onclick: () => {
            const first = DATA.pages.find(p => p.group === g.id);
            openPage(pageById(lastInGroup[g.id]) ? lastInGroup[g.id] : first.id);
        },
    }, [icon(g.icon), el("span", {text: g.label})]);
    $("rail").replaceChildren(...groups.filter(g => !g.end).map(button), el("div.spacer"),
                              ...groups.filter(g => g.end).map(button));
}

function drawNav(page) {
    const group = DATA.groups.find(g => g.id === page.group);
    $("group-name").textContent = group && group.id === "ai" ? "AI and models" : group ? group.label : "";
    $("pages").replaceChildren(...DATA.pages.filter(p => p.group === page.group).map(p => el("button.set-page", {
        type: "button", role: "listitem", "aria-current": p.id === current ? "page" : null,
        onclick: () => openPage(p.id),
    }, [el("span.set-page-name", {text: p.title})])));
}

function draw() {
    drawRail();
    const query = $("search").value.trim();
    if (query) return drawResults(query);
    const page = pageById(current);
    if (!page) return;
    drawNav(page);
    $("title").textContent = page.title;
    $("subtitle").textContent = page.subtitle || "";
    form.replaceChildren(el("div.set-page-body", {"data-section": page.owner, "data-page": page.id},
                            cards(page.fields, page.owner)));
    if (flash && flash.page === page.id) {
        const keys = flash.keys;
        flash = null;
        let first = null;
        for (const node of form.querySelectorAll("[data-key]")) {
            if (!node.matches(".set-row, .set-model") || !keys.includes(node.dataset.key)) continue;
            node.classList.add("set-hit");
            first = first || node;
        }
        if (first) first.scrollIntoView({block: "center"});
        setTimeout(() => form.querySelectorAll(".set-hit").forEach(n => n.classList.remove("set-hit")), 1800);
    }
}

$("search").addEventListener("input", () => { form.scrollTop = 0; draw(); });
$("search").addEventListener("keydown", e => {
    if (e.key === "Escape" && $("search").value) { e.preventDefault(); e.stopPropagation(); $("search").value = ""; draw(); }
    if (e.key === "Enter") {
        const first = form.querySelector(".set-crumb");
        if (first) first.click();
    }
});
document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") { e.preventDefault(); $("search").focus(); $("search").select(); }
});

Buddy.on("settings", data => {
    // Keep the field being typed in: its focus, caret and (for a field that
    // saves when it's left) what's been typed so far.
    const active = document.activeElement;
    const key = active && active.dataset && form.contains(active) ? active.dataset.key : null;
    const section = key ? active.closest("[data-section]")?.dataset.section : null;
    const typed = active && ((active.tagName === "INPUT" && active.type !== "checkbox" && active.type !== "range")
                             || active.tagName === "TEXTAREA") ? active.value : null;
    const scrolledTo = active && active.tagName === "TEXTAREA" ? active.scrollTop : 0;
    const caret = typed !== null ? [active.selectionStart, active.selectionEnd] : null;
    const scroll = form.scrollTop;

    DATA = data;
    if (data.open && !pageById(current)) current = data.open;
    if (!pageById(current)) current = (DATA.pages[0] || {}).id;
    const page = pageById(current);
    if (page) lastInGroup[page.group] = current;
    draw();
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
