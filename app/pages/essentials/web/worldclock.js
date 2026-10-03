/* Essentials' World Clock (loaded after essentials.js, whose helpers it
   uses): the time where you are and in each city you add, how far ahead or
   behind it is, whether it's day there and inside working hours - live, or
   at a time you plan ("10 am Tuesday in Los Angeles"). The time zone work
   is the browser's own Intl data; Python only keeps the list (page.py). */
"use strict";

const LOCAL_ZONE = (() => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (err) { return "UTC"; }
})();
const WORK_START = 9, WORK_END = 18, DAY_START = 6, DAY_END = 18, NIGHT_START = 22;

// Cities people look for that aren't a time zone's own name.
const CITY_ALIASES = [
    ["San Francisco", "America/Los_Angeles"], ["Seattle", "America/Los_Angeles"], ["Las Vegas", "America/Los_Angeles"],
    ["San Diego", "America/Los_Angeles"], ["Portland", "America/Los_Angeles"], ["Hollywood", "America/Los_Angeles"],
    ["Salt Lake City", "America/Denver"], ["Austin", "America/Chicago"], ["Dallas", "America/Chicago"],
    ["Houston", "America/Chicago"], ["Nashville", "America/Chicago"], ["Atlanta", "America/New_York"],
    ["Washington DC", "America/New_York"], ["Boston", "America/New_York"], ["Miami", "America/New_York"],
    ["Philadelphia", "America/New_York"], ["Montreal", "America/Toronto"], ["Ottawa", "America/Toronto"],
    ["Calgary", "America/Edmonton"], ["Rio de Janeiro", "America/Sao_Paulo"], ["Brasilia", "America/Sao_Paulo"],
    ["Edinburgh", "Europe/London"], ["Manchester", "Europe/London"], ["Barcelona", "Europe/Madrid"],
    ["Munich", "Europe/Berlin"], ["Hamburg", "Europe/Berlin"], ["Frankfurt", "Europe/Berlin"],
    ["Milan", "Europe/Rome"], ["Geneva", "Europe/Zurich"], ["Krakow", "Europe/Warsaw"], ["St Petersburg", "Europe/Moscow"],
    ["Abu Dhabi", "Asia/Dubai"], ["Doha", "Asia/Qatar"], ["Tel Aviv", "Asia/Jerusalem"], ["Mumbai", "Asia/Kolkata"],
    ["Delhi", "Asia/Kolkata"], ["New Delhi", "Asia/Kolkata"], ["Bangalore", "Asia/Kolkata"], ["Bengaluru", "Asia/Kolkata"],
    ["Chennai", "Asia/Kolkata"], ["Hanoi", "Asia/Bangkok"], ["Saigon", "Asia/Ho_Chi_Minh"], ["Beijing", "Asia/Shanghai"],
    ["Shenzhen", "Asia/Shanghai"], ["Guangzhou", "Asia/Shanghai"], ["Osaka", "Asia/Tokyo"], ["Kyoto", "Asia/Tokyo"],
    ["Busan", "Asia/Seoul"], ["Canberra", "Australia/Sydney"], ["Gold Coast", "Australia/Brisbane"],
    ["Wellington", "Pacific/Auckland"], ["Cape Town", "Africa/Johannesburg"], ["Hawaii", "Pacific/Honolulu"],
    ["UTC", "UTC"], ["GMT", "UTC"],
];

const ZONES = (() => {
    let names = [];
    try { names = Intl.supportedValuesOf("timeZone"); } catch (err) { /* older engines: the aliases only */ }
    if (!names.includes("UTC")) names = [...names, "UTC"];
    return names;
})();

const cityOf = zone => zone === "UTC" ? "UTC" : zone.split("/").pop().replace(/_/g, " ");
const lang = () => document.documentElement.lang || "en";

const formatters = new Map();
function fmt(key, zone, options) {
    const id = `${key}|${zone}|${lang()}`;
    if (!formatters.has(id)) {
        const locale = key === "parts" ? "en-US" : lang();
        try {
            formatters.set(id, new Intl.DateTimeFormat(locale, {timeZone: zone, ...options}));
        } catch (err) {
            formatters.set(id, new Intl.DateTimeFormat("en-US", {timeZone: "UTC", ...options}));
        }
    }
    return formatters.get(id);
}

