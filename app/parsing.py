"""Pull numbers out of call transcripts. Decision models choose; they do not emit numbers.

Speech-to-text often writes numbers as words ("two hundred", "six fifty", "saat sau"),
so transcripts are normalised to digits first.
"""
from __future__ import annotations

import re
from typing import Any

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    # common Hindi / Hinglish
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chheh": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "bees": 20, "tees": 30, "chalis": 40, "pachaas": 50,
    "pachas": 50, "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90,
}
_MULT = {"hundred": 100, "sau": 100, "thousand": 1000, "hazaar": 1000, "hazar": 1000, "lakh": 100_000}
_WORD = re.compile(r"[A-Za-z]+|[^A-Za-z]+")
# Hindi number words that are also everyday words ("do it", "saath mein", "ek din").
_AMBIGUOUS = {"do", "ek", "saath", "teen", "char", "das"}
_CURRENCY = {"rupees", "rupee", "rupaye", "rupay", "rs", "inr", "unit", "units", "bag", "bags"}


def _number_follows(toks: list[str], n: int) -> bool:
    """True if the next word after toks[n] makes it a number: 'do sau', 'ek hazaar', 'teen rupaye'."""
    for tok in toks[n + 1:]:
        if tok.isspace():
            continue
        low = tok.lower()
        return low in _MULT or low in _CURRENCY or (low in _UNITS and low not in _AMBIGUOUS)
    return False

_AMOUNT = re.compile(
    r"(?<![\d,:.])(\d{1,3}(?:,\d{3})+|\d{2,6})(?![\d,:.])"
    r"(?!\s*(?:am\b|pm\b|baje|o'?clock|minutes?|mins?\b|hours?|hrs?\b|days?\b|years?\b|km\b|%|star))",
    re.I,
)
_SLOT = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)|morning|afternoon|evening|tonight|tomorrow|today|day after)\b",
    re.I,
)
_TOTAL = re.compile(r"\btotal\b", re.I)
_PRICE_KEYS = ("quoted_price", "price", "total_price", "final_price")
_SLOT_KEYS = ("available_slot", "slot", "time_slot")


def words_to_digits(text: str) -> str:
    """'six fifty' -> '650', 'two hundred' -> '200', 'one thousand two hundred' -> '1200', 'saat sau' -> '700'."""
    out: list[str] = []
    total = current = 0
    in_num = bare_small = False  # bare_small: last word was a lone 1-9 ("six" in "six fifty")
    gap = ""  # whitespace/"and" seen inside a number, emitted only if the number ends there

    def flush() -> None:
        nonlocal total, current, in_num, bare_small
        if in_num:
            out.append(str(total + current))
        total = current = 0
        in_num = bare_small = False

    def fits(val: int) -> bool:
        """Can `val` fill an empty place in `current`? (twenty + five, two hundred + fifty)"""
        if current == 0:
            return total > 0  # "one thousand | two hundred"
        tail = current % 100
        if val < 10:
            return current > 0 and tail % 10 == 0 and not 10 <= tail <= 19
        return tail == 0 and current > 0

    toks = _WORD.findall(text)
    for n, tok in enumerate(toks):
        low = tok.lower()
        if low in _AMBIGUOUS and not in_num and not _number_follows(toks, n):
            out.append(gap + tok)
            gap = ""
            continue
        if low in _UNITS:
            val = _UNITS[low]
            if not in_num:
                current = val
            elif bare_small and current < 10 and val >= 10:
                current = current * 100 + val  # colloquial price: "six fifty" = 650
            elif fits(val):
                current += val
            else:  # two separate numbers, e.g. "five six"
                flush()
                out.append(gap)
                current = val
            in_num, bare_small, gap = True, val < 10 and current == val, ""
        elif low in _MULT and in_num:
            if _MULT[low] == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * _MULT[low]
                current = 0
            bare_small, gap = False, ""
        elif in_num and (tok.isspace() or low == "and" or tok.strip() == "-"):
            gap += tok
        else:
            flush()
            out.append(gap + tok)
            gap = ""
    flush()
    out.append(gap)
    return "".join(out)


def _lines(transcript: str, role: str) -> list[tuple[int, str]]:
    return [
        (i, words_to_digits(ln.split(":", 1)[1]))
        for i, ln in enumerate(transcript.splitlines())
        if ln.lower().startswith(f"{role}:")
    ]


def _amounts(text: str) -> list[int]:
    vals = []
    for m in _AMOUNT.finditer(text):
        v = int(m.group(1).replace(",", ""))
        if 30 <= v <= 500_000:
            vals.append(v)
    return vals


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    found = _amounts(words_to_digits(str(v)))
    return found[-1] if found else None


