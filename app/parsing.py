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
_CURRENCY = {
    "rupees", "rupee", "rupaye", "rupay", "rs", "inr", "unit", "units", "bag", "bags", "bottle", "bottles",
    "pint", "pints", "packet", "packets",
}


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
# STT often hears "O" as "oh" or "zero" (which words_to_digits turns into 0), and splits "AB" into "A B".
_SIGNS = r"positive|negative|pos\b|neg\b|plus|minus|\+ve|-ve|\+|-"
_GROUP = re.compile(rf"(?<![\w])(a\s+b|ab|a|b|o|oh|0)\s*-?\s*({_SIGNS})", re.I)
# A sign word on its own ("O negative nahi hai, positive hai") refers back to the last letter named.
# "plus"/"minus" are left out: they are everyday words ("two units plus one").
_BARE_SIGN = re.compile(r"(?<![\w+-])(positive|negative|\+ve|-ve)\b", re.I)
# Letters STT produces for B/D/P/E/V confusions; never a valid group, so always ambiguous.
_BAD_LETTER = re.compile(rf"(?<![\w])(d|p|e|v|bee|dee|pee)\s*-?\s*({_SIGNS})", re.I)
# A group letter with no sign ("O is there", "haan B hai"). Lowercase a/ab are the English article
# and Hindi "ab" (now), so only uppercase A/AB count.
_LETTER = re.compile(r"(?<![\w'])(AB|A|[OoBb])(?![\w'])")
_LETTER_BEFORE = re.compile(r"\b(have|got|only|sirf|bas|haan|han|yes|group|just|is)\s*$", re.I)
_LETTER_AFTER = re.compile(
    r"^\s*(?:[,.?!]|$|(?:group|hai|he|h|is|are|available|there|ka|ki|ke|wala|wali|chahiye|stock|only|bhi)\b)", re.I
)
_UNIT_COUNT = re.compile(r"(?<!\d)(\d{1,2})\s*(?:units?|bags?|bottles?|pints?|packets?)\b", re.I)
_STAFF = re.compile(r"\b(?i:this is|my name is|i am|i'm|name is|main)\s+([A-Z][a-z]{2,15})\b")
_NOT_NAMES = {"Sorry", "Calling", "From", "Speaking", "Not", "The", "Blood", "Here", "Busy", "Checking"}
_STAFF_ANY = re.compile(r"\b(?:this is|my name is|name is)\s+([A-Za-z]{3,15})\b", re.I)
_NOT_NAME_WORDS = {
    "sorry", "calling", "from", "speaking", "not", "the", "blood", "here", "busy", "checking", "available",
    "fine", "okay", "sure", "correct", "right", "wrong", "going", "coming", "the", "for", "about", "only",
    "what", "who", "your", "our", "staff", "doctor", "nurse", "reception", "counter", "hospital", "bank",
}
_NEGATE = re.compile(r"\b(no|not|nope|nah|nahi|nahin|illa|wrong|galat|cancel)\b", re.I)
_BENIGN = re.compile(r"\b(no|koi) (problem|issue|worries|tension|dikkat)\b|\bnot a problem\b", re.I)
_UNAVAILABLE = re.compile(
    r"not available|no stock|out of stock|don'?t have|do not have|nahi hai|nahin hai|khatam|not there|finished"
    r"|none left|ek bhi nahi|koi nahi|nahi milega|nahi milegi|\billa\b|\bnil\b|exhausted|used up|all gone"
    r"|\b0 (units?|bags?|hai)\b|\bno units?\b|\bgone\b",
    re.I,
)
# Words a clean "yes" to the read-back may contain. Anything else ("yes, let me check", "yes but expired",
# "ok sorry not sure") means the staff did not simply agree, so the read-back does not count.
_YES = {
    "yes", "yeah", "yep", "yup", "ya", "haan", "han", "ha", "haanji", "hanji", "ji", "correct", "right", "sahi",
    "ok", "okay", "confirmed", "exactly", "bilkul", "absolutely", "definitely", "perfect", "done", "sure", "theek", "thik",
}
_FILLER = {
    "hai", "he", "h", "sir", "madam", "maam", "ma'am", "mam", "bhai", "that's", "thats", "that", "is", "it", "it's",
    "its", "all", "fine", "good", "um", "umm", "uh", "hmm", "ah", "achha", "acha", "accha", "so", "very", "true",
    "everything", "we", "will", "keep", "kept", "aside", "rakhenge", "of", "blood", "the", "same",
    # STT tail noise seen on a real call ("sure perfect work")
    "work", "works", "great", "thanks", "thank", "you",
}
_TIME = re.compile(r"\b(?:for )?\d+ (?:hours?|hrs?|minutes?|mins?|ghante?|ghanta)\b", re.I)
_CONFIRM = re.compile(r"\bconfirm", re.I)
# The opening question ("Can you confirm whether you have O negative?") is not a read-back.
_ASKING = re.compile(r"\b(do you have|have you got|is there|are there|whether|we need|kya)\b|\bavailable( right now)?\s*\?", re.I)
_RETRACT = re.compile(r"\b(wait|sorry|actually|check)\b", re.I)
_PHONE = re.compile(
    r"(?<!\d)(?:\+?91[\s-]?)?(?:\d[\s-]?){9}\d(?!\d)|(?<!\d)(?:0\d{2,4}|1800)[\s-]\d{3,4}[\s-]?\d{3,4}(?!\d)"
)
_NOT_FEE = re.compile(r"\b\d+\s*ml\b|\b(?:ward|room|bed|floor|counter|window|gate|token)\s*(?:no\.?|number)?\s*\d+", re.I)


