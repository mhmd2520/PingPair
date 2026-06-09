"""Reusable input validators with diagnostic tooltips.

Used by Setup, Config, Ping, Save Options, Analysis, and Run tabs so the
user gets immediate visual + textual feedback when input doesn't parse.
Validation is **permissive mid-typing** (no keypress blocking — that's
user-hostile when typing an IP from memory). Pydantic on Apply remains
the authoritative final check; this module is the friendly first-line UX.

Four-layer mechanism:

0. **Keypress restriction** — for fields with a narrow character set
   (IPv4 = digits + dots, integer lists = digits + commas + spaces),
   a ``QRegularExpressionValidator`` blocks any out-of-set character
   from being typed in the first place. Pasted text outside the set
   is also rejected wholesale. Fields with broad character sets
   (paths, filenames, fping flags) skip this layer because their
   allowed set is too wide for keypress restriction — they rely on
   the diagnostic.

And the original three:

1. **Diagnostic** — each ``attach_*`` helper calls a per-shape diagnose
   function that returns ``(valid, message)``. On valid input the
   message is the empty string; on invalid input it pinpoints *what*
   is wrong (e.g. "Octet 4 (999) exceeds 255 — max is 255." instead of
   the generic format hint).

2. **Visual highlight** — sets the dynamic property ``invalid`` on the
   widget + calls ``style.unpolish/polish`` so Qt's stylesheet engine
   re-evaluates the property-based rule installed at app startup via
   ``install_global_qss``. The rule
   ``QLineEdit[invalid="true"] { red border + tint }`` matches only
   invalid fields, no sibling cascade.

3. **Hover tooltip** — a per-widget ``_ForceTooltipFilter`` catches
   ``QEvent::ToolTip`` and calls ``QToolTip.showText()`` directly.
   Bypasses Qt's default tooltip-show path which (under PySide6 6.11)
   skips widgets whose property-based QSS rule isn't currently active.
   On invalid input the diagnostic message is shown; on valid input
   the base format hint is shown so the user always learns the shape.

Shipped 2026-05-17 as part of the Group F follow-up batch (Round-15).
"""

from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import QLineEdit, QToolTip


# ----- regex predicates ---------------------------------------------------

_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
)

# Valid IPv4 subnet masks (contiguous 1-bits). Used as a fast pass; the
# diagnose function falls back to bit-pattern checking for a helpful
# error message on non-standard input.
_SUBNET_RE = re.compile(
    r"^(?:"
    r"255\.255\.255\.(?:0|128|192|224|240|248|252|254|255)"
    r"|255\.255\.(?:0|128|192|224|240|248|252|254|255)\.0"
    r"|255\.(?:0|128|192|224|240|248|252|254|255)\.0\.0"
    r"|(?:0|128|192|224|240|248|252|254|255)\.0\.0\.0"
    r")$"
)

_PATH_BAD_RE = re.compile(r'[<>|"*?]')
_FILENAME_BAD_RE = re.compile(r'[<>|"*?:/\\]')
_SHELL_META_CHARS = set("|&><^`$;()")


# ----- diagnostic functions ----------------------------------------------
# Each returns (valid: bool, error_message: str). On valid input the
# message is empty (caller substitutes the base format hint). On invalid
# input the message pinpoints WHAT is wrong.

def _diagnose_ipv4(text: str) -> tuple[bool, str]:
    """Diagnose IPv4 dotted-quad input. Returns (valid, error_message)."""
    t = text.strip()
    if not t:
        return False, "IPv4 cannot be empty — type four octets separated by dots."
    parts = t.split(".")
    if len(parts) != 4:
        return False, (
            f"IPv4 needs 4 octets separated by dots, got {len(parts)} "
            f"({'.'.join(parts)!r})."
        )
    for i, p in enumerate(parts, 1):
        if not p:
            return False, f"Octet {i} is empty — expected 0-255 between dots."
        if not p.isdigit():
            return False, (
                f"Octet {i} ({p!r}) is not numeric — only digits 0-9 allowed."
            )
        n = int(p)
        if n > 255:
            return False, f"Octet {i} ({n}) exceeds 255 — max is 255."
        if len(p) > 1 and p[0] == "0":
            return False, (
                f"Octet {i} ({p!r}) has a leading zero — write {n} without padding."
            )
    return True, ""


