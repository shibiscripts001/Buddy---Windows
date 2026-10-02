#!/usr/bin/env python3
"""
The Web tab's ad blocker: EasyList-style filter lists (EasyList,
EasyPrivacy, uBlock Origin's own) read into an index the request filter
asks about every request (engine.TrackerFilter), and the element-hiding
rules for the page a tab is opening. No Qt here.

What's read, of Adblock Plus / uBlock Origin syntax:
  ||ads.example.com^          a domain and everything under it
  ||example.com/ads/*$script  an address on a domain
  /banner/ad_  -ad-unit.      a piece of any address (* and ^ allowed)
  @@...                       an exception: never blocked
  options  third-party / 3p / 1p / first-party, the resource types
           (script, image, stylesheet, xhr, subdocument / frame, media,
           font, ping, websocket, object, other, document), domain= / from=,
           important, match-case, badfilter, and on exceptions document,
           elemhide / ehide, generichide / ghide
  example.com##.ad-box        hide on that site; ##.ad-box on every site
  example.com#@#.ad-box       don't hide that there
Anything else - redirects, removeparam, csp, scriptlets (+js), uBO's own
:has-text() and friends - is skipped: a rule only half understood could
block a page's own content, so it's better left out.

Matching mirrors how the lists mean it: a rule's literal "token" (a run
of letters and digits) indexes it, so an address is only tried against
rules sharing one of its tokens, and domain rules by the request's host
and the hosts above it. A page's own document is never blocked unless a
rule says $document.
"""

import re
from urllib.parse import urlsplit

from pages.web import browser

TYPES = {"script", "image", "stylesheet", "xmlhttprequest", "subdocument", "media", "font", "ping",
         "websocket", "object", "other", "document"}
_TYPE_ALIASES = {"xhr": "xmlhttprequest", "frame": "subdocument", "css": "stylesheet", "doc": "document",
                 "beacon": "ping", "object-subrequest": "object"}
_SKIP_OPTIONS = {"popup", "popunder", "redirect", "redirect-rule", "removeparam", "csp", "replace", "urlskip",
                 "permissions", "uritransform", "rewrite", "denyallow", "to", "method", "header", "cname",
                 "inline-script", "inline-font", "empty", "mp4", "webrtc", "genericblock", "specifichide",
                 "shide", "urltransform", "ipaddress", "reason"}
# uBO's own selectors, beyond what Chromium's CSS knows.
_PROCEDURAL = re.compile(r":(has-text|upward|xpath|matches-css|matches-css-before|matches-css-after|"
                         r"min-text-length|watch-attr|remove|style|matches-path|others|matches-attr|"
                         r"matches-prop|-abp-|contains|if|if-not|nth-ancestor|remove-attr|remove-class)\b")
_TOKEN = re.compile(r"[a-z0-9%]{2,}")
_BAD_TOKENS = {"http", "https", "www", "com", "js", "html", "php", "net", "org", "jpg", "png", "gif", "css"}
_HOST = re.compile(r"[a-z0-9.\-*]+")
SEPARATOR = r"(?:[^\w\-.%]|$)"


class Rule:
    __slots__ = ("text", "exception", "host", "regex", "third_party", "types", "not_types",
                 "domains", "not_domains", "important", "document", "elemhide", "generichide")

    def __init__(self, text):
        self.text = text
        self.exception = False
        self.host = None                      # an ||anchored host, or None
        self.regex = None                     # the rest of the address, or None for "anything"
        self.third_party = None               # True: only other sites' requests; False: only the site's own
        self.types = None                     # frozenset, or None for every type but a document
        self.not_types = frozenset()
        self.domains = frozenset()            # on these sites only (and under them)
        self.not_domains = frozenset()
        self.important = False
        self.document = False                 # the page's own document too
        self.elemhide = False                 # an exception: no element hiding on the site
        self.generichide = False              # an exception: no every-site hiding on the site

    def __repr__(self):
        return f"Rule({self.text!r})"