/* A moment's wall clock in a zone: {y, m, d, h, min, s, weekday 0-6}. */
function wall(zone, t) {
    const parts = {};
    for (const p of fmt("parts", zone, {hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
                                        minute: "2-digit", second: "2-digit", weekday: "short"}).formatToParts(t)) {
        parts[p.type] = p.value;
    }
    return {y: +parts.year, m: +parts.month, d: +parts.day, h: +parts.hour % 24, min: +parts.minute, s: +parts.second,
            weekday: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(parts.weekday)};
}

/* The zone's offset from UTC at that moment, in minutes. */
function offsetMin(zone, t) {
    const w = wall(zone, t);
    return Math.round((Date.UTC(w.y, w.m - 1, w.d, w.h, w.min, w.s) - Math.floor(t / 1000) * 1000) / 60000);
}

/* The moment a zone's clock reads y-m-d h:min. An hour a clock change
   repeats gives its first time round; one it skips (2:30 the night clocks
   go forward) moves on by the change, the way calendars do - to 3:30. */
function instantIn(zone, y, m, d, h, min) {
    const guess = Date.UTC(y, m - 1, d, h, min);
    const first = guess - offsetMin(zone, guess) * 60000;
    const second = guess - offsetMin(zone, first) * 60000;
    const reads = t => { const w = wall(zone, t); return w.h === h && w.min === min && w.d === d; };
    for (const t of [Math.min(first, second), Math.max(first, second)]) if (reads(t)) return t;
    return Math.max(first, second);
}

function gapText(minutes) {
    if (!minutes) return "Same time";
    const sign = minutes > 0 ? "+" : "−";
    const a = Math.abs(minutes), h = Math.floor(a / 60), m = a % 60;
    return `${sign}${h ? `${h} h` : ""}${h && m ? " " : ""}${m ? `${m} min` : ""}`;
}

/* ------------------------------------------------------------- the list -- */

let clocks = [];
let planAt = null;   // a planned moment (ms), or null for now

const hours12 = () => prefs.clock_hours === "12";

function saveClocks() {
    send("clocks", {items: clocks});
    drawPlanZones();
    drawClocks();
}

let tickers = [];   // one per card: (t) => fills in its time, date, gap and state

function clockCard(zone, label, here, index) {
    const time = el("div.ess-clock-time", {translate: "no"});
    const date = el("span", {translate: "no"});
    const rel = el("span.ess-clock-rel");
    const gap = el("span.ess-clock-gap");
    const dot = el("span.ess-clock-dot", {"aria-hidden": "true"});
    const state = el("span");
    const chip = el("span.chip.ess-clock-state", {}, [dot, state]);
    const zoneLine = el("div.ess-clock-zone.muted.small", {translate: "no"});
    const card = el("div.card.ess-clock-card", {}, [
        el("div.ess-clock-head", {}, [
            el("div.ess-clock-name", {}, [
                el("span.ess-clock-city", {text: label || cityOf(zone), translate: label ? "no" : null}),
                here ? el("span.chip", {text: "Here"}) : null,
            ]),
            here ? null : el("button.btn.ghost.icon.ess-clock-more", {type: "button", title: "More", onclick: e => {
                const r = e.currentTarget.getBoundingClientRect();
                Buddy.menu({x: r.left, y: r.bottom + 4, items: [
                    {label: "Rename…", onClick: () => renameClock(index)},
                    {label: "Move earlier", disabled: index === 0, onClick: () => moveClock(index, -1)},
                    {label: "Move later", disabled: index === clocks.length - 1, onClick: () => moveClock(index, 1)},
                    {sep: true},
                    {label: "Remove", danger: true, onClick: () => { clocks.splice(index, 1); saveClocks(); }},
                ]});
            }}, Buddy.icon("more")),
        ]),
        zoneLine,
        time,
        el("div.ess-clock-date", {}, [date, rel]),
        el("div.ess-clock-foot", {}, [gap, chip]),
    ]);
    const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
    tickers.push(t => {
        const w = wall(zone, t), local = wall(LOCAL_ZONE, t);
        const live = planAt === null;
        setText(time, fmt(`time${hours12() ? 12 : 24}${live ? "s" : ""}`, zone, {
            hour: hours12() ? "numeric" : "2-digit", minute: "2-digit", second: live ? "2-digit" : undefined,
            hour12: hours12()}).format(t));
        setText(date, fmt("date", zone, {weekday: "short", month: "short", day: "numeric"}).format(t));
        const zoneName = (fmt("zname", zone, {timeZoneName: "long"}).formatToParts(t).find(p => p.type === "timeZoneName") || {}).value || zone;
        setText(zoneLine, label ? `${cityOf(zone)} · ${zoneName}` : zoneName);
        const dayRel = Date.UTC(w.y, w.m - 1, w.d) - Date.UTC(local.y, local.m - 1, local.d);
        setText(rel, dayRel > 0 ? "tomorrow" : dayRel < 0 ? "yesterday" : "");
        setText(gap, here ? "Your time" : gapText(offsetMin(zone, t) - offsetMin(LOCAL_ZONE, t)));
        const day = w.h >= DAY_START && w.h < DAY_END;
        const working = w.weekday >= 1 && w.weekday <= 5 && w.h >= WORK_START && w.h < WORK_END;
        setText(dot, day ? "☀" : "☾");
        setText(state, working ? "Working hours" : w.h >= NIGHT_START || w.h < DAY_START ? "Night" : "Off hours");
        chip.classList.toggle("on", working);
        card.classList.toggle("working", working);
        card.classList.toggle("night", !day);
    });
    return card;
}

