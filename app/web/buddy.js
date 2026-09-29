/*
 * Buddy's shared web runtime - every web tool page loads this after
 * qrc:///qtwebchannel/qwebchannel.js and before its own script.
 *
 *   Buddy.on("event", data => ...)   Python's self.emit("event", data)
 *   Buddy.send("action", data)       calls the page's on_action(data)
 *   Buddy.el("div.card", {...}, [children])   small DOM builder
 *   Buddy.toast("Copied")
 *   Buddy.modal / confirm / menu     dialogs and popup menus
 *   Buddy.pickColor({hex, onPick})   the colour picker
 *   Buddy.t("Saved")                 text in the chosen language - rarely
 *                                    needed: pages are translated as drawn
 *
 * Python owns the state (core/web_page.py); a page's script only draws
 * what it is sent and reports what the user did. The "theme" event is
 * handled here for every page: its variables go on :root.
 *
 * Opened outside Buddy (no Qt transport, e.g. in a normal browser while
 * working on the CSS), sends are logged and Buddy.receive(name, data)
 * plays Python's part by hand.
 */
"use strict";

const Buddy = (() => {
    const handlers = {};
    let backend = null;
    const outbox = [];

    function on(name, fn) {
        (handlers[name] = handlers[name] || []).push(fn);
    }

    function receive(name, data) {
        for (const fn of handlers[name] || []) {
            try {
                fn(data);
            } catch (err) {
                console.error(`handler for "${name}" failed:`, err);
            }
        }
    }

    function send(name, data) {
        const payload = JSON.stringify(data === undefined ? null : data);
        if (backend) backend.send(name, payload);
        else if (window.qt) outbox.push([name, payload]);
        else console.log("[Buddy.send]", name, data);
    }

    on("theme", theme => {
        const root = document.documentElement;
        for (const [key, value] of Object.entries(theme.vars)) {
            root.style.setProperty(`--${key}`, value);
        }
        root.dataset.family = theme.family;
        root.dataset.light = String(theme.light);
    });

    /* Languages. Python sends the chosen language's strings ("i18n":
       {language, lang, strings: {english: translation} | null for English},
       core/i18n.py) and the page is translated here as it's drawn - its
       text, tooltips, placeholders and labels - so a page's script keeps
       writing English. Never touched: what people type (inputs, textareas,
       contenteditable) and anything inside translate="no", which a view
       puts on text people named themselves (palettes, clips, messages).
       The matching is core/i18n.py's (_Catalog.lookup) - change one,
       change both: runs of whitespace are one space, "..." is "…", "Name:"
       comes from "Name", and {placeholders} stand for values that change,
       which are kept as they are - except a {t_name}'s, one of Buddy's own
       words (a marker colour), which is translated too. */
    const i18n = (() => {
        const ATTRS = ["title", "placeholder", "aria-label", "alt"];
        // Not translated at all, text or attributes.
        const TREE_SKIP = 'script, style, [translate="no"], .notranslate';
        // Their text is the user's (or code); their placeholder and title aren't.
        const TEXT_SKIP = "textarea, code, kbd";
        const PH = /\{(\w+)\}/g;
        const seen = new WeakMap();   // text node -> {src, shown}; element -> {attr: {src, shown}}
        let exact = null;             // null: English, nothing to do
        let templates = [];
        let observer = null;

        const norm = s => s.replace(/\s+/g, " ").trim();
        const escape = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

        function load(strings) {
            exact = strings ? new Map(Object.entries(strings)) : null;
            templates = [];
            for (const [key, text] of exact || []) {
                if (!key.includes("{")) continue;
                const literal = key.replace(PH, "");
                if ((literal.match(/[\p{L}\p{M}]/gu) || []).length < 2) continue;   // core/i18n.py is_template
                const names = [];
                const parts = key.split(/(\{\w+\})/);
                const source = parts.map(part => {
                    const m = /^\{(\w+)\}$/.exec(part);
                    if (!m) return escape(part);
                    names.push(m[1]);
                    return "([\\s\\S]+?)";
                }).join("");
                if (!names.length) continue;
                // The longest fixed piece: a cheap test before the regex.
                const hint = parts.filter(p => !/^\{\w+\}$/.test(p)).sort((a, b) => b.length - a.length)[0];
                templates.push({re: new RegExp(`^${source}$`), names, text, hint, weight: literal.length});
            }
            templates.sort((a, b) => b.weight - a.weight);
        }

        function exactHit(key) {
            let hit = exact.get(key);
            if (hit !== undefined) return hit;
            if (key.includes("...") || key.includes("…")) {
                hit = exact.get(key.replaceAll("...", "…")) ?? exact.get(key.replaceAll("…", "..."));
                if (hit !== undefined) return hit;
            }
            for (const tail of [":", "…"]) {
                if (key.length > tail.length && key.endsWith(tail)) {
                    hit = exact.get(key.slice(0, -tail.length).trimEnd());
                    if (hit !== undefined) return hit + tail;
                }
            }
            hit = exact.get(key + ":");
            return hit === undefined ? undefined : hit.replace(/[:：]+$/, "").trimEnd();
        }

        /* text in the chosen language, or text itself. */
        function t(text) {
            if (!exact || typeof text !== "string" || !text) return text;
            const key = norm(text);
            if (!key || key.length > 4000 || !/\p{L}/u.test(key)) return text;
            let hit = exactHit(key);
            if (hit === undefined) {
                for (const tp of templates) {
                    if (tp.hint && !key.includes(tp.hint)) continue;
                    const m = tp.re.exec(key);
                    if (!m) continue;
                    const values = {};
                    tp.names.forEach((name, i) => {
                        values[name] = name.startsWith("t_") ? exactHit(norm(m[i + 1])) ?? m[i + 1] : m[i + 1];
                    });
                    hit = tp.text.replace(PH, (all, name) => (name in values ? values[name] : all));
                    break;
                }
            }
            if (hit === undefined) return text;
            return /^\s*/.exec(text)[0] + hit + /\s*$/.exec(text)[0];
        }

        function doText(node) {
            const rec = seen.get(node);
            // Unchanged since it was translated: translate its English again
            // (the language may have changed); otherwise the page wrote new text.
            const src = rec && node.data === rec.shown ? rec.src : node.data;
            const out = t(src);
            if (out === src && !rec) return;
            seen.set(node, {src, shown: out});
            if (node.data !== out) node.data = out;
        }

        function doAttrs(elm) {
            let recs = seen.get(elm);
            for (const name of ATTRS) {
                const value = elm.getAttribute(name);
                if (value === null) continue;
                const rec = recs && recs[name];
                const src = rec && value === rec.shown ? rec.src : value;
                const out = t(src);
                if (out === src && !rec) continue;
                if (!recs) seen.set(elm, recs = {});
                recs[name] = {src, shown: out};
                if (value !== out) elm.setAttribute(name, out);
            }
        }

        const textSkipped = elm => !elm || elm.isContentEditable || !!elm.closest(`${TREE_SKIP}, ${TEXT_SKIP}`);

        function walk(root) {
            if (root.nodeType === Node.TEXT_NODE) {
                if (!textSkipped(root.parentElement)) doText(root);
                return;
            }
            if (root.nodeType !== Node.ELEMENT_NODE || root.closest(TREE_SKIP)) return;
            if (root.isContentEditable || root.closest(TEXT_SKIP)) { doAttrs(root); return; }
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
                acceptNode: n => {
                    if (n.nodeType !== Node.ELEMENT_NODE) return NodeFilter.FILTER_ACCEPT;
                    if (n.matches(TREE_SKIP)) return NodeFilter.FILTER_REJECT;
                    doAttrs(n);
                    return n.isContentEditable || n.matches(TEXT_SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP;
                },
            });
            doAttrs(root);
            for (let n = walker.nextNode(); n; n = walker.nextNode()) doText(n);
        }

        function onMutations(records) {
            for (const r of records) {
                if (r.type === "characterData") {
                    if (!textSkipped(r.target.parentElement)) doText(r.target);
                } else if (r.type === "attributes") {
                    if (!r.target.closest(TREE_SKIP)) doAttrs(r.target);
                } else {
                    for (const n of r.addedNodes) walk(n);
                }
            }
        }

        function apply(data) {
            load(data && data.strings);
            document.documentElement.lang = (data && data.lang) || "en";
            const root = document.body || document.documentElement;
            if (observer) observer.takeRecords();
            walk(root);   // for English, puts back what was translated
            if (exact && !observer) {
                observer = new MutationObserver(onMutations);
                observer.observe(root, {subtree: true, childList: true, characterData: true,
                                        attributes: true, attributeFilter: ATTRS});
            } else if (!exact && observer) {
                observer.disconnect();
                observer = null;
            }
        }

        return {apply, t};
    })();

    on("i18n", data => i18n.apply(data));

    /* el("button.btn.accent#send", {title: "Send", onclick: fn}, ["Send"]) */
    function el(spec, props, children) {
        const match = /^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i.exec(spec) || [];
        const node = document.createElement(match[1] || "div");
        for (const part of (match[2] || "").match(/[.#][\w-]+/g) || []) {
            if (part[0] === ".") node.classList.add(part.slice(1));
            else node.id = part.slice(1);
        }
        for (const [key, value] of Object.entries(props || {})) {
            if (value === undefined || value === null || value === false) continue;
            if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
            else if (key === "text") node.textContent = value;
            else if (key === "html") node.innerHTML = value;   // only ever Python-rendered HTML
            else if (key === "dataset") Object.assign(node.dataset, value);
            else if (key in node && typeof value !== "string") node[key] = value;
            else node.setAttribute(key, value === true ? "" : value);
        }
        for (const child of [].concat(children || [])) {
            if (child === null || child === undefined || child === false) continue;
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
        return node;
    }

    /* Inline SVG icons (stroke style, 24px grid), so pages need no files. */
    const ICONS = {
        send: '<path d="M4 12l16-8-6 16-3-6.5z"/><path d="M11 13.5l9-9.5"/>',
        copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
        check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
        plus: '<path d="M12 5v14M5 12h14"/>',
        left: '<path d="M15 5l-7 7 7 7"/>',
        right: '<path d="M9 5l7 7-7 7"/>',
        trash: '<path d="M4 7h16M9 7V4.5h6V7"/><path d="M6.5 7l.9 12a2 2 0 0 0 2 1.8h5.2a2 2 0 0 0 2-1.8l.9-12"/><path d="M10 11v6M14 11v6"/>',
        download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
        warning: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
        tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>',
        refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 20v-4h-4"/>',
        undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
        play: '<path d="M7 4.5v15l12.5-7.5z"/>',
        pause: '<path d="M8 5v14M16 5v14"/>',
        stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
        spark: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 17l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
        folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
        arrow: '<path d="M4 12h15M13 6l6 6-6 6"/>',
        image: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/>',
        music: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
        film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
        file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
        search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4.2-4.2"/>',
        chat: '<path d="M4 5h16v11H9l-5 4z"/>',
        dropper: '<path d="M14.5 5.5l4 4"/><path d="M17.5 3.5a2.1 2.1 0 0 1 3 3l-2 2-3-3z"/><path d="M15.5 8.5L7 17l-3 1 1-3 8.5-8.5"/>',
        more: '<circle cx="5.5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18.5" cy="12" r="1.2"/>',
        swap: '<path d="M7 7h12l-3-3M17 17H5l3 3"/>',
        pin: '<path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>',
        gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
        split: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M12 2.5v19"/>',
        programs: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="3.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="3.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
        cascade: '<rect x="3" y="3" width="12" height="10" rx="2"/><path d="M7 17v2a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-2"/>',
        tile: '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
        popout: '<path d="M14 4h6v6M20 4l-8.5 8.5"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
        palette: '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.7-.8 1.7-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-.9.8-1.7 1.7-1.7H16a5 5 0 0 0 5-5c0-4-4-7.2-9-7.2z"/><circle cx="7.5" cy="11" r="1"/><circle cx="10" cy="7" r="1"/><circle cx="15" cy="7.5" r="1"/>',
        eye: '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.8"/>',
        bug: '<path d="M8 2l1.9 1.9M16 2l-1.9 1.9"/><path d="M9 7.1V6a3 3 0 0 1 6 0v1.1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6z"/><path d="M12 20v-9"/><path d="M6.5 9C4.6 8.8 3 7.1 3 5M6 13H2M3 21c0-2.1 1.7-3.9 3.8-4M21 5c0 2.1-1.6 3.8-3.5 4M22 13h-4M17.2 17c2.1.1 3.8 1.9 3.8 4"/>',
    };

    function icon(name) {
        const span = document.createElement("span");
        span.innerHTML = `<svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
        return span.firstChild;
    }

    let toastNode = null;
    let toastTimer = 0;

    /* toast("Stopped", 6000, {label: "Undo", onClick: fn}) - the action
       button is optional; the toast goes when it's pressed or times out. */
    function toast(text, ms, action) {
        if (!toastNode) {
            toastNode = el("div.toast", {role: "status", "aria-live": "polite"});
            document.body.append(toastNode);
        }
        const hide = () => toastNode.classList.remove("show", "has-action");
        toastNode.replaceChildren(el("span", {text}));
        if (action) {
            toastNode.append(el("button.toast-action", {text: action.label, onclick: () => {
                clearTimeout(toastTimer);
                hide();
                action.onClick();
            }}));
        }
        toastNode.classList.toggle("has-action", !!action);
        toastNode.classList.add("show");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(hide, ms || 2200);
    }

    /* A modal over the page. modal({title, body: node|[nodes], buttons:
       [{label, kind: "accent"|"danger"|"", onClick: close => ..., id}],
       onClose}). Escape and a click outside close it - the top one only,
       when one opens over another. Returns {close, node}. */
    const modals = [];
    const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
                      'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    function modal({title, body, buttons, onClose, wide}) {
        const previous = document.activeElement;
        const dialog = el(`div.modal${wide ? ".wide" : ""}`, {role: "dialog", "aria-modal": "true", "aria-label": title});
        const backdrop = el("div.modal-backdrop", {}, dialog);
        let closed = false;
        const close = () => {
            if (closed) return;
            closed = true;
            modals.splice(modals.indexOf(dialog), 1);
            backdrop.remove();
            document.removeEventListener("keydown", onKey, true);
            if (onClose) onClose();
            if (previous && previous.focus) previous.focus();
        };
        const onKey = e => {
            if (modals[modals.length - 1] !== dialog) return;
            if (e.key === "Escape") { e.stopImmediatePropagation(); close(); }
            // Tab stays inside the dialog: the page behind is inert while it's up.
            if (e.key === "Tab") {
                const stops = [...dialog.querySelectorAll(FOCUSABLE)].filter(n => n.offsetParent !== null);
                if (!stops.length) { e.preventDefault(); return; }
                const first = stops[0], last = stops[stops.length - 1];
                const inside = dialog.contains(document.activeElement);
                if (e.shiftKey && (!inside || document.activeElement === first)) { e.preventDefault(); last.focus(); }
                else if (!e.shiftKey && (!inside || document.activeElement === last)) { e.preventDefault(); first.focus(); }
            }
        };
        dialog.append(
            el("h2.modal-title", {text: title}),
            el("div.modal-body", {}, [].concat(body || [])),
            el("div.modal-buttons", {}, (buttons || []).map(b => el(
                `button.btn${b.kind ? "." + b.kind : ""}`,
                {type: "button", id: b.id, text: b.label, onclick: () => b.onClick ? b.onClick(close) : close()},
            ))),
        );
        backdrop.addEventListener("mousedown", e => { if (e.target === backdrop) close(); });
        document.addEventListener("keydown", onKey, true);
        document.body.append(backdrop);
        modals.push(dialog);
        requestAnimationFrame(() => {
            // A field if there is one; otherwise the main button - but never
            // a destructive one, so Enter can't delete by accident: a Delete
            // dialog starts on Cancel.
            const danger = dialog.querySelector(".modal-buttons .btn.danger");
            const first = dialog.querySelector("input, select, textarea") ||
                          (danger ? dialog.querySelector(".modal-buttons .btn:not(.danger)")
                                  : dialog.querySelector(".modal-buttons .btn:last-child"));
            if (first) first.focus();
        });
        return {close, node: dialog};
    }

    /* await confirm({title, text, ok: "Delete", danger: true}) -> true/false */
    function confirm({title, text, ok, cancel, danger}) {
        return new Promise(resolve => {
            let answer = false;
            modal({
                title,
                body: el("p.modal-text", {text}),
                buttons: [
                    {label: cancel || "Cancel"},
                    {label: ok || "OK", kind: danger ? "danger" : "accent",
                     onClick: close => { answer = true; close(); }},
                ],
                onClose: () => resolve(answer),
            });
        });
    }

    let menuNode = null;

    /* A popup menu at (x, y) - a right-click or a "more" button:
       menu({x, y, items: [{label, onClick, danger, disabled, swatch: "#hex",
       raw: true for a label people named - a folder, a palette - so it isn't
       translated, checked: true/false for a choice of which one is current -
       a tick, and room for one on the rest} | {sep: true} | {heading: "text"}]}).
       Escape, a click elsewhere or a pick closes it; Up/Down/Enter work.
       Returns close. */
    function menu({x, y, items}) {
        closeMenu();
        const node = el("div.menu-pop", {role: "menu"});
        const ticks = items.some(item => item && "checked" in item);
        for (const item of items) {
            if (item.sep) { node.append(el("hr")); continue; }
            if (item.heading) { node.append(el("div.menu-heading", {text: item.heading})); continue; }
            node.append(el(`button${item.danger ? ".danger" : ""}`, {
                type: "button", role: ticks && "checked" in item ? "menuitemradio" : "menuitem",
                "aria-checked": ticks && "checked" in item ? String(!!item.checked) : undefined,
                disabled: !!item.disabled,
                onclick: () => { closeMenu(); if (item.onClick) item.onClick(); },
            }, [ticks ? el("span.menu-tick", {}, item.checked ? [icon("check")] : []) : null,
                item.swatch ? el("i.menu-swatch", {style: `background:${item.swatch}`}) : null,
                el("span", {text: item.label, translate: item.raw ? "no" : undefined})]));
        }
        document.body.append(node);
        menuNode = node;
        const w = node.offsetWidth, h = node.offsetHeight;
        node.style.left = `${Math.max(6, Math.min(x, innerWidth - w - 6))}px`;
        node.style.top = `${Math.max(6, y + h > innerHeight - 6 ? y - h : y)}px`;
        const first = node.querySelector("button:not(:disabled)");
        if (first) first.focus();
        return closeMenu;
    }

    function closeMenu() {
        if (menuNode) { menuNode.remove(); menuNode = null; }
    }

    document.addEventListener("mousedown", e => { if (menuNode && !menuNode.contains(e.target)) closeMenu(); }, true);
    document.addEventListener("keydown", e => {
        if (!menuNode) return;
        if (e.key === "Escape") { e.stopPropagation(); closeMenu(); return; }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            const buttons = [...menuNode.querySelectorAll("button:not(:disabled)")];
            const at = buttons.indexOf(document.activeElement);
            const next = buttons[(at + (e.key === "ArrowDown" ? 1 : buttons.length - 1)) % buttons.length];
            if (next) next.focus();
        }
    }, true);
    addEventListener("blur", closeMenu);
    addEventListener("resize", closeMenu);

    /* A colour picker popover: saturation/value square, hue bar, hex box,
       before/after chips and (when the page has one) a screen dropper.
       pickColor({hex, title, at: Element|DOMRect|{x, y}, onPick(hex), okLabel,
       dropper(set) - set(hex) feeds the sampled colour back}). */
    const picker = (() => {
        let node = null, state = null;

        const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
        function hexToRgb(hex) {
            const n = parseInt(hex.slice(1), 16);
            return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
        }
        function rgbToHex(r, g, b) {
            return "#" + [r, g, b].map(v => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();
        }
        function rgbToHsv(r, g, b) {
            r /= 255; g /= 255; b /= 255;
            const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
            let h = 0;
            if (d) {
                if (max === r) h = ((g - b) / d) % 6;
                else if (max === g) h = (b - r) / d + 2;
                else h = (r - g) / d + 4;
                h *= 60;
                if (h < 0) h += 360;
            }
            return [h, max ? d / max : 0, max];
        }
        function hsvToHex(h, s, v) {
            const f = n => {
                const k = (n + h / 60) % 6;
                return v - v * s * Math.max(0, Math.min(k, 4 - k, 1));
            };
            return rgbToHex(f(5) * 255, f(3) * 255, f(1) * 255);
        }
        function parseHex(text) {
            const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(text).trim());
            if (!m) return null;
            const d = m[1].length === 3 ? [...m[1]].map(c => c + c).join("") : m[1];
            return "#" + d.toUpperCase();
        }

        function build() {
            node = el("div.picker#picker", {role: "dialog"});
            node.innerHTML = `
                <div class="pk-title"></div>
                <div class="pk-sv" tabindex="0" role="slider" aria-label="Saturation and brightness"><i class="pk-white"></i><i class="pk-black"></i><b class="pk-knob"></b></div>
                <div class="pk-hue" tabindex="0" role="slider" aria-label="Hue" aria-valuemin="0" aria-valuemax="360"><b class="pk-knob"></b></div>
                <div class="pk-row">
                    <span class="pk-chips"><i class="pk-old" title="Before"></i><i class="pk-new" title="After"></i></span>
                    <input class="field pk-hex" spellcheck="false" maxlength="7" aria-label="Hex colour">
                    <button type="button" class="btn icon pk-drop"></button>
                </div>
                <div class="pk-buttons"><button type="button" class="btn pk-cancel"></button><button type="button" class="btn accent pk-ok"></button></div>`;
            node.querySelector(".pk-drop").append(icon("dropper"));
            // The spectrum and the white/black ramps are the picker's content,
            // not the theme's colours.
            node.querySelector(".pk-hue").style.background =
                "linear-gradient(to right,#f00,#ff0 17%,#0f0 33%,#0ff 50%,#00f 67%,#f0f 83%,#f00)";
            node.querySelector(".pk-white").style.background = "linear-gradient(to right,#fff,rgba(255,255,255,0))";
            node.querySelector(".pk-black").style.background = "linear-gradient(to top,#000,rgba(0,0,0,0))";
            drag(node.querySelector(".pk-sv"), (fx, fy) => { state.s = fx; state.v = 1 - fy; update(); });
            drag(node.querySelector(".pk-hue"), fx => { state.h = fx * 360; update(); });
            // The keyboard does what the pointer does: arrows nudge (Shift for
            // bigger steps), Home/End jump to the ends.
            keys(node.querySelector(".pk-sv"), (key, big) => {
                const step = big ? 0.1 : 0.01;
                if (key === "ArrowLeft") state.s = clamp(state.s - step, 0, 1);
                else if (key === "ArrowRight") state.s = clamp(state.s + step, 0, 1);
                else if (key === "ArrowUp") state.v = clamp(state.v + step, 0, 1);
                else if (key === "ArrowDown") state.v = clamp(state.v - step, 0, 1);
                else if (key === "Home") state.s = 0;
                else if (key === "End") state.s = 1;
                else return false;
                return true;
            });
            keys(node.querySelector(".pk-hue"), (key, big) => {
                const step = big ? 10 : 1;
                if (key === "ArrowLeft" || key === "ArrowDown") state.h = clamp(state.h - step, 0, 360);
                else if (key === "ArrowRight" || key === "ArrowUp") state.h = clamp(state.h + step, 0, 360);
                else if (key === "Home") state.h = 0;
                else if (key === "End") state.h = 360;
                else return false;
                return true;
            });
            const hexField = node.querySelector(".pk-hex");
            hexField.addEventListener("input", () => {
                const hex = parseHex(hexField.value);
                hexField.classList.toggle("invalid", !hex);
                if (hex) setHex(hex, false);
            });
            hexField.addEventListener("keydown", e => { if (e.key === "Enter") ok(); });
            node.querySelector(".pk-old").onclick = () => setHex(state.old);
            node.querySelector(".pk-drop").onclick = () => {
                if (state && state.dropper) state.dropper(hex => { if (state) setHex(hex); });
            };
            node.querySelector(".pk-cancel").onclick = close;
            node.querySelector(".pk-ok").onclick = ok;
            node.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
            document.addEventListener("mousedown", e => {
                if (state && !node.contains(e.target) && !e.target.closest(".menu-pop")) close();
            }, true);
            document.body.append(node);
        }

        function drag(area, onMove) {
            area.addEventListener("pointerdown", e => {
                area.setPointerCapture(e.pointerId);
                const move = ev => {
                    const r = area.getBoundingClientRect();
                    onMove(clamp((ev.clientX - r.left) / r.width, 0, 1), clamp((ev.clientY - r.top) / r.height, 0, 1));
                };
                move(e);
                area.onpointermove = move;
                area.onpointerup = () => { area.onpointermove = null; };
            });
        }

        function keys(area, onKey) {
            area.addEventListener("keydown", e => {
                if (!state || !onKey(e.key, e.shiftKey)) return;
                e.preventDefault();
                update();
            });
        }

        function setHex(hex, updateField = true) {
            const [h, s, v] = rgbToHsv(...hexToRgb(hex));
            // Greys have no hue - keep the one the bar is on.
            state.h = s ? h : state.h;
            state.s = s;
            state.v = v;
            update(updateField, hex);
        }

        function update(updateField = true, exact) {
            const hex = exact || hsvToHex(state.h, state.s, state.v);
            state.hex = hex;
            node.querySelector(".pk-sv").style.backgroundColor = hsvToHex(state.h, 1, 1);
            const sv = node.querySelector(".pk-sv .pk-knob");
            sv.style.left = `${state.s * 100}%`;
            sv.style.top = `${(1 - state.v) * 100}%`;
            sv.style.background = hex;
            const hue = node.querySelector(".pk-hue .pk-knob");
            hue.style.left = `${state.h / 360 * 100}%`;
            hue.style.background = hsvToHex(state.h, 1, 1);
            const svArea = node.querySelector(".pk-sv"), hueArea = node.querySelector(".pk-hue");
            svArea.setAttribute("aria-valuetext",
                `Saturation ${Math.round(state.s * 100)}%, brightness ${Math.round(state.v * 100)}%`);
            hueArea.setAttribute("aria-valuenow", String(Math.round(state.h)));
            hueArea.setAttribute("aria-valuetext", `${Math.round(state.h)} degrees`);
            node.querySelector(".pk-new").style.background = hex;
            if (updateField) {
                const field = node.querySelector(".pk-hex");
                field.value = hex;
                field.classList.remove("invalid");
            }
        }

        function open({hex, title, at, onPick, okLabel, dropper, dropperLabel}) {
            if (!node) build();
            closeMenu();
            state = {h: 0, s: 0, v: 0, old: hex || "#FFFFFF", onPick, dropper};
            node.querySelector(".pk-title").textContent = title || "Choose a colour";
            node.querySelector(".pk-cancel").textContent = "Cancel";
            node.querySelector(".pk-ok").textContent = okLabel || "OK";
            const drop = node.querySelector(".pk-drop");
            drop.hidden = !dropper;
            drop.title = dropperLabel || "Pick a colour from anywhere on screen";
            node.querySelector(".pk-old").style.background = state.old;
            setHex(state.old);
            node.hidden = false;
            const w = node.offsetWidth, h = node.offsetHeight;
            // An element has no x/left of its own: anchor to where it's drawn.
            if (at instanceof Element) at = at.getBoundingClientRect();
            if (!at) at = {x: (innerWidth - w) / 2, y: 120};
            const x = at.left !== undefined ? at.left : at.x, y = at.bottom !== undefined ? at.bottom + 6 : at.y;
            node.style.left = `${clamp(x, 8, innerWidth - w - 8)}px`;
            node.style.top = `${y + h > innerHeight - 8 ? Math.max(8, (at.top !== undefined ? at.top : y) - h - 6) : y}px`;
            const field = node.querySelector(".pk-hex");
            field.focus();
            field.select();
        }

        function ok() {
            if (!state) return;
            const pick = state.onPick, hex = state.hex;
            close();
            if (pick) pick(hex);
        }

        function close() {
            if (node) node.hidden = true;
            state = null;
        }

        return {open, close, parseHex};
    })();

    function connect() {
        if (!(window.qt && qt.webChannelTransport)) return;
        new QWebChannel(qt.webChannelTransport, channel => {
            backend = channel.objects.backend;
            backend.event.connect((name, json) => receive(name, JSON.parse(json)));
            for (const [name, payload] of outbox.splice(0)) backend.send(name, payload);
            backend.ready();
        });
    }

    // After the page's own script has registered its handlers.
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", connect);
    else setTimeout(connect, 0);

    // ---------------------------------------------------------- dropdowns --
    /* Every <select class="field"> opens Buddy's own list rather than
       Chromium's native popup, which ignores the theme (a grey Windows
       list, whatever the page looks like). The <select> stays the real
       control: pages still set .value, fill its options and listen for
       "change" exactly as before - this only draws the list and sets the
       value. Keyboard: Enter/Space/Alt+Down open it, arrows and typing move,
       Enter picks, Escape closes; arrows on a closed one still step through
       the options natively.

       A view too small for the list (the shell's header) can set
       Buddy.dropdownRoom to the room it can make below itself; it hears
       "buddy:dropdown" ({open, bottom}) to grow while the list is open. */
    let dropdown = null;   // {select, node, items, index, above}

    function dropdownOptions(select) {
        const out = [];
        for (const child of select.children) {
            if (child.tagName === "OPTGROUP") {
                out.push({heading: child.label});
                for (const o of child.children) out.push({option: o});
            } else if (child.tagName === "OPTION") {
                out.push({option: child});
            }
        }
        return out;
    }

    function openDropdown(select) {
        closeDropdown(true);
        const rows = dropdownOptions(select);
        if (!rows.some(r => r.option)) return;
        const node = el("div.dropdown-pop", {role: "listbox", "aria-label": select.getAttribute("aria-label") || select.title || ""});
        const items = [];
        for (const row of rows) {
            if (row.heading !== undefined) { node.append(el("div.dropdown-heading", {text: row.heading})); continue; }
            const o = row.option;
            const selected = o.value === select.value;
            const item = el("div.dropdown-opt", {role: "option", "aria-selected": String(selected), "aria-disabled": o.disabled ? "true" : undefined},
                            [el("span.dropdown-check", {}, selected ? icon("check") : null), el("span.dropdown-label", {text: o.textContent,
                                translate: o.closest('[translate="no"]') ? "no" : null})]);
            const index = items.length;
            item.addEventListener("mousemove", () => highlight(index, false));
            item.addEventListener("mouseup", e => { if (e.button === 0) pickDropdown(index); });
            items.push({node: item, option: o});
            node.append(item);
        }
        document.body.append(node);
        const r = select.getBoundingClientRect();
        node.style.minWidth = `${r.width}px`;
        // scrollHeight leaves out the border, which max-height (border-box) counts.
        const needed = node.scrollHeight + node.offsetHeight - node.clientHeight;
        const extra = typeof Buddy.dropdownRoom === "number" ? Buddy.dropdownRoom : 0;
        const below = Math.max(innerHeight, r.bottom + extra) - r.bottom - 8;
        const above = r.top - 8;
        const up = needed > below && above > below;
        const room = Math.max(80, up ? above : below);
        node.style.maxHeight = `${Math.min(needed, room, 420)}px`;
        const width = node.offsetWidth;
        node.style.left = `${Math.max(6, Math.min(r.left, innerWidth - width - 6))}px`;
        if (up) { node.style.bottom = `${innerHeight - r.top + 4}px`; node.classList.add("up"); }
        else node.style.top = `${r.bottom + 4}px`;
        select.classList.add("open");
        select.setAttribute("aria-expanded", "true");
        const start = Math.max(0, items.findIndex(i => i.option.value === select.value));
        dropdown = {select, node, items, index: -1, typed: "", typedAt: 0};
        highlight(start, true);
        if (!up) document.dispatchEvent(new CustomEvent("buddy:dropdown",
                                                        {detail: {open: true, bottom: r.bottom + 4 + node.offsetHeight}}));
        requestAnimationFrame(() => node.classList.add("show"));
    }

    function highlight(index, scroll) {
        if (!dropdown) return;
        const items = dropdown.items;
        if (index < 0 || index >= items.length || items[index].option.disabled) return;
        if (dropdown.index >= 0) items[dropdown.index].node.classList.remove("active");
        dropdown.index = index;
        items[index].node.classList.add("active");
        if (scroll) items[index].node.scrollIntoView({block: "nearest"});
    }

    function stepDropdown(delta) {
        const items = dropdown.items;
        let i = dropdown.index;
        for (let n = 0; n < items.length; n++) {
            i = Math.max(0, Math.min(items.length - 1, i + delta));
            if (!items[i].option.disabled) break;
        }
        highlight(i, true);
    }

    function pickDropdown(index) {
        if (!dropdown) return;
        const {select, items} = dropdown;
        const item = items[index];
        if (!item || item.option.disabled) return;
        const changed = select.value !== item.option.value;
        closeDropdown();
        select.focus();
        if (changed) {
            select.value = item.option.value;
            select.dispatchEvent(new Event("input", {bubbles: true}));
            select.dispatchEvent(new Event("change", {bubbles: true}));
        }
    }

    function closeDropdown(instant) {
        if (!dropdown) return;
        const {select, node} = dropdown;
        dropdown = null;
        select.classList.remove("open");
        select.setAttribute("aria-expanded", "false");
        document.dispatchEvent(new CustomEvent("buddy:dropdown", {detail: {open: false}}));
        if (instant) { node.remove(); return; }
        node.classList.remove("show");
        node.classList.add("closing");
        node.addEventListener("transitionend", () => node.remove(), {once: true});
        setTimeout(() => node.remove(), 250);
    }

    const isDropdown = t => t && t.matches && t.matches("select.field") && !t.multiple && !(t.size > 1);

    document.addEventListener("mousedown", e => {
        if (dropdown && dropdown.node.contains(e.target)) { e.preventDefault(); return; }
        const select = e.target.closest ? e.target.closest("select.field") : null;
        if (select && isDropdown(select)) {
            e.preventDefault();          // no native popup
            if (select.disabled) return;
            const same = dropdown && dropdown.select === select;
            select.focus();
            if (same) closeDropdown(); else openDropdown(select);
            return;
        }
        if (dropdown) closeDropdown();
    }, true);

    document.addEventListener("keydown", e => {
        if (dropdown) {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); stepDropdown(e.key === "ArrowDown" ? 1 : -1); }
            else if (e.key === "Home" || e.key === "End") { e.preventDefault(); highlight(e.key === "Home" ? 0 : dropdown.items.length - 1, true); }
            else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickDropdown(dropdown.index); }
            else if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeDropdown(); }
            else if (e.key === "Tab") closeDropdown();
            else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
                // Type-ahead: the first option starting with what's typed.
                const now = Date.now();
                dropdown.typed = (now - dropdown.typedAt < 700 ? dropdown.typed : "") + e.key.toLowerCase();
                dropdown.typedAt = now;
                const i = dropdown.items.findIndex(it => it.option.textContent.trim().toLowerCase().startsWith(dropdown.typed));
                if (i >= 0) highlight(i, true);
            }
            e.stopPropagation();
            return;
        }
        if (isDropdown(e.target) && !e.target.disabled &&
                (e.key === "Enter" || e.key === " " || e.key === "F4" || (e.altKey && e.key === "ArrowDown"))) {
            e.preventDefault();
            openDropdown(e.target);
        }
    }, true);

    // Anything that moves the page from under it closes it.
    document.addEventListener("scroll", e => {
        if (dropdown && !dropdown.node.contains(e.target)) closeDropdown(true);
    }, true);
    // Only a change of width: a view that grows downward to show the list
    // (the shell's header) mustn't close it by doing so.
    let dropdownWidth = innerWidth;
    addEventListener("resize", () => {
        if (innerWidth !== dropdownWidth) closeDropdown(true);
        dropdownWidth = innerWidth;
    });
    addEventListener("blur", () => closeDropdown(true));

    return {on, send, receive, el, icon, toast, modal, confirm, menu, closeMenu, closeDropdown, t: i18n.t,
            pickColor: picker.open, closePicker: picker.close, parseHex: picker.parseHex};
})();