def _pattern(text, match_case):
    """ABP pattern (after any || host) -> compiled regex."""
    start = text.startswith("|")
    end = text.endswith("|")
    body = text[1 if start else 0:len(text) - 1 if end else len(text)]
    out = []
    for ch in body:
        if ch == "*":
            out.append(".*")
        elif ch == "^":
            out.append(SEPARATOR)
        else:
            out.append(re.escape(ch))
    regex = ("^" if start else "") + "".join(out) + ("$" if end else "")
    return re.compile(regex, 0 if match_case else re.IGNORECASE)


def _domains(value):
    yes, no = set(), set()
    for d in value.split("|"):
        d = d.strip().lower()
        if not d:
            continue
        if d.startswith("~"):
            no.add(d[1:])
        else:
            yes.add(d)
    return frozenset(yes), frozenset(no)


def parse_network(line):
    """A network rule, or None if it isn't one this reads."""
    rule = Rule(line)
    text = line
    if text.startswith("@@"):
        rule.exception = True
        text = text[2:]
    options = ""
    # Options come after the last $ - unless that $ is inside a /regex/.
    dollar = text.rfind("$")
    if dollar > 0 and not (text.startswith("/") and text.endswith("/")):
        text, options = text[:dollar], text[dollar + 1:]
    if text.startswith("/") and text.endswith("/") and len(text) > 2:
        return None                           # regex rules: few, and costly to get right
    match_case = False
    types, not_types = set(), set()
    for raw in options.split(",") if options else ():
        opt = raw.strip()
        negated = opt.startswith("~")
        name, _, value = opt.lstrip("~").partition("=")
        name = name.lower()
        name = _TYPE_ALIASES.get(name, name)
        if name in ("third-party", "3p", "strict3p"):
            rule.third_party = not negated
        elif name in ("first-party", "1p", "strict1p"):
            rule.third_party = negated
        elif name in TYPES:
            (not_types if negated else types).add(name)
            if name == "document" and not negated:
                rule.document = True
        elif name == "all":
            types |= TYPES
            rule.document = True
        elif name in ("domain", "from"):
            rule.domains, rule.not_domains = _domains(value)
        elif name == "important":
            rule.important = True
        elif name == "match-case":
            match_case = True
        elif name in ("elemhide", "ehide") and rule.exception:
            rule.elemhide = True
        elif name in ("generichide", "ghide") and rule.exception:
            rule.generichide = True
        elif name == "badfilter":
            return ("badfilter", line.replace("$badfilter", "").replace(",badfilter", ""))
        else:
            return None                       # _SKIP_OPTIONS and anything unknown
    if rule.elemhide or rule.generichide:
        types.discard("document")
        if not types:
            rule.document = False
    rule.types = frozenset(types) if types else None
    rule.not_types = frozenset(not_types)
    if text.startswith("||"):
        host = _HOST.match(text, 2)
        if not host or "*" in host.group(0):
            return None
        rule.host = host.group(0).lower().strip(".")
        rest = text[host.end():]
        if rest and rest not in ("^", "^|", "/", "|"):
            rule.regex = _pattern(rest, match_case)
    else:
        if not text or text in ("*", "|", "^"):
            if rule.elemhide or rule.generichide or rule.document:
                rule.regex = None
            else:
                return None                   # would match everything
        else:
            rule.regex = _pattern(text, match_case)
    return rule


def _tokens(text):
    return _TOKEN.findall(text.lower())


def _rule_token(rule):
    """The token a rule is indexed by: its longest literal run of letters
    and digits that any address it matches must contain, or None."""
    if rule.regex is None:
        return None
    pattern = rule.text.split("$", 1)[0].lstrip("@|")
    if rule.host:
        pattern = pattern[len(rule.host):]
    best = None
    for piece in re.split(r"[*^|]", pattern):
        for tok in _tokens(piece):
            # Only whole runs: a run cut at a * could be part of a longer one.
            if tok in _BAD_TOKENS:
                continue
            if best is None or len(tok) > len(best):
                best = tok
    return best