/* Builds the cards - when the list, the clock style or the language changes. */
function drawClocks() {
    tickers = [];
    $("clocks").replaceChildren(
        clockCard(LOCAL_ZONE, "", true, -1),
        ...clocks.map((c, i) => clockCard(c.zone, c.label, false, i)),
    );
    tickClocks();
}

/* Fills in every card's times. */
function tickClocks() {
    const t = planAt === null ? Date.now() : planAt;
    for (const tick of tickers) tick(t);
}

function moveClock(index, by) {
    const to = index + by;
    if (to < 0 || to >= clocks.length) return;
    clocks.splice(to, 0, clocks.splice(index, 1)[0]);
    saveClocks();
}

function renameClock(index) {
    const c = clocks[index];
    const field = el("input.field", {type: "text", maxlength: 60, value: c.label, placeholder: cityOf(c.zone), "aria-label": "Name"});
    const done = close => {
        c.label = field.value.trim().replace(/\s+/g, " ");
        close();
        saveClocks();
    };
    field.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); done(dialog.close); } });
    const dialog = Buddy.modal({
        title: "Name this clock",
        body: [el("p.modal-text", {text: "A name of your own - a client, a studio - shown instead of the city. Leave it empty for the city's name."}), field],
        buttons: [{label: "Cancel"}, {label: "Save", kind: "accent", onClick: done}],
    });
}

/* --------------------------------------------------------- adding one -- */

function searchZones(text) {
    const q = text.trim().toLowerCase().replace(/_/g, " ");
    const seen = new Set();
    const out = [];
    const push = (name, zone) => {
        const key = `${name}|${zone}`;
        if (!seen.has(key) && out.length < 60) {
            seen.add(key);
            out.push({name, zone});
        }
    };
    if (!q) {
        for (const [name, zone] of CITY_ALIASES.slice(0, 12)) push(name, zone);
        return out;
    }
    const starts = [], has = [];
    const consider = (name, zone, region) => {
        const n = name.toLowerCase(), z = zone.toLowerCase().replace(/_/g, " ");
        if (n.startsWith(q)) starts.push([name, zone]);
        else if (n.includes(q) || z.includes(q) || (region && region.toLowerCase().startsWith(q))) has.push([name, zone]);
    };
    for (const [name, zone] of CITY_ALIASES) consider(name, zone);
    for (const zone of ZONES) consider(cityOf(zone), zone, zone.split("/")[0]);
    for (const [name, zone] of [...starts, ...has]) push(name, zone);
    return out;
}

function addClock() {
    const list = el("div.ess-zone-list", {role: "listbox", "aria-label": "Cities"});
    const field = el("input.field", {type: "search", placeholder: "Search a city or time zone", "aria-label": "Search a city or time zone"});
    let dialog = null;
    const pick = (name, zone) => {
        const existing = clocks.findIndex(c => c.zone === zone);
        if (existing >= 0) {
            if (!clocks[existing].label && name !== cityOf(zone)) clocks[existing].label = name;
        } else {
            clocks.push({zone, label: name !== cityOf(zone) ? name : ""});
        }
        dialog.close();
        saveClocks();
    };
    const draw = () => {
        const now = Date.now();
        const rows = searchZones(field.value);
        list.replaceChildren(...(rows.length ? rows.map(r => el("button.ess-zone-row", {type: "button", onclick: () => pick(r.name, r.zone)}, [
            el("span.ess-zone-name", {text: r.name, translate: "no"}),
            el("span.muted.small", {text: r.zone === "UTC" ? "UTC" : r.zone.replace(/_/g, " "), translate: "no"}),
            el("span.ess-zone-time", {text: fmt("time24", r.zone, {hour: "2-digit", minute: "2-digit", hour12: false}).format(now), translate: "no"}),
        ])) : [el("div.muted.small.ess-zone-empty", {text: "No city or time zone by that name."})]));
    };
    field.addEventListener("input", draw);
    field.addEventListener("keydown", e => {
        if (e.key === "Enter") {
            e.preventDefault();
            const first = list.querySelector(".ess-zone-row");
            if (first) first.click();
        }
    });
    draw();
    dialog = Buddy.modal({title: "Add a city", body: [field, list], buttons: [{label: "Cancel"}]});
}

