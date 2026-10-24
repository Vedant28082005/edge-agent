"""Text normalisation and fuzzy token matching shared by the fast-path resolver and the router's sensor questions.

Pure functions, no I/O. Everything here treats the utterance as untrusted text: it is only ever compared with registry
labels, never forwarded anywhere (the LLM prompt gets the cleaned original text through the normal prompt builder)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from ..devices import Device

# type -> words that name it. An exact type word explains a target token fully (1.0), a synonym 0.8, so "lamp" prefers a
# device NAMED lamp over a ceiling light that is merely of type light.
TYPE_SYN: dict[str, frozenset[str]] = {
    "light": frozenset({"light", "lamp", "bulb", "lighting"}),
    "fan": frozenset({"fan", "ventilator"}),
    "outlet": frozenset({"outlet", "plug", "socket"}),
    "lock": frozenset({"lock"}),
    "cover": frozenset({"cover", "blind", "curtain", "shade", "shutter", "awning"}),
    "thermostat": frozenset({"thermostat", "heater"}),
    "sensor": frozenset({"sensor"}),
    "generic": frozenset(),
}

QUANT = frozenset({"all", "every", "each", "both"})
PREFIX_FILLER = ("can you", "could you", "would you", "will you", "i want you to", "i would like you to",
                 "i would like to", "i want to", "lets", "let us", "go ahead and", "please")
FILLER = frozenset({"please", "pls", "kindly", "hey", "ok", "okay", "the", "a", "an", "my", "our", "me", "now", "just",
                    "thanks", "thank", "you", "for"})
PREP = frozenset({"in", "on", "at", "of", "to", "by", "from", "inside", "within", "with"})
QUESTION = frozenset({"is", "are", "was", "what", "which", "how", "why", "when", "where", "who", "does", "do", "did",
                      "should", "has", "have", "whats", "hows"})
NEGATION = frozenset({"not", "dont", "never", "no", "cant", "wont", "without"})
TEMPORAL = frozenset({"if", "unless", "until", "whenever", "after", "before", "later", "tomorrow", "tonight", "timer",
                      "schedule", "remind", "daily", "weekly", "while", "then", "also", "again", "instead"})
RELATIVE = frozenset({"brighter", "dimmer", "higher", "lower", "more", "less", "increase", "decrease", "raise",
                      "warmer", "cooler", "faster", "slower", "louder", "quieter", "bit", "little", "slightly", "up", "down", "reduce", "lessen", "drop", "boost",
                      "brighten"})

_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
_NUM_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
                   "hundred": 100})
_TENS = {"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"}


def normalise(text: str) -> str:
    """Lowercase, NFKC, drop apostrophes, spoken numbers -> digits, punctuation -> spaces (keeps % and decimal points)."""
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"\bper\s*cent\b", "percent", s)
    s = re.sub(r"[^\w%. ]", " ", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)                  # keep only decimal points between digits
    words, out, i = s.split(), [], 0
    while i < len(words):
        w = words[i]
        if w in _TENS and i + 1 < len(words) and words[i + 1] in _NUM_WORDS and 1 <= _NUM_WORDS[words[i + 1]] <= 9:
            out.append(str(_NUM_WORDS[w] + _NUM_WORDS[words[i + 1]]))
            i += 2
            continue
        if w == "hundred":
            if out and out[-1] == "1":
                out[-1] = "100"
            else:
                out.append("100")
        elif w in _NUM_WORDS:
            out.append(str(_NUM_WORDS[w]))
        else:
            out.append(w)
        i += 1
    return " ".join(out)


def strip_polite(s: str) -> str:
    changed = True
    while changed:
        changed = False
        for p in PREFIX_FILLER:
            if s == p or s.startswith(p + " "):
                s, changed = s[len(p):].strip(), True
    return s


def stem(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def tok_sim(x: str, y: str) -> float:
    if x == y:
        return 1.0
    if len(x) >= 5 and len(y) >= 5:
        r = SequenceMatcher(None, x, y).ratio()
        return r if r >= 0.86 else 0.0
    return 0.0


def _label_tokens(s: str) -> set[str]:
    return {stem(t) for t in normalise(s).split() if t}


def explain(x: str, d: Device) -> tuple[float, bool]:
    """How well device `d` explains target token `x` (already stemmed), and whether it did so by something other than the
    room (name, type or tag). Exact label tokens 1.0, an exact type word 1.0, a type synonym 0.8, fuzzy = its ratio."""
    best, ident = 0.0, False
    for n in _label_tokens(d.name) | {g for tag in d.tags for g in _label_tokens(tag.replace("-", " ").replace("_", " "))}:
        s = tok_sim(x, n)
        if s > best:
            best, ident = s, True
        elif s == best and s > 0:
            ident = True
    for r in _label_tokens(d.room):
        s = tok_sim(x, r)
        if s > best:
            best, ident = s, False
    ts = 1.0 if x == d.type else (0.8 if x in TYPE_SYN.get(d.type, ()) else 0.0)
    if ts > best:
        best, ident = ts, True
    elif ts == best and ts > 0:
        ident = True
    return best, ident


def score_device(xs: list[str], d: Device) -> tuple[float, bool]:
    """Coverage of the target tokens by one device: mean explanation, plus whether any token hit its identity."""
    if not xs:
        return 0.0, False
    total, ident = 0.0, False
    for x in xs:
        s, i = explain(x, d)
        total += s
        ident = ident or (i and s > 0)
    return total / len(xs), ident


def is_type_word(t: str) -> str | None:
    """The endpoint type a (stemmed) token names, if any."""
    for typ, words in TYPE_SYN.items():
        if t in words:
            return typ
    return None
