#!/usr/bin/env python3
"""
Games - a main tab of its own, under its own heading, with a sub-tab per
game (web/index.html): Pong (web/pong.js), against the computer or two
players on one keyboard, Snake (web/snake.js), Minesweeper (web/mines.js),
Solitaire (web/solitaire.js, Klondike), Falling blocks (web/blocks.js),
2048 (web/g2048.js), Breakout (web/breakout.js) and Sudoku
(web/sudoku.js).

Each game runs entirely in the view. Python keeps what should outlast a
restart: the open sub-tab, each game's settings (prefs, as Essentials
keeps its fields), Pong's record against the computer, Snake's best
score for each speed, walls on or off, Minesweeper's best time for
each difficulty, Solitaire's games won and best time for each deal, the
best Falling blocks, 2048 and Breakout scores, and Sudoku's puzzles
solved and best time for each difficulty.

Never touches Resolve or the network.

Protocol:
    to the view    state, record, snake_best, mines_best, solitaire_stats,
                   blocks_best, best_2048, breakout_best, sudoku_stats
    from the view  tab, pref, result
"""

import os

from core.web_page import WebToolPage

TABS = ("pong", "snake", "mines", "solitaire", "blocks", "2048", "breakout", "sudoku")
SOLITAIRE_MODES = ("draw1", "draw3")
MINES_LEVELS = ("beginner", "intermediate", "expert")
MAX_MINES_SECONDS = 24 * 3600
DIFFICULTIES = ("easy", "medium", "hard")
SNAKE_KEYS = tuple(f"{speed}_{edge}" for speed in ("slow", "normal", "fast") for edge in ("walls", "wrap"))
MAX_SNAKE_SCORE = 32 * 20       # every square of the board
SUDOKU_LEVELS = ("easy", "medium", "hard")
MAX_SCORE = 100_000_000
MAX_2048_TILE = 1 << 17         # the biggest tile a 4 x 4 board can hold
DEFAULTS = {"tab": "pong", "prefs": {}, "pong_record": {}, "snake_best": {}, "mines_best": {},
            "solitaire_stats": {}, "blocks_best": {}, "best_2048": {}, "breakout_best": {}, "sudoku_stats": {}}
MAX_PREFS = 40
MAX_PREF_KEY = 40
MAX_PREF_TEXT = 100
MAX_COUNT = 1_000_000


def clean_pref(value):
    """A pref is one plain value - a choice, a number or a tick."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if value == value and abs(value) < 1e9 else None
    if isinstance(value, str):
        return value[:MAX_PREF_TEXT]
    return None


def clean_record(raw) -> dict:
    """Pong against the computer: {difficulty: {"won": n, "lost": n}}."""
    out = {}
    for level in DIFFICULTIES:
        entry = raw.get(level) if isinstance(raw, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        out[level] = {key: min(MAX_COUNT, value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                      else 0 for key, value in ((k, entry.get(k)) for k in ("won", "lost"))}
    return out


def clean_snake_best(raw) -> dict:
    """Snake's best score for each speed and walls setting ("normal_walls")."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw[key] for key in SNAKE_KEYS
            if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) and 0 < raw[key] <= MAX_SNAKE_SCORE}


def clean_seconds(value):
    """A Minesweeper time: seconds to a tenth, or None if it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_MINES_SECONDS:
        return None
    return round(float(value), 1)


def clean_mines_best(raw) -> dict:
    """Minesweeper's best time for each difficulty."""
    raw = raw if isinstance(raw, dict) else {}
    return {level: clean_seconds(raw[level]) for level in MINES_LEVELS if clean_seconds(raw.get(level)) is not None}


