/*
 * Transcribe's view. page.py owns every setting, runs every job and asks
 * every question; this draws what it sends and reports what the user did.
 */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);

$("refresh").append(icon("refresh"));

for (const node of document.querySelectorAll("[data-action]")) {
    node.addEventListener("click", () => send(node.dataset.action));
}

// --------------------------------------------------------------- tabs --

function showTab(tab) {
    for (const b of $("tabs").querySelectorAll("button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    for (const p of document.querySelectorAll(".panel")) p.hidden = p.id !== `panel-${tab}`;
}
function goTo(tab) {
    showTab(tab);
    send("tab", {tab});
}
$("tabs").onclick = e => {
    const b = e.target.closest("button[data-tab]");
    if (b) goTo(b.dataset.tab);
};
document.addEventListener("click", e => {
    const b = e.target.closest("[data-goto]");
    if (b) goTo(b.dataset.goto);
});
Buddy.on("tab", showTab);
showTab("subtitles");

let CATALOG = {languages: [], spoken: [], targets: [], mixed: "mixed"};
Buddy.on("catalog", c => { CATALOG = c; });

// ------------------------------------------------------------ timeline --

Buddy.on("timeline", t => {
    const node = $("timeline");
    node.textContent = t.text;
    node.classList.toggle("bad", !t.ok);
    node.hidden = !t.connected;   // offline is said once, in Buddy's header
});

// ---------------------------------------------------------------- job --

let JOB = {kind: null};
Buddy.on("job", j => {
    JOB = j;
    const running = !!j.kind;
    $("jobbar").hidden = !running;
    document.body.classList.toggle("busy", running);
    $("stage").textContent = j.stage || "";
    const bar = $("bar");
    bar.classList.toggle("indeterminate", running && (j.progress === null || j.progress === undefined));
    bar.firstElementChild.style.width = `${j.progress || 0}%`;
    $("pct").textContent = running && j.progress !== null && j.progress !== undefined ? `${j.progress}%` : "";
    drawRun();
    drawTranslateButtons();
});

$("stop").onclick = async () => {
    if (JOB.kind === "setup") {
        const yes = await Buddy.confirm({title: "Stop?", text: "Stop this download or install?", ok: "Stop", danger: true});
        if (!yes) return;
    }
    send("stop");
};

// ------------------------------------------------------------ options --

let OPT = null;

Buddy.on("options", o => {
    OPT = o;
    const model = $("model");
    model.replaceChildren(...(o.models.length ? o.models.map(m => el("option", {value: m.id, text: m.label, title: m.tip}))
                                               : [el("option", {value: "", text: "No models installed"})]));
    model.value = o.model;
    model.disabled = !o.models.length;
    $("model-tip").textContent = (o.models.find(m => m.id === o.model) || {}).tip || "";

    const lang = $("language");
    lang.replaceChildren(
        el("option", {value: "", text: "Auto-detect"}),
        el("option", {value: CATALOG.mixed, text: o.mixed.length >= 2 ? o.mixed_label : "Mixed languages…"}),
        ...CATALOG.languages.filter(l => l.code).map(l => el("option", {value: l.code, text: l.label})),
    );
    lang.value = o.language;
    $("language-tip").replaceChildren(...(o.language === CATALOG.mixed
        ? ["Each part is transcribed in its own language. ",
           el("button.link-btn", {type: "button", text: "Change…", onclick: e => { e.preventDefault(); pickMixed(); }})]
        : [o.language ? "" : "Detected from the audio."]));

    const chars = $("max-chars");
    if (document.activeElement !== chars) chars.value = o.max_chars;
    for (const b of $("max-lines").children) b.setAttribute("aria-pressed", String(Number(b.dataset.value) === o.max_lines));
    if (document.activeElement !== $("hotwords")) $("hotwords").value = o.hotwords;

    $("not-ready").hidden = o.ready || !o.why;
    $("not-ready-why").textContent = o.why;
    $("setup-dot").hidden = o.ready;
    drawRun();
});

function drawRun() {
    if (!OPT) return;
    const run = $("run");
    run.disabled = !OPT.ready || !!JOB.kind;
    run.textContent = JOB.kind === "transcribe" ? "Transcribing…" : "Transcribe timeline";
}

$("model").onchange = e => send("option", {key: "model", value: e.target.value});
$("language").onchange = e => {
    const value = e.target.value;
    if (value === CATALOG.mixed) pickMixed();
    else send("option", {key: "language", value});
};

function pickMixed() {
    pickLanguages({
        title: "Languages spoken",
        hint: "Tick every language spoken in the timeline. Each part is transcribed in its own – choosing only the ones really there keeps short replies from being mistaken.",
        entries: CATALOG.spoken, selected: OPT.mixed, min: 2,
    }).then(codes => {
        if (codes) send("mixed", {codes});
        else $("language").value = OPT.language;
    });
}

$("max-chars").addEventListener("change", e => {
    const n = parseInt(e.target.value, 10);
    if (Number.isFinite(n)) send("option", {key: "max_chars", value: n});
    else e.target.value = OPT.max_chars;
});
$("max-chars").addEventListener("input", e => { e.target.value = e.target.value.replace(/\D/g, ""); });
$("max-lines").onclick = e => {
    const b = e.target.closest("button[data-value]");
    if (b) send("option", {key: "max_lines", value: Number(b.dataset.value)});
};
$("hotwords").addEventListener("change", e => send("option", {key: "hotwords", value: e.target.value}));

// ------------------------------------------------------------ results --

Buddy.on("result", r => {
    const t = r && r.kind === "transcribe" ? r : null;
    const failedT = r && !r.ok && r.kind === "transcribe";
    $("result-transcribe").hidden = !(t || failedT);
    if (t || failedT) {
        $("rt-icon").replaceChildren(icon(r.ok ? "check" : "warning"));
        $("result-transcribe").classList.toggle("bad", !r.ok);
        $("rt-message").textContent = r.message;
        $("rt-summary").textContent = r.summary || "";
        $("rt-save").hidden = !r.ok;
        $("rt-animator").hidden = !(r.ok && r.placed);
        $("rt-translate").hidden = !r.ok;
    }
    const tr = r && r.kind === "translate" ? r : null;
    $("result-translate").hidden = !tr;
    if (tr) {
        $("rtr-icon").replaceChildren(icon(tr.ok ? "check" : "warning"));
        $("result-translate").classList.toggle("bad", !tr.ok);
        $("rtr-message").textContent = tr.message;
        $("rtr-files").replaceChildren(...(tr.translated || []).map(f => el("li", {title: f.path}, [
            el("b", {text: f.language}), el("span.muted", {text: f.cues === 1 ? " · 1 subtitle" : ` · ${f.cues} subtitles`}),
        ])));
    }
    if (r && r.kind === "setup" && !r.ok) Buddy.toast(`Didn't finish: ${r.message}`, 6000);
});

// ---------------------------------------------------------- translate --

let TR = null;

Buddy.on("translate", t => {
    TR = t;
    const engine = $("engine");
    engine.replaceChildren(...t.engines.map(e => el("option", {value: e.id, text: e.label, title: e.tip})));
    engine.value = t.engine;
    $("engine-status").textContent = t.status;
    $("targets").replaceChildren(...(t.targets.length
        ? t.targets.map(c => el("span.chip.lang", {text: c.name}))
        : [el("span.muted", {text: "No languages chosen yet."})]));
    $("folder").replaceChildren(el("bdi", {text: t.folder, translate: "no"}));
    $("folder").title = t.folder;
    $("tr-auto").checked = t.auto;
    $("tr-timeline").checked = t.timeline;
    $("tr-show").hidden = !t.can_show;
    drawTranslateButtons();
});

function drawTranslateButtons() {
    if (!TR) return;
    const free = !JOB.kind;
    const last = $("tr-last");
    last.disabled = !(TR.ready && free && TR.last && TR.targets.length);
    last.title = !TR.last ? "Transcribe a timeline first." : !TR.targets.length ? "Choose the languages first." : `Translate '${TR.last}'`;
    last.textContent = TR.last ? `Translate "${TR.last}"` : "Translate last transcript";
    $("tr-srt").disabled = !(TR.ready && free && TR.targets.length);
    $("choose-targets").disabled = !free;
}

$("engine").onchange = e => send("tr_option", {key: "engine", value: e.target.value});
$("tr-auto").onchange = e => send("tr_option", {key: "auto", value: e.target.checked});
$("tr-timeline").onchange = e => send("tr_option", {key: "timeline", value: e.target.checked});
$("choose-targets").onclick = () => pickLanguages({
    title: "Translate into", hint: "Each language you tick gets its own SRT file.",
    entries: CATALOG.targets, selected: TR.targets.map(c => c.code), min: 0,
}).then(codes => { if (codes) send("targets", {codes}); });

// ------------------------------------------------------------- setup --

Buddy.on("setup", s => {
    $("hardware").textContent = s.probing ? "Checking…" : s.hardware;
    $("rescan").disabled = s.checking || s.busy;
    $("rescan").textContent = s.checking ? "Checking…" : "Check again";
    const env = $("env-button");
    $("env-status").textContent = s.probing ? "Checking the engine…"
        : s.env_ready ? `Installed${s.env_versions ? ` (${s.env_versions})` : ""}.` : s.env_detail;
    $("env-hint").textContent = `Installs faster-whisper into Buddy's own folder (${s.root}), built from the Python Buddy runs on. Download: ${s.env_size}.`;
    env.textContent = s.env_ready ? "Repair engine" : "Install engine";
    env.classList.toggle("accent", !s.env_ready);
    env.disabled = s.busy || s.probing;
    $("models-hint").textContent = "Bigger models are more accurate and slower." + (s.recommended
        ? ` Recommended for this computer: ${s.recommended}, plus Parakeet v3 if you work in European languages – with both, Auto uses each for what it's best at.` : "");
    $("tmodels-hint").textContent = "Translates subtitles on this computer. Meta's NLLB-200 covers 200 languages, free for non-commercial use only (CC-BY-NC 4.0). Google's MADLAD-400 covers about 180 and is free for commercial use too (Apache 2.0) – pick it for client work. " +
        `Recommended: ${s.tr_recommended}. AI translation, with Ask Buddy's model, needs nothing from here.`;
    $("models").replaceChildren(...s.models.map(m => modelRow(m, s)));
    $("tmodels").replaceChildren(...s.tmodels.map(m => modelRow(m, s)));
    $("pick-model").disabled = $("pick-tmodel").disabled = s.busy;
});

function modelRow(m, s) {
    const actions = [];
    if (m.installed) {
        actions.push(el("span.installed", {title: m.where}, [icon("check"), el("span", {text: m.where === "Buddy's folder" ? "Installed" : "Installed – your copy"})]));
    } else {
        if (m.found) actions.push(el("button.btn", {type: "button", text: "Use this copy", title: m.found, disabled: s.busy,
                                                    onclick: () => send("use_copy", {id: m.id, path: m.found})}));
        actions.push(el("button.btn", {type: "button", text: `Download (${m.size})`, disabled: s.busy || !s.env_ready,
                                       title: s.env_ready ? "" : "Install the engine first.",
                                       onclick: () => send("download", {id: m.id})}));
    }
    return el(`div.model${m.installed ? ".have" : ""}`, {}, [
        el("div.model-text", {}, [
            el("div", {}, [el("b", {text: m.label}), el("span.muted", {text: ` · ${m.size}`}),
                           m.recommended ? el("span.chip.rec", {text: "Recommended"}) : null]),
            el("span.hint", {text: m.fit}),
        ]),
        el("div.model-actions", {}, actions),
    ]);
}

$("pick-model").onclick = () => send("pick_model_folder", {kind: "speech"});
$("pick-tmodel").onclick = () => send("pick_model_folder", {kind: "translation"});

// --------------------------------------------------- language picker --

/* A searchable, tickable list; resolves to the codes ticked, or null. */
function pickLanguages({title, hint, entries, selected, min, single}) {
    return new Promise(resolve => {
        const chosen = new Set(selected || []);
        const search = el("input.field", {type: "search", placeholder: "Search languages…", autocomplete: "off", spellcheck: "false"});
        const list = el("div.lang-list", {role: single ? "listbox" : "group"});
        const count = el("span.muted.small");
        const error = el("div.field-error");
        const ordered = [...entries.filter(e => chosen.has(e.code)), ...entries.filter(e => !chosen.has(e.code))];
        const rows = ordered.map(e => {
            const input = el("input", {type: single ? "radio" : "checkbox", name: "lang", checked: chosen.has(e.code)});
            input.addEventListener("change", () => {
                if (single) { chosen.clear(); chosen.add(e.code); }
                else if (input.checked) chosen.add(e.code);
                else chosen.delete(e.code);
                update();
            });
            const row = el("label.lang-row", {}, [input, el("span", {text: e.name}), el("span.muted.small.code", {text: e.code})]);
            row.dataset.find = `${e.name} ${e.code}`.toLowerCase();
            return row;
        });
        list.append(...rows);
        const update = () => {
            count.textContent = single ? "" : chosen.size ? `${chosen.size} selected` : "None selected";
            error.textContent = "";
        };
        search.addEventListener("input", () => {
            const q = search.value.trim().toLowerCase();
            for (const r of rows) r.hidden = !!q && !r.dataset.find.includes(q);
        });
        let answer = null;
        const buttons = [{label: "Cancel"}, {label: "OK", kind: "accent", onClick: close => {
            if (chosen.size < (min || 0)) { error.textContent = `Tick at least ${min}.`; return; }
            if (single && !chosen.size) { error.textContent = "Choose one."; return; }
            answer = [...chosen];
            close();
        }}];
        if (!single) buttons.unshift({label: "Clear all", onClick: () => {
            chosen.clear();
            for (const r of rows) r.querySelector("input").checked = false;
            update();
        }});
        Buddy.modal({title, body: [el("p.modal-text.muted", {text: hint}), search, list, el("div.row", {}, [count]), error],
                     buttons, onClose: () => resolve(answer)});
        update();
        requestAnimationFrame(() => {
            search.focus();
            const first = list.querySelector("input:checked");
            if (first && single) first.closest("label").scrollIntoView({block: "center"});
        });
    });
}

// ------------------------------------------------------------ asking --

Buddy.on("ask", a => {
    const answer = (ok, value) => send("answer", {id: a.id, ok, value});
    if (a.kind === "replace") {
        let done = false;
        Buddy.modal({
            title: "Subtitle track 1 isn't empty",
            body: el("p.modal-text", {text: `Subtitle track 1 already has ${a.count} subtitle(s).\n\nResolve only lets scripts place subtitles on track 1, so adding these means replacing what's there. The new subtitles are saved as an SRT either way.`}),
            buttons: [
                {label: "Keep the SRT file only", onClick: close => { done = true; answer(false); close(); }},
                {label: `Replace ${a.count} subtitle(s)`, kind: "danger", onClick: close => { done = true; answer(true); close(); }},
            ],
            onClose: () => { if (!done) answer(false); },
        });
    } else if (a.kind === "consent") {
        Buddy.confirm({
            title: "AI translation", ok: "Allow",
            text: `AI translation sends the transcript's text to ${a.name} over the internet, under your own API key (the provider may charge for it).\n\nAllow this? Buddy remembers the answer for this provider.`,
        }).then(ok => answer(ok));
    } else if (a.kind === "language") {
        pickLanguages({title: "Translate", hint: a.prompt, entries: CATALOG.targets, selected: [a.value], single: true})
            .then(codes => answer(!!codes, codes ? codes[0] : null));
    }
});

// --------------------------------------------------------- messages --

Buddy.on("toast", t => Buddy.toast(t.text, 3000));
Buddy.on("alert", a => Buddy.modal({title: a.title, body: el("p.modal-text", {text: a.text}), buttons: [{label: "OK", kind: "accent"}]}));

Buddy.on("log", entries => {
    $("activity").hidden = !entries.length;
    const lastEntry = entries[entries.length - 1];
    $("activity-last").textContent = lastEntry ? lastEntry.text : "";
    $("log").replaceChildren(...entries.slice().reverse().map(e => el(`li.${e.kind}`, {}, [
        el("span.muted", {text: e.time}), " ", el("span", {text: e.text}),
    ])));
});
