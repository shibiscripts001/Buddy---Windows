/* Breakout (loaded after games.js, whose helpers it uses): keep the ball
   in play with the paddle and knock out every brick. Where the ball meets
   the paddle sets the angle it leaves at. Bricks in the top rows score
   more; a ringed brick takes two hits. Clear the wall for the next level,
   a little faster; three lives. Arrow keys or A and D - or the mouse, if
   Mouse control is ticked (off unless it is, as in Pong). Pauses itself
   when it can't be seen or Buddy loses the focus. */
"use strict";

const breakout = (() => {
    const W = 800, H = 500;
    const COLS = 12, BRICK_GAP = 4, BRICK_H = 20, SIDE = 30, TOP = 52;
    const BRICK_W = (W - 2 * SIDE - (COLS - 1) * BRICK_GAP) / COLS;
    const PADDLE_W = 104, PADDLE_H = 12, PADDLE_Y = 462, PADDLE_SPEED = 720;
    const BALL_R = 7;
    const MAX_ANGLE = Math.PI / 3;                   // off the paddle's very end: 60 degrees from straight up
    const STEP = 4;                                  // the ball moves at most this far between collision checks
    const LIVES = 3;
    // The walls, a row a line: a digit is how many hits a brick takes.
    const LEVELS = [
        ["111111111111", "111111111111", "111111111111", "111111111111", "111111111111", "111111111111"],
        ["....1111....", "...111111...", "..11111111..", ".1111111111.", "111111111111", "222222222222"],
        ["212121212121", "121212121212", "212121212121", "111111111111", "111111111111", "111111111111"],
        ["..22....22..", ".2112..2112.", "211112211112", "211111111112", ".2111111112.", "..21111112..", "...211112...", "....2112...."],
        ["222222222222", "............", "111111111111", "1.1.1.1.1.1.", "111111111111", ".2.2.2.2.2.2", "111111111111"],
    ];
    const ROW_COLORS = ["#ee5a5a", "#f2994a", "#f2c94c", "#5ccc6b", "#3cc6e6", "#4a7cf0", "#a66cf0", "#e86ac3"];

    const court = $("breakout-court"), canvas = $("breakout-canvas"), ctx = canvas.getContext("2d");
    const keys = new Set();
    let bricks = [];          // {x, y, row, hits}
    let paddleX = (W - PADDLE_W) / 2;
    const ball = {x: 0, y: 0, vx: 0, vy: 0, speed: 0};
    let level = 1, lives = LIVES, score = 0, baseSpeed = 360;
    let best = {};            // {score, level} (page.py)
    let state = "ready";      // ready, serve, play, paused, cleared, over
    let resumeTo = "play";
    let mouseX = null;
    let running = false, lastFrame = 0;
    let colors = {}, scale = 1;

    const soundOn = () => prefs.breakout_sound !== false;
    const mouseOn = () => prefs.breakout_mouse === true;          // off unless ticked
    const blip = (freq, length, type, at) => gameSound.blip(soundOn(), freq, length, type, at);

    /* ---------------------------------------------------- the game -- */
    function buildLevel() {
        const layout = LEVELS[(level - 1) % LEVELS.length];
        bricks = [];
        layout.forEach((line, row) => [...line].forEach((ch, col) => {
            const hits = Number(ch) || 0;
            if (hits) bricks.push({x: SIDE + col * (BRICK_W + BRICK_GAP), y: TOP + row * (BRICK_H + BRICK_GAP), row, hits});
        }));
        // A little faster each level, and after the fifth wall, faster again round the same walls.
        baseSpeed = Math.min(640, 360 + 28 * (level - 1));
    }

    function newGame() {
        level = 1;
        lives = LIVES;
        score = 0;
        buildLevel();
        toServe();
        drawStats();
    }

    function toServe() {
        paddleX = Math.max(0, Math.min(W - PADDLE_W, paddleX));
        ball.speed = baseSpeed;
        ball.vx = ball.vy = 0;
        stickBall();
    }

    function stickBall() {
        ball.x = paddleX + PADDLE_W / 2;
        ball.y = PADDLE_Y - BALL_R - 1;
    }

    function launch() {
        const angle = (Math.random() * 2 - 1) * (Math.PI / 6);
        ball.vx = Math.sin(angle) * ball.speed;
        ball.vy = -Math.cos(angle) * ball.speed;
        blip(440, 0.05);
        setState("play");
    }

    function points(b) {
        return 10 * Math.max(1, 8 - b.row);
    }

    /* The first brick the ball overlaps, if any. */
    function brickAt() {
        for (const b of bricks) {
            const nx = Math.max(b.x, Math.min(ball.x, b.x + BRICK_W));
            const ny = Math.max(b.y, Math.min(ball.y, b.y + BRICK_H));
            if ((ball.x - nx) ** 2 + (ball.y - ny) ** 2 <= BALL_R * BALL_R) return b;
        }
        return null;
    }

    function hit(b) {
        b.hits -= 1;
        if (b.hits <= 0) {
            bricks.splice(bricks.indexOf(b), 1);
            score += points(b);
            blip(520 + (7 - Math.min(7, b.row)) * 45, 0.05);
        } else {
            score += 5;
            blip(330, 0.05, "triangle");
        }
        drawStats();
    }

    /* The ball, one short step - x then y, so it bounces off the side it hit. */
    function moveBall(dt) {
        const steps = Math.max(1, Math.ceil(Math.hypot(ball.vx, ball.vy) * dt / STEP));
        for (let i = 0; i < steps && state === "play"; i++) {
            // Worked out each step: a bounce turns the rest of the move round with it.
            const sx = ball.vx * dt / steps, sy = ball.vy * dt / steps;
            ball.x += sx;
            if (ball.x < BALL_R) { ball.x = BALL_R; ball.vx = Math.abs(ball.vx); blip(300, 0.03); }
            if (ball.x > W - BALL_R) { ball.x = W - BALL_R; ball.vx = -Math.abs(ball.vx); blip(300, 0.03); }
            let b = brickAt();
            if (b) { ball.x -= sx; ball.vx = -ball.vx; hit(b); }
            ball.y += sy;
            if (ball.y < BALL_R) { ball.y = BALL_R; ball.vy = Math.abs(ball.vy); blip(300, 0.03); }
            b = brickAt();
            if (b) { ball.y -= sy; ball.vy = -ball.vy; hit(b); }
            paddleBounce();
            if (ball.y - BALL_R > H) return lose();
            if (!bricks.length) return cleared();
        }
    }

    function paddleBounce() {
        if (ball.vy <= 0) return;
        const bottom = ball.y + BALL_R;
        if (bottom < PADDLE_Y || bottom > PADDLE_Y + PADDLE_H + 6) return;
        if (ball.x < paddleX - BALL_R || ball.x > paddleX + PADDLE_W + BALL_R) return;
        const rel = Math.max(-1, Math.min(1, (ball.x - (paddleX + PADDLE_W / 2)) / (PADDLE_W / 2)));
        ball.speed = Math.min(baseSpeed * 1.35, ball.speed * 1.01);
        const angle = rel * MAX_ANGLE;
        ball.vx = Math.sin(angle) * ball.speed;
        ball.vy = -Math.cos(angle) * ball.speed;
        ball.y = PADDLE_Y - BALL_R;
        blip(440, 0.04);
    }

    function lose() {
        lives -= 1;
        drawStats();
        blip(200, 0.15, "triangle");
        if (lives <= 0) return end();
        toServe();
        setState("serve");
    }

    function cleared() {
        blip(660, 0.08, "sine");
        blip(880, 0.08, "sine", 0.08);
        blip(1175, 0.16, "sine", 0.16);
        setState("cleared");
    }

    function nextLevel() {
        level += 1;
        buildLevel();
        toServe();
        drawStats();
        setState("serve");
    }

    function end() {
        const record = score > (best.score || 0);
        if (record) best = {score, level};
        blip(150, 0.3, "triangle", 0.12);
        setState("over", {record});
        if (score > 0) send("result", {game: "breakout", score, level});
        drawStats();
    }

    /* ------------------------------------------------------ paddle -- */
    function movePaddle(dt) {
        const left = keys.has("ArrowLeft") || keys.has("KeyA");
        const right = keys.has("ArrowRight") || keys.has("KeyD");
        if (left || right) {
            paddleX += ((right ? 1 : 0) - (left ? 1 : 0)) * PADDLE_SPEED * dt;
            mouseX = null;
        } else if (mouseOn() && mouseX !== null) {
            const gap = mouseX - (paddleX + PADDLE_W / 2);
            paddleX += Math.sign(gap) * Math.min(Math.abs(gap), PADDLE_SPEED * 2.5 * dt);
        }
        paddleX = Math.max(0, Math.min(W - PADDLE_W, paddleX));
        if (state === "serve") stickBall();
    }

    /* -------------------------------------------------------- time -- */
    function frame(now) {
        if (!running) return;
        const dt = Math.min(0.05, (now - lastFrame) / 1000);
        lastFrame = now;
        if (state === "play" || state === "serve") movePaddle(dt);
        if (state === "play") moveBall(dt);
        draw();
        if (state === "play" || state === "serve") requestAnimationFrame(frame);
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

    function draw() {
        if (!canvas.width) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        for (const b of bricks) {
            const color = ROW_COLORS[b.row % ROW_COLORS.length];
            ctx.fillStyle = color;
            ctx.globalAlpha = b.hits > 1 ? 1 : 0.88;
            ctx.beginPath();
            ctx.roundRect(b.x, b.y, BRICK_W, BRICK_H, 4);
            ctx.fill();
            ctx.globalAlpha = 0.25;
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(b.x + 3, b.y + 2, BRICK_W - 6, 3);
            if (b.hits > 1) {
                // Two hits: ringed.
                ctx.globalAlpha = 0.9;
                ctx.strokeStyle = colors.fg;
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.roundRect(b.x + 2, b.y + 2, BRICK_W - 4, BRICK_H - 4, 3);
                ctx.stroke();
            }
            ctx.globalAlpha = 1;
        }
        // The paddle, and the ball.
        ctx.fillStyle = colors.fg;
        ctx.beginPath();
        ctx.roundRect(paddleX, PADDLE_Y, PADDLE_W, PADDLE_H, PADDLE_H / 2);
        ctx.fill();
        if (state !== "ready" && state !== "over" && state !== "cleared") {
            ctx.fillStyle = colors.accent;
            ctx.beginPath();
            ctx.arc(ball.x, ball.y, BALL_R, 0, Math.PI * 2);
            ctx.fill();
        }
        // Lives left, as balls in the corner.
        ctx.fillStyle = colors.accent;
        ctx.globalAlpha = 0.85;
        for (let i = 0; i < lives - (state === "play" || state === "serve" || state === "paused" ? 1 : 0); i++) {
            ctx.beginPath();
            ctx.arc(W - 20 - i * 20, 22, 5, 0, Math.PI * 2);
            ctx.fill();
        }
        ctx.globalAlpha = 1;
    }

    /* ------------------------------------------------------ states -- */
    function setState(next, how = {}) {
        state = next;
        const shown = next === "ready" || next === "paused" || next === "over" || next === "cleared";
        $("breakout-overlay").hidden = !shown;
        court.classList.toggle("idle", shown);
        court.classList.toggle("mouse", mouseOn() && (next === "play" || next === "serve"));
        if (next === "ready") {
            $("breakout-overlay-title").textContent = "Breakout";
            $("breakout-overlay-text").textContent = "Press Space or click to start";
        } else if (next === "paused") {
            $("breakout-overlay-title").textContent = "Paused";
            $("breakout-overlay-text").textContent = "Press Space or click to carry on";
        } else if (next === "cleared") {
            $("breakout-overlay-title").textContent = `Level ${level} cleared!`;
            $("breakout-overlay-text").textContent = `Press Space or click for level ${level + 1}`;
        } else if (next === "over") {
            $("breakout-overlay-title").textContent = how.record ? "New best!" : "Game over";
            $("breakout-overlay-text").textContent = `You scored ${score}. Press Space or click to play again`;
        }
        $("breakout-serve").hidden = next !== "serve";
        if (next === "play" || next === "serve") run();
        draw();
    }

    function pause() {
        if (state === "play" || state === "serve") {
            resumeTo = state;
            keys.clear();
            setState("paused");
        }
    }

    function go() {
        gameSound.prime();
        court.focus({preventScroll: true});
        if (state === "ready") setState("serve");
        else if (state === "over") { newGame(); setState("serve"); }
        else if (state === "cleared") nextLevel();
        else if (state === "paused") setState(resumeTo);
        else if (state === "serve") launch();
    }

    function reset() {
        if ((state === "play" || state === "serve" || state === "paused" || state === "cleared") && score > 0) {
            send("result", {game: "breakout", score, level});
        }
        newGame();
        setState("ready");
    }

    /* ------------------------------------------------------- input -- */
    document.addEventListener("keydown", e => {
        if (gameTab !== "breakout" || e.ctrlKey || e.altKey || e.metaKey) return;
        if (e.target.closest && e.target.closest("input, select, textarea, button, .modal, .dropdown-pop")) return;
        if (["ArrowLeft", "ArrowRight", "KeyA", "KeyD"].includes(e.code)) {
            e.preventDefault();
            keys.add(e.code);
        } else if (e.code === "Space" || e.code === "Enter") {
            e.preventDefault();
            if (!e.repeat) (state === "play" ? pause : go)();
        } else if (e.code === "Escape" || e.code === "KeyP") {
            pause();
        }
    });
    document.addEventListener("keyup", e => keys.delete(e.code));
    court.addEventListener("mousedown", e => {
        e.preventDefault();
        if (state !== "play") go();
        else court.focus({preventScroll: true});
    });
    court.addEventListener("mousemove", e => {
        const r = canvas.getBoundingClientRect();
        mouseX = (e.clientX - r.left) / r.width * W;
    });
    court.addEventListener("mouseleave", () => { mouseX = null; });

    addEventListener("blur", pause);
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    onTab(name => { if (name !== "breakout") pause(); else requestAnimationFrame(fit); });

    $("breakout-new").addEventListener("click", reset);
    onPrefs(key => {
        drawStats();
        if (key === "breakout_mouse") court.classList.toggle("mouse", mouseOn() && (state === "play" || state === "serve"));
    });

    function drawStats() {
        $("breakout-score").textContent = String(score);
        $("breakout-level-now").textContent = String(level);
        $("breakout-lives").textContent = String(Math.max(0, lives));
        $("breakout-best").replaceChildren(
            el("span.muted", {text: "Best"}), ` ${best.score || 0}`,
            ...(best.score ? ["  ·  ", el("span.muted", {text: "Level"}), ` ${best.level || 1}`] : []),
        );
    }

    Buddy.on("state", s => { best = {...(s.breakout_best || {})}; drawStats(); });
    Buddy.on("breakout_best", b => { best = {...b}; drawStats(); });
    Buddy.on("theme", () => requestAnimationFrame(() => { colors = gameColors(); draw(); }));
    new ResizeObserver(() => fit()).observe(court.parentElement);

    colors = gameColors();
    newGame();
    setState("ready");
    return {pause, go, get state() { return state; }, get score() { return score; }, get level() { return level; },
            get lives() { return lives; }, get bricks() { return bricks; }, ball,
            _paddle: x => { paddleX = x; }, _move: moveBall, _level: n => { level = n; buildLevel(); toServe(); }};
})();
