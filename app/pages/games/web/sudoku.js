/* Sudoku (loaded after games.js, whose helpers it uses): fill the grid so
   every row, column and 3 x 3 box holds 1 to 9 once. Each puzzle is made
   here: a full grid at random, then clues taken out in pairs that mirror
   round the middle, each only if the puzzle still has exactly one answer -
   Easy keeps 40 clues, Medium about 32, Hard about 26. Pencil marks,
   mistakes shown or not, undo; the clock stops and the grid is covered
   while paused. */
"use strict";

const sudoku = (() => {
    const CELL = 54, GX = 20, GY = 20, GRID = CELL * 9;
    const KEY = 66, KEY_GAP = 8, KX = GX + GRID + 34, KY = GY + (GRID - (KEY * 4 + KEY_GAP * 3)) / 2;
    const W = KX + KEY * 3 + KEY_GAP * 2 + 20, H = GY * 2 + GRID;
    const CLUES = {easy: 40, medium: 32, hard: 26};
    const RED = "#ee5a5a";

    const court = $("sudoku-court"), canvas = $("sudoku-canvas"), ctx = canvas.getContext("2d");
    let given = [], values = [], notes = [], solution = [];
    let sel = -1;
    let pencil = false;
    let history = [];         // what values and notes were, before each change
    let state = "play";       // play, paused, won
    let started = false, seconds = 0, lastTick = 0, ticking = null;
    let levelName = "easy";
    let stats = {};           // {easy: {won, best}, ...} (page.py)
    let colors = {}, scale = 1;

    const soundOn = () => prefs.sudoku_sound !== false;
    const mistakesOn = () => prefs.sudoku_mistakes !== false;
    const blip = (freq, length, type, at) => gameSound.blip(soundOn(), freq, length, type, at);
    const box = i => Math.floor(Math.floor(i / 9) / 3) * 3 + Math.floor((i % 9) / 3);
    const peers = i => {
        const r = Math.floor(i / 9), c = i % 9, b = box(i), out = [];
        for (let j = 0; j < 81; j++) if (j !== i && (Math.floor(j / 9) === r || j % 9 === c || box(j) === b)) out.push(j);
        return out;
    };
    const PEERS = Array.from({length: 81}, (_, i) => peers(i));

    /* ---------------------------------------------- making puzzles -- */
    /* Counts the grid's solutions, up to `limit`; the first is written to
       `out`. Fills the most constrained square first. `shuffle` tries the
       digits in a random order (to make a random full grid). */
    function solve(grid, limit, out, shuffle) {
        const g = Int8Array.from(grid);
        const rows = new Uint16Array(9), cols = new Uint16Array(9), boxes = new Uint16Array(9);
        for (let i = 0; i < 81; i++) {
            if (!g[i]) continue;
            const bit = 1 << (g[i] - 1), r = Math.floor(i / 9), c = i % 9, b = box(i);
            if ((rows[r] | cols[c] | boxes[b]) & bit) return 0;     // already broken
            rows[r] |= bit; cols[c] |= bit; boxes[b] |= bit;
        }
        let count = 0;
        (function search() {
            let pick = -1, pickMask = 0, fewest = 10;
            for (let i = 0; i < 81; i++) {
                if (g[i]) continue;
                const mask = ~(rows[Math.floor(i / 9)] | cols[i % 9] | boxes[box(i)]) & 0x1ff;
                const n = popcount(mask);
                if (n < fewest) { fewest = n; pick = i; pickMask = mask; if (n <= 1) break; }
            }
            if (pick < 0) {
                count += 1;
                if (out && count === 1) out.set(g);
                return;
            }
            if (!pickMask) return;
            const digits = [];
            for (let d = 1; d <= 9; d++) if (pickMask & (1 << (d - 1))) digits.push(d);
            if (shuffle) for (let k = digits.length - 1; k > 0; k--) {
                const j = Math.floor(Math.random() * (k + 1));
                [digits[k], digits[j]] = [digits[j], digits[k]];
            }
            const r = Math.floor(pick / 9), c = pick % 9, b = box(pick);
            for (const d of digits) {
                const bit = 1 << (d - 1);
                g[pick] = d; rows[r] |= bit; cols[c] |= bit; boxes[b] |= bit;
                search();
                g[pick] = 0; rows[r] &= ~bit; cols[c] &= ~bit; boxes[b] &= ~bit;
                if (count >= limit) return;
            }
        })();
        return count;
    }

    function popcount(m) {
        let n = 0;
        while (m) { m &= m - 1; n += 1; }
        return n;
    }

    function makePuzzle(level) {
        const full = new Int8Array(81);
        solve(new Int8Array(81), 1, full, true);
        const puzzle = Int8Array.from(full);
        let clues = 81;
        const order = [...Array(41).keys()];                 // a square and its mirror, together
        for (let k = order.length - 1; k > 0; k--) {
            const j = Math.floor(Math.random() * (k + 1));
            [order[k], order[j]] = [order[j], order[k]];
        }
        for (const i of order) {
            if (clues <= CLUES[level]) break;
            const j = 80 - i;
            const keep = [puzzle[i], puzzle[j]];
            puzzle[i] = 0;
            puzzle[j] = 0;
            if (solve(puzzle, 2, null, false) === 1) clues -= i === j ? 1 : 2;
            else { puzzle[i] = keep[0]; puzzle[j] = keep[1]; }
        }
        return {puzzle: [...puzzle], solution: [...full]};
    }

    /* ---------------------------------------------------- the game -- */
    function newGame() {
        levelName = CLUES[prefs.sudoku_level] ? prefs.sudoku_level : "easy";
        const made = makePuzzle(levelName);
        solution = made.solution;
        values = made.puzzle;
        given = values.map(v => v > 0);
        notes = Array(81).fill(0);
        history = [];
        sel = -1;
        started = false;
        seconds = 0;
        stopClock();
        setState("play");
        drawStats();
    }

    function snapshot() {
        history.push({values: [...values], notes: [...notes]});
        if (history.length > 500) history.shift();
    }

    function begin() {
        if (!started && state === "play") {
            started = true;
            startClock();
        }
    }

    function enter(d, asNote) {
        if (state !== "play" || sel < 0 || given[sel]) return;
        begin();
        gameSound.prime();
        if (asNote) {
            if (values[sel]) return;
            snapshot();
            notes[sel] ^= 1 << (d - 1);
            blip(700, 0.02, "sine");
        } else {
            if (values[sel] === d) return;
            snapshot();
            values[sel] = d;
            notes[sel] = 0;
            // The number can't go anywhere else in its row, column or box: those marks go.
            for (const j of PEERS[sel]) notes[j] &= ~(1 << (d - 1));
            const wrong = d !== solution[sel];
            blip(wrong && mistakesOn() ? 200 : 560, 0.05, wrong && mistakesOn() ? "triangle" : "sine");
            if (values.every((v, i) => v === solution[i])) return win();
        }
        draw();
        drawStats();
    }

    function erase() {
        if (state !== "play" || sel < 0 || given[sel] || (!values[sel] && !notes[sel])) return;
        begin();
        snapshot();
        values[sel] = 0;
        notes[sel] = 0;
        draw();
        drawStats();
    }

    function takeBack() {
        if (state !== "play" || !history.length) return;
        const last = history.pop();
        values = last.values;
        notes = last.notes;
        draw();
        drawStats();
    }

    function win() {
        stopClock();
        const time = Math.round(seconds * 10) / 10;
        const entry = stats[levelName] || {won: 0, best: 0};
        stats[levelName] = {won: entry.won + 1, best: !entry.best || time < entry.best ? time : entry.best};
        const notes2 = [523, 659, 784, 1047];
        notes2.forEach((f, k) => blip(f, 0.12, "sine", k * 0.09));
        setState("won");
        send("result", {game: "sudoku", level: levelName, seconds: time});
        drawStats();
    }

    /* ------------------------------------------------------- clock -- */
    function startClock() {
        stopClock();
        lastTick = performance.now();
        ticking = setInterval(() => {
            const now = performance.now();
            seconds += (now - lastTick) / 1000;
            lastTick = now;
            drawTime();
        }, 250);
    }

    function stopClock() {
        if (ticking) {
            seconds += (performance.now() - lastTick) / 1000;
            clearInterval(ticking);
            ticking = null;
        }
        drawTime();
    }

    const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

    function drawTime() {
        $("sudoku-time").textContent = clock(seconds);
    }

    /* ----------------------------------------------------- drawing -- */
    function fit() {
        if (court.offsetParent === null) return;
        scale = fitCourt(court, canvas, W, H);
        draw();
    }

    function conflicts(i) {
        const v = values[i];
        return v > 0 && PEERS[i].some(j => values[j] === v);
    }

    /* Yours and wrong: the same number twice in a row, column or box - or,
       with Show mistakes, not the answer. */
    function wrongAt(i) {
        return values[i] > 0 && !given[i] && (conflicts(i) || (mistakesOn() && values[i] !== solution[i]));
    }

    function left(d) {
        return 9 - values.filter(v => v === d).length;
    }

    function draw() {
        if (!canvas.width || !values.length) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        const cx = i => GX + (i % 9) * CELL, cy = i => GY + Math.floor(i / 9) * CELL;
        const selValue = sel >= 0 ? values[sel] : 0;
        // Shading: the selected square's row, column and box; every square with its number.
        for (let i = 0; i < 81; i++) {
            let fill = null, alpha = 0;
            if (sel >= 0 && i !== sel && (PEERS[sel].includes(i))) { fill = colors.dim; alpha = 0.1; }
            if (selValue && values[i] === selValue && i !== sel) { fill = colors.accent; alpha = 0.22; }
            if (i === sel) { fill = colors.accent; alpha = 0.4; }
            // A wrong number's square is tinted red too: in a theme whose accent is red, the
            // number's colour alone wouldn't tell it from yours.
            if (wrongAt(i) && i !== sel) { fill = RED; alpha = 0.22; }
            if (fill) {
                ctx.fillStyle = fill;
                ctx.globalAlpha = alpha;
                ctx.fillRect(cx(i), cy(i), CELL, CELL);
            }
        }
        ctx.globalAlpha = 1;
        // Lines: thin between squares, thick round the boxes.
        ctx.fillStyle = colors.dim;
        ctx.globalAlpha = 0.35;
        for (let k = 1; k < 9; k++) {
            if (k % 3 === 0) continue;
            ctx.fillRect(GX + k * CELL, GY, 1, GRID);
            ctx.fillRect(GX, GY + k * CELL, GRID, 1);
        }
        ctx.globalAlpha = 0.85;
        ctx.fillStyle = colors.fg;
        for (let k = 0; k <= 9; k += 3) {
            ctx.fillRect(GX + k * CELL - 1, GY - 1, 2, GRID + 2);
            ctx.fillRect(GX - 1, GY + k * CELL - 1, GRID + 2, 2);
        }
        ctx.globalAlpha = 1;
        // Numbers: clues plain and bold, yours in the accent, wrong ones red.
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        for (let i = 0; i < 81; i++) {
            const x = cx(i) + CELL / 2, y = cy(i) + CELL / 2;
            if (values[i]) {
                ctx.fillStyle = given[i] ? colors.fg : wrongAt(i) ? RED : colors.accent;
                ctx.font = `${given[i] ? 800 : 600} 30px ${colors.font}`;
                ctx.fillText(String(values[i]), x, y + 1);
            } else if (notes[i]) {
                ctx.fillStyle = colors.dim;
                ctx.font = `500 14px ${colors.font}`;
                for (let d = 1; d <= 9; d++) {
                    if (!(notes[i] & (1 << (d - 1)))) continue;
                    const nx = cx(i) + 9 + ((d - 1) % 3) * 18, ny = cy(i) + 9 + Math.floor((d - 1) / 3) * 18;
                    ctx.fillStyle = selValue === d ? colors.accent : colors.dim;
                    ctx.fillText(String(d), nx, ny + 1);
                }
            }
        }
        // The keypad: each number, and how many are left to place.
        for (let d = 1; d <= 9; d++) {
            const [x, y] = keyAt(d);
            const done = left(d) <= 0;
            ctx.fillStyle = selValue === d ? colors.accent : colors.dim;
            ctx.globalAlpha = selValue === d ? 0.3 : 0.12;
            ctx.beginPath();
            ctx.roundRect(x, y, KEY, KEY, 10);
            ctx.fill();
            ctx.globalAlpha = done ? 0.3 : 1;
            ctx.fillStyle = pencil ? colors.dim : colors.fg;
            ctx.font = `700 ${pencil ? 22 : 30}px ${colors.font}`;
            ctx.fillText(String(d), x + KEY / 2, y + KEY / 2 - 3);
            ctx.font = `500 12px ${colors.font}`;
            ctx.fillStyle = colors.dim;
            ctx.fillText(String(Math.max(0, left(d))), x + KEY / 2, y + KEY - 11);
            ctx.globalAlpha = 1;
        }
        // Erase, under the numbers.
        const [ex, ey] = keyAt(10);
        ctx.fillStyle = colors.dim;
        ctx.globalAlpha = 0.12;
        ctx.beginPath();
        ctx.roundRect(ex, ey, KEY * 3 + KEY_GAP * 2, KEY, 10);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.fillStyle = colors.fg;
        ctx.font = `600 18px ${colors.font}`;
        ctx.fillText(Buddy.t("Erase"), ex + (KEY * 3 + KEY_GAP * 2) / 2, ey + KEY / 2);
    }

    /* Where keypad key d sits (10: erase, under the rest). */
    function keyAt(d) {
        const k = d - 1;
        return [KX + (k % 3) * (KEY + KEY_GAP), KY + Math.floor(k / 3) * (KEY + KEY_GAP)];
    }

    /* ------------------------------------------------------ states -- */
    function setState(next) {
        state = next;
        const shown = next !== "play";
        $("sudoku-overlay").hidden = !shown;
        $("sudoku-overlay").classList.toggle("covered", next === "paused");
        if (next === "paused") {
            $("sudoku-overlay-title").textContent = "Paused";
            $("sudoku-overlay-text").textContent = "Click to carry on";
        } else if (next === "won") {
            $("sudoku-overlay-title").textContent = "Solved!";
            $("sudoku-overlay-text").textContent = `Solved in ${clock(seconds)}. Click for a new puzzle`;
        }
        draw();
    }

    function pause() {
        if (state === "play" && started) {
            stopClock();
            setState("paused");
        }
    }

    function resume() {
        if (state !== "paused") return;
        setState("play");
        startClock();
    }

    function setPencil(on) {
        pencil = on;
        $("sudoku-notes").setAttribute("aria-pressed", String(pencil));
        draw();
    }

    /* ------------------------------------------------------- input -- */
    function pointAt(e) {
        const r = canvas.getBoundingClientRect();
        return [(e.clientX - r.left) / r.width * W, (e.clientY - r.top) / r.height * H];
    }

    court.addEventListener("mousedown", e => {
        e.preventDefault();
        court.focus({preventScroll: true});
        gameSound.prime();
        if (state === "paused") return resume();
        if (state === "won") return newGame();
        const [x, y] = pointAt(e);
        if (x >= GX && x < GX + GRID && y >= GY && y < GY + GRID) {
            sel = Math.floor((y - GY) / CELL) * 9 + Math.floor((x - GX) / CELL);
            begin();
            draw();
            return;
        }
        for (let d = 1; d <= 10; d++) {
            const [kx, ky] = keyAt(d);
            const w = d === 10 ? KEY * 3 + KEY_GAP * 2 : KEY;
            if (x >= kx && x < kx + w && y >= ky && y < ky + KEY) {
                if (d === 10) erase();
                else enter(d, pencil || e.shiftKey);
                return;
            }
        }
    });

    document.addEventListener("keydown", e => {
        if (gameTab !== "sudoku" || e.altKey) return;
        if (e.target.closest && e.target.closest("input, select, textarea, button, .modal, .dropdown-pop")) return;
        if (e.ctrlKey || e.metaKey) {     // Ctrl+Z, or ⌘Z on a Mac
            if (e.code === "KeyZ" && !e.shiftKey) { e.preventDefault(); takeBack(); }
            return;
        }
        if (state === "paused") {
            if (e.code === "Space" || e.code === "Enter" || e.code === "KeyP") { e.preventDefault(); resume(); }
            return;
        }
        if (state === "won") {
            if (e.code === "Space" || e.code === "Enter") { e.preventDefault(); newGame(); }
            return;
        }
        const digit = /^(Digit|Numpad)([0-9])$/.exec(e.code);
        if (digit) {
            e.preventDefault();
            const d = Number(digit[2]);
            if (d === 0) erase();
            else enter(d, pencil || e.shiftKey);
            return;
        }
        const moves = {ArrowUp: -9, ArrowDown: 9, ArrowLeft: -1, ArrowRight: 1};
        if (moves[e.code] !== undefined) {
            e.preventDefault();
            if (sel < 0) sel = 40;
            else {
                const r = Math.floor(sel / 9), c = sel % 9;
                const dr = moves[e.code] === 9 ? 1 : moves[e.code] === -9 ? -1 : 0;
                const dc = moves[e.code] === 1 ? 1 : moves[e.code] === -1 ? -1 : 0;
                sel = ((r + dr + 9) % 9) * 9 + (c + dc + 9) % 9;
            }
            begin();
            draw();
        } else if (e.code === "Backspace" || e.code === "Delete") {
            e.preventDefault();
            erase();
        } else if (e.code === "KeyN") {
            setPencil(!pencil);
        } else if (e.code === "Escape" || e.code === "KeyP") {
            pause();
        }
    });

    addEventListener("blur", pause);
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    onTab(name => { if (name !== "sudoku") pause(); else requestAnimationFrame(fit); });

    $("sudoku-new").addEventListener("click", newGame);
    $("sudoku-undo").addEventListener("click", takeBack);
    $("sudoku-notes").addEventListener("click", () => setPencil(!pencil));
    onPrefs(key => {
        drawStats();
        if (key === "sudoku_level") newGame();
        if (key === "sudoku_mistakes") draw();
    });

    function drawStats() {
        const s = stats[levelName];
        $("sudoku-best").replaceChildren(
            el("span.muted", {text: "Solved"}), ` ${s ? s.won : 0}`,
            ...(s && s.best ? ["  ·  ", el("span.muted", {text: "Best"}), ` ${clock(s.best)}`] : []),
        );
        $("sudoku-undo").disabled = !history.length || state !== "play";
    }

    Buddy.on("state", s => {
        stats = JSON.parse(JSON.stringify(s.sudoku_stats || {}));
        // The puzzle for the saved difficulty (prefs have just arrived).
        if ((CLUES[prefs.sudoku_level] ? prefs.sudoku_level : "easy") !== levelName || !values.length) newGame();
        drawStats();
    });
    Buddy.on("sudoku_stats", s => { stats = JSON.parse(JSON.stringify(s)); drawStats(); });
    Buddy.on("theme", () => requestAnimationFrame(() => { colors = gameColors(); draw(); }));
    Buddy.on("i18n", () => requestAnimationFrame(draw));
    new ResizeObserver(() => fit()).observe(court.parentElement);

    colors = gameColors();
    newGame();
    return {enter, erase, takeBack, pause, resume, setPencil, get state() { return state; },
            get values() { return values; }, get solution() { return solution; }, get given() { return given; },
            get notes() { return notes; }, get seconds() { return seconds; },
            select: i => { sel = i; draw(); }, _solve: solve, _make: makePuzzle};
})();
