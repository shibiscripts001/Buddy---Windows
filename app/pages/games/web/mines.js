/* Minesweeper (loaded after games.js, whose helpers it uses): dig every
   square that isn't a mine. A number says how many of the eight squares
   round it are mines; right-click (or Shift+click) flags one; clicking a
   number whose flags are all placed digs the rest round it. The first dig
   is always safe and opens an area. Like the other games it pauses - the
   clock stopped, the board covered - when it can't be seen or Buddy loses
   the focus. */
"use strict";

const mines = (() => {
    const LEVELS = {
        beginner: {cols: 9, rows: 9, mines: 10},
        intermediate: {cols: 16, rows: 16, mines: 40},
        expert: {cols: 30, rows: 16, mines: 99},
    };
    const CELL = 32;                                 // court units per square
    // One colour per number, readable on a light or a dark theme.
    const NUMBER_COLORS = [null, "#3d8bfd", "#2fa84f", "#e5484d", "#8e6cef", "#d9822b", "#12a594", null, null];

    const court = $("mines-court"), canvas = $("mines-canvas"), ctx = canvas.getContext("2d");
    let size = LEVELS.beginner;
    let cells = [];          // {mine, n (mines round it), open, flag}
    let state = "ready";     // ready (nothing dug), play, paused, over
    let won = false;
    let blown = -1;          // the square that went off
    let hover = -1;
    let elapsed = 0, since = 0;    // seconds banked, and when the running clock last started
    let best = {};           // level -> best time, seconds (page.py)
    let colors = {};
    let scale = 1;
    let ticker = 0;

    const levelName = () => (LEVELS[prefs.mines_level] ? prefs.mines_level : "beginner");
    const soundOn = () => prefs.mines_sound !== false;
    const blip = (freq, length, type, at) => gameSound.blip(soundOn(), freq, length, type, at);
    const seconds = () => elapsed + (state === "play" ? (performance.now() - since) / 1000 : 0);
    const flagsPlaced = () => cells.reduce((n, c) => n + (c.flag ? 1 : 0), 0);

    function around(i) {
        const x = i % size.cols, y = Math.floor(i / size.cols), out = [];
        for (let dy = -1; dy <= 1; dy++) {
            for (let dx = -1; dx <= 1; dx++) {
                const nx = x + dx, ny = y + dy;
                if ((dx || dy) && nx >= 0 && ny >= 0 && nx < size.cols && ny < size.rows) out.push(ny * size.cols + nx);
            }
        }
        return out;
    }

    /* --------------------------------------------------- the game -- */
    function newGame() {
        size = LEVELS[levelName()];
        cells = Array.from({length: size.cols * size.rows}, () => ({mine: false, n: 0, open: false, flag: false}));
        state = "ready";
        won = false;
        blown = -1;
        elapsed = 0;
        showOverlay();
        fit();
        drawCounters();
    }

    /* Mines go in once the first square is dug - never in it or round it,
       so the first dig always opens an area. */
    function layMines(first) {
        const keep = new Set([first, ...around(first)]);
        const spots = [];
        for (let i = 0; i < cells.length; i++) if (!keep.has(i)) spots.push(i);
        for (let k = 0; k < size.mines; k++) {
            const j = k + Math.floor(Math.random() * (spots.length - k));
            [spots[k], spots[j]] = [spots[j], spots[k]];
            cells[spots[k]].mine = true;
        }
        cells.forEach((c, i) => { c.n = around(i).filter(j => cells[j].mine).length; });
    }

    function start() {
        state = "play";
        since = performance.now();
        clearInterval(ticker);
        ticker = setInterval(drawCounters, 250);
    }

    function dig(i) {
        if (state === "over" || state === "paused") return;
        const c = cells[i];
        if (c.flag) return;
        if (state === "ready") {
            gameSound.prime();
            layMines(i);
            start();
        }
        if (c.open) return chord(i);
        if (c.mine) return lose(i);
        // A square with no mines round it opens its neighbours too, and so on.
        const queue = [i];
        let opened = 0;
        while (queue.length) {
            const j = queue.pop();
            const cell = cells[j];
            if (cell.open || cell.flag) continue;
            cell.open = true;
            opened++;
            if (cell.n === 0) for (const k of around(j)) if (!cells[k].open && !cells[k].mine) queue.push(k);
        }
        if (opened) blip(opened > 1 ? 520 : 440, 0.04);
        checkWin();
        draw();
    }

    /* A number whose flags are all placed: dig every other square round it. */
    function chord(i) {
        const c = cells[i];
        if (!c.open || !c.n) return;
        const near = around(i);
        if (near.filter(j => cells[j].flag).length !== c.n) return;
        for (const j of near) {
            if (!cells[j].open && !cells[j].flag) {
                if (cells[j].mine) return lose(j);
                dig(j);
            }
        }
    }

    function flag(i) {
        if (state !== "play" && state !== "ready") return;
        const c = cells[i];
        if (c.open) return;
        c.flag = !c.flag;
        blip(c.flag ? 660 : 330, 0.04);
        drawCounters();
        draw();
    }

    function stopClock() {
        elapsed = seconds();
        clearInterval(ticker);
    }

    function lose(i) {
        stopClock();
        blown = i;
        state = "over";
        won = false;
        blip(160, 0.25, "sawtooth");
        blip(90, 0.4, "triangle", 0.12);
        showOverlay();
        drawCounters();
        draw();
    }

    function checkWin() {
        const left = cells.reduce((n, c) => n + (!c.open && !c.mine ? 1 : 0), 0);
        if (left) return;
        stopClock();
        for (const c of cells) if (c.mine) c.flag = true;
        state = "over";
        won = true;
        const time = Math.round(elapsed * 10) / 10;
        const record = !best[levelName()] || time < best[levelName()];
        if (record) best[levelName()] = time;
        [523, 659, 784].forEach((f, k) => blip(f, 0.12, "square", k * 0.1));
        showOverlay(record);
        drawCounters();
        send("result", {game: "mines", level: levelName(), seconds: time});
    }

    function pause() {
        if (state !== "play") return;
        stopClock();
        state = "paused";
        showOverlay();
        draw();
    }

    function resume() {
        if (state === "paused") {
            start();
            showOverlay();
            draw();
        }
    }

    /* ------------------------------------------------------ overlay -- */
    function showOverlay(record) {
        const shown = state === "paused" || state === "over";
        $("mines-overlay").hidden = !shown;
        $("mines-overlay").classList.toggle("covered", state === "paused");   // no studying the board while the clock's stopped
        court.classList.toggle("idle", shown);
        if (state === "paused") {
            $("mines-overlay-title").textContent = "Paused";
            $("mines-overlay-text").textContent = "Click to carry on";
        } else if (state === "over") {
            $("mines-overlay-title").textContent = won ? (record ? "New best!" : "You win!") : "Boom!";
            $("mines-overlay-text").textContent = won ? `Cleared in ${clock(elapsed)}. Click to play again`
                                                     : "Click to play again";
        }
    }

    const clock = s => `${(Math.round(s * 10) / 10).toFixed(1)} s`;     // as the best is kept: to the nearest tenth

    function drawCounters() {
        $("mines-left").textContent = String(size.mines - flagsPlaced());
        $("mines-time").textContent = String(Math.floor(seconds()));
        const b = best[levelName()];
        $("mines-best").replaceChildren(el("span.muted", {text: "Best"}), ` ${b ? clock(b) : "–"}`);
    }

    /* ----------------------------------------------------- drawing -- */
    function fit() {
        if (court.offsetParent === null || !cells.length) return;    // its tab isn't showing
        scale = fitCourt(court, canvas, size.cols * CELL, size.rows * CELL);
        draw();
    }

    function rounded(x, y, w, h, r) {
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, r);
        ctx.fill();
    }

    function drawMine(cx, cy) {
        ctx.fillStyle = colors.fg;
        ctx.strokeStyle = colors.fg;
        ctx.lineWidth = 2.2;
        ctx.beginPath();
        for (let k = 0; k < 4; k++) {
            const a = k * Math.PI / 4;
            ctx.moveTo(cx - Math.cos(a) * 11, cy - Math.sin(a) * 11);
            ctx.lineTo(cx + Math.cos(a) * 11, cy + Math.sin(a) * 11);
        }
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(cx, cy, 7.5, 0, Math.PI * 2);
        ctx.fill();
    }

    function drawFlag(cx, cy) {
        ctx.fillStyle = colors.fg;
        ctx.fillRect(cx - 1, cy - 9, 2.2, 18);
        ctx.fillRect(cx - 6, cy + 7, 12, 2.5);
        ctx.fillStyle = colors.accent;
        ctx.beginPath();
        ctx.moveTo(cx + 1, cy - 9);
        ctx.lineTo(cx + 10, cy - 4.5);
        ctx.lineTo(cx + 1, cy);
        ctx.closePath();
        ctx.fill();
    }

    function draw() {
        if (!canvas.width || !cells.length) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        const showAll = state === "over";
        ctx.font = `700 19px ${colors.font}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        cells.forEach((c, i) => {
            const x = (i % size.cols) * CELL, y = Math.floor(i / size.cols) * CELL;
            const cx = x + CELL / 2, cy = y + CELL / 2;
            if (c.open || (showAll && c.mine && !c.flag)) {
                // Dug: flat, a shade darker than the board.
                ctx.fillStyle = i === blown ? colors.accent : colors.dim;
                ctx.globalAlpha = i === blown ? 0.9 : 0.08;
                ctx.fillRect(x + 0.5, y + 0.5, CELL - 1, CELL - 1);
                ctx.globalAlpha = 1;
                if (c.mine) drawMine(cx, cy);
                else if (c.n) {
                    ctx.fillStyle = NUMBER_COLORS[c.n] || (c.n === 7 ? colors.fg : colors.dim);
                    ctx.fillText(String(c.n), cx, cy + 1);
                }
            } else {
                // Not dug yet: a raised tile, lighter under the pointer.
                ctx.fillStyle = colors.fg;
                ctx.globalAlpha = i === hover && state !== "over" ? 0.26 : 0.16;
                rounded(x + 1.5, y + 1.5, CELL - 3, CELL - 3, 4);
                ctx.globalAlpha = 1;
                if (c.flag) {
                    drawFlag(cx, cy);
                    if (showAll && !won && !c.mine) {    // a wrong flag: crossed out
                        ctx.strokeStyle = colors.accent;
                        ctx.lineWidth = 2.5;
                        ctx.beginPath();
                        ctx.moveTo(x + 7, y + 7); ctx.lineTo(x + CELL - 7, y + CELL - 7);
                        ctx.moveTo(x + CELL - 7, y + 7); ctx.lineTo(x + 7, y + CELL - 7);
                        ctx.stroke();
                    }
                }
            }
        });
    }

    /* ------------------------------------------------------- input -- */
    function cellAt(e) {
        const r = canvas.getBoundingClientRect();
        const x = Math.floor((e.clientX - r.left) / r.width * size.cols);
        const y = Math.floor((e.clientY - r.top) / r.height * size.rows);
        return x >= 0 && y >= 0 && x < size.cols && y < size.rows ? y * size.cols + x : -1;
    }

    court.addEventListener("contextmenu", e => e.preventDefault());
    court.addEventListener("mousedown", e => {
        e.preventDefault();
        court.focus({preventScroll: true});
        if (state === "paused") return resume();
        if (state === "over") return newGame();
        const i = cellAt(e);
        if (i < 0) return;
        if (e.button === 2 || (e.button === 0 && e.shiftKey)) flag(i);
        else if (e.button === 0) dig(i);
        else if (e.button === 1) chord(i);
    });
    court.addEventListener("mousemove", e => {
        const i = cellAt(e);
        if (i !== hover) { hover = i; draw(); }
    });
    court.addEventListener("mouseleave", () => { hover = -1; draw(); });

    // Out of sight or out of focus: the clock stops and the board is covered.
    addEventListener("blur", pause);
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    onTab(name => { if (name !== "mines") pause(); else requestAnimationFrame(fit); });

    $("mines-new").addEventListener("click", newGame);
    onPrefs(key => {
        if (key === "mines_level" || key === null) newGame();
        else drawCounters();
    });

    Buddy.on("state", s => { best = {...(s.mines_best || {})}; drawCounters(); });
    Buddy.on("mines_best", b => { best = {...b}; drawCounters(); });
    Buddy.on("theme", () => requestAnimationFrame(() => { colors = gameColors(); draw(); }));
    new ResizeObserver(() => fit()).observe(court.parentElement);

    colors = gameColors();
    newGame();
    return {dig, flag, chord, pause, resume, newGame, get state() { return state; }, get won() { return won; },
            get cells() { return cells; }, get size() { return size; }, get seconds() { return seconds(); },
            _lay: list => {   // tests: these squares are the mines, and the game has started
                cells.forEach(c => { c.mine = false; });
                for (const i of list) cells[i].mine = true;
                cells.forEach((c, i) => { c.n = around(i).filter(j => cells[j].mine).length; });
                start();
            }};
})();