def normalize_group(letter: str, sign: str) -> str:
    letter = "O" if letter.lower() in {"o", "oh", "0"} else re.sub(r"\s+", "", letter).upper()
    neg = sign.lower() in {"negative", "neg", "minus", "-ve", "-"}
    return letter + ("-" if neg else "+")


def parse_groups(text: str) -> list[str]:
    """Blood groups mentioned, normalised: 'O negative', 'zero minus', 'B +ve' -> 'O-', 'O-', 'B+'.
    A lone sign word takes the last letter named before it: 'O negative nahi, positive hai' -> O-, O+."""
    t = words_to_digits(text)
    found = [(m.start(), m.end(), normalize_group(m.group(1), m.group(2))) for m in _GROUP.finditer(t)]
    for m in _BARE_SIGN.finditer(t):
        if any(s <= m.start() < e for s, e, _ in found):
            continue
        before = [g for s, _, g in sorted(found) if s < m.start()]
        if before:
            found.append((m.start(), m.end(), normalize_group(before[-1][:-1], m.group(1))))
    return [g for _, _, g in sorted(found)]


def ambiguous_groups(text: str, group: str) -> list[str]:
    """Group mentions we cannot pin to exactly `group`: a letter with no sign ('O hai'), a non-group
    letter ('D negative'), or a lone sign opposite to the requested one ('positive hai' for O-)."""
    t = words_to_digits(text)
    spans = [(m.start(), m.end()) for m in _GROUP.finditer(t)]
    out = [m.group(0) for m in _BAD_LETTER.finditer(t)]
    for m in _LETTER.finditer(t):
        if any(s <= m.start() < e for s, e in spans):
            continue
        if _LETTER_BEFORE.search(t[: m.start()]) or _LETTER_AFTER.match(t[m.end():]):
            out.append(m.group(0))
    if not spans:
        want_neg = group.endswith("-")
        out += [m.group(0) for m in _BARE_SIGN.finditer(t) if (m.group(1).lower() in {"negative", "-ve"}) != want_neg]
    return out


def vendor_groups(transcript: str) -> list[str]:
    return [g for _, ln in _lines(transcript, "user") for g in parse_groups(ln)]


def vendor_ambiguous(transcript: str, group: str) -> list[str]:
    return [a for _, ln in _lines(transcript, "user") for a in ambiguous_groups(ln, group)]


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
    """Processing fee per unit (rupees) as stated by the facility. Phone numbers ('98765 43210'),
    bag sizes ('350 ml') and ward/counter numbers are removed first."""
    found = [
        a
        for _, ln in _lines(transcript, "user")
        for a in _amounts(_NOT_FEE.sub(" ", _PHONE.sub(" ", ln)))
        if 100 <= a <= 20_000
    ]
    return found[-1] if found else None