def parse_price(transcript: str, extracted: dict[str, Any] | None = None) -> int | None:
    """Total price, in order of trust:
    1. a Bolna extraction field;
    2. the agent's read-back "so the total is X" (it adds the extras), unless the business
       named a different amount after it (a correction);
    3. the last amount the business said."""
    for k in _PRICE_KEYS:
        if extracted and (v := _as_int(extracted.get(k))):
            return v
    vendor = _lines(transcript, "user")
    readbacks = []
    for i, ln in _lines(transcript, "assistant"):
        if not (ms := list(_TOTAL.finditer(ln))):
            continue
        after, before = _amounts(ln[ms[-1].end():]), _amounts(ln[: ms[-1].start()])
        if after or before:  # "total is 250" or "250 rupees total"
            readbacks.append((i, after[0] if after else before[-1]))
    if readbacks:
        idx, amount = readbacks[-1]
        later = [a for i, ln in vendor if i > idx for a in _amounts(ln)]
        return later[-1] if later else amount
    found = [a for _, ln in vendor for a in _amounts(ln)]
    return found[-1] if found else None


# ---------------------------------------------------------------- blood bank calls
# STT often hears "O" as "oh" or "zero" (which words_to_digits turns into 0).
_GROUP = re.compile(
    r"(?<![\w])(ab|a|b|o|oh|0)\s*-?\s*(positive|negative|pos\b|neg\b|plus|minus|\+ve|-ve|\+|-)",
    re.I,
)
_UNIT_COUNT = re.compile(r"(?<!\d)(\d{1,2})\s*(?:units?|bags?|bottles?|pints?)\b", re.I)
_STAFF = re.compile(r"\b(?i:this is|my name is|i am|i'm|name is|main)\s+([A-Z][a-z]{2,15})\b")
_NOT_NAMES = {"Sorry", "Calling", "From", "Speaking", "Not", "The", "Blood", "Here", "Busy", "Checking"}
_AFFIRM = re.compile(r"^\s*(yes|yeah|yep|haan|ha|han|ji|correct|right|sahi|ok|okay|confirmed|exactly)\b", re.I)
_NEGATE = re.compile(r"\b(no|not|nahi|nahin|wrong|galat)\b", re.I)
_UNAVAILABLE = re.compile(
    r"not available|no stock|out of stock|don'?t have|do not have|nahi hai|khatam|not there|finished|none left",
    re.I,
)


def normalize_group(letter: str, sign: str) -> str:
    letter = "O" if letter.lower() in {"o", "oh", "0"} else letter.upper()
    neg = sign.lower() in {"negative", "neg", "minus", "-ve", "-"}
    return letter + ("-" if neg else "+")


def parse_groups(text: str) -> list[str]:
    """Blood groups mentioned, normalised: 'O negative', 'zero minus', 'B +ve' -> 'O-', 'O-', 'B+'."""
    return [normalize_group(m.group(1), m.group(2)) for m in _GROUP.finditer(words_to_digits(text))]


def vendor_groups(transcript: str) -> list[str]:
    return [g for _, ln in _lines(transcript, "user") for g in parse_groups(ln)]


def parse_units(transcript: str) -> int | None:
    """Units available: last count the facility said, else the agent's read-back
    ("Confirming: ... 3 units"). Never the agent's own "we need N units"."""
    found = [int(m.group(1)) for _, ln in _lines(transcript, "user") for m in _UNIT_COUNT.finditer(ln)]
    if not found:
        found = [
            int(m.group(1))
            for _, ln in _lines(transcript, "assistant")
            if re.search(r"\bconfirm", ln, re.I)
            for m in _UNIT_COUNT.finditer(ln)
        ]
    return found[-1] if found else None


def parse_fee(transcript: str) -> int | None:
    """Processing fee per unit (rupees) as stated by the facility."""
    found = [a for _, ln in _lines(transcript, "user") for a in _amounts(ln) if a >= 100]
    return found[-1] if found else None


def parse_staff(transcript: str) -> str | None:
    for raw in (ln.split(":", 1)[1] for ln in transcript.splitlines() if ln.lower().startswith("user:")):
        for m in _STAFF.finditer(raw):
            if m.group(1) not in _NOT_NAMES:
                return m.group(1)
    return None


def said_unavailable(transcript: str) -> bool:
    return any(_UNAVAILABLE.search(ln) for _, ln in _lines(transcript, "user"))


def readback_confirmed(transcript: str, group: str) -> bool | None:
    """Did the facility say yes right after the agent read back the requested group?
    None = no read-back happened."""
    lines = transcript.splitlines()
    last_rb = None
    for i, ln in enumerate(lines):
        # only an explicit read-back counts, not the agent's opening question ("Do you have O negative?")
        if ln.lower().startswith("assistant:") and re.search(r"\bconfirm", ln, re.I) and group in parse_groups(ln):
            last_rb = i
    if last_rb is None:
        return None
    for ln in lines[last_rb + 1:]:
        if ln.lower().startswith("user:"):
            reply = re.sub(r"no (problem|issue|worries)", "", ln.split(":", 1)[1], flags=re.I)
            return bool(_AFFIRM.search(reply)) and not _NEGATE.search(reply)
    return None


def parse_slot(transcript: str, extracted: dict[str, Any] | None = None) -> str | None:
    for k in _SLOT_KEYS:
        if extracted and extracted.get(k):
            return str(extracted[k])
    hits = [m.group(1) for _, ln in _lines(transcript, "user") for m in _SLOT.finditer(ln)]
    return " ".join(dict.fromkeys(h.lower() for h in hits[-2:])) or None
