/* Snake (loaded after games.js, whose helpers it uses): steer the snake
   to the food on a 32 x 20 board; each bite makes it longer and a little
   faster. With Walls on (the default) the edge ends the game; off, the
   snake goes out one side and comes in the other. Like Pong it pauses
   itself when it can't be seen or Buddy loses the focus. */
"use strict";

const snake = (() => {
    const COLS = 32, ROWS = 20, CELL = 25;           // the court: 800 x 500 units
    const W = COLS * CELL, H = ROWS * CELL;
    const SPEEDS = {slow: 6, normal: 9, fast: 13};  // moves a second at the start
    const SPEED_UP = 0.35, SPEED_MAX_EXTRA = 8;     // ...a little more per bite, up to this much more
    const DIRS = {up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0]};
    const OPPOSITE = {up: "down", down: "up", left: "right", right: "left"};
    const KEYS = {ArrowUp: "up", KeyW: "up", ArrowDown: "down", KeyS: "down",
                  ArrowLeft: "left", KeyA: "left", ArrowRight: "right", KeyD: "right"};

    const court = $("snake-court"), canvas = $("snake-canvas"), ctx = canvas.getContext("2d");
    let body = [];            // cells, head first
    let dir = "right";
    let turns = [];           // turns pressed, not yet made (so two quick presses both count)
    let food = null;
    let score = 0;
    let best = {};            // "normal_walls" -> best score (page.py)
    let state = "ready";      // ready, play, paused, over
    let carry = 0, lastFrame = 0, running = false;
    let colors = {};
    let scale = 1;

    const speedName = () => (SPEEDS[prefs.snake_speed] ? prefs.snake_speed : "normal");
    const walls = () => prefs.snake_walls !== false;
    const soundOn = () => prefs.snake_sound !== false;
    const bestKey = () => `${speedName()}_${walls() ? "walls" : "wrap"}`;
    const movesPerSecond = () => SPEEDS[speedName()] + Math.min(SPEED_MAX_EXTRA, (body.length - 4) * SPEED_UP);
    const blip = (freq, length, type, at) => gameSound.blip(soundOn(), freq, length, type, at);

    /* --------------------------------------------------- the game -- */
    function newGame() {
        const y = Math.floor(ROWS / 2), x = Math.floor(COLS / 2) - 4;
        body = [3, 2, 1, 0].map(i => ({x: x + i, y}));
        dir = "right";
        turns = [];
        score = 0;
        placeFood();
        drawScore();
    }

    function placeFood() {
        const taken = new Set(body.map(c => c.y * COLS + c.x));
        const free = [];
        for (let i = 0; i < COLS * ROWS; i++) if (!taken.has(i)) free.push(i);
        if (!free.length) { food = null; return; }
        const i = free[Math.floor(Math.random() * free.length)];
        food = {x: i % COLS, y: Math.floor(i / COLS)};
    }

    function turn(to) {
        const last = turns.length ? turns[turns.length - 1] : dir;
        if (to !== last && to !== OPPOSITE[last] && turns.length < 3) turns.push(to);
    }

    function move() {
        if (turns.length) dir = turns.shift();
        const [dx, dy] = DIRS[dir];
        let x = body[0].x + dx, y = body[0].y + dy;
        if (walls()) {
            if (x < 0 || x >= COLS || y < 0 || y >= ROWS) return end(false);
        } else {
            x = (x + COLS) % COLS;
            y = (y + ROWS) % ROWS;
        }
        const eats = food && x === food.x && y === food.y;
        // The tail moves out of the way this same step - unless the snake is growing.
        const hits = body.some((c, i) => c.x === x && c.y === y && (eats || i < body.length - 1));
        if (hits) return end(false);
        body.unshift({x, y});
        if (eats) {
            score += 1;
            blip(660, 0.06);
            blip(880, 0.06, "square", 0.06);
            placeFood();
            drawScore();
            if (!food) return end(true);     // the whole board: nowhere left to go
        } else {
            body.pop();
        }
    }

    function end(filled) {
        const previous = best[bestKey()] || 0;
        const record = score > previous;
        if (record) best[bestKey()] = score;
        blip(220, 0.18, "triangle");
        blip(150, 0.3, "triangle", 0.16);
        setState("over", {filled, record});
        if (score > 0) send("result", {game: "snake", speed: speedName(), walls: walls(), score});
        drawScore();
    }

    /* ----------------------------------------------------- drawing -- */
    function fit() {
        if (court.offsetParent === null) return;    // its tab isn't showing
        scale = fitCourt(court, canvas, W, H);
        draw();
    }

    function cell(c, inset, radius) {
        const x = c.x * CELL + inset, y = c.y * CELL + inset, s = CELL - inset * 2;
        ctx.beginPath();
        ctx.roundRect(x, y, s, s, radius);
        ctx.fill();
    }

    function draw() {
        if (!canvas.width || !body.length) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        // A faint chequer, so the grid can be read.
        ctx.fillStyle = colors.dim;
        ctx.globalAlpha = 0.06;
        for (let y = 0; y < ROWS; y++) for (let x = (y % 2); x < COLS; x += 2) ctx.fillRect(x * CELL, y * CELL, CELL, CELL);
        ctx.globalAlpha = 1;
        if (food) {
            ctx.fillStyle = colors.accent;
            ctx.beginPath();
            ctx.arc(food.x * CELL + CELL / 2, food.y * CELL + CELL / 2, CELL * 0.34, 0, Math.PI * 2);
            ctx.fill();
        }
        // The body fades a little toward the tail; the head is solid.
        ctx.fillStyle = colors.fg;
        for (let i = body.length - 1; i >= 1; i--) {
            ctx.globalAlpha = 0.55 + 0.4 * (1 - i / body.length);
            cell(body[i], 2, 5);
        }
        ctx.globalAlpha = 1;
        cell(body[0], 1, 7);
        // Eyes, looking the way it's going.
        const [dx, dy] = DIRS[dir];
        const cx = body[0].x * CELL + CELL / 2 + dx * 4, cy = body[0].y * CELL + CELL / 2 + dy * 4;
        ctx.fillStyle = colors.accent;
        for (const side of [-1, 1]) {
            ctx.beginPath();
            ctx.arc(cx + dy * side * 5, cy + dx * side * 5, 2.4, 0, Math.PI * 2);
            ctx.fill();
        }
    }

    function frame(now) {
        if (!running) return;
        const dt = Math.min(0.1, (now - lastFrame) / 1000);
        lastFrame = now;
        carry += dt;
        let step = 1 / movesPerSecond();
        while (state === "play" && carry >= step) {
            carry -= step;
            move();
            step = 1 / movesPerSecond();
        }
        draw();
        if (state === "play") requestAnimationFrame(frame);
        else running = false;
    }

    function run() {
        if (running) return;
        running = true;
        carry = 0;
        lastFrame = performance.now();
        requestAnimationFrame(frame);
    }

    /* ------------------------------------------------------ states -- */
    function setState(next, how = {}) {
        state = next;
        const shown = next !== "play";
        $("snake-overlay").hidden = !shown;
        court.classList.toggle("idle", shown);
        if (next === "ready") {
            $("snake-overlay-title").textContent = "Snake";
            $("snake-overlay-text").textContent = "Press Space or click to start";
        } else if (next === "paused") {
            $("snake-overlay-title").textContent = "Paused";
            $("snake-overlay-text").textContent = "Press Space or click to carry on";
        } else if (next === "over") {
            $("snake-overlay-title").textContent = how.filled ? "You filled the board!" : how.record ? "New best!" : "Game over";
            $("snake-overlay-text").textContent = `You scored ${score}. Press Space or click to play again`;
        }
        if (next === "play") run();
        draw();
    }

    function pause() {
        if (state === "play") setState("paused");
    }

    function go(heading) {
        gameSound.prime();
        court.focus({preventScroll: true});
        if (state === "ready" || state === "over") {
            if (state === "over") newGame();
            if (heading) turn(heading);
            setState("play");
        } else if (state === "paused") {
            setState("play");
        }
    }

    function reset() {
        newGame();
        setState("ready");
    }

    /* ------------------------------------------------------- input -- */
    document.addEventListener("keydown", e => {
        if (gameTab !== "snake" || e.ctrlKey || e.altKey || e.metaKey) return;
        if (e.target.closest && e.target.closest("input, select, textarea, button, .modal, .dropdown-pop")) return;
        const to = KEYS[e.code];
        if (to) {
            e.preventDefault();
            if (state === "play") turn(to);
            else if ((state === "ready" || state === "over") && !e.repeat) go(to);
        } else if (e.code === "Space" || e.code === "Enter") {
            e.preventDefault();
            if (!e.repeat) (state === "play" ? pause : go)();
        } else if (e.code === "Escape" || e.code === "KeyP") {
            pause();
        }
    });
    court.addEventListener("mousedown", e => {
        e.preventDefault();
        if (state !== "play") go();
        else court.focus({preventScroll: true});
    });

    // Out of sight or out of focus: paused.
    addEventListener("blur", pause);
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    onTab(name => { if (name !== "snake") pause(); else requestAnimationFrame(fit); });

    $("snake-new").addEventListener("click", reset);
    onPrefs(key => {
        drawScore();
        // Another speed or walls setting is another game, with its own best.
        if (key === "snake_speed" || key === "snake_walls") reset();
    });

    function drawScore() {
        $("snake-score").replaceChildren(
            el("span.muted", {text: "Score"}), ` ${score}  ·  `,
            el("span.muted", {text: "Best"}), ` ${best[bestKey()] || 0}`,
        );
    }

    Buddy.on("state", s => { best = {...(s.snake_best || {})}; drawScore(); });
    Buddy.on("snake_best", b => { best = {...b}; drawScore(); });
    Buddy.on("theme", () => requestAnimationFrame(() => { colors = gameColors(); draw(); }));
    new ResizeObserver(() => fit()).observe(court.parentElement);

    colors = gameColors();
    newGame();
    setState("ready");
    return {pause, go, turn, get state() { return state; }, get score() { return score; },
            get body() { return body; }, get food() { return food; }, _move: move,
            _place: (cells, at, heading) => { body = cells; food = at; dir = heading || dir; turns = []; }};
})();
