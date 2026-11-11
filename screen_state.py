"""Pure screen-text classifiers shared by the Windows and Unix activity checkers.

Both look only at the live bottom of the screen so scrolled-back history that
merely quotes a marker (e.g. "esc to interrupt") never counts as live state.
"""
import re

BUSY_ROWS = 12
INPUT_ROWS = 20

# A live status line puts the marker and an elapsed timer in one parenthesis:
#   "(12s · esc to interrupt)"  "(1m 3s • esc to interrupt)"  "(esc to cancel, 5s)"
_BUSY = re.compile(
    r"\((?=[^)\n]*\d+\s*[smh]\b)[^)\n]*(?:esc|ctrl\+c) to (?:interrupt|cancel)[^)\n]*\)"
    r"|\((?=[^)\n]*(?:esc|ctrl\+c) to (?:interrupt|cancel))[^)\n]*\d+\s*[smh]\b[^)\n]*\)",
    re.I,
)

# Permission / confirmation prompts shown by Claude Code, Codex, Gemini.
_NEEDS_INPUT = re.compile(
    r"do you want to (?:proceed|make this edit|create|allow|run)"
    r"|would you like to run the following command"
    r"|\[y/n\]|\(y/n\)"
    r"|^\s*[❯>›]\s*1\.\s*(?:yes|allow)"
    r"|(?:press )?enter to confirm"
    r"|allow (?:this|once|always)\b",
    re.I | re.M,
)


def _tail(rows, n):
    if isinstance(rows, str):
        rows = rows.splitlines()
    return [r.rstrip() for r in rows if r.strip()][-n:]


def has_busy_marker(rows) -> bool:
    return any(_BUSY.search(r) for r in _tail(rows, BUSY_ROWS))


def needs_user_input(rows) -> bool:
    return bool(_NEEDS_INPUT.search("\n".join(_tail(rows, INPUT_ROWS))))