def _diagnose_subnet(text: str) -> tuple[bool, str]:
    """Diagnose IPv4 subnet mask. Returns (valid, error_message)."""
    t = text.strip()
    if not t:
        return True, ""  # empty handled as allow_blank by caller
    # Reuse the IPv4 diagnoser for basic shape (4 octets, 0-255).
    shape_ok, shape_err = _diagnose_ipv4(t)
    if not shape_ok:
        return False, shape_err.replace("IPv4", "Netmask")
    # Then check the contiguous-bits rule.
    if _SUBNET_RE.match(t):
        return True, ""
    # Non-contiguous — find the broken transition and report.
    octets = [int(p) for p in t.split(".")]
    bits = "".join(f"{o:08b}" for o in octets)
    # Walk bits: must be all 1s then all 0s.
    saw_zero = False
    for i, b in enumerate(bits):
        if b == "0":
            saw_zero = True
        elif saw_zero:
            return False, (
                f"Netmask must be contiguous 1-bits from the left "
                f"(found a 1-bit after a 0-bit at bit {i + 1}). "
                f"Standard examples: 255.255.255.0, 255.255.0.0."
            )
    return False, "Netmask format unexpected — use 255.255.255.0 or similar."


def _diagnose_int_list(text: str) -> tuple[bool, str]:
    """Diagnose comma-separated positive integers. Returns (valid, error_message)."""
    if not text.strip():
        return False, "List cannot be empty — type at least one positive integer."
    items = [s.strip() for s in text.split(",")]
    if items[-1] == "":
        return False, "Trailing comma — drop it or add another number after."
    for i, item in enumerate(items, 1):
        if not item:
            return False, f"Item {i} is empty — drop the extra comma."
        if not item.isdigit():
            return False, (
                f"Item {i} ({item!r}) is not a positive integer — "
                f"only digits 0-9 allowed."
            )
        if item == "0":
            # Match config_view._parse_int_list, which rejects 0 on Apply.
            # Without this the field looked valid (no red border) yet Apply
            # failed with a confusing dialog.
            return False, f"Item {i} (0) must be positive — the minimum is 1."
        if item.startswith("0"):
            return False, (
                f"Item {i} ({item!r}) has a leading zero — "
                f"write {int(item)} without padding."
            )
    return True, ""


def _diagnose_shell_safe(text: str) -> tuple[bool, str]:
    """Diagnose fping extra-args field for shell metacharacters."""
    if not text.strip():
        return True, ""
    for i, ch in enumerate(text, 1):
        if ch in _SHELL_META_CHARS:
            return False, (
                f"Shell metacharacter {ch!r} at position {i} is not allowed "
                f"in fping flags."
            )
    return True, ""


def _diagnose_path_safe(text: str) -> tuple[bool, str]:
    """Diagnose folder path for Windows-illegal characters."""
    t = text.strip()
    if not t:
        return False, "Folder path cannot be empty."
    m = _PATH_BAD_RE.search(t)
    if m:
        return False, (
            f"Character {m.group(0)!r} at position {m.start() + 1} is not "
            f"allowed in a path. Forbidden: < > | \" * ?"
        )
    return True, ""


