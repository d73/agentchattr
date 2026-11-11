"""Pure screen-text classifiers shared by the Windows and Unix activity checkers.

Both look only at the live bottom of the screen (physical rows, blank or not) and
only at lines shaped like real status/permission UI, so scrolled-back history or
quoted examples never count as live state.
"""
import re

BUSY_ROWS = 12
INPUT_ROWS = 20

_MARKER = r"(?:esc|ctrl\+c) to (?:interrupt|cancel)"
_TIMER = r"\d+\s*[smh]\b"
# A whole status line: optional spinner/bullet glyphs, a short label with no colon
# ("Thinking…", "Working"), then ONE parenthesis ending the line that holds the
# marker and a timer joined by the TUIs' separators (· • ,):
#   "✻ Thinking… (12s · esc to interrupt)"  "• Working (1m 3s • esc to interrupt)"
#   "⠋ Thinking (esc to cancel, 5s)"
_BUSY_LINE = re.compile(
    r"^[^\w(:]*[^\W\d_][\w …\.\-]{0,40}?\s*"
    r"\((?=[^)\n]*" + _MARKER + r")(?=[^)\n]*" + _TIMER + r")"
    r"[^)\n]*[·•,][^)\n]*\)\s*$",
    re.I,
)

# Option rows of a live permission menu.
_OPTION_ROW = re.compile(r"^[\s│|]*[❯>›]?\s*(?:\d+\.\s+\S|yes, proceed|\(y\))", re.I)
_QUESTION_LINE = re.compile(
    r"^[\s│|╭╰─>❯›•●]*(?:do you want to (?:proceed|make this edit|create|allow|run)"
    r"|would you like to run the following command)",
    re.I,
)
_YN_LINE = re.compile(r"[\[(]y/n[\])]\??\s*$", re.I)
_ENTER_LINE = re.compile(r"^[\s│|]*(?:press )?enter to confirm", re.I)


def _tail(rows, n):
    """Last n physical rows first, then drop blanks (so old text can't slide in)."""
    if isinstance(rows, str):
        rows = rows.splitlines()
    return [r.rstrip() for r in list(rows)[-n:] if r.strip()]


def has_busy_marker(rows) -> bool:
    return any(_BUSY_LINE.match(r.strip()) for r in _tail(rows, BUSY_ROWS))


def needs_user_input(rows) -> bool:
    live = _tail(rows, INPUT_ROWS)
    if any(_YN_LINE.search(r) or _ENTER_LINE.match(r) for r in live):
        return True
    return (any(_QUESTION_LINE.match(r) for r in live)
            and any(_OPTION_ROW.match(r) for r in live))
