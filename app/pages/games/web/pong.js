/* Pong (loaded after games.js, whose helpers it uses): you against the
   computer, or two players on one keyboard. The court is 800 x 500 units
   drawn to fit; the game moves in fixed 1/240 s steps whatever the
   screen's refresh rate, and pauses itself when it can't be seen or
   Buddy loses the focus - a point never goes by while you're in Resolve. */
"use strict";

const pong = (() => {
    const W = 800, H = 500;
    const PADDLE_W = 12, PADDLE_H = 90, INSET = 26, BALL = 12;
    const PLAYER_SPEED = 560;            // units a second
    const SERVE_SPEED = 380, SPEED_UP = 1.06, MAX_SPEED = 1000;
    const MAX_ANGLE = Math.PI / 3;       // off a paddle's very end
    const SERVE_WAIT = 0.7;              // seconds before a serve leaves the middle
    const STEP = 1 / 240;
    // The computer: how fast its paddle moves, how far off its aim is, how
    // long it takes to see where the ball's going, and how often it misjudges
    // a shot altogether (more often the faster the ball) - so it can lose.
    const LEVELS = {
        easy: {speed: 260, error: 30, react: 0.28, miss: 0.35},
        medium: {speed: 380, error: 20, react: 0.16, miss: 0.17},
        hard: {speed: 540, error: 10, react: 0.07, miss: 0.07},
    };

    const court = $("pong-court"), canvas = $("pong-canvas"), ctx = canvas.getContext("2d");
    const left = {y: (H - PADDLE_H) / 2, score: 0};
    const right = {y: (H - PADDLE_H) / 2, score: 0};
    const ball = {x: (W - BALL) / 2, y: (H - BALL) / 2, vx: 0, vy: 0, speed: SERVE_SPEED};
    const keys = new Set();
    const ai = {target: H / 2, wait: 0, heading: 0, off: 0};
    let state = "ready";                 // ready, serve, play, paused, over
    let resumeTo = "serve";
    let serveLeft = 0, serveTo = 1;       // serve countdown; +1 toward the right, -1 the left
    let mouseY = null;
    let lastFrame = 0, carry = 0, running = false;
    let colors = {};
    let scale = 1;

    const mode = () => (prefs.pong_mode === "two" ? "two" : "cpu");
    const level = () => (LEVELS[prefs.pong_level] ? prefs.pong_level : "medium");
    const target = () => ([5, 7, 11].includes(+prefs.pong_points) ? +prefs.pong_points : 7);
    const soundOn = () => prefs.pong_sound !== false;
    const mouseOn = () => mode() === "cpu" && prefs.pong_mouse === true;   // off unless ticked

    /* -------------------------------------------------------- sound -- */
    const primeAudio = gameSound.prime;
    const blip = (freq, length, type) => gameSound.blip(soundOn(), freq, length, type);

    /* --------------------------------------------------- the match -- */
    function newMatch(start) {
        left.score = right.score = 0;
        left.y = right.y = (H - PADDLE_H) / 2;
        centreBall();
        serveTo = Math.random() < 0.5 ? -1 : 1;
        if (start) beginServe();
        else setState("ready");
    }

    function centreBall() {
        Object.assign(ball, {x: (W - BALL) / 2, y: (H - BALL) / 2, vx: 0, vy: 0, speed: SERVE_SPEED});
    }

    function beginServe() {
        centreBall();
        serveLeft = SERVE_WAIT;
        setState("serve");
    }

    function launch() {
        const angle = (Math.random() * 2 - 1) * (Math.PI / 7);
        ball.vx = Math.cos(angle) * ball.speed * serveTo;
        ball.vy = Math.sin(angle) * ball.speed;
        aimAi(true);
    }

    function point(toLeft) {
        (toLeft ? left : right).score += 1;
        blip(toLeft ? 523 : 196, 0.22, "triangle");
        const winner = left.score >= target() ? "left" : right.score >= target() ? "right" : null;
        if (winner) {
            centreBall();
            setState("over", winner);
            if (mode() === "cpu") send("result", {game: "pong", difficulty: level(), won: winner === "left"});
            return;
        }
        serveTo = toLeft ? 1 : -1;        // toward whoever just lost the point
        beginServe();
    }

    /* ---------------------------------------------- the computer -- */
    function predictY() {
        // Where the ball's middle crosses the right paddle's face, walls and all.
        const face = W - INSET - PADDLE_W;
        if (ball.vx <= 0) return H / 2;
        const t = (face - (ball.x + BALL)) / ball.vx;
        const span = H - BALL;
        let y = ball.y + ball.vy * Math.max(0, t);
        y = ((y % (2 * span)) + 2 * span) % (2 * span);
        if (y > span) y = 2 * span - y;
        return y + BALL / 2;
    }

    function aimAi(fresh) {
        const lv = LEVELS[level()];
        const meets = ball.vx > 0 ? predictY() : H / 2;
        if (fresh) {
            // Once per shot: where on the paddle it means to meet the ball -
            // or, now and then, a misjudgement that leaves the paddle short
            // (away from the nearer wall, where the paddle could still reach).
            ai.wait = lv.react;
            ai.heading = Math.sign(ball.vx);
            ai.off = (Math.random() * 2 - 1) * lv.error;
            if (ball.vx > 0 && Math.random() < lv.miss * (0.5 + ball.speed / MAX_SPEED)) {
                ai.off = (meets < H / 2 ? 1 : -1) * (PADDLE_H / 2 + BALL + 8 + Math.random() * 30);
            }
        }
        ai.target = meets + ai.off;
    }

    function moveAi(dt) {
        if (Math.sign(ball.vx) !== ai.heading) aimAi(true);
        if (ai.wait > 0) {
            ai.wait -= dt;
            if (ai.wait <= 0) aimAi(false);
            return;
        }
        const lv = LEVELS[level()];
        const centre = right.y + PADDLE_H / 2;
        const gap = ai.target - centre;
        if (Math.abs(gap) > 3) right.y += Math.sign(gap) * Math.min(Math.abs(gap), lv.speed * dt);
    }

    /* --------------------------------------------------- movement -- */
    const clampPaddle = p => { p.y = Math.max(0, Math.min(H - PADDLE_H, p.y)); };

    function movePlayers(dt) {
        const two = mode() === "two";
        const up = two ? keys.has("KeyW") : keys.has("KeyW") || keys.has("ArrowUp");
        const down = two ? keys.has("KeyS") : keys.has("KeyS") || keys.has("ArrowDown");
        if (up || down) {
            left.y += ((down ? 1 : 0) - (up ? 1 : 0)) * PLAYER_SPEED * dt;
            mouseY = null;
        } else if (mouseOn() && mouseY !== null) {
            const gap = mouseY - (left.y + PADDLE_H / 2);
            left.y += Math.sign(gap) * Math.min(Math.abs(gap), PLAYER_SPEED * 2.2 * dt);
        }
        if (two) {
            const up2 = keys.has("ArrowUp"), down2 = keys.has("ArrowDown");
            right.y += ((down2 ? 1 : 0) - (up2 ? 1 : 0)) * PLAYER_SPEED * dt;
        } else {
            moveAi(dt);
        }
        clampPaddle(left);
        clampPaddle(right);
    }

    function hitPaddle(p, x, dir) {
        // The ball meets a paddle's face while heading for it.
        const top = p.y - BALL, bottom = p.y + PADDLE_H;
        if (ball.y < top || ball.y > bottom) return false;
        const rel = Math.max(-1, Math.min(1, ((ball.y + BALL / 2) - (p.y + PADDLE_H / 2)) / (PADDLE_H / 2 + BALL / 2)));
        ball.speed = Math.min(MAX_SPEED, ball.speed * SPEED_UP);
        ball.vx = Math.cos(rel * MAX_ANGLE) * ball.speed * dir;
        ball.vy = Math.sin(rel * MAX_ANGLE) * ball.speed;
        ball.x = x;
        blip(dir > 0 ? 440 : 494);
        return true;
    }

    function step(dt) {
        movePlayers(dt);
        if (state === "serve") {
            serveLeft -= dt;
            if (serveLeft <= 0) {
                setState("play");
                launch();
            }
            return;
        }
        if (state !== "play") return;
        ball.x += ball.vx * dt;
        ball.y += ball.vy * dt;
        if (ball.y < 0) { ball.y = -ball.y; ball.vy = Math.abs(ball.vy); blip(330, 0.04); }
        if (ball.y > H - BALL) { ball.y = 2 * (H - BALL) - ball.y; ball.vy = -Math.abs(ball.vy); blip(330, 0.04); }
        const leftFace = INSET + PADDLE_W, rightFace = W - INSET - PADDLE_W;
        if (ball.vx < 0 && ball.x <= leftFace && ball.x + BALL >= INSET) hitPaddle(left, leftFace, 1);
        else if (ball.vx > 0 && ball.x + BALL >= rightFace && ball.x <= W - INSET) hitPaddle(right, rightFace - BALL, -1);
        if (ball.x + BALL < 0) point(false);
        else if (ball.x > W) point(true);
    }

    /* ----------------------------------------------------- drawing -- */
    function readColors() {
        colors = gameColors();
        draw();
    }

    function fit() {
        if (court.offsetParent === null) return;    // its tab isn't showing
        scale = fitCourt(court, canvas, W, H);
        draw();
    }

    function draw() {
        if (!canvas.width) return;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        // The net.
        ctx.fillStyle = colors.dim;
        ctx.globalAlpha = 0.45;
        for (let y = 8; y < H; y += 26) ctx.fillRect(W / 2 - 2, y, 4, 14);
        // The scores.
        ctx.globalAlpha = 0.55;
        ctx.font = `700 64px ${colors.font}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(String(left.score), W / 2 - 90, 24);
        ctx.fillText(String(right.score), W / 2 + 90, 24);
        ctx.globalAlpha = 1;
        // Paddles and ball.
        ctx.fillStyle = colors.fg;
        ctx.fillRect(INSET, left.y, PADDLE_W, PADDLE_H);
        ctx.fillRect(W - INSET - PADDLE_W, right.y, PADDLE_W, PADDLE_H);
        if (state === "play" || state === "serve" || state === "paused") {
            ctx.fillStyle = colors.accent;
            ctx.fillRect(ball.x, ball.y, BALL, BALL);
        }
    }

    function frame(now) {
        if (!running) return;
        const dt = Math.min(0.05, (now - lastFrame) / 1000);
        lastFrame = now;
        carry += dt;
        while (carry >= STEP) {
            step(STEP);
            carry -= STEP;
            if (state !== "play" && state !== "serve") { carry = 0; break; }
        }
        draw();
        if (state === "play" || state === "serve") requestAnimationFrame(frame);
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
    function setState(next, winner) {
        state = next;
        const shown = next === "ready" || next === "paused" || next === "over";
        $("pong-overlay").hidden = !shown;
        court.classList.toggle("idle", shown);
        if (next === "ready") {
            $("pong-overlay-title").textContent = "Pong";
            $("pong-overlay-text").textContent = "Press Space or click to start";
        } else if (next === "paused") {
            $("pong-overlay-title").textContent = "Paused";
            $("pong-overlay-text").textContent = "Press Space or click to carry on";
        } else if (next === "over") {
            $("pong-overlay-title").textContent = mode() === "cpu"
                ? (winner === "left" ? "You win!" : "The computer wins")
                : (winner === "left" ? "Left player wins" : "Right player wins");
            $("pong-overlay-text").textContent = "Press Space or click to play again";
        }
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
        primeAudio();
        court.focus({preventScroll: true});
        if (state === "ready" || state === "over") newMatch(true);
        else if (state === "paused") setState(resumeTo);
    }

    /* ------------------------------------------------------- input -- */
    const GAME_KEYS = new Set(["KeyW", "KeyS", "ArrowUp", "ArrowDown"]);

    document.addEventListener("keydown", e => {
        if (gameTab !== "pong" || e.ctrlKey || e.altKey || e.metaKey) return;
        if (e.target.closest && e.target.closest("input, select, textarea, button, .modal, .dropdown-pop")) return;
        if (GAME_KEYS.has(e.code)) {
            e.preventDefault();
            keys.add(e.code);
            if (state === "play" || state === "serve") primeAudio();
        } else if (e.code === "Space" || e.code === "Enter") {
            e.preventDefault();
            if (!e.repeat) (state === "play" || state === "serve" ? pause : go)();
        } else if (e.code === "Escape" || e.code === "KeyP") {
            pause();
        }
    });
    document.addEventListener("keyup", e => keys.delete(e.code));
    court.addEventListener("mousedown", e => {
        e.preventDefault();
        if (state !== "play" && state !== "serve") go();
        else court.focus({preventScroll: true});
    });
    court.addEventListener("mousemove", e => {
        const r = court.getBoundingClientRect();
        mouseY = (e.clientY - r.top) / r.height * H;
    });
    court.addEventListener("mouseleave", () => { mouseY = null; });

    // Out of sight or out of focus: paused, so no point goes by unseen.
    addEventListener("blur", () => { keys.clear(); pause(); });
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    onTab(name => { if (name !== "pong") pause(); else requestAnimationFrame(fit); });

    $("pong-mode").addEventListener("click", e => {
        const b = e.target.closest("button[data-mode]");
        if (b && b.dataset.mode !== mode()) setPref("pong_mode", b.dataset.mode);
    });
    $("pong-new").addEventListener("click", () => newMatch(false));

    function drawOptions() {
        for (const b of document.querySelectorAll("#pong-mode button")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode()));
        $("pong-level-wrap").hidden = mode() !== "cpu";
        $("pong-mouse-wrap").hidden = mode() !== "cpu";
        court.classList.toggle("mouse", mouseOn());
        if (!mouseOn()) mouseY = null;
        $("pong-keys").textContent = mode() !== "cpu"
            ? "Left player: W and S. Right player: the arrow keys. Space pauses."
            : mouseOn() ? "Move with W and S, the arrow keys or the mouse. Space pauses."
                        : "Move with W and S or the arrow keys. Space pauses.";
        $("pong-record").hidden = mode() !== "cpu";
    }

    onPrefs(key => {
        drawOptions();
        // A different game to play: a fresh match, from the start screen.
        if (key === "pong_mode" || key === "pong_level" || key === "pong_points") newMatch(false);
    });

    function drawRecord(record) {
        const parts = [el("span", {text: "Your record against the computer"}), ": "];
        ["easy", "medium", "hard"].forEach((lv, i) => {
            const r = (record || {})[lv] || {won: 0, lost: 0};
            if (i) parts.push(" · ");
            parts.push(el("span.muted", {text: {easy: "Easy", medium: "Medium", hard: "Hard"}[lv]}), ` ${r.won}–${r.lost}`);
        });
        $("pong-record").replaceChildren(...parts);
    }

    Buddy.on("state", s => drawRecord(s.record));
    Buddy.on("record", drawRecord);
    Buddy.on("theme", () => requestAnimationFrame(readColors));
    new ResizeObserver(() => fit()).observe(court.parentElement);

    readColors();
    drawOptions();
    drawRecord(null);
    setState("ready");
    return {pause, go, get state() { return state; }, get score() { return [left.score, right.score]; },
            _ball: ball, _left: left, _right: right, _step: step};
})();
