/* Runs in every page and frame of the Web tab, in Buddy's own script world
   (engine.py video_script), so the page can't see or change it.

   Chromium here has no H.264 or AAC decoder, so most sites' videos fail to
   load. This watches for a video that has, and offers to play it in Buddy's
   own player (video_window.py, which can): a "Play in Buddy" button over
   it - and when the video failed right after a click on it, straight away.
   It tells Buddy by a console message that starts with a secret the page
   doesn't know (TOKEN is filled in per run), so a page can't make Buddy
   open anything itself. */
(() => {
    if (window.__buddyVideo) return;
    window.__buddyVideo = true;
    const TOKEN = "__TOKEN__";
    const GESTURE_MS = 5000;        // a click this recent, on the video, that then failed: play it at once
    const MIN_W = 140, MIN_H = 80;

    let gesture = {t: 0, x: -1, y: -1};
    addEventListener("pointerdown", e => {
        if (e.isTrusted) gesture = {t: Date.now(), x: e.clientX, y: e.clientY};
    }, true);

    const overlays = new Map();     // video -> {host, button}
    const opened = new WeakSet();
    let frame = 0;

    const videos = () => {
        const found = [];
        const walk = root => {
            root.querySelectorAll("video").forEach(v => found.push(v));
            root.querySelectorAll("*").forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
        };
        walk(document);
        return found;
    };

    // The address Buddy's player should open: what the video was told to
    // play, else its best-looking <source> (a plain mp4 over a stream).
    const address = v => {
        const own = v.currentSrc || v.getAttribute("src") || "";
        let pick = own;
        if (!pick) {
            const sources = [...v.querySelectorAll("source")].filter(s => s.getAttribute("src"));
            const plain = sources.find(s => /mp4|quicktime/i.test(s.type || "") && !/codecs/i.test(s.type || ""));
            pick = (plain || sources.find(s => /mp4|webm|quicktime/i.test(s.type || "")) || sources[0] || {}).src || "";
        }
        try {
            const url = new URL(pick, document.baseURI);
            return /^https?:$/.test(url.protocol) ? url.href : "";
        } catch (e) { return ""; }
    };

    const failed = v => {
        if (v.error) return v.error.code === 4;                   // MEDIA_ERR_SRC_NOT_SUPPORTED
        const hasSource = v.currentSrc || v.getAttribute("src") || v.querySelector("source[src]");
        return v.networkState === 3 && !!hasSource;               // NETWORK_NO_SOURCE: none could be used
    };

    const open = v => {
        const url = address(v);
        if (!url) return;
        let title = document.title || "";
        try { title = top.document.title || title; } catch (e) { /* another site's frame */ }
        try { v.pause(); } catch (e) { /* nothing to pause */ }
        console.info(TOKEN + JSON.stringify({url, title, time: v.currentTime || 0}));
    };

    const inside = (rect, x, y) => x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;

    function makeOverlay(v) {
        const host = document.createElement("div");
        host.style.cssText = "position:fixed;z-index:2147483647;left:0;top:0;width:0;height:0;pointer-events:none;";
        const root = host.attachShadow({mode: "closed"});
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "▶  Play in Buddy";
        button.title = "This video needs a decoder the Web tab doesn't have - Buddy's player has it";
        button.style.cssText = "all:initial;position:fixed;pointer-events:auto;cursor:pointer;font:600 13px 'Segoe UI',sans-serif;" +
            "color:#fff;background:rgba(20,20,24,.86);border:1px solid rgba(255,255,255,.35);border-radius:999px;" +
            "padding:7px 14px;box-shadow:0 2px 10px rgba(0,0,0,.45);";
        button.addEventListener("click", e => {
            if (!e.isTrusted) return;
            e.preventDefault();
            e.stopPropagation();
            open(v);
        });
        // Not a press that reaches the page's own player underneath.
        for (const type of ["pointerdown", "mousedown", "mouseup", "dblclick"]) {
            button.addEventListener(type, e => e.stopPropagation());
        }
        root.append(button);
        (document.documentElement || document.body).append(host);
        return {host, button};
    }

    function place() {
        frame = 0;
        for (const [v, o] of overlays) {
            const r = v.getBoundingClientRect();
            const visible = v.isConnected && r.width >= MIN_W && r.height >= MIN_H && r.bottom > 0 && r.top < innerHeight &&
                r.right > 0 && r.left < innerWidth;
            o.button.style.display = visible ? "" : "none";
            if (visible) {
                o.button.style.left = `${Math.max(4, Math.min(r.right - o.button.offsetWidth - 10, innerWidth - o.button.offsetWidth - 4))}px`;
                o.button.style.top = `${Math.max(4, r.top + 10)}px`;
            }
        }
        if (overlays.size) frame = requestAnimationFrame(place);
    }

    function scan() {
        if (document.hidden) return;
        const live = new Set();
        for (const v of videos()) {
            if (!failed(v) || !address(v)) continue;
            const r = v.getBoundingClientRect();
            if (r.width < MIN_W || r.height < MIN_H) continue;
            live.add(v);
            if (!overlays.has(v)) overlays.set(v, makeOverlay(v));
            // It failed right after a click on it: that was the user pressing play.
            if (!opened.has(v) && Date.now() - gesture.t < GESTURE_MS &&
                inside(r, gesture.x, gesture.y)) {
                opened.add(v);
                open(v);
            }
        }
        for (const [v, o] of [...overlays]) {
            if (!live.has(v)) { o.host.remove(); overlays.delete(v); }
        }
        if (overlays.size && !frame) frame = requestAnimationFrame(place);
    }

    setInterval(scan, 700);
})();
