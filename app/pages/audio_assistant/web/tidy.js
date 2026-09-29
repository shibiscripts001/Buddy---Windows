/*
 * Tidy up: page.py's list of what Buddy made that nothing uses any more
 * ("tidy" - resolve_ext.tidy_scan), shown in a dialog with a tick for each,
 * and the ones still ticked sent back ("tidy_remove") once the user says so.
 * page.py does the removing; there's no Undo for it, so this asks first.
 * Its own scope: timeline.js's top-level names are shared with it otherwise.
 */
(() => {
    const {el} = Buddy;

    const KIND = {
        curve: "Curve timeline",
        wav: "Processed audio",
        file: "Processed audio file, not in the media pool",
    };

    function size(bytes) {
        if (!bytes) return "";
        const mb = bytes / (1024 * 1024);
        if (mb >= 100) return `${Math.round(mb)} MB`;
        if (mb >= 1) return `${mb.toFixed(1)} MB`;
        return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    }

    Buddy.on("tidy", data => {
        const items = (data && data.items) || [];
        if (!items.length) {
            return Buddy.modal({title: "Nothing to tidy", buttons: [{label: "OK", kind: "accent"}],
                                body: el("p.modal-text", {text: "Everything Buddy made is still in use."})});
        }
        const boxes = items.map(() => el("input", {type: "checkbox", checked: true}));
        Buddy.modal({
            title: "Tidy up",
            wide: true,
            body: [
                el("p.modal-text", {text: "Nothing on any timeline uses these any more. Curve timelines are deleted " +
                    "from the project. Processed audio leaves the media pool, and its file goes to the Recycle Bin " +
                    "once no other project uses it. There's no Undo for this."}),
                el("div.tidy-list", {}, items.map((item, i) => el("label.tidy-row", {title: item.path || item.name}, [
                    boxes[i],
                    el("span.tidy-name", {text: item.name, translate: "no"}),     // Buddy's and the files' names
                    el("span.muted.small", {text: KIND[item.kind] || ""}),
                    el("span.muted.small.tidy-size", {text: size(item.size), translate: "no"}),
                ]))),
            ],
            buttons: [
                {label: "Cancel"},
                {label: "Remove", kind: "danger", onClick: close => {
                    const ids = items.filter((_, i) => boxes[i].checked).map(item => item.id);
                    close();
                    if (ids.length) Buddy.send("tidy_remove", {ids});
                }},
            ],
        });
    });

    Buddy.on("tidy_done", d => {
        if (d.note) {
            Buddy.modal({title: "Tidy up", body: el("p.modal-text", {text: d.note}), buttons: [{label: "OK", kind: "accent"}]});
        }
        if (d.failed) {
            Buddy.modal({title: "Some weren't removed", buttons: [{label: "OK", kind: "accent"}],
                         body: el("p.modal-text", {text: d.failed === 1
                             ? "One is in use again, or Resolve refused it."
                             : `${d.failed} are in use again, or Resolve refused them.`})});
        }
        if (d.removed) Buddy.toast(d.removed === 1 ? "Tidied up 1 item" : `Tidied up ${d.removed} items`, 3500);
    });
})();