def _diagnose_filename_safe(text: str) -> tuple[bool, str]:
    """Diagnose filename pattern for Windows-illegal characters."""
    t = text.strip()
    if not t:
        return False, "Filename cannot be empty."
    m = _FILENAME_BAD_RE.search(t)
    if m:
        bad = m.group(0)
        hint = (
            " (use {date} / {time} tokens for variable parts)"
            if bad in ("/", "\\", ":")
            else ""
        )
        return False, (
            f"Character {bad!r} at position {m.start() + 1} is not allowed "
            f"in a filename{hint}."
        )
    return True, ""


# ----- global QSS ---------------------------------------------------------

GLOBAL_INVALID_QSS = (
    'QLineEdit[invalid="true"] {\n'
    '    border: 2px solid #d04040;\n'
    '    background-color: rgba(208, 64, 64, 60);\n'
    '}\n'
)


def install_global_qss(app) -> None:
    """Append the validator's invalid-state rule to the app stylesheet.

    Idempotent — safe to call multiple times.
    """
    existing = app.styleSheet() or ""
    if "QLineEdit[invalid=" in existing:
        return
    sep = "\n\n" if existing.strip() else ""
    app.setStyleSheet(existing + sep + GLOBAL_INVALID_QSS)


# ----- per-widget machinery ----------------------------------------------

class _ForceTooltipFilter(QObject):
    """Force-show a tooltip on hover regardless of widget styling state.

    Under PySide6 6.11, Qt's default tooltip-show path sometimes fails
    to fire on widgets whose dynamic-property QSS selector isn't
    currently matching. This filter listens for ``QEvent::ToolTip`` and
    explicitly calls ``QToolTip.showText()``.
    """

    def __init__(self, widget: QLineEdit, tooltip_text: str) -> None:
        super().__init__(widget)
        self._tooltip = tooltip_text
        widget.installEventFilter(self)

    def update_tooltip(self, text: str) -> None:
        self._tooltip = text

    def eventFilter(self, obj, event) -> bool:  # noqa: D401
        if event.type() == QEvent.Type.ToolTip:
            try:
                pos = event.globalPos()
            except AttributeError:
                pos = event.globalPosition().toPoint()
            # Always handle the ToolTip event for this widget — show the
            # current text (an empty string hides any tooltip). Consuming
            # it unconditionally means Qt's default tooltip path never
            # races this one regardless of the widget's QSS state.
            QToolTip.showText(pos, self._tooltip, obj)
            return True
        return False


def _mark(widget: QLineEdit, *, invalid: bool) -> None:
    """Set the ``invalid`` dynamic property + re-polish so QSS re-evaluates."""
    new_val = "true" if invalid else "false"
    if widget.property("invalid") == new_val:
        return
    widget.setProperty("invalid", new_val)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _attach(
    widget: QLineEdit,
    diagnose: Callable[[str], tuple[bool, str]],
    base_hint: str,
    *,
    allow_blank: bool,
) -> None:
    """Wire diagnose-based validation with dynamic tooltip text.

    On valid input the tooltip shows ``base_hint`` (the format help).
    On invalid input the tooltip shows the specific error message
    returned by ``diagnose``. This way users always learn the format,
    AND get pinpoint feedback on what's wrong.
    """
    widget.setToolTip(base_hint)  # docs / fallback
    filt = _ForceTooltipFilter(widget, base_hint)
    widget._validator_tooltip_filter = filt  # type: ignore[attr-defined]

    def _refresh(text: str) -> None:
        if not text.strip() and allow_blank:
            _mark(widget, invalid=False)
            filt.update_tooltip(base_hint)
            return
        valid, err_msg = diagnose(text)
        _mark(widget, invalid=not valid)
        filt.update_tooltip(base_hint if valid else err_msg)

    widget.textChanged.connect(_refresh)
    _refresh(widget.text())


# ----- keypress-level character-class filters ----------------------------

# Patterns are partial-match (no $ anchor) so QRegularExpressionValidator
# treats typing-in-progress as Acceptable. Only characters outside the
# set are rejected at the keypress.
_IPV4_CHARSET = r"^[0-9.]*$"
_INT_LIST_CHARSET = r"^[0-9,\s]*$"


