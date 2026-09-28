/*
 * The Text+ tabs' placement canvas: every Text+ clip under Resolve's playhead, drawn at its
 * real position, font, size and colour, to drag, resize (corner handles) and nudge (arrow
 * keys). text_animator/text_plus.py measures each clip's text (canvas_math.text_box - fractions of the frame
 * width, placed around the clip's Center the way Text+ lays it out: the Center is the middle of the
 * line box, not of the ink) and sends it with "canvas"; this draws and moves it locally and reports only a
 * finished edit ("move", "group_move", "resize", "bounding", through send) - nothing reaches Resolve
 * mid-drag. Snapping (frame centre, other clips' edges, centres and baselines, safe-zone edges; never
 * back to where a drag started, and off while Alt is held) and the
 * overlays (grid, safe zone, bounding lines) are drawn here from "overlay".
 *
 *   const c = PlacementCanvas(node, {tab: "layout", multi: false, send});
 *   c.update(canvasPayload); c.setOverlay(overlay); c.setBounding({on, left, right});
 */
"use strict";

function PlacementCanvas(root, {tab, multi, send}) {
    const {el} = Buddy;
    const SNAP_PX = 8;
    const MIN_SIZE = 0.005;
    const GUIDE = "#FFEB3B";          // guide yellow - drawn on the frame, not the theme
    const FRAME = "#1E1E1E";          // the empty frame: always a dark "video", whatever the theme
    const SVG = "http://www.w3.org/2000/svg";
    const svg = (tag, attrs) => {
        const node = document.createElementNS(SVG, tag);
        for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
        return node;
    };

    const stage = el("div.stage", {tabindex: "0", "aria-label": "Placement preview"});
    stage.style.background = FRAME;
    const under = svg("svg", {class: "layer under"});
    const boxes = el("div.boxes");
    const over = svg("svg", {class: "layer over"});
    const band = el("div.band", {hidden: true});
    const empty = el("div.stage-empty", {hidden: true});
    stage.append(under, boxes, over, band, empty);
    root.append(stage);

    let data = {items: [], resolution: [1920, 1080], padding: 4 / 640};
    let overlay = {};
    let bounding = {on: false, left: 0.15, right: 0.85};
    let W = 0, H = 0;
    const selected = new Set();
    const local = new Map();    // id -> {cx, cy, size, until} while an edit is on its way to Resolve
    const nodes = new Map();
    let guides = {x: null, y: null};
    let interaction = null;

    // ------------------------------------------------------------ geometry --

    function view(item) {
        const o = local.get(item.id);
        return {cx: o ? o.cx : item.cx, cy: o ? o.cy : item.cy, size: o && o.size !== undefined ? o.size : item.size};
    }

    // The ink box (padded) where Text+ draws it: box.left/top are from the Center, in frame
    // widths. baselines: each line's, in canvas pixels - what words in a row share.
    function rectOf(item, v = view(item)) {
        const k = item.size ? v.size / item.size : 1, b = item.box, pad = data.padding * W;
        const w = b.w * W * k + 2 * pad, h = b.h * W * k + 2 * pad;
        return {x: v.cx * W + b.left * W * k - pad, y: v.cy * H + b.top * W * k - pad, w, h,
                baselines: b.lines.map(([, y]) => v.cy * H + y * W * k)};
    }

    function fit() {
        const [rw, rh] = data.resolution;
        const avail = root.clientWidth;
        const maxH = Math.max(220, window.innerHeight * 0.62);
        let w = avail, h = avail * rh / rw;
        if (h > maxH) { h = maxH; w = h * rw / rh; }
        stage.style.width = `${Math.round(w)}px`;
        stage.style.height = `${Math.round(h)}px`;
        W = Math.round(w);
        H = Math.round(h);
        for (const layer of [under, over]) layer.setAttribute("viewBox", `0 0 ${W} ${H}`);
    }

    // --------------------------------------------------------------- drawing --

    function drawItem(item) {
        let node = nodes.get(item.id);
        if (!node) {
            node = el("div.tbox", {dataset: {id: item.id}});
            node.append(svg("svg", {class: "tsvg", translate: "no"}));   // the clip's own text
            for (const corner of ["tl", "tr", "bl", "br"]) node.append(el(`i.handle.${corner}`, {dataset: {corner}}));
            boxes.append(node);
            nodes.set(item.id, node);
        }
        const v = view(item), r = rectOf(item, v);
        const k = item.size ? v.size / item.size : 1, b = item.box, pad = data.padding * W;
        node.style.cssText = `left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px`;
        node.classList.toggle("selected", selected.has(item.id));
        node.title = `${item.text.replace(/\n/g, " ")} · track ${item.track} · size ${v.size.toFixed(3)}`;
        const text = node.firstChild;
        text.setAttribute("width", r.w);
        text.setAttribute("height", r.h);
        text.replaceChildren(...item.text.split("\n").map((line, i) => {
            const [ox, oy] = b.lines[i] || b.lines[0];      // the line's origin, from the Center
            const t = svg("text", {
                x: pad + (ox - b.left) * W * k,
                y: pad + (oy - b.top) * W * k,
                "font-size": b.px * W * k,
                "font-weight": item.font.weight,
                "font-style": item.font.italic ? "italic" : "normal",
                fill: item.color,
            });
            t.style.fontFamily = `"${item.font.family}", sans-serif`;
            t.textContent = line || " ";
            return t;
        }));
    }

    function drawOverlays() {
        under.replaceChildren();
        over.replaceChildren();
        const rects = (overlay.safe_rects || {})[overlay.safe_type] || [];
        if (overlay.safe_type && overlay.safe_type !== "None" && rects.length) {
            let d = `M0 0H${W}V${H}H0Z`;
            for (const [l, t, r, b] of rects) d += `M${l * W} ${t * H}H${r * W}V${b * H}H${l * W}Z`;
            under.append(svg("path", {d, "fill-rule": "evenodd", fill: overlay.safe_color, "fill-opacity": overlay.safe_opacity}));
        }
        const line = (x1, y1, x2, y2, attrs) => over.append(svg("line", Object.assign({x1, y1, x2, y2}, attrs)));
        const grid = {stroke: overlay.grid_color, "stroke-opacity": overlay.grid_opacity, "stroke-width": 1};
        if (overlay.grid_type === "Standard") {
            const step = Math.max(4, (overlay.grid_spacing || overlay.default_spacing || 1 / 16) * W);
            for (let x = step; x < W; x += step) line(x, 0, x, H, grid);
            for (let y = step; y < H; y += step) line(0, y, W, y, grid);
        } else {
            for (const f of (overlay.fractions || {})[overlay.grid_type] || []) {
                line(f * W, 0, f * W, H, grid);
                line(0, f * H, W, f * H, grid);
            }
        }
        const dash = {stroke: GUIDE, "stroke-width": 1, "stroke-dasharray": "5 4"};
        if (guides.x !== null) line(guides.x, 0, guides.x, H, dash);
        if (guides.y !== null) line(0, guides.y, W, guides.y, dash);
        if (bounding.on) {
            for (const side of ["left", "right"]) {
                const x = bounding[side] * W;
                const g = svg("g", {class: "bound", "data-side": side});
                g.append(svg("rect", {x: x - 7, y: 0, width: 14, height: H, fill: "transparent"}),
                         svg("line", {x1: x, y1: 0, x2: x, y2: H, stroke: GUIDE, "stroke-width": 2, "stroke-dasharray": "8 5"}));
                over.append(g);
            }
        }
    }

    function draw() {
        if (!W) fit();
        const now = Date.now();
        for (const [id, o] of local) if (o.until && o.until < now) local.delete(id);
        const ids = new Set(data.items.map(i => i.id));
        for (const [id, node] of nodes) if (!ids.has(id)) { node.remove(); nodes.delete(id); selected.delete(id); }
        for (const item of data.items) drawItem(item);
        drawOverlays();
        empty.hidden = data.items.length > 0;
        empty.textContent = data.items.length ? "" : "No Text+ clips under the playhead. Move Resolve's playhead over a Text+ clip.";
    }

    // --------------------------------------------------------------- snapping --

    function snap(moving, r) {
        guides = {x: null, y: null};
        if (!overlay.snap) return {dx: 0, dy: 0};
        const xs = [W / 2], ys = [H / 2], bases = [];
        if (overlay.snap_elements) {
            for (const item of data.items) {
                if (moving.has(item.id)) continue;
                const o = rectOf(item);
                xs.push(o.x, o.x + o.w / 2, o.x + o.w);
                ys.push(o.y, o.y + o.h / 2, o.y + o.h);
                bases.push(...o.baselines);
            }
        }
        if (overlay.snap_safe) {
            for (const [l, t, rr, b] of (overlay.safe_rects || {})[overlay.safe_type] || []) {
                xs.push(l * W, rr * W);
                ys.push(t * H, b * H);
            }
        }
        const best = (edges, candidates) => {
            let delta = 0, guide = null, dist = SNAP_PX;
            for (const e of edges) for (const c of candidates) {
                if (Math.abs(e - c) < dist) { dist = Math.abs(e - c); delta = c - e; guide = c; }
            }
            return [delta, guide];
        };
        const [dx, gx] = best([r.x, r.x + r.w / 2, r.x + r.w], xs);
        // Baseline to baseline wins when it's in reach: a word dropped on a line of the group
        // then sits on that line as Resolve draws it, whatever its letters hang below or reach above.
        let [dy, gy] = best(r.baselines, bases);
        if (gy === null) [dy, gy] = best([r.y, r.y + r.h / 2, r.y + r.h], ys);
        guides = {x: gx, y: gy};
        return {dx, dy};
    }

    // ----------------------------------------------------------- interaction --

    const byId = id => data.items.find(i => i.id === id);
    const point = e => {
        const r = stage.getBoundingClientRect();
        return {x: e.clientX - r.left, y: e.clientY - r.top};
    };
    const clamp01 = v => Math.min(1, Math.max(0, v));

    stage.addEventListener("pointerdown", e => {
        if (e.button !== 0) return;
        stage.focus({preventScroll: true});
        const p = point(e);
        const boundNode = e.target.closest(".bound");
        const handle = e.target.closest(".handle");
        const boxNode = e.target.closest(".tbox");
        try { stage.setPointerCapture(e.pointerId); } catch (_err) { /* a synthetic event */ }
        if (boundNode) {
            interaction = {kind: "bound", side: boundNode.dataset.side};
        } else if (handle && boxNode) {
            const item = byId(boxNode.dataset.id), v = view(item);
            const c = {x: v.cx * W, y: v.cy * H};
            interaction = {kind: "resize", id: item.id, c, d0: Math.max(1, Math.hypot(p.x - c.x, p.y - c.y)), size0: v.size};
            selected.clear();
            selected.add(item.id);
        } else if (boxNode) {
            const id = boxNode.dataset.id;
            if (multi && (e.shiftKey || e.ctrlKey || e.metaKey)) {
                if (selected.has(id)) selected.delete(id); else selected.add(id);
            } else if (!selected.has(id)) {
                selected.clear();
                selected.add(id);
            }
            const start = new Map([...selected].filter(byId).map(sid => [sid, {...view(byId(sid))}]));
            interaction = {kind: "move", p0: p, primary: id, start, moved: false};
        } else {
            if (!(multi && (e.shiftKey || e.ctrlKey))) selected.clear();
            interaction = multi ? {kind: "band", p0: p, base: new Set(selected)} : null;
        }
        draw();
        onSelect();
    });

    stage.addEventListener("pointermove", e => {
        if (!interaction) return;
        const p = point(e);
        const it = interaction;
        if (it.kind === "bound") {
            bounding[it.side] = clamp01(p.x / W);
            drawOverlays();
        } else if (it.kind === "resize") {
            const size = Math.max(MIN_SIZE, it.size0 * Math.hypot(p.x - it.c.x, p.y - it.c.y) / it.d0);
            const item = byId(it.id);
            local.set(it.id, {cx: view(item).cx, cy: view(item).cy, size});
            it.size = size;
            drawItem(item);
        } else if (it.kind === "move") {
            let dx = p.x - it.p0.x, dy = p.y - it.p0.y;
            if (!it.moved && Math.hypot(dx, dy) < 3) return;
            it.moved = true;
            const primary = byId(it.primary), s0 = it.start.get(it.primary);
            if (primary && s0 && !e.altKey) {             // Alt: place freely, no snapping
                const r = rectOf(primary, {cx: s0.cx + dx / W, cy: s0.cy + dy / H, size: s0.size});
                const s = snap(new Set(it.start.keys()), r);
                // Never snapped back to where it started: a word on a line (or an edge) could
                // otherwise not be nudged off it - every small move landed back in place.
                if (Math.abs(dx + s.dx) < 0.5) { s.dx = 0; guides.x = null; }
                if (Math.abs(dy + s.dy) < 0.5) { s.dy = 0; guides.y = null; }
                dx += s.dx;
                dy += s.dy;
            } else {
                guides = {x: null, y: null};
            }
            for (const [id, s] of it.start) local.set(id, {cx: s.cx + dx / W, cy: s.cy + dy / H, size: s.size});
            for (const id of it.start.keys()) drawItem(byId(id));
            drawOverlays();
        } else if (it.kind === "band") {
            const x = Math.min(p.x, it.p0.x), y = Math.min(p.y, it.p0.y);
            const w = Math.abs(p.x - it.p0.x), h = Math.abs(p.y - it.p0.y);
            band.hidden = false;
            band.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px`;
            selected.clear();
            for (const id of it.base) selected.add(id);
            for (const item of data.items) {
                const r = rectOf(item);
                if (r.x < x + w && r.x + r.w > x && r.y < y + h && r.y + r.h > y) selected.add(item.id);
            }
            for (const item of data.items) drawItem(item);
        }
    });

    const finish = () => {
        const it = interaction;
        interaction = null;
        band.hidden = true;
        guides = {x: null, y: null};
        if (!it) return;
        const hold = Date.now() + 1500;
        if (it.kind === "bound") {
            send("bounding", {on: true, left: bounding.left, right: bounding.right});
        } else if (it.kind === "resize" && it.size) {
            local.get(it.id).until = hold;
            send("resize", {tab, id: it.id, size: it.size});
        } else if (it.kind === "move" && it.moved) {
            const moves = [...it.start.keys()].map(id => {
                const o = local.get(id);
                o.until = hold;
                return {id, cx: clamp01(o.cx), cy: clamp01(o.cy)};
            });
            if (moves.length > 1) send("group_move", {tab, moves});
            else send("move", Object.assign({tab}, moves[0]));
        } else if (it.kind === "band") {
            onSelect();
        }
        draw();
    };
    stage.addEventListener("pointerup", finish);
    stage.addEventListener("pointercancel", finish);

    // Arrow keys nudge the selection (Shift: 10 px); sent once the keys stop.
    let nudgeTimer = 0;
    stage.addEventListener("keydown", e => {
        const step = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[e.key];
        if (!step || !selected.size) return;
        e.preventDefault();
        const n = e.shiftKey ? 10 : 1;
        for (const id of selected) {
            const item = byId(id);
            if (!item) continue;
            const v = view(item);
            local.set(id, {cx: clamp01(v.cx + step[0] * n / W), cy: clamp01(v.cy + step[1] * n / H), size: v.size});
            drawItem(item);
        }
        clearTimeout(nudgeTimer);
        nudgeTimer = setTimeout(() => {
            const moves = [...selected].filter(id => local.has(id)).map(id => {
                const o = local.get(id);
                o.until = Date.now() + 1500;
                return {id, cx: o.cx, cy: o.cy};
            });
            if (moves.length > 1) send("group_move", {tab, moves});
            else if (moves.length) send("move", Object.assign({tab}, moves[0]));
        }, 350);
    });

    let selectListener = () => {};
    const onSelect = () => selectListener([...selected]);

    // Only the host's width sizes the frame (its height follows from the frame), so a
    // height change - the frame itself resizing - mustn't loop back into here.
    let hostWidth = 0;
    new ResizeObserver(() => {
        if (root.clientWidth === hostWidth) return;
        hostWidth = root.clientWidth;
        fit();
        draw();
    }).observe(root);
    addEventListener("resize", () => { fit(); draw(); });

    return {
        update(payload) {
            const resized = !data || payload.resolution.join() !== data.resolution.join();
            data = payload;
            // An edit that just landed: Resolve now agrees, so the local copy can go.
            for (const item of data.items) {
                const o = local.get(item.id);
                if (o && !interaction && Math.abs(o.cx - item.cx) < 0.002 && Math.abs(o.cy - item.cy) < 0.002
                        && Math.abs((o.size ?? item.size) - item.size) < 1e-4) local.delete(item.id);
            }
            if (resized) fit();
            if (!interaction) draw();
        },
        setOverlay(o) { overlay = o; drawOverlays(); },
        setBounding(b) { if (!interaction || interaction.kind !== "bound") { bounding = {...b}; drawOverlays(); } },
        selected: () => [...selected],
        onSelect(fn) { selectListener = fn; },
        count: () => data.items.length,
    };
}
