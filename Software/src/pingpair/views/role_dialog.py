"""Role picker — first-run modal dialog asking which side this app plays.

Sets ``ctx.run_state.role`` and (for Client) ``ctx.run_state.server_host_override``.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
)

from ..context import AppContext, Role


class RoleDialog(QDialog):
    """Modal dialog: pick Server / Client / Loopback."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Choose role")
        self.setModal(True)
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(12)

        outer.addWidget(QLabel("<h2>Which side is this laptop playing?</h2>"))
        outer.addWidget(
            QLabel(
                "Each PingPair instance plays one role. Run this dialog on "
                "both laptops; one picks Server, the other picks Client."
            )
        )

        # ----- radio choices -----
        self._server_radio = QRadioButton(
            f"Server  —  this laptop is {ctx.config.network.server_ip}, "
            f"listens on TCP {ctx.config.network.control_port} and 5201"
        )
        self._client_radio = QRadioButton(
            f"Client  —  this laptop is {ctx.config.network.client_ip}, "
            f"drives the 20-case sweep against the Server"
        )
        self._loopback_radio = QRadioButton(
            "Loopback (dev / single laptop)  —  Server and Client both on 127.0.0.1"
        )
        # Default: whatever was in run_state, else Loopback for new users.
        current = ctx.run_state.role
        if current is Role.SERVER:
            self._server_radio.setChecked(True)
        elif current is Role.CLIENT:
            self._client_radio.setChecked(True)
        else:
            self._loopback_radio.setChecked(True)

        group = QButtonGroup(self)
        group.addButton(self._server_radio)
        group.addButton(self._client_radio)
        group.addButton(self._loopback_radio)

        outer.addWidget(self._server_radio)
        outer.addWidget(self._client_radio)
        outer.addWidget(self._loopback_radio)

        # ----- Client-only: server hostname override -----
        host_row = QHBoxLayout()
        host_row.addSpacing(20)
        self._host_label = QLabel("Server host:")
        self._host_input = QLineEdit()
        self._host_input.setPlaceholderText(str(ctx.config.network.server_ip))
        if ctx.run_state.server_host_override:
            self._host_input.setText(ctx.run_state.server_host_override)
        host_row.addWidget(self._host_label)
        host_row.addWidget(self._host_input, stretch=1)
        outer.addLayout(host_row)

        self._client_radio.toggled.connect(self._sync_host_enabled)
        self._sync_host_enabled()

        # ----- buttons -----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ----- helpers -----------------------------------------------------

    def _sync_host_enabled(self) -> None:
        is_client = self._client_radio.isChecked()
        self._host_label.setEnabled(is_client)
        self._host_input.setEnabled(is_client)

    def selected_role(self) -> Role:
        if self._server_radio.isChecked():
            return Role.SERVER
        if self._client_radio.isChecked():
            return Role.CLIENT
        return Role.LOOPBACK

    def server_host(self) -> str | None:
        text = self._host_input.text().strip()
        return text or None

    # ----- accept ------------------------------------------------------

    def accept(self) -> None:
        rs = self.ctx.run_state
        rs.role = self.selected_role()
        rs.loopback = (rs.role is Role.LOOPBACK)
        if rs.role is Role.CLIENT:
            rs.server_host_override = self.server_host()
        else:
            rs.server_host_override = None
        super().accept()
