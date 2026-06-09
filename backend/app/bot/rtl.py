"""
Make Persian (optionally HTML) Telegram messages render correctly when they embed Latin runs
(commands like /cancel, technical terms like USDT/TON/TXID/BEP-20, amounts like «30.86 USDT»,
wallet addresses, hashes, URLs). Telegram's bidi auto-detection reorders such runs and their
neighbouring punctuation, which jumbles the line. `rtl()` fixes that universally:

  • each Latin-containing run is wrapped in a First-Strong Isolate … Pop (U+2068 … U+2069) so it
    renders as one atomic LTR unit without disturbing the surrounding RTL text, and
  • every line is prefixed with an RLM (U+200F) so its base direction is RTL (consistent
    right-alignment) regardless of which character comes first.

It is HTML-aware and idempotent: HTML tags, the content inside <code>/<pre> (so tap-to-copy
stays exactly the address), and runs that are ALREADY isolated (⁨…⁩) are passed through
untouched. Pure-Persian text and lone numbers are left alone.
"""
from __future__ import annotations

import re

_RLM, _FSI, _PDI = "‏", "⁨", "⁩"

# A run to isolate: a /command, or a maximal run of Latin/number/symbol chars (spaces allowed
# inside so «30.86 USDT» stays one unit), or a single Latin letter. Only runs that actually
# contain a Latin LETTER are isolated (a lone number renders fine in RTL and is left as-is).
_LTR_RUN = re.compile(
    r"/[A-Za-z][\w-]*"
    r"|[A-Za-z0-9][A-Za-z0-9 .,:/_+=@#%×–-]*[A-Za-z0-9]"
    r"|[A-Za-z]"
)
# Only an actual Telegram HTML tag is passed through as markup; a stray '<' (e.g. «مصرف < سقف»
# or a name like «Ali<VIP>») is plain text, so it can never swallow a real <code> block.
_TAG_AT = re.compile(r"</?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|span|tg-spoiler)\b", re.I)
_CODE_TAG = re.compile(r"<(/?)(?:code|pre)\b[^>]*>", re.I)


def _isolate(seg: str) -> str:
    def repl(m: re.Match) -> str:
        run = m.group(0)
        return f"{_FSI}{run}{_PDI}" if re.search(r"[A-Za-z]", run) else run
    return _LTR_RUN.sub(repl, seg)


def rtl(text: str) -> str:
    """Return `text` made bidi-safe for Telegram (see module docstring)."""
    if not text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    in_code = False
    while i < n:
        ch = text[i]
        if ch == _FSI:                          # already-isolated run → pass through to its PDI
            j = text.find(_PDI, i)
            if j == -1:                         # lone FSI (no PDI) → emit as a single char
                out.append(ch)
                i += 1
                continue
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if ch == "<" and _TAG_AT.match(text, i):  # a real HTML tag → pass through; track code
            j = text.find(">", i)
            j = n if j == -1 else j + 1
            tag = text[i:j]
            low = tag.lower()
            if low.startswith(("<code", "<pre")):
                in_code = True
            elif low.startswith(("</code", "</pre")):
                in_code = False
            out.append(tag)
            i = j
            continue
        j = i                                   # plain run up to the next isolate / real tag
        while j < n:
            c = text[j]
            if c == _FSI or (c == "<" and _TAG_AT.match(text, j)):
                break
            j += 1
        seg = text[i:j]
        out.append(seg if in_code else _isolate(seg))
        i = j
    # Force each line base-RTL with an RLM — but NEVER a line that begins inside a <code>/<pre>
    # block (its content must stay byte-exact for tap-to-copy).
    isolated = "".join(out)
    result: list[str] = []
    line_in_code = False
    for ln in isolated.split("\n"):
        starts_in_code = line_in_code
        for m in _CODE_TAG.finditer(ln):
            line_in_code = (m.group(1) != "/")
        if ln.strip() and not starts_in_code and not ln.startswith(_RLM):
            result.append(_RLM + ln)
        else:
            result.append(ln)
    return "\n".join(result)
