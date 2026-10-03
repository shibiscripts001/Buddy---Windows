/* Solitaire - Klondike (loaded after games.js, whose helpers it uses).
   Seven columns, a deck dealt one or three at a time, four foundations to
   build up from ace to king by suit. Drag cards, or click one to send it
   where it fits; click the deck to draw. Undo goes back as far as you like.
   When every card is face up and the deck is used, it finishes itself.
   Like the other games it pauses - the clock stopped, the table covered -
   when it can't be seen or Buddy loses the focus. Deals are random, so
   some can't be won, as in any Klondike. */
"use strict";

const solitaire = (() => {
    const CW = 100, CH = 140, GAP = 14, M = 14;            // card size, gap and margin, in court units
    const W = M * 2 + CW * 7 + GAP * 6, H = 600;          // tall enough for a king-to-ace column
    const TOP = M, TAB_Y = M + CH + 22;                   // the top row, and where the columns start
    const DOWN_STEP = 12, UP_STEP = 30;                   // how much of a card shows under the next one
    const FAN = 34;                                       // the draw-three fan of the waste: room for a "10"
    const RANKS = ["", "A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"];
    const SUITS = ["♠", "♥", "♦", "♣"];
    const FACE = "#fbfbfa", INK_BLACK = "#1f2328", INK_RED = "#cf2f3a", EDGE = "rgba(0,0,0,0.28)";
    const MAX_UNDO = 1000;

    const court = $("sol-court"), canvas = $("sol-canvas"), ctx = canvas.getContext("2d");
    let g = null;                // the game: {stock, waste, found[4], tab[7], moves}
    let undo = [];
    let state = "play";          // play, paused, over
    let started = false;         // the clock starts with the first move
    let elapsed = 0, since = 0, ticker = 0;
    let finishing = 0;           // the auto-finish's timer
    let drag = null;             // {from, idx, cards, dx, dy, x, y, moved, sx, sy}
    let hits = [];               // what's drawn where, for the pointer: {pile, idx, x, y, w, h}
    let stats = {};              // "draw1" -> {won, best} (page.py)
    let colors = {};
    let scale = 1;

    const drawCount = () => (prefs.sol_draw === "3" ? 3 : 1);
    const modeKey = () => `draw${drawCount()}`;
    const soundOn = () => prefs.sol_sound !== false;
    const blip = (freq, length, type, at) => gameSound.blip(soundOn(), freq, length, type, at);
    const red = c => c.s === 1 || c.s === 2;
    const last = a => a[a.length - 1];
    const seconds = () => elapsed + (started && state === "play" ? (performance.now() - since) / 1000 : 0);

    /* --------------------------------------------------- dealing -- */
    function newGame() {
        clearTimeout(finishing);
        const deck = [];
        for (let s = 0; s < 4; s++) for (let r = 1; r <= 13; r++) deck.push({s, r, up: false});
        for (let i = deck.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [deck[i], deck[j]] = [deck[j], deck[i]];
        }
        const tab = [];
        for (let col = 0; col < 7; col++) {
            tab.push(deck.splice(0, col + 1));
            last(tab[col]).up = true;
        }
        g = {stock: deck, waste: [], found: [[], [], [], []], tab, moves: 0};
        undo = [];
        state = "play";
        started = false;
        elapsed = 0;
        clearInterval(ticker);
        showOverlay();
        drawCounters();
        fit();
    }

    function startClock() {
        if (started) return;
        started = true;
        since = performance.now();
        clearInterval(ticker);
        ticker = setInterval(drawCounters, 500);
    }

    function stopClock() {
        elapsed = seconds();
        clearInterval(ticker);
    }

    /* ----------------------------------------------------- piles -- */
    const pileCards = p => (p.kind === "stock" ? g.stock : p.kind === "waste" ? g.waste
        : p.kind === "found" ? g.found[p.i] : g.tab[p.i]);
    const same = (a, b) => a.kind === b.kind && a.i === b.i;

    /* The cards a pointer can pick up from a pile, from card idx: a whole
       face-up run from a column, the top card anywhere else. */
    function movable(p, idx) {
        const cards = pileCards(p);
        if (p.kind === "stock" || idx < 0 || idx >= cards.length || !cards[idx].up) return null;
        if (p.kind !== "tab" && idx !== cards.length - 1) return null;
        return cards.slice(idx);
    }

    function fits(cards, to) {
        const target = pileCards(to), top = last(target), c = cards[0];
        if (to.kind === "found") return cards.length === 1 && (top ? top.s === c.s && c.r === top.r + 1 : c.r === 1);
        if (to.kind === "tab") return top ? top.up && red(top) !== red(c) && c.r === top.r - 1 : c.r === 13;
        return false;
    }

    function snapshot() {
        undo.push(JSON.stringify(g));
        if (undo.length > MAX_UNDO) undo.shift();
    }

    function move(from, idx, to) {
        const cards = movable(from, idx);
        if (!cards || same(from, to) || !fits(cards, to)) return false;
        snapshot();
        startClock();
        pileCards(from).splice(idx);
        pileCards(to).push(...cards);
        if (from.kind === "tab" && g.tab[from.i].length) last(g.tab[from.i]).up = true;   // the card underneath turns over
        g.moves++;
        blip(to.kind === "found" ? 660 : 440, 0.05);
        afterMove();
        return true;
    }

    function drawFromStock() {
        if (state !== "play") return;
        if (!g.stock.length && !g.waste.length) return;
        snapshot();
        startClock();
        if (!g.stock.length) {
            // The pile turned back over to be dealt again.
            g.stock = g.waste.reverse().map(c => ({...c, up: false}));
            g.waste = [];
            blip(300, 0.06);
        } else {
            for (let k = 0; k < drawCount() && g.stock.length; k++) g.waste.push({...g.stock.pop(), up: true});
            blip(520, 0.03);
        }
        g.moves++;
        afterMove();
    }

    /* A click on a card: to a foundation if it can go, else to the column
       it fits best - one that already has cards before an empty one. */
    function smartMove(from, idx) {
        const cards = movable(from, idx);
        if (!cards) return false;
        if (cards.length === 1 && from.kind !== "found") {
            for (let i = 0; i < 4; i++) if (move(from, idx, {kind: "found", i})) return true;
        }
        const columns = [0, 1, 2, 3, 4, 5, 6].filter(i => !(from.kind === "tab" && from.i === i));
        for (const i of columns) if (g.tab[i].length && move(from, idx, {kind: "tab", i})) return true;
        // An empty column only for a king that isn't already at the head of one.
        if (!(from.kind === "tab" && idx === 0)) {
            for (const i of columns) if (!g.tab[i].length && move(from, idx, {kind: "tab", i})) return true;
        }
        blip(180, 0.05, "triangle");
        return false;
    }

    function takeBack() {
        if (state !== "play" || !undo.length || finishing) return;
        g = JSON.parse(undo.pop());
        blip(360, 0.04);
        drawCounters();
        draw();
    }

    function afterMove() {
        drawCounters();
        draw();
        if (g.found.every(f => f.length === 13)) return win();
        const allUp = g.tab.every(col => col.every(c => c.up));
        if (allUp && !g.stock.length && !g.waste.length) finishSoon();
    }

    /* Everything face up and the deck used: the rest goes up by itself. */
    function finishSoon() {
        clearTimeout(finishing);
        finishing = setTimeout(() => {
            finishing = 0;
            if (state !== "play") return;
            let best = null;
            for (let i = 0; i < 7; i++) {
                const c = last(g.tab[i]);
                if (c && (!best || c.r < best.c.r)) best = {i, c};
            }
            if (!best) return;
            for (let f = 0; f < 4; f++) if (move({kind: "tab", i: best.i}, g.tab[best.i].length - 1, {kind: "found", i: f})) return;
        }, 90);
    }

    function win() {
        stopClock();
        state = "over";
        const time = Math.round(elapsed * 10) / 10;
        const s = stats[modeKey()] || {won: 0, best: 0};
        const record = !s.best || time < s.best;
        stats[modeKey()] = {won: s.won + 1, best: record ? time : s.best};
        [523, 659, 784, 1047].forEach((f, k) => blip(f, 0.14, "square", k * 0.11));
        showOverlay(record);
        drawCounters();
        send("result", {game: "solitaire", draw: drawCount(), seconds: time});
    }

    function pause() {
        if (state !== "play" || !started) return;     // nothing to pause before the first move
        stopClock();
        drag = null;
        state = "paused";
        showOverlay();
        draw();
    }

    function resume() {
        if (state !== "paused") return;
        state = "play";
        if (started) { started = false; startClock(); }
        showOverlay();
        draw();
    }

    /* ---------------------------------------------------- display -- */
    const clock = s => {
        const t = Math.round(s);
        return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
    };

    function showOverlay(record) {
        const shown = state !== "play";
        $("sol-overlay").hidden = !shown;
        $("sol-overlay").classList.toggle("covered", state === "paused");
        court.classList.toggle("idle", shown);
        if (state === "paused") {
            $("sol-overlay-title").textContent = "Paused";
            $("sol-overlay-text").textContent = "Click to carry on";
        } else if (state === "over") {
            $("sol-overlay-title").textContent = record ? "New best!" : "You win!";
            $("sol-overlay-text").textContent = `Won in ${clock(elapsed)} with ${g.moves} moves. Click to deal again`;
        }
    }

    function drawCounters() {
        $("sol-moves").textContent = String(g ? g.moves : 0);
        $("sol-time").textContent = clock(seconds());
        $("sol-undo").disabled = !undo.length || state !== "play";
        const s = stats[modeKey()] || {won: 0, best: 0};
        $("sol-stats").replaceChildren(el("span.muted", {text: "Won"}), ` ${s.won}  ·  `,
                                       el("span.muted", {text: "Best"}), ` ${s.best ? clock(s.best) : "–"}`);
    }

    function fit() {
        if (court.offsetParent === null || !g) return;    // its tab isn't showing
        scale = fitCourt(court, canvas, W, H);
        draw();
    }

    /* ------------------------------------------------------ drawing -- */
    function roundRect(x, y, w, h, r) {
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, r);
    }

    function drawCard(c, x, y) {
        roundRect(x, y, CW, CH, 8);
        if (!c.up) {
            ctx.fillStyle = colors.accent;
            ctx.fill();
            ctx.save();
            ctx.clip();
            ctx.strokeStyle = "rgba(255,255,255,0.18)";
            ctx.lineWidth = 2;
            for (let k = -CH; k < CW + CH; k += 12) {
                ctx.beginPath();
                ctx.moveTo(x + k, y);
                ctx.lineTo(x + k - CH, y + CH);
                ctx.stroke();
            }
            ctx.restore();
            roundRect(x + 5, y + 5, CW - 10, CH - 10, 5);
            ctx.strokeStyle = "rgba(255,255,255,0.55)";
            ctx.lineWidth = 1.5;
            ctx.stroke();
            roundRect(x, y, CW, CH, 8);
            ctx.strokeStyle = EDGE;
            ctx.lineWidth = 1;
            ctx.stroke();
            return;
        }
        ctx.fillStyle = FACE;
        ctx.fill();
        ctx.strokeStyle = EDGE;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.fillStyle = red(c) ? INK_RED : INK_BLACK;
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.font = `700 21px ${FONT}`;
        ctx.fillText(RANKS[c.r], x + 8, y + 4);
        ctx.font = `20px ${FONT}`;
        ctx.fillText(SUITS[c.s], x + 9, y + 28);
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.font = c.r > 10 ? `700 40px ${FONT}` : `52px ${FONT}`;
        ctx.fillText(c.r > 10 ? RANKS[c.r] : SUITS[c.s], x + CW / 2 + 6, y + CH / 2 + 10);
        if (c.r > 10) {
            ctx.font = `26px ${FONT}`;
            ctx.fillText(SUITS[c.s], x + CW / 2 + 6, y + CH / 2 + 44);
        }
    }
    const FONT = '"Segoe UI", "Segoe UI Symbol", "Open Sans", sans-serif';

    function drawSlot(x, y, mark) {
        roundRect(x + 1, y + 1, CW - 2, CH - 2, 8);
        ctx.setLineDash([6, 5]);
        ctx.strokeStyle = colors.dim;
        ctx.globalAlpha = 0.5;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.setLineDash([]);
        if (mark) {
            ctx.fillStyle = colors.dim;
            ctx.font = `34px ${FONT}`;
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(mark, x + CW / 2, y + CH / 2);
        }
        ctx.globalAlpha = 1;
    }

    const colX = i => M + i * (CW + GAP);

    /* Where each card of a column goes: face-down cards close together,
       face-up ones further apart - both squeezed so a long column fits. */
    function columnYs(col) {
        const downs = col.filter(c => !c.up).length, ups = col.length - downs;
        const room = H - M - CH - TAB_Y;
        let up = UP_STEP, down = DOWN_STEP;
        const need = down * downs + up * Math.max(0, ups - 1);
        if (need > room) {
            const squeeze = room / need;
            up = Math.max(20, up * squeeze);       // never less than a card's number
            down = Math.max(5, down * squeeze);
        }
        const ys = [];
        let y = TAB_Y;
        col.forEach(c => { ys.push(y); y += c.up ? up : down; });
        return ys;
    }

    function draw() {
        if (!canvas.width || !g) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        hits = [];
        const dragging = drag && drag.moved ? drag : null;
        const hidden = (p, i) => dragging && same(p, dragging.from) && i >= dragging.idx;

        // The deck.
        const stockP = {kind: "stock", i: 0};
        if (g.stock.length) drawCard(last(g.stock), colX(0), TOP);
        else drawSlot(colX(0), TOP, g.waste.length ? "↺" : "");
        hits.push({pile: stockP, idx: g.stock.length - 1, x: colX(0), y: TOP, w: CW, h: CH});

        // The waste: the top one to three cards, fanned.
        const wasteP = {kind: "waste", i: 0};
        const shown = g.waste.slice(-(drawCount() === 3 ? 3 : 1));
        if (!g.waste.length) drawSlot(colX(1), TOP, "");
        shown.forEach((c, k) => {
            const idx = g.waste.length - shown.length + k;
            if (hidden(wasteP, idx)) return;
            const x = colX(1) + k * FAN;
            drawCard(c, x, TOP);
            hits.push({pile: wasteP, idx, x, y: TOP, w: CW, h: CH});
        });

        // The foundations.
        for (let i = 0; i < 4; i++) {
            const p = {kind: "found", i}, f = g.found[i], x = colX(3 + i);
            const under = hidden(p, f.length - 1) ? f[f.length - 2] : last(f);
            if (under) drawCard(under, x, TOP);
            else drawSlot(x, TOP, "A");
            hits.push({pile: p, idx: f.length - 1, x, y: TOP, w: CW, h: CH});
        }

        // The columns.
        for (let i = 0; i < 7; i++) {
            const p = {kind: "tab", i}, col = g.tab[i], x = colX(i), ys = columnYs(col);
            if (!col.length) drawSlot(x, TAB_Y, "K");
            hits.push({pile: p, idx: -1, x, y: TAB_Y, w: CW, h: H - TAB_Y});      // the column's own space
            col.forEach((c, k) => {
                if (hidden(p, k)) return;
                drawCard(c, x, ys[k]);
                hits.push({pile: p, idx: k, x, y: ys[k], w: CW, h: CH});
            });
        }

        // What's being dragged, on top of everything.
        if (dragging) {
            ctx.save();
            ctx.shadowColor = "rgba(0,0,0,0.45)";
            ctx.shadowBlur = 16;
            ctx.shadowOffsetY = 6;
            dragging.cards.forEach((c, k) => drawCard(c, dragging.x - dragging.dx, dragging.y - dragging.dy + k * UP_STEP));
            ctx.restore();
        }
    }

    /* ------------------------------------------------------- input -- */
    function point(e) {
        const r = canvas.getBoundingClientRect();
        return {x: (e.clientX - r.left) / r.width * W, y: (e.clientY - r.top) / r.height * H};
    }

    function hitAt(x, y) {
        for (let k = hits.length - 1; k >= 0; k--) {
            const h = hits[k];
            if (x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h) return h;
        }
        return null;
    }

    /* Where dropped cards go: the column or top-row pile under the pointer
       (a little to either side counts, for a column). */
    function dropTarget(x, y) {
        if (y >= TAB_Y - 10) {
            const i = Math.round((x - M - CW / 2) / (CW + GAP));
            return i >= 0 && i < 7 && Math.abs(x - (colX(i) + CW / 2)) <= CW / 2 + GAP ? {kind: "tab", i} : null;
        }
        for (let i = 0; i < 4; i++) if (x >= colX(3 + i) - GAP / 2 && x <= colX(3 + i) + CW + GAP / 2) return {kind: "found", i};
        return null;
    }

    canvas.addEventListener("pointerdown", e => {
        if (e.button !== 0) return;
        e.preventDefault();
        court.focus({preventScroll: true});
        if (state === "paused") return resume();
        if (state === "over") return newGame();
        if (finishing) return;
        gameSound.prime();
        const {x, y} = point(e);
        const h = hitAt(x, y);
        if (!h) return;
        if (h.pile.kind === "stock") return drawFromStock();
        const cards = movable(h.pile, h.idx);
        if (!cards) return;
        drag = {from: h.pile, idx: h.idx, cards, dx: x - h.x, dy: y - h.y, x, y, sx: x, sy: y, moved: false};
        try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* the drag still works inside the table */ }
    });

    canvas.addEventListener("pointermove", e => {
        if (!drag) return;
        const {x, y} = point(e);
        drag.x = x;
        drag.y = y;
        if (!drag.moved && Math.hypot(x - drag.sx, y - drag.sy) > 6) drag.moved = true;
        if (drag.moved) draw();
    });

    canvas.addEventListener("pointerup", e => {
        if (!drag) return;
        const d = drag;
        drag = null;
        if (!d.moved) {
            smartMove(d.from, d.idx);     // a click: send it where it fits
        } else {
            const to = dropTarget(d.x, d.y);
            if (!to || !move(d.from, d.idx, to)) blip(180, 0.05, "triangle");
        }
        draw();
    });
    canvas.addEventListener("pointercancel", () => { drag = null; draw(); });
    court.addEventListener("mousedown", e => {
        // The overlay sits over the canvas: a click on it resumes or deals again.
        if (e.target === canvas) return;
        e.preventDefault();
        if (state === "paused") resume();
        else if (state === "over") newGame();
    });
    court.addEventListener("contextmenu", e => e.preventDefault());

    document.addEventListener("keydown", e => {
        if (gameTab !== "solitaire") return;
        if (e.target.closest && e.target.closest("input, select, textarea, .modal, .dropdown-pop")) return;
        if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.code === "KeyZ") {
            e.preventDefault();
            takeBack();
        }
    });

    // Out of sight or out of focus: the clock stops and the table is covered.
    addEventListener("blur", pause);
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    onTab(name => { if (name !== "solitaire") pause(); else requestAnimationFrame(fit); });

    $("sol-new").addEventListener("click", newGame);
    $("sol-undo").addEventListener("click", takeBack);
    onPrefs(key => {
        if (key === "sol_draw" || key === null) newGame();
        else drawCounters();
    });

    Buddy.on("state", s => { stats = JSON.parse(JSON.stringify(s.solitaire_stats || {})); drawCounters(); });
    Buddy.on("solitaire_stats", s => { stats = JSON.parse(JSON.stringify(s || {})); drawCounters(); });
    Buddy.on("theme", () => requestAnimationFrame(() => { colors = gameColors(); draw(); }));
    new ResizeObserver(() => fit()).observe(court.parentElement);

    colors = gameColors();
    newGame();
    return {newGame, move, smartMove, drawFromStock, takeBack, pause, resume,
            get state() { return state; }, get game() { return g; }, get finishing() { return !!finishing; },
            get seconds() { return seconds(); },
            _set: game => { g = game; undo = []; state = "play"; showOverlay(); drawCounters(); draw(); }};
})();