def clean_count(value, top=MAX_SCORE):
    """A whole number from 0 to `top`, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= top:
        return None
    return value


def clean_best(raw, fields) -> dict:
    """A best score and what went with it: {"score": n, field: n, ...}
    ({} if there's no score yet). fields: {name: largest}."""
    raw = raw if isinstance(raw, dict) else {}
    score = clean_count(raw.get("score"))
    if not score:
        return {}
    out = {"score": score}
    for name, top in fields.items():
        value = clean_count(raw.get(name), top)
        if value is not None:
            out[name] = value
    return out


BLOCKS_FIELDS = {"lines": 1_000_000, "level": 100}
FIELDS_2048 = {"tile": MAX_2048_TILE}
BREAKOUT_FIELDS = {"level": 10_000}


def clean_solitaire_stats(raw) -> dict:
    """Solitaire, for each deal (draw one, draw three): games won and the best time."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for mode in SOLITAIRE_MODES:
        entry = raw.get(mode) if isinstance(raw.get(mode), dict) else {}
        won = entry.get("won")
        won = min(MAX_COUNT, won) if isinstance(won, int) and not isinstance(won, bool) and won > 0 else 0
        best = clean_seconds(entry.get("best"))
        if won:
            out[mode] = {"won": won, "best": best or 0}
    return out


def clean_sudoku_stats(raw) -> dict:
    """Sudoku, for each difficulty: puzzles solved and the best time."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for level in SUDOKU_LEVELS:
        entry = raw.get(level) if isinstance(raw.get(level), dict) else {}
        won = clean_count(entry.get("won"), MAX_COUNT)
        if won:
            out[level] = {"won": won, "best": clean_seconds(entry.get("best")) or 0}
    return out


class GamesPage(WebToolPage):
    tool_id = "games"
    display_name = "Games"
    category = "Games"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, dict(DEFAULTS))
        prefs = self.settings.get("prefs")
        self.prefs = {str(k)[:MAX_PREF_KEY]: clean_pref(v) for k, v in prefs.items()} if isinstance(prefs, dict) else {}
        self.record = clean_record(self.settings.get("pong_record"))
        self.snake_best = clean_snake_best(self.settings.get("snake_best"))
        self.mines_best = clean_mines_best(self.settings.get("mines_best"))
        self.solitaire_stats = clean_solitaire_stats(self.settings.get("solitaire_stats"))
        self.blocks_best = clean_best(self.settings.get("blocks_best"), BLOCKS_FIELDS)
        self.best_2048 = clean_best(self.settings.get("best_2048"), FIELDS_2048)
        self.breakout_best = clean_best(self.settings.get("breakout_best"), BREAKOUT_FIELDS)
        self.sudoku_stats = clean_sudoku_stats(self.settings.get("sudoku_stats"))

    def web_ready(self):
        tab = self.settings.get("tab")
        self.emit("state", {"tab": tab if tab in TABS else TABS[0], "prefs": self.prefs, "record": self.record,
                            "snake_best": self.snake_best, "mines_best": self.mines_best,
                            "solitaire_stats": self.solitaire_stats, "blocks_best": self.blocks_best,
                            "best_2048": self.best_2048, "breakout_best": self.breakout_best,
                            "sudoku_stats": self.sudoku_stats})

    def on_tab(self, payload):
        tab = (payload or {}).get("tab")
        if tab in TABS and tab != self.settings.get("tab"):
            self.settings["tab"] = tab
            self.settings.save()

    def on_pref(self, payload):
        payload = payload or {}
        key = payload.get("key")
        if not isinstance(key, str) or not key or len(key) > MAX_PREF_KEY:
            return
        if key not in self.prefs and len(self.prefs) >= MAX_PREFS:
            return
        value = clean_pref(payload.get("value"))
        if self.prefs.get(key) == value:
            return
        self.prefs[key] = value
        self.settings["prefs"] = dict(self.prefs)
        self.settings.save()

    def on_result(self, payload):
        """A game finished: Pong against the computer is one more won or
        lost; Snake's score and a Minesweeper win's time are kept if
        they're the best yet."""
        payload = payload or {}
        if payload.get("game") == "snake":
            return self._snake_result(payload)
        if payload.get("game") == "mines":
            return self._mines_result(payload)
        if payload.get("game") == "solitaire":
            return self._solitaire_result(payload)
        if payload.get("game") == "sudoku":
            return self._sudoku_result(payload)
        best = {"blocks": ("blocks_best", BLOCKS_FIELDS), "2048": ("best_2048", FIELDS_2048),
                "breakout": ("breakout_best", BREAKOUT_FIELDS)}.get(payload.get("game"))
        if best:
            return self._best_score(payload, *best)
        level, won = payload.get("difficulty"), payload.get("won")
        if payload.get("game") != "pong" or level not in DIFFICULTIES or not isinstance(won, bool):
            return
        entry = self.record[level]
        key = "won" if won else "lost"
        entry[key] = min(MAX_COUNT, entry[key] + 1)
        self.settings["pong_record"] = self.record
        self.settings.save()
        self.emit("record", self.record)

    def _snake_result(self, payload):
        speed, walls, score = payload.get("speed"), payload.get("walls"), payload.get("score")
        key = f"{speed}_{'walls' if walls else 'wrap'}"
        if (key not in SNAKE_KEYS or not isinstance(walls, bool) or not isinstance(score, int)
                or isinstance(score, bool) or not 0 < score <= MAX_SNAKE_SCORE):
            return
        if score > self.snake_best.get(key, 0):
            self.snake_best[key] = score
            self.settings["snake_best"] = dict(self.snake_best)
            self.settings.save()
        self.emit("snake_best", self.snake_best)

    def _mines_result(self, payload):
        """A board cleared (only wins are sent): the time, if it's the best."""
        level, seconds = payload.get("level"), clean_seconds(payload.get("seconds"))
        if level not in MINES_LEVELS or seconds is None:
            return
        if level not in self.mines_best or seconds < self.mines_best[level]:
            self.mines_best[level] = seconds
            self.settings["mines_best"] = dict(self.mines_best)
            self.settings.save()
        self.emit("mines_best", self.mines_best)

    def _solitaire_result(self, payload):
        """A game of Solitaire won (only wins are sent): one more won, and the time if it's the best."""
        draw, seconds = payload.get("draw"), clean_seconds(payload.get("seconds"))
        mode = f"draw{draw}" if draw in (1, 3) and not isinstance(draw, bool) else None
        if mode is None or seconds is None:
            return
        entry = self.solitaire_stats.get(mode, {"won": 0, "best": 0})
        self.solitaire_stats[mode] = {"won": min(MAX_COUNT, entry["won"] + 1),
                                      "best": seconds if not entry["best"] or seconds < entry["best"] else entry["best"]}
        self.settings["solitaire_stats"] = {k: dict(v) for k, v in self.solitaire_stats.items()}
        self.settings.save()
        self.emit("solitaire_stats", self.solitaire_stats)

    def _best_score(self, payload, key, fields):
        """Falling blocks, 2048 or Breakout over: kept if it's the best
        score yet. 2048 keeps its biggest tile whatever the score."""
        result = clean_best(payload, fields)
        if not result:
            return
        current = getattr(self, key)
        updated = dict(result) if result["score"] > current.get("score", 0) else dict(current)
        if key == "best_2048" and "tile" in result:
            updated["tile"] = max(result["tile"], current.get("tile", 0))
        if updated != current:
            setattr(self, key, updated)
            self.settings[key] = dict(updated)
            self.settings.save()
        self.emit(key, updated)

    def _sudoku_result(self, payload):
        """A puzzle solved: one more solved, and the time if it's the best."""
        level, seconds = payload.get("level"), clean_seconds(payload.get("seconds"))
        if level not in SUDOKU_LEVELS or seconds is None:
            return
        entry = self.sudoku_stats.get(level, {"won": 0, "best": 0})
        self.sudoku_stats[level] = {"won": min(MAX_COUNT, entry["won"] + 1),
                                    "best": seconds if not entry["best"] or seconds < entry["best"] else entry["best"]}
        self.settings["sudoku_stats"] = {k: dict(v) for k, v in self.sudoku_stats.items()}
        self.settings.save()
        self.emit("sudoku_stats", self.sudoku_stats)