class Cosmetic:
    """Element hiding: selectors by site, and for every site."""

    def __init__(self):
        self.generic = set()
        self.specific = {}                    # host -> set of selectors
        self.not_specific = {}                # host -> selectors not hidden there
        self.generic_not = {}                 # selector -> hosts where an every-site one isn't
        self.excluded = {}                    # selector -> hosts excluded (~site##...)

    def add(self, domains, selector, exception):
        if _PROCEDURAL.search(selector) or selector.startswith("+js(") or "{" in selector:
            return False
        yes, no = _domains(domains.replace(",", "|")) if domains else (frozenset(), frozenset())
        if exception:
            if not yes:
                self.generic.discard(selector)
            for d in yes:
                self.not_specific.setdefault(d, set()).add(selector)
            return True
        if not yes:
            self.generic.add(selector)
            for d in no:
                self.generic_not.setdefault(selector, set()).add(d)
        for d in yes:
            self.specific.setdefault(d, set()).add(selector)
        for d in no:
            self.excluded.setdefault(selector, set()).add(d)
        return True

    def for_host(self, host, generic=True):
        """The selectors to hide on `host`: (every-site ones, as a set - or
        None for all of them, unchanged), and its own ones (sorted)."""
        hosts = set(_up(host))
        chosen, dropped = set(), set()
        for h in hosts:
            chosen |= self.specific.get(h, set())
            dropped |= self.not_specific.get(h, set())
        dropped |= {s for s, where in self.excluded.items() if where & hosts}
        own = sorted(chosen - dropped)
        if not generic:
            return set(), own
        dropped |= {s for s, where in self.generic_not.items() if where & hosts}
        dropped &= self.generic
        return (None if not dropped else self.generic - dropped), own

    def generic_css(self):
        """Every site's hiding stylesheet, made once."""
        if getattr(self, "_generic_css", None) is None:
            self._generic_css = rules_css(sorted(self.generic))
        return self._generic_css


def rules_css(selectors):
    # One rule each: a selector Chromium can't read drops only itself.
    return "".join(f"{s}{{display:none!important}}\n" for s in selectors)


def _up(host):
    """example.com's host and every host above it: a.b.example.com,
    b.example.com, example.com, com."""
    host = (host or "").lower().rstrip(".")
    while host:
        yield host
        host = host.partition(".")[2]


_PLAIN = {}