def _install_charset_filter(widget: QLineEdit, pattern: str) -> None:
    """Reject any character outside ``pattern`` at keypress time.

    The Qt ``QRegularExpressionValidator`` treats the input as one of
    three states: Acceptable (full match), Intermediate (prefix
    match), or Invalid (no match). Anchor-less patterns like
    ``r\"^[0-9.]*$\"`` accept every typing-in-progress prefix and
    reject only out-of-set characters, which is the behaviour we want.

    Pasted text outside the set is rejected by Qt automatically; the
    widget keeps its previous value.
    """
    rx = QRegularExpression(pattern)
    validator = QRegularExpressionValidator(rx, widget)
    widget.setValidator(validator)


# ----- public attach_* helpers -------------------------------------------

def attach_ipv4(
    widget: QLineEdit,
    tooltip: str = "",
    *,
    allow_blank: bool = False,
) -> None:
    """Red border + diagnostic tooltip when text isn't a valid IPv4."""
    tt = tooltip or "IPv4 only - four octets 0-255, e.g. 192.168.1.10."
    _install_charset_filter(widget, _IPV4_CHARSET)
    _attach(widget, _diagnose_ipv4, tt, allow_blank=allow_blank)


def attach_ipv4_optional(widget: QLineEdit, tooltip: str = "") -> None:
    """IPv4 validator that treats blank as valid (Gateway, override IP)."""
    tt = tooltip or (
        "IPv4 only - four octets 0-255. Blank = use the profile default."
    )
    _install_charset_filter(widget, _IPV4_CHARSET)
    _attach(widget, _diagnose_ipv4, tt, allow_blank=True)


def attach_subnet(widget: QLineEdit, tooltip: str = "") -> None:
    """Red border + diagnostic tooltip on bad IPv4 subnet mask."""
    tt = tooltip or "Standard IPv4 netmask, e.g. 255.255.255.0."
    _install_charset_filter(widget, _IPV4_CHARSET)
    _attach(widget, _diagnose_subnet, tt, allow_blank=True)


def attach_int_list(
    widget: QLineEdit,
    tooltip: str = "",
    *,
    allow_blank: bool = False,
) -> None:
    """Red border + diagnostic tooltip on bad comma-separated int list.

    ``allow_blank=True`` treats an empty field as valid — used by the
    Analysis tab's payload / bandwidth filters, where blank means "keep
    every value" rather than an error.
    """
    tt = tooltip or (
        "Comma-separated positive integers, e.g. 200, 600, 1000, 1300."
    )
    _install_charset_filter(widget, _INT_LIST_CHARSET)
    _attach(widget, _diagnose_int_list, tt, allow_blank=allow_blank)


def attach_shell_safe(widget: QLineEdit, tooltip: str = "") -> None:
    """Red border + diagnostic tooltip on shell metacharacters."""
    tt = tooltip or (
        "fping flags only - no shell metacharacters (| & > < ^ ` $ ; ( ) )."
    )
    _attach(widget, _diagnose_shell_safe, tt, allow_blank=True)


def attach_path_safe(
    widget: QLineEdit,
    tooltip: str = "",
    *,
    allow_blank: bool = False,
) -> None:
    """Red border + diagnostic tooltip on Windows-illegal path characters."""
    tt = tooltip or "Folder path. Forbidden characters: < > | \" * ?"
    _attach(widget, _diagnose_path_safe, tt, allow_blank=allow_blank)


def attach_filename_safe(
    widget: QLineEdit,
    tooltip: str = "",
    *,
    allow_blank: bool = False,
) -> None:
    """Red border + diagnostic tooltip on Windows-illegal filename characters."""
    tt = tooltip or "Filename. Forbidden: < > | \" * ? : / \\"
    _attach(widget, _diagnose_filename_safe, tt, allow_blank=allow_blank)