def parse_staff(transcript: str) -> str | None:
    """Staff name for the family ('confirmed by Ramesh'). STT often lowercases names ('my name is
    arian'), so this match is case-insensitive. _STAFF stays capitalised-only because _clean_yes
    strips its matches, and stripping 'this is wrong' would hide a no."""
    for raw in (ln.split(":", 1)[1] for ln in transcript.splitlines() if ln.lower().startswith("user:")):
        for m in _STAFF_ANY.finditer(raw):
            name = m.group(1).capitalize()
            if name not in _NOT_NAMES and m.group(1).lower() not in _NOT_NAME_WORDS:
                return name
    return None


def denies(text: str) -> bool:
    """The text says no / not available (ignoring 'no problem' and the like)."""
    t = _BENIGN.sub(" ", words_to_digits(text))
    return bool(_UNAVAILABLE.search(t) or _NEGATE.search(t))


def said_unavailable(transcript: str) -> bool:
    return any(_UNAVAILABLE.search(_BENIGN.sub(" ", ln)) for _, ln in _lines(transcript, "user"))


def _role(ln: str) -> str | None:
    low = ln.lower()
    return "user" if low.startswith("user:") else "assistant" if low.startswith("assistant:") else None


def _last_readback(lines: list[str]) -> int | None:
    """Index of the agent's last read-back: an assistant line saying 'confirm' and naming a group,
    that is not itself a question about stock."""
    idx = None
    for i, ln in enumerate(lines):
        if _role(ln) == "assistant":
            body = ln.split(":", 1)[1]
            if _CONFIRM.search(body) and not _ASKING.search(body) and parse_groups(body):
                idx = i
    return idx


def readback_units(transcript: str) -> int | None:
    lines = transcript.splitlines()
    if (i := _last_readback(lines)) is None:
        return None
    counts = [int(m.group(1)) for m in _UNIT_COUNT.finditer(words_to_digits(lines[i].split(":", 1)[1]))]
    return counts[-1] if counts else None


def _clean_yes(reply: str, group: str, units: int | None) -> bool:
    t = _BENIGN.sub(" ", words_to_digits(reply))
    if "?" in t:  # "correct?" is an echo of the agent's question, not an answer
        return False
    t = _STAFF.sub(" ", t)
    if any(g != group for g in parse_groups(t)) or ambiguous_groups(t, group):
        return False
    t = _GROUP.sub(" ", t)
    if any(int(m.group(1)) != units for m in _UNIT_COUNT.finditer(t)):
        return False
    t = _TIME.sub(" ", _UNIT_COUNT.sub(" ", t))
    toks = re.findall(r"[a-z']+|\d+", t.lower())
    return any(w in _YES for w in toks) and all(w in _YES or w in _FILLER for w in toks)


def readback_confirmed(transcript: str, group: str) -> bool | None:
    """Did the facility clearly say yes right after the agent read back the requested group?
    None = no read-back happened. False when the last read-back named any other group, the reply
    was anything but a plain yes, or the staff later took it back ('wait, sorry, it's finished')."""
    lines = transcript.splitlines()
    if (last := _last_readback(lines)) is None:
        return None
    if set(parse_groups(lines[last].split(":", 1)[1])) != {group}:
        return False
    reply: list[str] = []
    later: list[str] = []
    ended = False  # an agent turn after the reply ends the direct reply
    for ln in lines[last + 1:]:
        role = _role(ln)
        if role == "assistant" and reply:
            ended = True
        elif role == "user":
            (later if ended else reply).append(ln.split(":", 1)[1])
    if not reply:
        return None
    if not _clean_yes(" ".join(reply), group, readback_units(transcript)):
        return False
    for ln in later:
        if denies(ln) or _RETRACT.search(ln) or any(g != group for g in parse_groups(ln)) or ambiguous_groups(ln, group):
            return False
    return True


def parse_slot(transcript: str, extracted: dict[str, Any] | None = None) -> str | None:
    for k in _SLOT_KEYS:
        if extracted and extracted.get(k):
            return str(extracted[k])
    hits = [m.group(1) for _, ln in _lines(transcript, "user") for m in _SLOT.finditer(ln)]
    return " ".join(dict.fromkeys(h.lower() for h in hits[-2:])) or None
