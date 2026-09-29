/* Dailies: the strip under the player, and the transcript beside it.

   The strip draws the player's position two ways (the "viewer" setting):
   the clip on its own, or the whole source tape laid end to end, each clip
   at its place on it. Pressing or dragging on it moves the playhead there -
   seek_to names a clip and a time in it, and Python opens that clip if it
   isn't the one playing. The page's position messages are always a time
   inside the current clip; everything about where that sits on the tape
   is worked out here, from the tape rows' lengths. */
"use strict";

// Resolve's clip colours, as its own picker draws them.
const CLIP_COLORS = {
    Orange: "#E8742B", Apricot: "#F0A052", Yellow: "#E2C33D", Lime: "#9FC43A",
    Olive: "#76902F", Green: "#3F9E4D", Teal: "#2FA39A", Navy: "#2B4F8F",
    Blue: "#4A83D8", Purple: "#8C5BC9", Violet: "#B066C9", Pink: "#E0709F",
    Tan: "#C9A77C", Beige: "#D8C9A8", Brown: "#8A5A3A", Chocolate: "#6B4331",
};

function clockText(seconds, long = false) {
    const s = Math.max(0, Math.floor(seconds + 1e-6));
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = s % 60;
    const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
    return h || long ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

const Strip = (() => {
    const {el, send} = Buddy;
    const $ = id => document.getElementById(id);
    const strip = $("strip"), ruler = $("ruler"), blocks = $("blocks"), playhead = $("playhead");
    let clips = [], starts = [], total = 0;
    let viewer = "clip", currentId = null, playing = false;
    let local = 0, localLength = 0;         // seconds, inside the current clip
    let view = null;                        // [from, to] seconds shown; null = fit
    let wholeTape = false;                  // zoomed right out by hand: leave it there
    // A tape of hundreds of clips fitted to the strip is hundreds of slivers,
    // so a long one opens on the clips around the current one instead.
    const FIT_CLIPS = 24, BEFORE = 2, AFTER = 9;
    let dragging = null;                    // {id, seconds} while the pointer is down
    let seekTimer = 0;
    const onChange = [];

    const index = () => clips.findIndex(c => c.id === currentId);
    const clipLength = i => {
        const c = clips[i];
        return c ? (c.id === currentId && localLength > 0 ? localLength : c.seconds || 0) : 0;
    };
    // The span the strip covers, and where the current clip starts on it.
    const span = () => viewer === "source" ? total : Math.max(clipLength(index()), 0.001);
    const origin = () => viewer === "source" && index() >= 0 ? starts[index()] : 0;
    const shown = () => view || [0, span()];

    function opening() {
        const i = index();
        if (viewer !== "source" || wholeTape || clips.length <= FIT_CLIPS || i < 0) return null;
        const first = Math.max(0, i - BEFORE), last = Math.min(clips.length - 1, i + AFTER);
        return [starts[first], starts[last] + (clips[last].seconds || 0)];
    }
    function layout() {
        starts = []; total = 0;
        for (const c of clips) { starts.push(total); total += Math.max(0, c.seconds || 0); }
    }
    function x(seconds) {
        const [from, to] = shown();
        return (seconds - from) / Math.max(to - from, 1e-6) * strip.clientWidth;
    }
    function secondsAt(clientX) {
        const [from, to] = shown();
        const rect = strip.getBoundingClientRect();
        return from + (clientX - rect.left) / Math.max(rect.width, 1) * (to - from);
    }
    // Which clip, and how far into it, a time on the strip is.
    function locate(seconds) {
        if (viewer !== "source") {
            return currentId ? {id: currentId, at: Math.min(Math.max(seconds, 0), span())} : null;
        }
        if (!clips.length) return null;
        let i = starts.findLastIndex(start => start <= seconds);
        i = Math.max(0, i);
        return {id: clips[i].id, at: Math.min(Math.max(seconds - starts[i], 0), clips[i].seconds || 0)};
    }

    function drawRuler() {
        const [from, to] = shown();
        const width = strip.clientWidth || 1;
        const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
        const step = steps.find(s => s / (to - from) * width >= 72) || steps.at(-1);
        const long = span() >= 3600;     // matching the time readout
        const ticks = [];
        for (let t = Math.ceil(from / step) * step; t <= to; t += step) {
            ticks.push(el("span.tick", {text: clockText(t, long), style: `left:${x(t)}px`}));
        }
        ruler.replaceChildren(...ticks);
    }
    function drawBlocks() {
        const [from, to] = shown();
        const items = [];
        const rows = viewer === "source" ? clips.map((c, i) => [c, starts[i], c.seconds || 0])
            : index() >= 0 ? [[clips[index()], 0, span()]] : [];
        for (const [clip, start, length] of rows) {
            if (start + length < from || start > to) continue;
            const left = x(start), width = Math.max(1, x(start + length) - left);
            const block = el("div.block", {title: `${clip.name} · ${clockText(length)}`}, [
                el("span.block-name", {text: clip.name, translate: "no"}),
            ]);
            block.style.left = `${left}px`;
            block.style.width = `${width}px`;
            if (clip.id === currentId) block.classList.add("current");
            if (CLIP_COLORS[clip.color]) block.style.setProperty("--clip-color", CLIP_COLORS[clip.color]);
            if (width < 28) block.classList.add("narrow");
            items.push(block);
        }
        blocks.replaceChildren(...items);
    }
    function drawPlayhead() {
        const seconds = origin() + (dragging && dragging.id === currentId ? dragging.at : local);
        const px = x(dragging && viewer === "source" ? dragging.seconds : seconds);
        playhead.style.transform = `translateX(${Math.round(px)}px)`;
        playhead.hidden = !currentId || px < -1 || px > strip.clientWidth + 1;
    }
    function draw() {
        drawRuler(); drawBlocks(); drawPlayhead();
        for (const fn of onChange) fn();
    }

    // Keeps the playhead on screen while playing, and the chosen clip on
    // screen when it changes, without undoing a zoom.
    function follow(seconds, margin = 0.1) {
        if (!view) return;
        const [from, to] = view, width = to - from;
        if (seconds < from || seconds > to) {
            const start = Math.min(Math.max(seconds - width * margin, 0), Math.max(span() - width, 0));
            view = [start, start + width];
        }
    }

    function seek(target, final) {
        if (!target) return;
        clearTimeout(seekTimer);
        const go = () => send("seek_to", {id: target.id, ms: Math.round(target.at * 1000)});
        // Dragging inside the playing clip seeks as it goes; onto another
        // clip, only where the drag ends - every clip crossed would be
        // opened otherwise.
        if (final) go();
        else if (target.id === currentId) seekTimer = setTimeout(go, 60);
    }
    strip.addEventListener("pointerdown", e => {
        if (e.button !== 0 || !clips.length) return;
        strip.setPointerCapture(e.pointerId);
        const seconds = secondsAt(e.clientX);
        dragging = {seconds, ...locate(seconds)};
        drawPlayhead();
        seek(dragging, false);
    });
    strip.addEventListener("pointermove", e => {
        if (!dragging) return;
        const seconds = secondsAt(e.clientX);
        dragging = {seconds, ...locate(seconds)};
        drawPlayhead();
        seek(dragging, false);
    });
    const release = () => {
        if (!dragging) return;
        const target = dragging;
        dragging = null;
        if (target.id === currentId) local = target.at;
        seek(target, true);
        drawPlayhead();
    };
    strip.addEventListener("pointerup", release);
    strip.addEventListener("pointercancel", release);

    // Alt+scroll zooms around the pointer; scrolling (or Shift+scroll, or a
    // sideways swipe) pans. A plain scroll over a strip with nothing to pan
    // is left to the page.
    strip.addEventListener("wheel", e => {
        if (!clips.length) return;
        const [from, to] = shown(), width = to - from, full = span();
        if (e.altKey) {
            e.preventDefault();
            const at = secondsAt(e.clientX);
            // Alt turns a vertical wheel into a horizontal one on some systems.
            const amount = e.deltaY || e.deltaX;
            const next = Math.min(Math.max(width * Math.exp(amount * 0.0015), Math.min(2, full)), full);
            if (next >= full - 1e-6) {
                view = null;
                wholeTape = viewer === "source";
            } else {
                const start = Math.min(Math.max(at - (at - from) * next / width, 0), full - next);
                view = [start, start + next];
            }
            draw();
            return;
        }
        if (!view) return;
        e.preventDefault();
        const pan = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        const shift = pan / Math.max(strip.clientWidth, 1) * width;
        const start = Math.min(Math.max(from + shift, 0), Math.max(full - width, 0));
        view = [start, start + width];
        draw();
    }, {passive: false});
    new ResizeObserver(draw).observe(strip);

    return {
        onChange: fn => onChange.push(fn),
        setTape(rows) {
            clips = rows || [];
            layout();
            if (view && view[1] > span()) view = null;
            if (!view) view = opening();
            draw();
        },
        setViewer(value) {
            if (value === viewer) return;
            viewer = value;
            view = opening();
            draw();
        },
        setCurrent(id) {
            if (id === currentId) return;
            currentId = id;
            local = 0;
            localLength = 0;
            if (viewer === "clip") view = null;
            else if (!view) view = opening();
            else if (index() >= 0) follow(starts[index()], BEFORE / (BEFORE + AFTER + 1));
            draw();
        },
        setPlaying(value) { playing = value; },
        setPosition(ms, durationMs) {
            local = Math.max(0, ms / 1000);
            const length = durationMs > 0 ? durationMs / 1000 : 0;
            if (length && Math.abs(length - localLength) > 0.05) {
                localLength = length;
                if (viewer === "clip") { draw(); }
            }
            if (playing && !dragging) follow(origin() + local);
            if (view && playing) { drawRuler(); drawBlocks(); }
            drawPlayhead();
            for (const fn of onChange) fn();
        },
        // "00:12 / 01:40", in the strip's own terms.
        time() {
            if (!currentId) return "00:00 / 00:00";
            const long = span() >= 3600;
            return `${clockText(origin() + local, long)} / ${clockText(span(), long)}`;
        },
        info() {
            if (viewer !== "source") return "";
            const i = index();
            return clips.length ? `${clips.length} clip${clips.length === 1 ? "" : "s"} · ${clockText(total, total >= 3600)}`
                + (i >= 0 ? ` · clip ${i + 1}` : "") : "";
        },
        localSeconds: () => local,
    };
})();

const Transcript = (() => {
    const {el, send} = Buddy;
    const $ = id => document.getElementById(id);
    const body = $("transcript-body"), status = $("transcript-status");
    let clipId = null, words = [], lastIndex = -1, scrolledAt = 0;
    body.addEventListener("wheel", () => { scrolledAt = performance.now(); }, {passive: true});

    function wordsOf(segment) {
        const list = segment.words?.length ? segment.words
            : [{start: segment.start, end: segment.end, word: segment.text || ""}];
        return list.map(w => ({start: +w.start || 0, end: +w.end || 0, text: String(w.word || "")}));
    }
    function append(segment) {
        const line = el("span.segment");
        for (const w of wordsOf(segment)) {
            const span = el("span.word.ahead", {text: w.text});
            span.addEventListener("click", () => { if (clipId) send("seek_to", {id: clipId, ms: Math.round(w.start * 1000)}); });
            w.el = span;
            words.push(w);
            line.append(span);
        }
        body.querySelector(".transcript-empty")?.remove();
        body.append(line);
        lastIndex = -1;
        mark(Strip.localSeconds());
    }
    function empty(text) {
        body.replaceChildren(el("div.transcript-empty.muted.small", {text}));
    }
    // Spoken, now, and still to come: the text reads as if it were being
    // written while the clip plays, and all of it can still be read ahead.
    function mark(seconds) {
        if (!words.length) return;
        let i = words.findLastIndex(w => w.start <= seconds + 0.05);
        if (i === lastIndex) return;
        const lo = Math.min(i, lastIndex), hi = Math.max(i, lastIndex);
        for (let k = Math.max(lo, 0); k <= hi && k < words.length; k++) {
            words[k].el.className = `word ${k < i ? "spoken" : k === i ? "now" : "ahead"}`;
        }
        if (lastIndex === -1) {
            words.forEach((w, k) => { w.el.className = `word ${k < i ? "spoken" : k === i ? "now" : "ahead"}`; });
        }
        lastIndex = i;
        if (i >= 0 && performance.now() - scrolledAt > 4000) words[i].el.scrollIntoView({block: "nearest"});
    }

    return {
        set(data) {
            const fresh = data.id !== clipId || !data.segments.length || data.segments.length !== body.querySelectorAll(".segment").length;
            clipId = data.id;
            $("transcribe-on").checked = !!data.on;
            $("transcript-to-log").disabled = !data.segments.length;
            const count = data.segments.reduce((n, s) => n + wordsOf(s).length, 0);
            status.textContent = !data.id ? "" : !data.on ? "Off" :
                data.status === "running" ? "Transcribing…" :
                data.status === "done" ? (count ? `${count} words` : "No speech found") :
                data.status === "error" ? "Couldn't transcribe" : "";
            if (fresh) {
                words = []; lastIndex = -1;
                body.replaceChildren();
                for (const segment of data.segments) append(segment);
            }
            if (!data.segments.length) {
                empty(!data.id ? "Choose a clip to see what's said in it." :
                    data.message ? data.message :
                    !data.on ? "Transcription is off. Tick Transcribe clips to have each clip's speech written out here." :
                    data.status === "running" ? "Listening…" :
                    data.status === "done" ? "No speech in this clip." : "");
            }
        },
        add(data) {
            if (data.id !== clipId) return;
            append(data.segment);
            $("transcript-to-log").disabled = false;
        },
        time: seconds => mark(seconds),
    };
})();
