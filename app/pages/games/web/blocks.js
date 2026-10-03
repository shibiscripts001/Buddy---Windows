/* Falling blocks (loaded after games.js, whose helpers it uses): pieces of
   four squares fall into a 10 x 20 well; a full row clears. The modern
   rules: pieces come in shuffled bags of all seven, turn by the standard
   rotation system (with its wall kicks), and wait half a second on landing
   before they lock - moving or turning them restarts that, 15 times at
   most. Hold one piece back, see the next three, and a ghost shows where
   the piece will land. Every 10 rows is a level, and each level falls
   faster. Pauses itself when it can't be seen or Buddy loses the focus. */
"use strict";

const blocks = (() => {
    const COLS = 10, ROWS = 20, HIDDEN = 2;          // two rows above the well, where pieces appear
    const CELL = 30, BX = 150, BY = 20;              // the well on the court
    const W = 600, H = 640;
    const DAS = 0.167, ARR = 0.033;                  // held sideways: first repeat, then every...
    const LOCK_DELAY = 0.5, LOCK_RESETS = 15;
    const CLEAR_TIME = 0.2;                          // full rows flash this long before they go
    const LINE_POINTS = [0, 100, 300, 500, 800];
    const MAX_LEVEL = 20;

    // Each piece's squares in its spawn turn, in its box (3 x 3; I and O 4 wide).
    const SHAPES = {
        I: [[0, 1], [1, 1], [2, 1], [3, 1]], O: [[1, 0], [2, 0], [1, 1], [2, 1]],
        T: [[1, 0], [0, 1], [1, 1], [2, 1]], S: [[1, 0], [2, 0], [0, 1], [1, 1]],
        Z: [[0, 0], [1, 0], [1, 1], [2, 1]], J: [[0, 0], [0, 1], [1, 1], [2, 1]],
        L: [[2, 0], [0, 1], [1, 1], [2, 1]],
    };
    const COLORS = {I: "#3cc6e6", O: "#f2c94c", T: "#a66cf0", S: "#5ccc6b", Z: "#ee5a5a", J: "#4a7cf0", L: "#f2994a"};
    const TYPES = Object.keys(SHAPES);
    // Wall kicks (SRS), tried in order; y here is down the screen.
    const KICKS = {
        "0>1": [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]], "1>0": [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]],
        "1>2": [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]], "2>1": [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]],
        "2>3": [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]], "3>2": [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]],
        "3>0": [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]], "0>3": [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]],
    };
    const KICKS_I = {
        "0>1": [[0, 0], [-2, 0], [1, 0], [-2, 1], [1, -2]], "1>0": [[0, 0], [2, 0], [-1, 0], [2, -1], [-1, 2]],
        "1>2": [[0, 0], [-1, 0], [2, 0], [-1, -2], [2, 1]], "2>1": [[0, 0], [1, 0], [-2, 0], [1, 2], [-2, -1]],
        "2>3": [[0, 0], [2, 0], [-1, 0], [2, -1], [-1, 2]], "3>2": [[0, 0], [-2, 0], [1, 0], [-2, 1], [1, -2]],
        "3>0": [[0, 0], [1, 0], [-2, 0], [1, 2], [-2, -1]], "0>3": [[0, 0], [-1, 0], [2, 0], [-1, -2], [2, 1]],
    };
    const KEYS = {ArrowLeft: "left", KeyA: "left", ArrowRight: "right", KeyD: "right", ArrowDown: "soft", KeyS: "soft",
                  ArrowUp: "cw", KeyX: "cw", KeyW: "cw", KeyZ: "ccw", KeyQ: "ccw", Space: "hard",
                  KeyC: "hold", ShiftLeft: "hold", ShiftRight: "hold"};

    const court = $("blocks-court"), canvas = $("blocks-canvas"), ctx = canvas.getContext("2d");
    let board = [];           // ROWS + HIDDEN rows of COLS: a type letter, or ""
    let piece = null;         // {type, rot, x, y}
    let queue = [], hold = null, canHold = true;
    let score = 0, lines = 0, level = 1, startLevel = 1, backToBack = false;
    let best = {};            // {score, lines, level} (page.py)
    let state = "ready";      // ready, play, paused, over
    let fallCarry = 0, lockTimer = null, lockResets = 0, lowestY = 0;
    let held = null;          // {dir, time, carry} while left or right is held
    let softDrop = false;
    let clearing = null;      // {rows, left} while full rows flash
    let running = false, lastFrame = 0;
    let colors = {}, scale = 1;

    const soundOn = () => prefs.blocks_sound !== false;
    const ghostOn = () => prefs.blocks_ghost !== false;
    const blip = (freq, length, type, at) => gameSound.blip(soundOn(), freq, length, type, at);
    const fallTime = () => Math.pow(0.8 - (level - 1) * 0.007, level - 1);     // seconds a row, the guideline's curve

    /* ------------------------------------------------------ pieces -- */
    function cells(p, rot = p.rot, x = p.x, y = p.y) {
        const n = p.type === "I" || p.type === "O" ? 4 : 3;
        return SHAPES[p.type].map(([cx, cy]) => {
            if (p.type !== "O") for (let i = 0; i < rot; i++) [cx, cy] = [n - 1 - cy, cx];   // a quarter turn clockwise
            return [x + cx, y + cy];
        });
    }

    function fits(p, rot = p.rot, x = p.x, y = p.y) {
        return cells(p, rot, x, y).every(([cx, cy]) =>
            cx >= 0 && cx < COLS && cy < ROWS + HIDDEN && (cy < 0 || !board[cy][cx]));
    }

    function nextType() {
        if (queue.length < 7) {
            const bag = [...TYPES];
            for (let i = bag.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [bag[i], bag[j]] = [bag[j], bag[i]];
            }
            queue.push(...bag);
        }
        return queue.shift();
    }

    function spawn(type) {
        piece = {type, rot: 0, x: 3, y: 0};
        lockTimer = null;
        lockResets = 0;
        lowestY = piece.y;
        fallCarry = 0;
        if (!fits(piece)) return end();
        // Appearing above the well, it drops straight into view if it can.
        if (fits(piece, piece.rot, piece.x, piece.y + 1)) piece.y += 1;
        lowestY = piece.y;
    }

    function grounded() {
        return !fits(piece, piece.rot, piece.x, piece.y + 1);
    }

    /* A successful move or turn on the ground restarts the lock delay (15 times). */
    function moved() {
        if (piece.y > lowestY) {
            lowestY = piece.y;
            lockResets = 0;
            lockTimer = null;
        } else if (lockTimer !== null && lockResets < LOCK_RESETS) {
            lockResets += 1;
            lockTimer = 0;
        }
        if (!grounded()) lockTimer = null;
    }

    function shift(dx) {
        if (!piece || !fits(piece, piece.rot, piece.x + dx, piece.y)) return false;
        piece.x += dx;
        moved();
        return true;
    }

    function rotate(dir) {
        if (!piece || piece.type === "O") return false;
        const to = (piece.rot + dir + 4) % 4;
        const kicks = (piece.type === "I" ? KICKS_I : KICKS)[`${piece.rot}>${to}`];
        for (const [kx, ky] of kicks) {
            if (fits(piece, to, piece.x + kx, piece.y + ky)) {
                piece.rot = to;
                piece.x += kx;
                piece.y += ky;
                blip(520, 0.03);
                moved();
                return true;
            }
        }
        return false;
    }

    function dropOne(points) {
        if (!fits(piece, piece.rot, piece.x, piece.y + 1)) return false;
        piece.y += 1;
        score += points;
        moved();
        return true;
    }

    function hardDrop() {
        let n = 0;
        while (fits(piece, piece.rot, piece.x, piece.y + 1)) { piece.y += 1; n += 1; }
        score += n * 2;
        blip(110, 0.08, "triangle");
        lock();
    }

    function holdPiece() {
        if (!canHold || !piece) return;
        const type = piece.type;
        canHold = false;
        blip(400, 0.04, "sine");
        if (hold) spawn(hold);
        else spawn(nextType());
        hold = type;
    }

    function lock() {
        const placed = cells(piece);
        for (const [cx, cy] of placed) if (cy >= 0) board[cy][cx] = piece.type;
        // Locked wholly above the well: it's full.
        if (placed.every(([, cy]) => cy < HIDDEN)) { piece = null; return end(); }
        piece = null;
        canHold = true;
        const full = [];
        for (let y = 0; y < ROWS + HIDDEN; y++) if (board[y].every(c => c)) full.push(y);
        if (full.length) {
            clearing = {rows: full, left: CLEAR_TIME};
            const notes = [523, 659, 784, 1047];
            for (let i = 0; i < full.length; i++) blip(notes[i], 0.09, "square", i * 0.06);
        } else {
            blip(140, 0.05, "triangle");
            spawn(nextType());
        }
        drawStats();
    }

    function finishClear() {
        const rows = clearing.rows;
        clearing = null;
        board = board.filter((_, y) => !rows.includes(y));
        while (board.length < ROWS + HIDDEN) board.unshift(Array(COLS).fill(""));
        const n = rows.length;
        const tetra = n === 4;
        score += Math.round(LINE_POINTS[n] * level * (tetra && backToBack ? 1.5 : 1));
        backToBack = tetra;
        lines += n;
        const was = level;
        level = Math.min(MAX_LEVEL, Math.max(startLevel, startLevel + Math.floor(lines / 10)));
        if (level > was) { blip(784, 0.08, "sine"); blip(1175, 0.12, "sine", 0.08); }
        drawStats();
        spawn(nextType());
    }

    function newGame() {
        board = Array.from({length: ROWS + HIDDEN}, () => Array(COLS).fill(""));
        queue = [];
        hold = null;
        canHold = true;
        score = 0;
        lines = 0;
        startLevel = [1, 5, 10, 15].includes(Number(prefs.blocks_level)) ? Number(prefs.blocks_level) : 1;
        level = startLevel;
        backToBack = false;
        clearing = null;
        held = null;
        softDrop = false;
        spawn(nextType());
        drawStats();
    }

    function end() {
        const record = score > (best.score || 0);
        if (record) best = {score, lines, level};
        blip(220, 0.18, "triangle");
        blip(150, 0.3, "triangle", 0.16);
        setState("over", {record});
        if (score > 0) send("result", {game: "blocks", score, lines, level});
        drawStats();
    }

    /* -------------------------------------------------------- time -- */
    function step(dt) {
        if (clearing) {
            clearing.left -= dt;
            if (clearing.left <= 0) finishClear();
            return;
        }
        if (!piece) return;
        // Held sideways: once at once (on the key press), then again after DAS, then every ARR.
        if (held) {
            held.time += dt;
            if (held.time >= DAS) {
                held.carry += dt;
                while (held.carry >= ARR) {
                    held.carry -= ARR;
                    if (!shift(held.dir)) { held.carry = 0; break; }
                }
            }
        }
        const interval = softDrop ? Math.min(fallTime(), 0.035) : fallTime();
        fallCarry += dt;
        while (fallCarry >= interval && piece) {
            fallCarry -= interval;
            if (!dropOne(softDrop ? 1 : 0)) { fallCarry = 0; break; }
        }
        if (piece && grounded()) {
            lockTimer = (lockTimer === null ? 0 : lockTimer) + dt;
            if (lockTimer >= LOCK_DELAY) lock();
        }
        if (softDrop) drawStats();
    }

    function frame(now) {
        if (!running) return;
        const dt = Math.min(0.05, (now - lastFrame) / 1000);
        lastFrame = now;
        if (state === "play") step(dt);
        draw();
        if (state === "play") requestAnimationFrame(frame);
        else running = false;
    }

    function run() {
        if (running) return;
        running = true;
        lastFrame = performance.now();
        requestAnimationFrame(frame);
    }

    /* ----------------------------------------------------- drawing -- */
    function fit() {
        if (court.offsetParent === null) return;
        scale = fitCourt(court, canvas, W, H);
        draw();
    }

    function square(x, y, size, color, alpha = 1) {
        ctx.globalAlpha = alpha;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.roundRect(x + 1, y + 1, size - 2, size - 2, Math.max(2, size * 0.14));
        ctx.fill();
        // A lit top edge and a shaded bottom one.
        ctx.globalAlpha = alpha * 0.28;
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(x + 3, y + 2, size - 6, Math.max(2, size * 0.12));
        ctx.globalAlpha = alpha * 0.22;
        ctx.fillStyle = "#000000";
        ctx.fillRect(x + 3, y + size - 2 - Math.max(2, size * 0.12), size - 6, Math.max(2, size * 0.12));
        ctx.globalAlpha = 1;
    }

    function preview(type, x, y, w, h, dim) {
        if (!type) return;
        const size = 22;
        const shape = SHAPES[type];
        const xs = shape.map(c => c[0]), ys = shape.map(c => c[1]);
        const pw = (Math.max(...xs) - Math.min(...xs) + 1) * size, ph = (Math.max(...ys) - Math.min(...ys) + 1) * size;
        const ox = x + (w - pw) / 2 - Math.min(...xs) * size, oy = y + (h - ph) / 2 - Math.min(...ys) * size;
        for (const [cx, cy] of shape) square(ox + cx * size, oy + cy * size, size, COLORS[type], dim ? 0.35 : 1);
    }

    function panel(label, x, y, w, h) {
        ctx.fillStyle = colors.dim;
        ctx.globalAlpha = 0.1;
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, 10);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.fillStyle = colors.dim;
        ctx.font = `600 15px ${colors.font}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(Buddy.t(label), x + w / 2, y + 10);
    }

    function draw() {
        if (!canvas.width || !board.length) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        // The well: a faint grid.
        ctx.fillStyle = colors.dim;
        ctx.globalAlpha = 0.07;
        ctx.fillRect(BX, BY, COLS * CELL, ROWS * CELL);
        ctx.globalAlpha = 0.1;
        for (let x = 1; x < COLS; x++) ctx.fillRect(BX + x * CELL, BY, 1, ROWS * CELL);
        for (let y = 1; y < ROWS; y++) ctx.fillRect(BX, BY + y * CELL, COLS * CELL, 1);
        ctx.globalAlpha = 1;
        const at = (cx, cy) => [BX + cx * CELL, BY + (cy - HIDDEN) * CELL];
        for (let y = HIDDEN; y < ROWS + HIDDEN; y++) {
            const flashing = clearing && clearing.rows.includes(y);
            for (let x = 0; x < COLS; x++) {
                if (!board[y][x]) continue;
                const [px, py] = at(x, y);
                if (flashing) {
                    const t = clearing.left / CLEAR_TIME;
                    square(px, py, CELL, "#ffffff", 0.35 + 0.65 * t);
                } else {
                    square(px, py, CELL, COLORS[board[y][x]]);
                }
            }
        }
        if (piece) {
            if (ghostOn()) {
                let gy = piece.y;
                while (fits(piece, piece.rot, piece.x, gy + 1)) gy += 1;
                if (gy !== piece.y) {
                    ctx.strokeStyle = COLORS[piece.type];
                    ctx.globalAlpha = 0.55;
                    ctx.lineWidth = 2;
                    for (const [cx, cy] of cells(piece, piece.rot, piece.x, gy)) {
                        if (cy < HIDDEN) continue;
                        const [px, py] = at(cx, cy);
                        ctx.beginPath();
                        ctx.roundRect(px + 3, py + 3, CELL - 6, CELL - 6, 4);
                        ctx.stroke();
                    }
                    ctx.globalAlpha = 1;
                }
            }
            for (const [cx, cy] of cells(piece)) {
                if (cy < HIDDEN) continue;
                const [px, py] = at(cx, cy);
                // Fading while it waits to lock, so the delay can be seen.
                const fade = lockTimer === null ? 1 : 1 - 0.35 * Math.min(1, lockTimer / LOCK_DELAY);
                square(px, py, CELL, COLORS[piece.type], fade);
            }
        }
        // The well's edge.
        ctx.strokeStyle = colors.dim;
        ctx.globalAlpha = 0.35;
        ctx.lineWidth = 2;
        ctx.strokeRect(BX - 1, BY - 1, COLS * CELL + 2, ROWS * CELL + 2);
        ctx.globalAlpha = 1;
        // Hold on the left, the next three on the right.
        panel("Hold", 20, BY, 110, 110);
        preview(hold, 20, BY + 30, 110, 74, !canHold);
        panel("Next", W - 130, BY, 110, 290);
        queue.slice(0, 3).forEach((type, i) => preview(type, W - 130, BY + 32 + i * 84, 110, 80, false));
    }

    /* ------------------------------------------------------ states -- */
    function setState(next, how = {}) {
        state = next;
        const shown = next !== "play";
        $("blocks-overlay").hidden = !shown;
        // Paused, the well is covered: no planning the next move with the clock stopped.
        $("blocks-overlay").classList.toggle("covered", next === "paused");
        court.classList.toggle("idle", shown);
        if (next === "ready") {
            $("blocks-overlay-title").textContent = "Falling blocks";
            $("blocks-overlay-text").textContent = "Press Enter or click to start";
        } else if (next === "paused") {
            $("blocks-overlay-title").textContent = "Paused";
            $("blocks-overlay-text").textContent = "Press Enter or click to carry on";
        } else if (next === "over") {
            $("blocks-overlay-title").textContent = how.record ? "New best!" : "Game over";
            $("blocks-overlay-text").textContent = `You scored ${score} with ${lines} lines. Press Enter or click to play again`;
        }
        if (next !== "play") { held = null; softDrop = false; }
        if (next === "play") run();
        draw();
    }

    function pause() {
        if (state === "play") setState("paused");
    }

    function go() {
        gameSound.prime();
        court.focus({preventScroll: true});
        if (state === "over") newGame();
        if (state !== "play") setState("play");
    }

    function reset() {
        if (state === "play" || state === "paused") {
            if (score > 0) send("result", {game: "blocks", score, lines, level});
        }
        newGame();
        setState("ready");
    }

    /* ------------------------------------------------------- input -- */
    document.addEventListener("keydown", e => {
        if (gameTab !== "blocks" || e.ctrlKey || e.altKey || e.metaKey) return;
        if (e.target.closest && e.target.closest("input, select, textarea, button, .modal, .dropdown-pop")) return;
        const action = KEYS[e.code];
        if (e.code === "Escape" || e.code === "KeyP") {
            e.preventDefault();
            if (state === "play") pause();
            else if (state === "paused" && e.code === "KeyP") go();
            return;
        }
        if (e.code === "Enter" || e.code === "NumpadEnter") {
            e.preventDefault();
            if (!e.repeat && state !== "play") go();
            return;
        }
        if (!action) return;
        e.preventDefault();
        if (state !== "play") {
            if (e.code === "Space" && !e.repeat) go();
            return;
        }
        if (clearing || !piece) {
            if (action === "soft") softDrop = true;
            return;
        }
        if (action === "left" || action === "right") {
            if (e.repeat) return;
            const dir = action === "left" ? -1 : 1;
            shift(dir);
            held = {dir, time: 0, carry: 0};
        } else if (action === "soft") {
            softDrop = true;
        } else if (e.repeat) {
            return;
        } else if (action === "cw") {
            rotate(1);
        } else if (action === "ccw") {
            rotate(-1);
        } else if (action === "hard") {
            hardDrop();
        } else if (action === "hold") {
            holdPiece();
        }
        draw();
    });
    document.addEventListener("keyup", e => {
        const action = KEYS[e.code];
        if (action === "soft") softDrop = false;
        if ((action === "left" || action === "right") && held && held.dir === (action === "left" ? -1 : 1)) held = null;
    });
    court.addEventListener("mousedown", e => {
        e.preventDefault();
        if (state !== "play") go();
        else court.focus({preventScroll: true});
    });

    addEventListener("blur", pause);
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    onTab(name => { if (name !== "blocks") pause(); else requestAnimationFrame(fit); });

    $("blocks-new").addEventListener("click", reset);
    onPrefs(key => {
        drawStats();
        if (key === "blocks_level" && state !== "play" && state !== "paused") reset();
        if (key === "blocks_ghost") draw();
    });

    function drawStats() {
        $("blocks-score").textContent = String(score);
        $("blocks-lines").textContent = String(lines);
        $("blocks-level-now").textContent = String(level);
        $("blocks-best").replaceChildren(
            el("span.muted", {text: "Best"}), ` ${best.score || 0}`,
            ...(best.score ? ["  ·  ", el("span.muted", {text: "Lines"}), ` ${best.lines || 0}`] : []),
        );
    }

    Buddy.on("state", s => { best = {...(s.blocks_best || {})}; newGame(); setState("ready"); });
    Buddy.on("blocks_best", b => { best = {...b}; drawStats(); });
    Buddy.on("theme", () => requestAnimationFrame(() => { colors = gameColors(); draw(); }));
    Buddy.on("i18n", () => requestAnimationFrame(draw));
    new ResizeObserver(() => fit()).observe(court.parentElement);

    colors = gameColors();
    newGame();
    setState("ready");
    return {pause, go, get state() { return state; }, get score() { return score; }, get lines() { return lines; },
            get level() { return level; }, get piece() { return piece; }, get board() { return board; },
            get hold() { return hold; }, get queue() { return queue; },
            _step: step, _rotate: rotate, _shift: shift, _hardDrop: hardDrop, _hold: holdPiece, _fits: fits, _cells: cells,
            _set: (rows, p) => { board = rows; piece = p; lockTimer = null; lockResets = 0; lowestY = p ? p.y : 0; }};
})();