class Filters:
    """The parsed lists. matches(...) answers the request filter; it only
    reads, so one built off the GUI thread can be swapped in whole."""

    def __init__(self):
        self.plain_hosts = set()              # ||host^ with nothing else: most of the lists, kept small
        self.by_host = {}                     # host -> [Rule]
        self.by_token = {}                    # token -> [Rule]
        self.loose = []                       # rules with no token to index them by
        self.counts = {"network": 0, "cosmetic": 0, "skipped": 0}
        self.cosmetic = Cosmetic()
        self._bad = set()

    def add_text(self, text):
        for line in text.splitlines():
            self.add_line(line.strip())

    def add_line(self, line):
        if not line or line.startswith(("!", "[")):
            return
        for marker, exception in (("#@#", True), ("##", False)):
            if marker in line:
                domains, _, selector = line.partition(marker)
                if "#" in domains or not selector:             # #?#, #$#, #+js(...): not this
                    self.counts["skipped"] += 1
                    return
                ok = self.cosmetic.add(domains, selector.strip(), exception)
                self.counts["cosmetic" if ok else "skipped"] += 1
                return
        if "#?#" in line or "#$#" in line or "#%#" in line or "#@?#" in line or "#@$#" in line:
            self.counts["skipped"] += 1
            return
        rule = parse_network(line)
        if rule is None:
            self.counts["skipped"] += 1
            return
        if isinstance(rule, tuple):
            self._bad.add(rule[1])
            return
        if rule.text in self._bad:
            return
        if rule.host and rule.regex is None and not rule.exception and rule.third_party is None                 and rule.types is None and not rule.not_types and not rule.domains and not rule.not_domains                 and not rule.important and not rule.document:
            self.plain_hosts.add(rule.host)
        elif rule.host:
            self.by_host.setdefault(rule.host, []).append(rule)
        else:
            token = _rule_token(rule)
            (self.by_token.setdefault(token, []) if token else self.loose).append(rule)
        self.counts["network"] += 1

    def finish(self):
        """badfilter rules seen after the rules they cancel."""
        if not self._bad:
            return self
        for text in self._bad:
            plain = text[2:].rstrip("^") if text.startswith("||") else None
            if plain and text == f"||{plain}^":
                self.plain_hosts.discard(plain)
        for table in (self.by_host, self.by_token):
            for key, rules in table.items():
                table[key] = [r for r in rules if r.text not in self._bad]
        self.loose = [r for r in self.loose if r.text not in self._bad]
        return self

    # ------------------------------------------------------------ requests --
    def _candidates(self, url, host):
        for h in _up(host):
            yield from self.by_host.get(h, ())
        seen = set()
        for tok in _tokens(url):
            if tok not in seen:
                seen.add(tok)
                yield from self.by_token.get(tok, ())
        yield from self.loose

    @staticmethod
    def _applies(rule, url, host, site, third_party, kind):
        if rule.third_party is not None and rule.third_party != third_party:
            return False
        if kind == "document":
            if not rule.document:
                return False
        elif rule.types is not None and kind not in rule.types:
            return False
        if kind in rule.not_types:
            return False
        if rule.domains or rule.not_domains:
            ups = set(_up(site))
            if rule.not_domains & ups:
                return False
            if rule.domains and not rule.domains & ups:
                return False
        if rule.regex is None:
            return True
        if rule.host:
            # What follows the host in the address.
            at = url.lower().find(rule.host, url.find("//") + 2)
            if at < 0:
                return False
            return bool(rule.regex.match(url, at + len(rule.host)))
        return bool(rule.regex.search(url))

    def matches(self, url, host, site, kind):
        """The rule blocking a request - `url` to `host`, from a page on
        `site`, of `kind` (TYPES) - or None. Exceptions win, unless the
        blocking rule is $important."""
        host, site = (host or "").lower(), (site or "").lower()
        third_party = bool(site) and browser.registrable(host) != browser.registrable(site)
        blocked = None
        if kind != "document":
            plain = next((h for h in _up(host) if h in self.plain_hosts), None)
            if plain is not None:
                blocked = _PLAIN.get(plain) or Rule(f"||{plain}^")
        for rule in () if blocked is not None else self._candidates(url, host):
            if not rule.exception and self._applies(rule, url, host, site, third_party, kind):
                blocked = rule
                if rule.important:
                    return rule
                break
        if blocked is None:
            return None
        for rule in self._candidates(url, host):
            if rule.exception and not (rule.elemhide or rule.generichide) and \
                    self._applies(rule, url, host, site, third_party, kind):
                return None
        return blocked

    def site_exception(self, page_url, which):
        """True if the lists turn `which` ("document", "elemhide",
        "generichide") off for a page at page_url."""
        host = (urlsplit(page_url).hostname or "").lower()
        for rule in self._candidates(page_url, host):
            if not rule.exception:
                continue
            flag = {"document": rule.document and not (rule.elemhide or rule.generichide),
                    "elemhide": rule.elemhide, "generichide": rule.generichide}[which]
            if flag and self._applies_to_page(rule, page_url, host):
                return True
        return False

    def _applies_to_page(self, rule, url, host):
        if rule.domains and not rule.domains & set(_up(host)):
            return False
        if rule.regex is None:
            return True
        if rule.host:
            at = url.lower().find(rule.host, url.find("//") + 2)
            return at >= 0 and bool(rule.regex.match(url, at + len(rule.host)))
        return bool(rule.regex.search(url))

    # -------------------------------------------------------------- pages --
    def hiding_css(self, page_url):
        """The stylesheet that hides a page's ad boxes - "" for none."""
        if self.site_exception(page_url, "elemhide") or self.site_exception(page_url, "document"):
            return ""
        host = urlsplit(page_url).hostname or ""
        generic, own = self.cosmetic.for_host(host, not self.site_exception(page_url, "generichide"))
        every = self.cosmetic.generic_css() if generic is None else rules_css(sorted(generic))
        return every + rules_css(own)


def build(texts):
    """Filters from the lists' texts."""
    filters = Filters()
    for text in texts:
        filters.add_text(text)
    return filters.finish()
