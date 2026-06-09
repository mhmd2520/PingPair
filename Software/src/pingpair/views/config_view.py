"""Config tab — full test-plan editor with import / export / apply.

The tab is split into three stacked areas, top to bottom:

1. **Toolbar** — Download Template, Import config, Save As, Apply,
   Reset to defaults, plus a "Current profile" label and a one-line
   status banner that turns red on parse errors and green on a
   successful apply.
2. **Form** — three group boxes (Test plan / Network / fping) with
   real validators on every field.  Editing the form re-renders the
   Raw JSON pane below in real time.
3. **Raw JSON pane** — a monospace QPlainTextEdit holding the same
   data the form does.  Bidirectional auto-sync: form edits push
   to the JSON pane in real time; JSON edits push back into the
   form ~800 ms after the user stops typing (debounced so partial
   edits mid-typing don't fight the user).  No manual sync buttons.

(A "Live CLI preview" pane was removed 2026-06-01 — it duplicated what the
form + Raw JSON already show and only ate vertical space.)

Mid-sweep guard: Apply is blocked while ``ctx.run_state.sweep_active``
so an imported config can never collide with a running sweep.  The
guard mirrors the Setup tab's role-switch guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class _NoWheelSpinBox(QSpinBox):
    """QSpinBox that ignores mouse-wheel scrolls.

    Default QSpinBox eats wheel events and changes its value, which is
    a footgun in the Config tab: a user reviewing an imported profile
    can accidentally scroll over Duration / Interval / Port spinners
    and silently change the loaded values.  Override ``wheelEvent``
    and call ``event.ignore()`` so the scroll bubbles up to the
    parent scroll area instead, where it belongs.
    """

    def wheelEvent(self, event):  # noqa: N802 — Qt naming
        event.ignore()


def _vline() -> QFrame:
    """Vertical sunken line for separating columns within a group box."""
    frame = QFrame()
    frame.setFrameShape(QFrame.Shape.VLine)
    frame.setFrameShadow(QFrame.Shadow.Sunken)
    return frame


# ---------------------------------------------------------------------------
# Line-numbered QPlainTextEdit for the Raw JSON pane
# ---------------------------------------------------------------------------


class _LineNumberArea(QWidget):
    """Sidebar widget that paints line numbers next to a QPlainTextEdit.

    Mirrors the Qt 'Code Editor' example pattern — owned by the parent
    :class:`_LinedPlainTextEdit`, which feeds it sizing and paint calls.
    """

    def __init__(self, editor: "_LinedPlainTextEdit") -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):  # noqa: N802 — Qt naming
        from PySide6.QtCore import QSize
        return QSize(self._editor.line_number_width(), 0)

    def paintEvent(self, event):  # noqa: N802 — Qt naming
        self._editor.line_number_paint(event)


class _LinedPlainTextEdit(QPlainTextEdit):
    """Monospace text edit with a left-side line-number gutter.

    Why we need this: JSON parse errors carry a ``(line N, column N)``
    hint that's useless without a visual line counter.  The user
    asked for line numbers in E3 testing — this widget is the
    cheapest way to add them without dragging in a code-editor
    dependency.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line_area = _LineNumberArea(self)
        # Re-flow margins when block count changes (e.g. new lines
        # added) or the viewport scrolls.
        self.blockCountChanged.connect(self._update_viewport_margins)
        self.updateRequest.connect(self._update_line_area)
        self._update_viewport_margins(0)

    # ----- public API used by _LineNumberArea -------------------------

    def line_number_width(self) -> int:
        """Pixel width of the gutter sized for the current block count."""
        digits = max(2, len(str(max(1, self.blockCount()))))
        # 10 px padding + advance-width of "9" * digit count.
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def line_number_paint(self, event) -> None:
        """Paint the visible line numbers into the gutter widget."""
        from PySide6.QtGui import QPainter, QPalette

        painter = QPainter(self._line_area)
        # Theme-aware: the gutter takes the editor's alternate-base shade and
        # the numbers use the muted placeholder colour, so it tracks Light /
        # Dark instead of the old hardcoded near-white (#f0f0f0 / #888) that
        # showed as a glaring white strip on the dark theme.
        pal = self.palette()
        painter.fillRect(event.rect(), pal.color(QPalette.ColorRole.AlternateBase))

        block = self.firstVisibleBlock()
        block_num = block.blockNumber()
        offset = self.contentOffset()
        top = self.blockBoundingGeometry(block).translated(offset).top()
        bottom = top + self.blockBoundingRect(block).height()

        text_pen = pal.color(QPalette.ColorRole.PlaceholderText)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(text_pen)
                painter.drawText(
                    0,
                    int(top),
                    self._line_area.width() - 4,
                    int(self.fontMetrics().height()),
                    Qt.AlignmentFlag.AlignRight,
                    str(block_num + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_num += 1

    # ----- internals --------------------------------------------------

    def _update_viewport_margins(self, _new_block_count: int) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_line_area(self, rect, dy: int) -> None:
        if dy:
            self._line_area.scroll(0, dy)
        else:
            self._line_area.update(
                0, rect.y(), self._line_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_viewport_margins(0)

    def resizeEvent(self, event):  # noqa: N802 — Qt naming
        super().resizeEvent(event)
        from PySide6.QtCore import QRect

        cr = self.contentsRect()
        self._line_area.setGeometry(
            QRect(
                cr.left(),
                cr.top(),
                self.line_number_width(),
                cr.height(),
            )
        )

from ..config import (
    AppConfig,
    ConfigIOError,
    apply_config,
    dump_config_file,
    load_config_file,
    load_default_config,
    write_template,
)
from ..config.config_io import TEMPLATE_FILENAME
from ..paths import CONFIGS_DIR
from ._base import BaseView, _shape_input
from ._validators import (
    attach_int_list,
    attach_ipv4,
    attach_ipv4_optional,
    attach_shell_safe,
    attach_subnet,
)


# ---------------------------------------------------------------------------
# Helpers for parsing comma-separated integer lists from QLineEdits.
# ---------------------------------------------------------------------------


def _parse_int_list(raw: str) -> list[int]:
    """Parse '200, 600, 1000' -> [200, 600, 1000].

    Raises ``ValueError`` with a user-readable message when any token
    isn't a positive integer.  Empty / whitespace-only input returns
    an empty list — the caller decides whether that's an error for
    its specific field.
    """
    tokens = [t.strip() for t in raw.replace(";", ",").split(",")]
    tokens = [t for t in tokens if t]
    out: list[int] = []
    for t in tokens:
        try:
            n = int(t)
        except ValueError as exc:
            raise ValueError(f"'{t}' is not an integer") from exc
        if n <= 0:
            raise ValueError(f"'{t}' must be a positive integer")
        out.append(n)
    return out


def _format_int_list(values: list[int]) -> str:
    return ", ".join(str(v) for v in values)


def _parse_str_list(raw: str) -> list[str]:
    """Parse '-l, -s, -D' -> ['-l', '-s', '-D'] (preserves order)."""
    return [t.strip() for t in raw.replace(";", ",").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class ConfigView(BaseView):
    title = "Test plan editor"

    # Emitted whenever the form changes so other views can react.
    config_changed = Signal()

    def _build_placeholder(self) -> None:
        # Suppress signal handlers while we programmatically populate
        # widgets from a freshly-loaded config.
        self._suppress_form_signals = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # ----- Header ----------------------------------------------------
        header = QLabel(f"<h2>{self.title}</h2>")
        outer.addWidget(header)

        intro = QLabel(
            "Edit the form (or paste a profile into the JSON pane below), "
            "then click <b>Apply to current session</b> to push the new "
            "test plan to the Run tab.  Use <b>Download Template</b> "
            "to bootstrap a commented <code>.json</code> profile file."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # ----- Toolbar ---------------------------------------------------
        outer.addWidget(self._build_toolbar())

        # ----- Status + current profile ---------------------------------
        self._profile_label = QLabel("Current profile: <i>built-in defaults</i>")
        outer.addWidget(self._profile_label)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        outer.addWidget(self._status_label)
        # Route the initial "Ready." through _set_status so it gets the same
        # theme-agnostic styling as later updates (no hardcoded light bg).
        self._set_status("Ready.", level="info")

        # ----- Form sections (Test plan / Network / fping) --------------
        # Added directly to ``outer`` so every section's group-box title
        # stays pinned at its natural position and never scrolls out of
        # view.  Each section sizes to its content (sizeHint); the
        # Raw JSON pane below absorbs any extra vertical space.
        form_widget = self._build_form_group()
        outer.addWidget(form_widget)

        # ----- Raw JSON --------------------------------------------------
        # Soaks up the leftover vertical space so the JSON pane grows
        # with the window while the form sections stay compact.
        outer.addWidget(self._build_raw_group(), stretch=1)

        # Initial populate — read everything from the live ctx.config.
        self._populate_from_config(self.ctx.config)

    # ----- Builders --------------------------------------------------

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._btn_template = QPushButton("Download Template…")
        self._btn_template.setToolTip(
            "Write a fully-commented Template.json into the "
            "Software\\Configs\\ folder.  Edit it externally, rename "
            "if you like, then come back and Import config."
        )
        self._btn_template.clicked.connect(self._on_download_template)
        row.addWidget(self._btn_template)

        self._btn_import = QPushButton("Import config…")
        self._btn_import.setToolTip(
            "Load a .json profile file from disk into the form below.  "
            "Doesn't apply automatically — click Apply to push the "
            "loaded values to the running session."
        )
        self._btn_import.clicked.connect(self._on_import)
        row.addWidget(self._btn_import)

        self._btn_save_as = QPushButton("Save As…")
        self._btn_save_as.setToolTip(
            "Write the current form / JSON values to a new "
            "<basename>.json file under Software\\Configs\\."
        )
        self._btn_save_as.clicked.connect(self._on_save_as)
        row.addWidget(self._btn_save_as)

        row.addStretch(1)

        self._btn_apply = QPushButton("Apply to current session")
        self._btn_apply.setStyleSheet(
            "QPushButton { background:#1565c0; color:#fff; "
            "padding:4px 14px; border-radius:3px; }"
            "QPushButton:hover { background:#0d47a1; }"
            "QPushButton:disabled { background:#999; color:#eee; }"
        )
        self._btn_apply.setToolTip(
            "Push the form's values into the live AppConfig.  "
            "Rebuilds the Run tab's 20-case grid and re-evaluates "
            "the Setup tab's prereqs against the new IPs.  Disabled "
            "while a sweep is in flight."
        )
        self._btn_apply.clicked.connect(self._on_apply)
        row.addWidget(self._btn_apply)

        self._btn_reset = QPushButton("Reset to defaults")
        self._btn_reset.setToolTip(
            "Wipe the form back to defaults.json — same shape PingPair "
            "ships with.  Doesn't apply automatically."
        )
        self._btn_reset.clicked.connect(self._on_reset_defaults)
        row.addWidget(self._btn_reset)

        return bar

    def _build_form_group(self) -> QWidget:
        box = QWidget()
        # The form must not be vertically squashed when the raw JSON
        # pane below claims all the stretch.  Setting Minimum vertical
        # means Qt's layout engine will respect the children's
        # sizeHint as a floor.
        box.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(6)

        # ----- Test plan group — one box with four side-by-side columns -----
        # All 12 fields packed into 4 columns of 3 rows each.  No
        # sub-headers — every field's label identifies the input
        # directly (fping fields are prefixed with `fping ` for clarity).
        # 3 vertical separators between the 4 columns.
        plan_box = QGroupBox("Test plan")
        plan_outer = QHBoxLayout(plan_box)
        plan_outer.setSpacing(14)

        # Build all the inputs up front, then place them into the
        # four columns below.  Keeps each widget definition close to
        # its tooltip/placeholder, and the column composition stays
        # readable at the bottom.
        self._payloads_edit = _shape_input(QLineEdit())
        self._payloads_edit.setPlaceholderText("200, 600, 1000, 1300")
        self._payloads_edit.setToolTip(
            "Comma-separated payload sizes in bytes.  The 20-case grid "
            "is the cartesian product of payloads × bandwidths."
        )
        self._payloads_edit.textEdited.connect(self._on_form_changed)
        attach_int_list(
            self._payloads_edit,
            "Comma-separated positive integers (bytes), e.g. 200, 600, 1000, 1300.",
        )

        self._bandwidths_edit = _shape_input(QLineEdit())
        self._bandwidths_edit.setPlaceholderText("10, 30, 50, 70, 90")
        self._bandwidths_edit.setToolTip(
            "Comma-separated target bandwidths in Mbps."
        )
        self._bandwidths_edit.textEdited.connect(self._on_form_changed)
        attach_int_list(
            self._bandwidths_edit,
            "Comma-separated positive integers (Mbps), e.g. 10, 30, 50, 70, 90.",
        )

        self._duration_spin = _shape_input(_NoWheelSpinBox())
        self._duration_spin.setRange(1, 3600)
        self._duration_spin.valueChanged.connect(self._on_form_changed)

        self._interval_spin = _shape_input(_NoWheelSpinBox())
        self._interval_spin.setRange(1, 60)
        self._interval_spin.valueChanged.connect(self._on_form_changed)

        proto_row = QHBoxLayout()
        self._proto_udp = QRadioButton("UDP (jitter + loss)")
        self._proto_tcp = QRadioButton("TCP (throughput only)")
        self._proto_group = QButtonGroup(self)
        self._proto_group.addButton(self._proto_udp)
        self._proto_group.addButton(self._proto_tcp)
        self._proto_udp.toggled.connect(self._on_form_changed)
        proto_row.addWidget(self._proto_udp)
        proto_row.addWidget(self._proto_tcp)
        proto_row.addStretch(1)

        self._server_ip_edit = _shape_input(QLineEdit())
        self._server_ip_edit.setPlaceholderText("192.168.1.1")
        self._server_ip_edit.textEdited.connect(self._on_form_changed)
        attach_ipv4(
            self._server_ip_edit,
            "Server-side IPv4 (canonical 192.168.1.1 on a Test Procedure LAN).",
        )

        self._client_ip_edit = _shape_input(QLineEdit())
        self._client_ip_edit.setPlaceholderText("192.168.1.2")
        self._client_ip_edit.textEdited.connect(self._on_form_changed)
        attach_ipv4(
            self._client_ip_edit,
            "Client-side IPv4 (canonical 192.168.1.2 on a Test Procedure LAN).",
        )

        self._subnet_edit = _shape_input(QLineEdit())
        self._subnet_edit.setPlaceholderText("255.255.255.0")
        self._subnet_edit.textEdited.connect(self._on_form_changed)
        attach_subnet(self._subnet_edit)

        # Group F (Q1, 2026-05-16): profile-level default gateway.
        # Empty = no gateway (point-to-point LAN, the canonical setup).
        # Per-PC overrides on the Setup tab can shadow this per machine.
        self._gateway_edit = _shape_input(QLineEdit())
        self._gateway_edit.setPlaceholderText("(blank = no gateway)")
        self._gateway_edit.setToolTip(
            "Profile-level default gateway. Blank = point-to-point LAN "
            "(no gateway), matching the canonical Test Procedure setup. "
            "Per-PC override available on the Setup tab."
        )
        self._gateway_edit.textEdited.connect(self._on_form_changed)
        attach_ipv4_optional(
            self._gateway_edit,
            "Profile default gateway. Blank = point-to-point LAN (no gateway).",
        )

        self._control_port_spin = _shape_input(_NoWheelSpinBox())
        self._control_port_spin.setRange(1, 65535)
        self._control_port_spin.valueChanged.connect(self._on_form_changed)

        self._iperf_port_spin = _shape_input(_NoWheelSpinBox())
        self._iperf_port_spin.setRange(1, 65535)
        self._iperf_port_spin.valueChanged.connect(self._on_form_changed)

        self._fping_interval_spin = _shape_input(_NoWheelSpinBox())
        self._fping_interval_spin.setRange(1, 10_000)
        self._fping_interval_spin.valueChanged.connect(self._on_form_changed)

        self._fping_extra_edit = _shape_input(QLineEdit())
        self._fping_extra_edit.setPlaceholderText("-l, -s, -D")
        self._fping_extra_edit.setToolTip(
            "Extra fping flags, comma-separated.  -l (loop) is replaced "
            "by -c <count> at runtime so the process self-terminates."
        )
        self._fping_extra_edit.textEdited.connect(self._on_form_changed)
        attach_shell_safe(self._fping_extra_edit)

        # === Column composition ====================================
        # 12 fields ÷ 4 columns = 3 rows each.
        columns: list[list[tuple[str, Any]]] = [
            # Column 1 — payload + bandwidth grid + duration
            [
                ("Payloads (B):", self._payloads_edit),
                ("Bandwidths (Mbps):", self._bandwidths_edit),
                ("Duration per case (s):", self._duration_spin),
            ],
            # Column 2 — iperf3 timing + protocol + server
            [
                ("Interval (iperf3 -i, s):", self._interval_spin),
                ("Protocol:", proto_row),
                ("Server IP:", self._server_ip_edit),
            ],
            # Column 3 — client + subnet + gateway + control port (4 rows
            # since Q1 added Gateway as the 13th profile parameter)
            [
                ("Client IP:", self._client_ip_edit),
                ("Subnet mask:", self._subnet_edit),
                ("Gateway:", self._gateway_edit),
                ("Control port:", self._control_port_spin),
            ],
            # Column 4 — iperf3 port + fping bits
            [
                ("iperf3 port:", self._iperf_port_spin),
                ("fping Interval (-p, ms):", self._fping_interval_spin),
                ("fping Extra args:", self._fping_extra_edit),
            ],
        ]

        for idx, col_rows in enumerate(columns):
            col_layout = QVBoxLayout()
            col_layout.setSpacing(6)
            form = QFormLayout()
            form.setVerticalSpacing(6)
            for label, widget in col_rows:
                form.addRow(label, widget)
            col_layout.addLayout(form)
            col_layout.addStretch(1)
            plan_outer.addLayout(col_layout, stretch=1)
            # VLine between columns, not after the last one.
            if idx < len(columns) - 1:
                plan_outer.addWidget(_vline())

        vbox.addWidget(plan_box)

        return box

    def _build_raw_group(self) -> QWidget:
        box = QGroupBox("Raw JSON")
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)

        # Inline explanation so the auto-sync behaviour is discoverable
        # without hovering for a tooltip.
        hint = QLabel(
            "Edits in the form above appear here instantly.  Paste or "
            "type a profile here and the form updates automatically "
            "after you stop typing (parse errors land in the status "
            "banner above; the form is only updated on a clean parse)."
        )
        hint.setWordWrap(True)
        # Muted but theme-neutral (the old #666 was unreadable on dark).
        hint.setStyleSheet("color:#8a97ad;")
        vbox.addWidget(hint)

        # Line-numbered subclass so JSON parse errors with
        # "(line N, column N)" hints are actionable visually.
        self._raw_edit = _LinedPlainTextEdit()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._raw_edit.setFont(mono)
        self._raw_edit.setMinimumHeight(180)
        self._raw_edit.setPlaceholderText(
            "The form's contents serialise here automatically.  Paste "
            "a complete profile here and the form will update "
            "~1 s after you stop typing."
        )
        # Guard flag — set True whenever we programmatically rewrite
        # the pane, so the textChanged handler below skips its work
        # and we don't fight ourselves in a feedback loop.
        self._suppress_raw_signals = False
        self._raw_edit.textChanged.connect(self._on_raw_text_changed)
        vbox.addWidget(self._raw_edit, stretch=1)

        # Debounced JSON → form auto-sync timer.  Each textChanged
        # resets it; only when the user has been idle for 800 ms do
        # we attempt to parse + populate.  This keeps the form from
        # flickering while the user types partial JSON.
        self._json_sync_timer = QTimer(self)
        self._json_sync_timer.setSingleShot(True)
        self._json_sync_timer.setInterval(800)
        self._json_sync_timer.timeout.connect(self._auto_sync_json_to_form)

        return box

    # ----- Form ↔ AppConfig --------------------------------------------

    def _populate_from_config(self, cfg: AppConfig) -> None:
        """Push every field from ``cfg`` into the form widgets.

        Signal handlers are temporarily suppressed so the textEdited /
        valueChanged callbacks don't fire 12 times and re-render the
        JSON pane on every step.  The JSON pane is rendered once at
        the end from the resulting in-memory state.
        """
        self._suppress_form_signals = True
        try:
            self._payloads_edit.setText(_format_int_list(cfg.test_plan.payloads_bytes))
            self._bandwidths_edit.setText(_format_int_list(cfg.test_plan.bandwidths_mbps))
            self._duration_spin.setValue(cfg.test_plan.duration_s)
            self._interval_spin.setValue(cfg.test_plan.interval_s)
            if cfg.test_plan.protocol == "udp":
                self._proto_udp.setChecked(True)
            else:
                self._proto_tcp.setChecked(True)

            self._server_ip_edit.setText(str(cfg.network.server_ip))
            self._client_ip_edit.setText(str(cfg.network.client_ip))
            self._subnet_edit.setText(cfg.network.subnet_mask)
            self._gateway_edit.setText(
                str(cfg.network.gateway) if cfg.network.gateway else ""
            )
            self._control_port_spin.setValue(cfg.network.control_port)
            self._iperf_port_spin.setValue(cfg.network.iperf3_port)

            self._fping_interval_spin.setValue(cfg.fping.interval_ms)
            self._fping_extra_edit.setText(", ".join(cfg.fping.extra_args))
        finally:
            self._suppress_form_signals = False

        self._render_raw_from_form_data(cfg)

    def _read_form_into_dict(self) -> dict[str, Any]:
        """Build the JSON-shaped dict from current form values.

        Raises :class:`ValueError` on any list-parse failure — the
        caller surfaces the message in the status banner.  IP /
        subnet validation is left to pydantic for consistency with
        the schema's IPvAnyAddress fields.
        """
        payloads = _parse_int_list(self._payloads_edit.text())
        if not payloads:
            raise ValueError("Payloads list is empty — enter at least one.")
        bandwidths = _parse_int_list(self._bandwidths_edit.text())
        if not bandwidths:
            raise ValueError("Bandwidths list is empty — enter at least one.")

        proto = "udp" if self._proto_udp.isChecked() else "tcp"

        gw_raw = self._gateway_edit.text().strip()
        return {
            "network": {
                "server_ip": self._server_ip_edit.text().strip(),
                "client_ip": self._client_ip_edit.text().strip(),
                "subnet_mask": self._subnet_edit.text().strip() or "255.255.255.0",
                "gateway": gw_raw or None,
                "control_port": int(self._control_port_spin.value()),
                "iperf3_port": int(self._iperf_port_spin.value()),
            },
            "test_plan": {
                "payloads_bytes": payloads,
                "bandwidths_mbps": bandwidths,
                "duration_s": int(self._duration_spin.value()),
                "interval_s": int(self._interval_spin.value()),
                "protocol": proto,
            },
            "fping": {
                "interval_ms": int(self._fping_interval_spin.value()),
                "extra_args": _parse_str_list(self._fping_extra_edit.text()),
            },
            "report": self.ctx.config.report.model_dump(mode="json"),
            "ui": self.ctx.config.ui.model_dump(mode="json"),
        }

    def _validate_form(self) -> AppConfig:
        """Read the form, build a dict, validate via pydantic.

        Raises :class:`ValueError` for form-level issues (empty lists,
        bad integer tokens) or :class:`ConfigIOError` for schema
        violations.  Caller decides how to surface them.
        """
        data = self._read_form_into_dict()
        try:
            return AppConfig.model_validate(data)
        except Exception as exc:  # pydantic.ValidationError + edge cases
            # Re-shape so the caller has one exception class to catch.
            raise ConfigIOError(str(exc)) from exc

    def _render_raw_from_form_data(self, cfg: AppConfig | None = None) -> None:
        """Re-serialise either the live cfg or the current form into the JSON pane.

        Sets ``_suppress_raw_signals`` while writing so the JSON-pane's
        ``textChanged`` handler doesn't bounce back into ``_auto_sync_json_to_form``
        and re-populate the form we just read from.  Without this guard
        every form keystroke would trigger an infinite ping-pong.
        """
        if cfg is not None:
            data = cfg.model_dump(mode="json")
        else:
            try:
                data = self._read_form_into_dict()
            except ValueError as exc:
                # Surface the form error but keep the previous JSON
                # text — clobbering it would lose the user's edit.
                self._set_status(str(exc), level="error")
                return
        text = json.dumps(data, indent=2)
        self._suppress_raw_signals = True
        try:
            self._raw_edit.setPlainText(text)
        finally:
            self._suppress_raw_signals = False

    # ----- Event handlers ---------------------------------------------

    def _on_form_changed(self) -> None:
        if self._suppress_form_signals:
            return
        # Re-render JSON pane in real time so the two stay in sync.
        # _render_raw_from_form_data toggles _suppress_raw_signals
        # internally so this doesn't bounce back through
        # _on_raw_text_changed.
        self._render_raw_from_form_data()
        # Clear stale red status the first time the user edits.
        if "❌" in self._status_label.text() or "✗" in self._status_label.text():
            self._set_status("Edited — click Apply when ready.", level="info")
        self.config_changed.emit()

    def _on_raw_text_changed(self) -> None:
        """JSON pane was edited — start the debounce timer.

        Skips when the change was programmatic (we wrote the pane
        ourselves via :meth:`_render_raw_from_form_data` or
        :meth:`_populate_from_config`).  Otherwise restart the 800 ms
        timer; if the user types another character before it fires
        we cancel and reschedule.
        """
        if self._suppress_raw_signals:
            return
        # Reschedule the debounced parse + populate.
        self._json_sync_timer.start()

    def _auto_sync_json_to_form(self) -> None:
        """Parse the JSON pane and quietly populate the form.

        Called by :class:`QTimer` 800 ms after the last user keystroke
        in the JSON pane.  Three outcomes:

        * **Empty pane** — leave the form alone, set a neutral status.
        * **Parse / schema error** — leave the form alone, surface the
          error in the status banner.  The JSON pane text is preserved
          so the user can fix it.
        * **Valid** — populate the form (which programmatically
          rewrites the JSON pane in canonical formatting; the
          re-entrance guard prevents a loop), and report success.
        """
        text = self._raw_edit.toPlainText().strip()
        if not text:
            self._set_status(
                "JSON pane is empty — form not updated.", level="info"
            )
            return
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            self._set_status(
                f"JSON parse error: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno}) — form not updated",
                level="error",
            )
            return
        if not isinstance(raw, dict):
            self._set_status(
                "Top-level JSON must be an object — form not updated",
                level="error",
            )
            return

        # Strip `_comment*` keys so a pasted Template.json works.
        from ..config.config_io import _merge_sections, _strip_comments

        cleaned = _strip_comments(raw)
        defaults = load_default_config().model_dump(mode="json")
        merged = _merge_sections(defaults, cleaned)
        try:
            cfg = AppConfig.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            self._set_status(
                f"Schema error — form not updated: {exc}", level="error"
            )
            return
        # Valid — populate the form quietly.  _populate_from_config
        # toggles _suppress_form_signals so the form widgets'
        # programmatic updates don't re-render the JSON pane, and the
        # canonical re-render that DOES happen inside it is guarded by
        # _suppress_raw_signals.
        self._populate_from_config(cfg)
        self._set_status(
            "JSON pane parsed — form updated.  Click Apply to push to "
            "the current session.",
            level="ok",
        )

    def _on_download_template(self) -> None:
        # Default destination — Software\Configs\Template.json.
        # Plain ``.json`` suffix: keeps the Save-As Qt dialog from
        # producing artefacts like ``Test.config.config.json`` when
        # the user types a `.config` suffix.
        default_path = CONFIGS_DIR / TEMPLATE_FILENAME

        # If the file already exists, warn the user before clobbering it.
        # Common failure mode: user downloaded a template, edited it
        # in place without renaming, and the second click wipes their
        # customisation.  Bail out unless they explicitly say overwrite.
        if default_path.exists():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Template already exists")
            box.setText(
                f"<b>{default_path.name}</b> already exists in "
                f"{default_path.parent}."
            )
            box.setInformativeText(
                "Re-downloading the template will <b>overwrite</b> the "
                "existing file.  If you have customised it in place, "
                "you'll lose those edits.\n\n"
                "Best practice: rename your customised profile to "
                "something like <i>MyProfile.json</i> before editing, "
                "so the next template download can't touch it.\n\n"
                "What would you like to do?"
            )
            overwrite_btn = box.addButton(
                "Overwrite template", QMessageBox.ButtonRole.DestructiveRole
            )
            saveas_btn = box.addButton(
                "Save fresh template as…", QMessageBox.ButtonRole.AcceptRole
            )
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(saveas_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is overwrite_btn:
                target = default_path
            elif clicked is saveas_btn:
                # Let the user pick a name — same dir, default to a
                # disambiguated filename.  Filter is `*.json` only so
                # Qt auto-appends `.json` if the user omits it.
                path_str, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save fresh template as",
                    str(default_path.parent / "Template_2.json"),
                    "JSON files (*.json);;All files (*.*)",
                )
                if not path_str:
                    return
                target = Path(path_str)
            else:
                # Cancel — do nothing.
                return
        else:
            target = default_path

        try:
            path = write_template(target)
        except ConfigIOError as exc:
            self._set_status(str(exc), level="error")
            return
        self._set_status(
            f"Wrote template to {path}.  Rename it before editing if you "
            f"want it to survive the next Download Template click, then "
            f"come back and use Import config to load it.",
            level="ok",
        )
        # Also offer to open the containing folder.
        self._maybe_open_folder(path.parent)

    def _on_import(self) -> None:
        # Default to CONFIGS_DIR if it exists, else PROJECT_ROOT.
        start_dir = str(CONFIGS_DIR) if CONFIGS_DIR.exists() else str(Path.cwd())
        # Single ``*.json`` filter (plus All files as escape hatch for
        # any unusual extensions).  load_config_file doesn't care
        # about the suffix — it just parses the JSON content.
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import profile",
            start_dir,
            "JSON files (*.json);;All files (*.*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            cfg = load_config_file(path)
        except ConfigIOError as exc:
            self._set_status(str(exc), level="error")
            return
        self._populate_from_config(cfg)
        self._profile_label.setText(f"Current profile: <b>{path.name}</b> ({path})")
        self._set_status(
            f"Loaded {path.name}.  Click Apply to push the values to "
            "the running session.",
            level="ok",
        )

    def _on_save_as(self) -> None:
        try:
            cfg = self._validate_form()
        except (ValueError, ConfigIOError) as exc:
            self._set_status(f"Cannot save: {exc}", level="error")
            return

        start_dir = CONFIGS_DIR
        try:
            start_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            start_dir = Path.cwd()
        default_path = start_dir / "MyProfile.json"

        # Single-extension filter — Qt auto-appends `.json` on Windows
        # if the user omits the suffix, producing ``MyProfile.json``.
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save profile as",
            str(default_path),
            "JSON files (*.json);;All files (*.*)",
        )
        if not path_str:
            return

        path = Path(path_str)
        try:
            dump_config_file(cfg, path)
        except ConfigIOError as exc:
            self._set_status(f"Cannot save: {exc}", level="error")
            return
        self._profile_label.setText(f"Current profile: <b>{path.name}</b> ({path})")
        self._set_status(f"Saved profile to {path}.", level="ok")

    def _on_apply(self) -> None:
        if self.ctx.run_state.sweep_active:
            QMessageBox.warning(
                self,
                "Sweep in progress",
                "A sweep is currently running.  Stop it on the Run tab "
                "before applying a new config.",
            )
            return
        try:
            cfg = self._validate_form()
        except (ValueError, ConfigIOError) as exc:
            self._set_status(f"Cannot apply: {exc}", level="error")
            return

        # Show the user immediate visual feedback BEFORE the heavy
        # Run tab rebuild starts.
        self._set_status("Applying - rebuilding Run tab...", level="info")
        self._btn_apply.setEnabled(False)
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

        # Defer the actual apply to the next event-loop tick so the
        # banner paints before the rebuild blocks the GUI thread. The
        # ``self`` context arg makes Qt cancel the deferred call if this
        # view is destroyed before the tick fires — otherwise the lambda
        # would invoke _do_apply on a deleted widget.
        QTimer.singleShot(0, self, lambda: self._do_apply(cfg))

    def _do_apply(self, cfg: AppConfig) -> None:
        """Second half of :meth:`_on_apply` - runs on the next event tick."""
        try:
            apply_config(self.ctx, cfg)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.exception("apply_config failed")
            self._set_status(f"Apply failed: {exc}", level="error")
            self._btn_apply.setEnabled(True)
            return
        self._set_status(
            "Applied to current session.  The Run tab's grid was "
            "rebuilt to match.",
            level="ok",
        )
        self._btn_apply.setEnabled(True)
        self.ctx.logger.info(
            "Config applied via Config tab: payloads=%s bandwidths=%s "
            "duration=%ss protocol=%s server_ip=%s client_ip=%s",
            cfg.test_plan.payloads_bytes,
            cfg.test_plan.bandwidths_mbps,
            cfg.test_plan.duration_s,
            cfg.test_plan.protocol,
            cfg.network.server_ip,
            cfg.network.client_ip,
        )

    def _on_reset_defaults(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Reset to defaults",
            "Wipe the form back to the shipped defaults?  This won't "
            "touch the running session until you click Apply.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        cfg = load_default_config()
        self._populate_from_config(cfg)
        self._profile_label.setText("Current profile: <i>built-in defaults</i>")
        self._set_status(
            "Form reset to defaults.json.  Click Apply to push.",
            level="info",
        )

    # ----- Status banner ----------------------------------------------

    def _set_status(self, msg: str, *, level: str = "info") -> None:
        prefix = {
            "ok": "OK  ",
            "error": "X  ",
            "info": "",
        }.get(level, "")
        # Theme-agnostic: ok / error use a saturated background with white
        # text (reads on both Light and Dark, like the role banner / status
        # pills); info is transparent with a muted text colour. This avoids
        # the old light-grey #f3f3f3 fill that turned into a white bar on the
        # dark theme, and survives a live theme switch without restyling.
        bg, fg = {
            "ok": ("#1f6f3a", "#ffffff"),
            "error": ("#8b1a1a", "#ffffff"),
            "info": ("transparent", "#8a97ad"),
        }.get(level, ("transparent", "#8a97ad"))
        self._status_label.setText(prefix + msg)
        self._status_label.setStyleSheet(
            f"color:{fg}; padding:4px 8px; background:{bg}; border-radius:3px;"
        )

    def _maybe_open_folder(self, folder: Path) -> None:
        """Offer to open the file-explorer at ``folder`` (best-effort)."""
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except Exception:  # noqa: BLE001
            pass

    # ----- BaseView override -----------------------------------------

    def refresh(self) -> None:
        """Called by MainWindow when the user activates the tab."""
        self._populate_from_config(self.ctx.config)
        self._btn_apply.setEnabled(not self.ctx.run_state.sweep_active)