/* ---------------------------------------------------------- planning -- */

const pad2 = n => String(n).padStart(2, "0");

function drawPlanZones() {
    const select = $("plan-zone");
    const keep = select.value;
    select.replaceChildren(
        el("option", {value: LOCAL_ZONE, text: `Here – ${cityOf(LOCAL_ZONE)}`}),
        ...clocks.filter(c => c.zone !== LOCAL_ZONE).map(c => el("option", {value: c.zone, text: c.label || cityOf(c.zone), translate: "no"})),
    );
    select.value = [...select.options].some(o => o.value === keep) ? keep : LOCAL_ZONE;
}

function fillPlanFields(t, zone) {
    const w = wall(zone, t);
    $("plan-date").value = `${w.y}-${pad2(w.m)}-${pad2(w.d)}`;
    $("plan-time").value = `${pad2(w.h)}:${pad2(w.min)}`;
}

function planFromFields() {
    const [y, m, d] = $("plan-date").value.split("-").map(Number);
    const [h, min] = $("plan-time").value.split(":").map(Number);
    if (![y, m, d, h, min].every(Number.isFinite)) return;
    planAt = instantIn($("plan-zone").value, y, m, d, h, min);
    drawPlanSay();
    tickClocks();
}

function drawPlanSay() {
    $("plan-now").hidden = planAt === null;
    $("plan-say").textContent = planAt === null
        ? "Pick a date and time - a client's \"10 am Tuesday\", say - and every clock shows what time that is there."
        : "Every clock shows the planned time. Back to now shows the live time again.";
}

function backToNow() {
    planAt = null;
    fillPlanFields(Date.now(), $("plan-zone").value);
    drawPlanSay();
    tickClocks();
}

$("plan-date").addEventListener("change", planFromFields);
$("plan-time").addEventListener("change", planFromFields);
$("plan-zone").addEventListener("change", () => {
    // The same moment, read in the newly picked zone.
    fillPlanFields(planAt === null ? Date.now() : planAt, $("plan-zone").value);
});
$("plan-now").addEventListener("click", backToNow);

/* ------------------------------------------------------------- the rest -- */

function copyTimes() {
    const t = planAt === null ? Date.now() : planAt;
    const line = (zone, name) => {
        const time = fmt(`copy${hours12() ? 12 : 24}`, zone, {weekday: "short", month: "short", day: "numeric",
            hour: hours12() ? "numeric" : "2-digit", minute: "2-digit", hour12: hours12()}).format(t);
        return `${name}: ${time}`;
    };
    copy([line(LOCAL_ZONE, cityOf(LOCAL_ZONE)), ...clocks.map(c => line(c.zone, c.label || cityOf(c.zone)))].join("\n"));
}

function drawHours() {
    for (const b of document.querySelectorAll("#clock-hours button")) b.setAttribute("aria-pressed", String(b.dataset.hours === (hours12() ? "12" : "24")));
}

$("clock-add").addEventListener("click", addClock);
$("clock-copy").addEventListener("click", copyTimes);
$("clock-hours").addEventListener("click", e => {
    const b = e.target.closest("button[data-hours]");
    if (!b) return;
    setPref("clock_hours", b.dataset.hours);
    drawHours();
    tickClocks();
});
$("ess-tabs").addEventListener("click", () => { if (tab === "clock") tickClocks(); });

// Live: once a second while the tab is open (and not showing a planned time).
setInterval(() => { if (tab === "clock" && planAt === null) tickClocks(); }, 1000);

Buddy.on("state", s => {
    clocks = Array.isArray(s.clocks) ? s.clocks : [];
    drawHours();
    drawPlanZones();
    if (planAt === null) fillPlanFields(Date.now(), $("plan-zone").value);
    drawPlanSay();
    drawClocks();
});
Buddy.on("i18n", () => drawClocks());   // dates in the new language

drawPlanZones();
fillPlanFields(Date.now(), LOCAL_ZONE);
drawPlanSay();
