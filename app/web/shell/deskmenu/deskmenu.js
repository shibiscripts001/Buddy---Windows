/* The taskbar's popup (core/desk_web.py DeskMenu): Programs - every tool,
   grouped as in the rail, searchable, each with a pin - or one taskbar
   button's actions. It reports its size; the shell fits the window to it. */
"use strict";

const {el, icon, send} = Buddy;
const menu = document.getElementById("menu");

function reportSize() {
    const r = menu.getBoundingClientRect();
    // Room for the hard shadow past the card's right and bottom.
    send("size", {width: Math.ceil(r.width + 8), height: Math.ceil(r.height + 8)});
}
new ResizeObserver(reportSize).observe(menu);

document.addEventListener("keydown", e => {
    if (e.key === "Escape") send("act", {action: "close"});
});

function programs(m) {
    const search = el("input.field.menu-search", {type: "search", placeholder: "Search tools", "aria-label": "Search tools"});
    const list = el("div.menu-list");
    const rows = [];
    const groups = [];
    for (const group of m.groups) {
        const members = group.items.map(item => {
            const row = el("div.menu-row", {"data-state": item.state}, [
                el("button.menu-open", {type: "button", role: "menuitem", text: item.label,
                                        onclick: () => send("open", {id: item.id})}),
                el("button.menu-pin", {
                    type: "button", "aria-pressed": String(item.pinned),
                    title: item.pinned ? "Unpin from the taskbar" : "Pin to the taskbar",
                    "aria-label": item.pinned ? `Unpin ${item.label} from the taskbar` : `Pin ${item.label} to the taskbar`,
                    onclick: () => send("pin", {id: item.id, on: !item.pinned}),
                }, [icon("pin")]),
            ]);
            row.dataset.name = item.label.toLowerCase();
            rows.push(row);
            return row;
        });
        const heading = group.heading ? el("div.menu-group", {text: group.heading}) : null;
        if (heading) { list.append(heading); groups.push({heading, members}); }
        list.append(...members);
    }
    search.oninput = () => {
        const q = search.value.trim().toLowerCase();
        for (const row of rows) row.hidden = !!q && !row.dataset.name.includes(q);
        for (const g of groups) g.heading.hidden = g.members.every(r => r.hidden);
    };
    search.onkeydown = e => {
        if (e.key !== "Enter") return;
        const first = rows.find(r => !r.hidden);
        if (first) first.querySelector(".menu-open").click();
    };
    const foot = el("div.menu-foot", {}, [
        el("button.btn.ghost", {type: "button", onclick: () => send("settings")}, [icon("gear"), el("span", {text: "Settings"})]),
    ]);
    menu.className = "desk-menu programs";
    menu.replaceChildren(el("div.menu-title", {text: "Programs"}), search, list, foot);
    requestAnimationFrame(() => search.focus());
}

function actions(m) {
    menu.className = "desk-menu actions";
    menu.replaceChildren(el("div.menu-title", {text: m.title}), ...m.items.map(item =>
        el(`button.menu-act${item.danger ? ".danger" : ""}`, {
            type: "button", role: "menuitem", text: item.label,
            onclick: () => send("act", {action: item.action, id: m.id}),
        })));
    requestAnimationFrame(() => { const first = menu.querySelector("button"); if (first) first.focus(); });
}

Buddy.on("menu", m => {
    if (m.mode === "programs") programs(m);
    else actions(m);
    requestAnimationFrame(reportSize);
});
