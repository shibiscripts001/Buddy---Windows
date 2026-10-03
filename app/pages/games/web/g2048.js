/* 2048 (loaded after games.js, whose helpers it uses): slide every tile on
   a 4 x 4 board one way; two of the same number meet and merge into their
   sum. Each slide adds a 2 (now and then a 4). Make a 2048 tile - and keep
   going if you like - until no slide is left. Arrow keys or W, A, S and D,
   or drag across the board; one slide can be undone. Turn by turn, so
   there's nothing to pause. */
"use strict";

const game2048 = (() => {
    const N = 4, GAP = 14, TILE = 104;
    const W = N * TILE + (N + 1) * GAP;               // 486 units square
    const SLIDE = 0.11, POP = 0.12;                   // seconds: tiles slide, then merges and new tiles pop
    const KEYS = {ArrowUp: "up", KeyW: "up", ArrowDown: "down", KeyS: "down",
                  ArrowLeft: "left", KeyA: "left", ArrowRight: "right", KeyD: "right"};
    const SWIPE = 24;                                  // CSS pixels a drag must cover to count

    const court = $("g2048-court"), canvas = $("g2048-canvas"), ctx = canvas.getContext("2d");
    let grid = [];            // grid[y][x]: a tile {id, v, x, y} or null
    let score = 0, bestScore = 0, bestTile = 0;
    let best = {};            // {score, tile} (page.py)
    let undo = null;          // {values, score} before the last slide
    let state = "play";       // play, won (2048 made, waiting for "keep going"), over
    let kept = false;         // carried on past 2048
    let anim = null;          // {moves: [{v, fx, fy, tx, ty}], pops: Set(ids), born: Set(ids), t}
    let nextId = 1, running = false, lastFrame = 0;
    let colors = {}, accentRgb = [230, 75, 61], scale = 1;
    let sentScore = 0;        // the score last sent to Python (a game sent once)

    const soundOn = () => prefs.g2048_sound !== false;
    const blip = (freq, length, type, at) => gameSound.blip(soundOn(), freq, length, type, at);

    /* ---------------------------------------------------- the game -- */
    function empty() {
        return Array.from({length: N}, () => Array(N).fill(null));
    }

    function addTile() {
        const free = [];
        for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) if (!grid[y][x]) free.push([x, y]);
        if (!free.length) return null;
        const [x, y] = free[Math.floor(Math.random() * free.length)];
        const tile = {id: nextId++, v: Math.random() < 0.9 ? 2 : 4, x, y};
        grid[y][x] = tile;
        return tile;
    }

    function newGame() {
        report();
        grid = empty();
        score = 0;
        sentScore = 0;
        undo = null;
        kept = false;
        anim = null;
        addTile();
        addTile();
        setState("play");
        drawStats();
    }

    /* The lines a slide works along, each listed from the side the tiles go to. */
    function lineCells(dir) {
        const out = [];
        for (let i = 0; i < N; i++) {
            const line = [];
            for (let j = 0; j < N; j++) {
                if (dir === "left") line.push([j, i]);
                else if (dir === "right") line.push([N - 1 - j, i]);
                else if (dir === "up") line.push([i, j]);
                else line.push([i, N - 1 - j]);
            }
            out.push(line);
        }
        return out;
    }

    function slide(dir) {
        if (anim) {                       // a quick player: the last slide finishes at once
            anim = null;
            settle();
        }
        if (state !== "play") return false;
        const before = {values: grid.map(row => row.map(t => (t ? t.v : 0))), score};
        const next = empty();
        const moves = [], pops = new Set();
        let gained = 0, moved = false;
        for (const line of lineCells(dir)) {
            const tiles = line.map(([x, y]) => grid[y][x]).filter(Boolean);
            let k = 0;
            for (let i = 0; i < tiles.length; i++) {
                const [tx, ty] = line[k];
                const a = tiles[i], b = tiles[i + 1];
                if (b && b.v === a.v) {
                    const merged = {id: nextId++, v: a.v * 2, x: tx, y: ty};
                    next[ty][tx] = merged;
                    pops.add(merged.id);
                    gained += merged.v;
                    moves.push({v: a.v, fx: a.x, fy: a.y, tx, ty}, {v: b.v, fx: b.x, fy: b.y, tx, ty});
                    moved = true;
                    i += 1;
                } else {
                    next[ty][tx] = {...a, x: tx, y: ty};
                    moves.push({v: a.v, fx: a.x, fy: a.y, tx, ty});
                    if (a.x !== tx || a.y !== ty) moved = true;
                }
                k += 1;
            }
        }
        if (!moved) return false;
        undo = before;
        grid = next;
        score += gained;
        const born = addTile();
        anim = {moves, pops, born: new Set(born ? [born.id] : []), t: 0};
        if (gained) {
            const top = Math.max(...[...pops].map(id => findTile(id).v));
            blip(300 + 60 * Math.log2(top), 0.06, "sine");
        } else {
            blip(180, 0.03, "triangle");
        }
        run();
        drawStats();
        return true;
    }

    function findTile(id) {
        for (const row of grid) for (const t of row) if (t && t.id === id) return t;
        return null;
    }

    function biggest() {
        let top = 0;
        for (const row of grid) for (const t of row) if (t) top = Math.max(top, t.v);
        return top;
    }

    function canSlide() {
        for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) {
            const t = grid[y][x];
            if (!t) return true;
            if (x + 1 < N && grid[y][x + 1] && grid[y][x + 1].v === t.v) return true;
            if (y + 1 < N && grid[y + 1][x] && grid[y + 1][x].v === t.v) return true;
        }
        return false;
    }

    /* After the slide has shown: 2048 made, or no slide left. */
    function settle() {
        if (!kept && biggest() >= 2048 && state === "play") {
            blip(784, 0.1, "sine");
            blip(1047, 0.16, "sine", 0.1);
            setState("won");
        } else if (!canSlide()) {
            blip(220, 0.18, "triangle");
            blip(150, 0.3, "triangle", 0.16);
            setState("over");
            report();
        }
    }

    function takeBack() {
        if (!undo) return;
        anim = null;
        grid = undo.values.map((row, y) => row.map((v, x) => (v ? {id: nextId++, v, x, y} : null)));
        score = undo.score;
        undo = null;
        if (state === "over") setState("play");
        blip(260, 0.04, "sine");
        draw();
        drawStats();
    }

    /* The score goes to Python once a game ends or is left for a new one. */
    function report() {
        if (score > 0 && score !== sentScore) {
            sentScore = score;
            send("result", {game: "2048", score, tile: biggest()});
        }
        if (score > (best.score || 0)) best.score = score;
        best.tile = Math.max(best.tile || 0, grid.length ? biggest() : 0);
    }

    /* ----------------------------------------------------- drawing -- */
    function fit() {
        if (court.offsetParent === null) return;
        scale = fitCourt(court, canvas, W, W);
        draw();
    }

    function readAccent() {
        const probe = document.createElement("canvas").getContext("2d");
        probe.fillStyle = colors.accent;
        const hex = probe.fillStyle;            // the browser's own #rrggbb (or rgba())
        const m = hex.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
        accentRgb = m ? [1, 2, 3].map(i => parseInt(m[i], 16)) : (hex.match(/\d+/g) || [230, 75, 61]).slice(0, 3).map(Number);
    }

    /* A tile's fill and text: the small ones plain, the theme's accent
       stronger with each doubling, the biggest glowing. */
    function tileLook(v) {
        const step = Math.log2(v);                        // 1 for a 2, 11 for 2048
        if (step <= 2) return {fill: colors.fg, alpha: step === 1 ? 0.12 : 0.2, text: colors.fg};
        const t = Math.min(1, (step - 2) / 9);
        const alpha = 0.35 + 0.65 * t;
        const [r, g, b] = accentRgb;
        const light = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
        return {fill: `rgb(${r}, ${g}, ${b})`, alpha, text: alpha > 0.6 ? (light > 0.6 ? "#1b1b1b" : "#ffffff") : colors.fg,
                glow: step >= 11};
    }

    function drawTile(v, cx, cy, size) {
        const look = tileLook(v);
        const x = cx - size / 2, y = cy - size / 2;
        if (look.glow) {
            ctx.shadowColor = look.fill;
            ctx.shadowBlur = 18;
        }
        ctx.globalAlpha = look.alpha;
        ctx.fillStyle = look.fill;
        ctx.beginPath();
        ctx.roundRect(x, y, size, size, size * 0.1);
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.globalAlpha = 1;
        const digits = String(v).length;
        const font = size * (digits <= 2 ? 0.46 : digits === 3 ? 0.38 : digits === 4 ? 0.3 : 0.24);
        ctx.fillStyle = look.text;
        ctx.font = `800 ${font}px ${colors.font}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(v), cx, cy + font * 0.04);
    }

    const centre = i => GAP + i * (TILE + GAP) + TILE / 2;

    function draw() {
        if (!canvas.width || !grid.length) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        ctx.fillStyle = colors.dim;
        ctx.globalAlpha = 0.1;
        for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) {
            ctx.beginPath();
            ctx.roundRect(centre(x) - TILE / 2, centre(y) - TILE / 2, TILE, TILE, TILE * 0.1);
            ctx.fill();
        }
        ctx.globalAlpha = 1;
        if (anim && anim.t < SLIDE) {
            // Sliding: every tile on its way from where it was.
            const k = anim.t / SLIDE, e = 1 - (1 - k) * (1 - k);
            for (const m of anim.moves) {
                drawTile(m.v, centre(m.fx + (m.tx - m.fx) * e), centre(m.fy + (m.ty - m.fy) * e), TILE);
            }
            return;
        }
        const popT = anim ? Math.min(1, (anim.t - SLIDE) / POP) : 1;
        for (const row of grid) for (const t of row) {
            if (!t) continue;
            let size = TILE;
            if (anim && anim.pops.has(t.id)) size = TILE * (1 + 0.14 * Math.sin(Math.PI * popT));
            else if (anim && anim.born.has(t.id)) size = TILE * (0.2 + 0.8 * popT);
            drawTile(t.v, centre(t.x), centre(t.y), size);
        }
    }

    function frame(now) {
        if (!running) return;
        const dt = Math.min(0.05, (now - lastFrame) / 1000);
        lastFrame = now;
        if (anim) {
            anim.t += dt;
            if (anim.t >= SLIDE + POP) {
                anim = null;
                draw();
                settle();
                running = false;
                return;
            }
        }
        draw();
        if (anim) requestAnimationFrame(frame);
        else running = false;
    }

    function run() {
        if (running) return;
        running = true;
        lastFrame = performance.now();
        requestAnimationFrame(frame);
    }

    /* ------------------------------------------------------ states -- */
    function setState(next) {
        state = next;
        const shown = next !== "play";
        $("g2048-overlay").hidden = !shown;
        court.classList.toggle("idle", shown);
        if (next === "won") {
            $("g2048-overlay-title").textContent = "You made 2048!";
            $("g2048-overlay-text").textContent = "Press Enter or click to keep going";
        } else if (next === "over") {
            $("g2048-overlay-title").textContent = score >= (best.score || 0) && score > 0 ? "New best!" : "No moves left";
            $("g2048-overlay-text").textContent = `You scored ${score}. Press Enter or click to play again`;
        }
        $("g2048-undo").disabled = !undo;
        draw();
    }

    function onward() {
        gameSound.prime();
        court.focus({preventScroll: true});
        if (state === "won") {
            kept = true;
            setState("play");
        } else if (state === "over") {
            newGame();
        }
    }

    /* ------------------------------------------------------- input -- */
    document.addEventListener("keydown", e => {
        if (gameTab !== "2048" || e.altKey) return;
        if (e.target.closest && e.target.closest("input, select, textarea, button, .modal, .dropdown-pop")) return;
        if (e.ctrlKey || e.metaKey) {     // Ctrl+Z, or ⌘Z on a Mac
            if (e.code === "KeyZ" && !e.shiftKey) { e.preventDefault(); takeBack(); }
            return;
        }
        const dir = KEYS[e.code];
        if (dir) {
            e.preventDefault();
            gameSound.prime();
            if (state === "play") slide(dir);
        } else if (e.code === "Enter" || e.code === "Space" || e.code === "NumpadEnter") {
            e.preventDefault();
            if (!e.repeat) onward();
        } else if (e.code === "KeyU" || e.code === "Backspace") {
            e.preventDefault();
            takeBack();
        }
    });

    let press = null;
    court.addEventListener("pointerdown", e => {
        if (e.button !== 0) return;
        e.preventDefault();
        court.focus({preventScroll: true});
        gameSound.prime();
        press = {x: e.clientX, y: e.clientY, id: e.pointerId};
        court.setPointerCapture(e.pointerId);
    });
    court.addEventListener("pointerup", e => {
        if (!press || e.pointerId !== press.id) return;
        const dx = e.clientX - press.x, dy = e.clientY - press.y;
        press = null;
        if (state !== "play") { onward(); return; }
        if (Math.max(Math.abs(dx), Math.abs(dy)) < SWIPE) return;
        slide(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up"));
    });
    court.addEventListener("pointercancel", () => { press = null; });

    $("g2048-new").addEventListener("click", newGame);
    $("g2048-undo").addEventListener("click", takeBack);
    onTab(name => { if (name === "2048") requestAnimationFrame(fit); else report(); });

    function drawStats() {
        $("g2048-score").textContent = String(score);
        $("g2048-undo").disabled = !undo;
        $("g2048-best").replaceChildren(
            el("span.muted", {text: "Best"}), ` ${Math.max(best.score || 0, score)}`,
            ...(best.tile ? ["  ·  ", el("span.muted", {text: "Biggest tile"}), ` ${Math.max(best.tile, biggest())}`] : []),
        );
    }

    Buddy.on("state", s => { best = {...(s.best_2048 || {})}; drawStats(); });
    Buddy.on("best_2048", b => { best = {...b}; drawStats(); });
    Buddy.on("theme", () => requestAnimationFrame(() => { colors = gameColors(); readAccent(); draw(); }));
    new ResizeObserver(() => fit()).observe(court.parentElement);
    addEventListener("beforeunload", report);

    colors = gameColors();
    readAccent();
    newGame();
    return {slide, takeBack, onward, get state() { return state; }, get score() { return score; },
            values: () => grid.map(row => row.map(t => (t ? t.v : 0))),
            _set: (values, sc = 0) => {
                grid = values.map((row, y) => row.map((v, x) => (v ? {id: nextId++, v, x, y} : null)));
                score = sc; undo = null; anim = null; kept = false; setState("play"); drawStats();
            },
            _finish: () => { if (anim) { anim.t = SLIDE + POP; } }};
})();
