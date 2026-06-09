"""Help tab — a navigable, theme-aware user guide (Feature 8).

The guide is a folder of HTML sections under
``resources/help/<NN-slug>/index.html`` (see :mod:`pingpair.help_loader`).
This view renders them with:

* a **sidebar** (``QListWidget``) — one button-styled entry per section,
  labelled by its ``<title>`` (the bare tab name for the per-tab guides; a
  descriptive name for the extra references like fping / iperf3),
* a **content pane** (``QTextBrowser``) that renders the selected section,
* a **guide-wide search** box: typing a term and pressing Enter / Find scans
  *every* section; **Prev / Next** then step through every match across the
  whole guide — switching sections as needed — with an ``N / total`` readout,
* **palette-driven CSS** injected at render time so the guide reads correctly
  on both the Light and Dark themes (the section HTML carries no colours of
  its own).

We deliberately use ``QTextBrowser`` (Qt's built-in rich-text widget), not
``QWebEngineView`` — it needs no Chromium dependency and keeps the Phase-5
one-folder build lean. The section HTML therefore targets Qt's HTML 4 / CSS
2.1 subset: headings, tables, images, links, and simple callout blocks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPalette,
    QPixmap,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
)

from .. import theme
from ..context import Role
from ..help_loader import HelpSection, list_sections
from ..paths import HELP_DIR
from ._base import BaseView
from ._image_zoom import (
    ImageZoomDialog,
    NoZoomTextBrowser,
    embed_scaled_image,
    open_zoom,
)


class HelpView(BaseView):
    title = "Help"

    def _build_placeholder(self) -> None:
        self._sections: list[HelpSection] = list_sections(HELP_DIR)
        self._browser: QTextBrowser | None = None
        self._current: int = 0
        # Guide-wide search state. _match_sections holds the indexes of every
        # section with >=1 hit (in display order); _match_pos is the 1-based
        # position of the currently-highlighted occurrence within _total_matches.
        self._search_query: str = ""
        self._match_sections: list[int] = []
        self._total_matches: int = 0
        self._match_pos: int = 0
        # Holds the live full-screen image viewer so it isn't garbage-collected
        # while shown (see _open_zoom).
        self._zoom_dialog: ImageZoomDialog | None = None
        # Memo for inline figures: (abs_path, width, dpr) -> (scaled QPixmap,
        # logical_width). Keeps _render off the decode+smooth-scale path on
        # repeat visits (tab activation, theme switch, cross-section search hop).
        self._scaled_cache: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        outer.addWidget(QLabel("<h2>Help — PingPair user guide</h2>"))
        intro = QLabel(
            "Step-by-step guidance for every tab, plus a troubleshooting "
            "section and the fping / iperf3 flag references. Pick a topic on "
            "the left, or search the whole guide from the box above the page."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        if not self._sections:
            # Assets missing (e.g. package-data not installed). Don't crash —
            # show a clear message and bail before wiring the nav widgets.
            fallback = QTextBrowser()
            fallback.setPlainText(
                "The guide content could not be found.\n\n"
                f"Expected section folders under:\n{HELP_DIR}\n\n"
                "If you're running from source, re-run "
                "`pip install -e .` so the help assets resolve."
            )
            outer.addWidget(fallback, stretch=1)
            return

        body = QHBoxLayout()
        body.setSpacing(12)

        # ---- sidebar (button-styled section list) ------------------------
        self._sidebar = QListWidget()
        for section in self._sections:
            self._sidebar.addItem(section.title)
        self._sidebar.currentRowChanged.connect(self._on_section_changed)
        self._size_sidebar_to_content()
        body.addWidget(self._sidebar, stretch=0)

        # ---- content column (search toolbar + browser) ------------------
        content = QVBoxLayout()
        content.setSpacing(8)

        toolbar = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search the whole guide — results appear as you type…")
        self._search.setClearButtonEnabled(True)
        self._search.returnPressed.connect(self._on_find)
        self._search.textChanged.connect(self._on_search_text_changed)
        toolbar.addWidget(self._search, stretch=1)

        # Round-25 (OOO, point 3): the guide searches **as you type** — no Find
        # button. A short debounce coalesces keystrokes so we don't re-scan
        # every section on each character (the scan builds a throwaway document
        # per section). Enter still triggers an immediate search; Prev / Next
        # step the matches.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._on_find)

        toolbar.addSpacing(8)
        # Live "N / total" readout of the active search position.
        self._results = QLabel("")
        self._results.setMinimumWidth(72)
        self._results.setAlignment(Qt.AlignmentFlag.AlignCenter)
        toolbar.addWidget(self._results)

        # Prev / Next step through search matches across the whole guide.
        # They stay disabled until a search finds something.
        self._prev_btn = QPushButton("Prev")
        self._prev_btn.setToolTip("Previous match")
        self._prev_btn.setEnabled(False)
        self._prev_btn.clicked.connect(self._go_prev)
        toolbar.addWidget(self._prev_btn)
        self._next_btn = QPushButton("Next")
        self._next_btn.setToolTip("Next match")
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(self._go_next)
        toolbar.addWidget(self._next_btn)
        content.addLayout(toolbar)

        self._browser = NoZoomTextBrowser()  # Ctrl+wheel scrolls, never zooms (point 5)
        # We handle every link click ourselves: in-guide ``help:<key>`` links
        # jump sections; genuine http(s)/mailto links open in the OS browser.
        # setOpenLinks(False) stops QTextBrowser trying to load the URL as a
        # document of its own.
        self._browser.setOpenLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        content.addWidget(self._browser, stretch=1)

        body.addLayout(content, stretch=4)
        outer.addLayout(body, stretch=1)

        # Keyboard: Ctrl+F focuses the search box, F3 / Shift+F3 step matches.
        # Scoped to this view so they don't hijack the keys on other tabs.
        self._add_shortcut(QKeySequence.StandardKey.Find, self._focus_search)
        self._add_shortcut(QKeySequence(Qt.Key.Key_F3), self._go_next)
        self._add_shortcut(QKeySequence("Shift+F3"), self._go_prev)

        # Theme the sidebar, then select the first section (-> _render).
        self._style_sidebar()
        self._sidebar.setCurrentRow(0)

    def _add_shortcut(
        self,
        key: QKeySequence | QKeySequence.StandardKey,
        handler: Callable[[], None],
    ) -> None:
        sc = QShortcut(key, self)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(handler)

    def _focus_search(self) -> None:
        if self._browser is not None:
            self._search.setFocus()
            self._search.selectAll()

    def _size_sidebar_to_content(self) -> None:
        """Fix the sidebar to the widest label so it hugs the text.

        A tight nav rail (just wide enough for the longest entry, e.g.
        "iperf3 reference") keeps the reader's eye on the content pane.
        Measured with a font matching the item QSS (11 pt DemiBold).
        """
        font = QFont(self.font())
        font.setPointSizeF(11)
        font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)
        widest = max(
            (metrics.horizontalAdvance(s.title) for s in self._sections),
            default=120,
        )
        # item padding (14*2) + border (2) + item margin (2*2) + frame, plus
        # headroom for a vertical scrollbar should one ever appear.
        self._sidebar.setFixedWidth(widest + 28 + 2 + 4 + 22)

    # ----- section navigation (sidebar) --------------------------------

    @Slot(int)
    def _on_section_changed(self, row: int) -> None:
        if 0 <= row < len(self._sections):
            self._render(row)

    @Slot(QUrl)
    def _on_anchor_clicked(self, url: QUrl) -> None:
        """Route a link click: ``zoom:`` figure, ``help:`` cross-link, or URL."""
        text = url.toString()
        if text.startswith("zoom:"):
            self._open_zoom(text[len("zoom:"):].strip("/"))
            return
        if text.startswith("help:"):
            self._jump_to_key(text[len("help:"):].strip("/").lower())
            return
        if url.scheme().lower() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)

    def _role_name(self) -> str:
        """Folder name for the running role's screenshots.

        Screenshots are captured per role (Server / Client / Loopback) — the
        reader should see the panel that matches *their* side of the test.
        Undecided (no role chosen yet) falls back to the Client capture, the
        most common end-user role.
        """
        role = self.ctx.run_state.role
        if role in (Role.SERVER, Role.CLIENT, Role.LOOPBACK):
            return role.value
        return "client"  # Undecided / unset

    def _shots_role_root(self) -> Path:
        """Theme- and role-matched screenshot *root* (``_shots/<theme>/<role>/``).

        Each tab's captures live under a per-tab subfolder of this root
        (``setup/``, ``run/``, ``save-options/`` …). Resolving against the root
        lets a section embed a screenshot that belongs to a *different* tab by
        its ``<tab>/<file>`` path — which is exactly what the generated Quick
        Start section does (it mirrors the welcome cards, whose figures come
        from the Setup / Run / Save tabs). Round-27 (point 5).
        """
        theme_name = self._theme_name()
        return HELP_DIR / "_shots" / theme_name / self._role_name()

    def _shots_dir(self, section: HelpSection) -> Path:
        """Theme- and role-matched screenshot folder for ``section``.

        ``_shots/<theme>/<role>/<section-key>/`` — so the Help guide shows the
        Light shot under Light and the Dark shot under Dark, and the Server /
        Client / Loopback capture that matches the running role.
        """
        return self._shots_role_root() / section.key

    def _shots_theme_root(self) -> Path:
        """Theme-matched ``_shots/<theme>/`` root (parent of the role folders).

        Searched so a section can embed a screenshot pinned to a *specific*
        role via a ``<role>/<tab>/<file>`` path — which is what the generated
        Quick Start does to mirror the welcome cards' per-role pins (Server /
        Client / Loopback Setup, plus the Client Run + finish-popup) regardless
        of the running role. (2026-06-02 — fixes Quick Start showing the running
        role's shot for every step instead of each card's pinned role, so it
        now matches the welcome tour screenshot-for-screenshot.)
        """
        return HELP_DIR / "_shots" / self._theme_name()

    def _assets_dir(self) -> Path:
        """Theme-matched folder for shared, role-agnostic help artwork.

        Holds the hand-drawn topology diagrams shown on the Overview page —
        ``_assets/<theme>/topology.png`` etc. These live *outside* ``_shots/``
        on purpose: they're Figma exports, not captured screenshots, so the
        screenshot rebuild (``tools/build_help_shots.py``, which wipes the
        ``_shots`` tree) must never delete them. Theme-matched (Dark/Light),
        role-agnostic (the topology is the same whichever side you're on).
        """
        theme_name = self._theme_name()
        return HELP_DIR / "_assets" / theme_name

    def _resolve_shot_path(self, name: str) -> Path | None:
        """Locate figure ``name`` for the current section + theme + role.

        Mirrors the search path used when rendering the inline ``<img>`` (see
        :meth:`_render`) so a figure's zoom target is the very same file the
        reader is looking at — searched across the section folder, the
        theme/role screenshot folder, AND the theme-matched ``_assets``
        diagrams (Round-23 point 15: diagrams are zoomable too, not just
        screenshots).
        """
        if not (0 <= self._current < len(self._sections)):
            return None
        section = self._sections[self._current]
        # The role-shots ROOT is searched too (Round-27 point 5) so a section can
        # embed another tab's capture by its "<tab>/<file>" path — the Quick
        # Start section reuses the Setup / Run screenshots that way.
        for base in (
            section.directory,
            self._shots_dir(section),
            self._shots_role_root(),
            self._shots_theme_root(),
            self._assets_dir(),
        ):
            candidate = base / name
            if candidate.is_file():
                return candidate
        return None

    def _open_zoom(self, name: str) -> None:
        """Open a figure (screenshot OR diagram) full-screen, maximised.

        Triggered by a ``zoom:<file>`` link wrapping a figure image. No-op if
        the file can't be found or decoded, so a bad link can't crash the tab.
        """
        path = self._resolve_shot_path(name)
        if path is None:
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        caption = self._sections[self._current].title if self._sections else name
        # Keep a reference; a previous viewer (if any) is dropped and GC'd.
        self._zoom_dialog = open_zoom(
            pixmap, caption, dark=self._is_dark(), parent=self
        )

    def _jump_to_key(self, key: str) -> None:
        """Select the section whose prefix-stripped slug matches ``key``."""
        for i, section in enumerate(self._sections):
            if section.key == key:
                self._sidebar.setCurrentRow(i)
                return

    def open_section(self, key: str) -> None:
        """Public cross-tab entry point (see :meth:`app.MainWindow.open_help`).

        Jumps the guide to the section with cross-link ``key`` — used when an
        error elsewhere in the app routes the user here for help.
        """
        self._jump_to_key(key)

    # ----- guide-wide search -------------------------------------------

    @Slot(str)
    def _on_search_text_changed(self, text: str) -> None:
        """Live search: emptying the box drops the search; otherwise (re)arm the
        debounce so a search fires shortly after the user stops typing."""
        if not text.strip():
            self._search_timer.stop()
            self._reset_search()
        else:
            self._search_timer.start()

    def _reset_search(self) -> None:
        self._search_query = ""
        self._match_sections = []
        self._total_matches = 0
        self._match_pos = 0
        self._results.setText("")
        self._prev_btn.setEnabled(False)
        self._next_btn.setEnabled(False)
        if self._browser is not None:
            self._browser.setExtraSelections([])

    @Slot()
    def _on_find(self) -> None:
        """Scan every section for the query and jump to the first match."""
        if self._browser is None:
            return
        query = self._search.text().strip()
        if not query:
            self._reset_search()
            return

        sections, total = self._scan_matches(query)
        self._search_query = query
        self._match_sections = sections
        self._total_matches = total

        if not sections:
            self._match_pos = 0
            self._results.setText("No matches")
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)
            self._browser.setExtraSelections([])
            return

        self._prev_btn.setEnabled(True)
        self._next_btn.setEnabled(True)
        # Jump to the first occurrence in the first matching section.
        self._render(sections[0])
        self._browser.find(query)
        self._match_pos = 1
        self._update_results()

    def _scan_matches(self, query: str) -> tuple[list[int], int]:
        """Count case-insensitive occurrences of ``query`` in every section.

        Returns ``(section_indexes_with_hits, total_occurrences)``. Counting
        runs through a throwaway ``QTextDocument`` so it matches exactly what
        ``QTextBrowser.find`` will navigate (same engine, same case-folding).
        """
        doc = QTextDocument()
        sections: list[int] = []
        total = 0
        for i, section in enumerate(self._sections):
            try:
                html = section.index_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            doc.setHtml(html)
            count = 0
            cursor = doc.find(query)
            while not cursor.isNull():
                count += 1
                cursor = doc.find(query, cursor)
            if count:
                sections.append(i)
                total += count
        return sections, total

    def _go_prev(self) -> None:
        self._navigate_match(forward=False)

    def _go_next(self) -> None:
        self._navigate_match(forward=True)

    def _navigate_match(self, *, forward: bool) -> None:
        """Move to the next/previous match, crossing sections (and wrapping)."""
        if self._browser is None or not self._match_sections:
            return
        query = self._search_query
        found = (
            self._browser.find(query)
            if forward
            else self._browser.find(query, QTextDocument.FindFlag.FindBackward)
        )
        if not found:
            # Exhausted this section in this direction -> hop to the adjacent
            # matching section, wrapping around the match list.
            self._hop_to_matching_section(forward=forward)
        self._step_counter(forward=forward)

    def _hop_to_matching_section(self, *, forward: bool) -> None:
        if self._browser is None:
            return
        query = self._search_query
        try:
            pos = self._match_sections.index(self._current)
        except ValueError:
            pos = 0
        step = 1 if forward else -1
        target = self._match_sections[(pos + step) % len(self._match_sections)]
        self._render(target)
        if forward:
            # _render leaves the cursor at the top -> first occurrence.
            self._browser.find(query)
        else:
            cursor = self._browser.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._browser.setTextCursor(cursor)
            self._browser.find(query, QTextDocument.FindFlag.FindBackward)

    def _step_counter(self, *, forward: bool) -> None:
        if self._total_matches <= 0:
            return
        if forward:
            self._match_pos = self._match_pos + 1 if self._match_pos < self._total_matches else 1
        else:
            self._match_pos = self._match_pos - 1 if self._match_pos > 1 else self._total_matches
        self._update_results()

    def _update_results(self) -> None:
        self._results.setText(f"{self._match_pos} / {self._total_matches}")

    def _highlight_all_matches(self) -> None:
        """Mark *every* occurrence of the active query on the current page.

        Uses ``setExtraSelections`` so all matches get a soft amber background;
        the current match keeps the normal (teal) selection on top, so it still
        stands out from its siblings. A no-op clear when no search is active.
        """
        if self._browser is None:
            return
        if not self._search_query:
            self._browser.setExtraSelections([])
            return
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#6b5d10") if self._is_dark() else QColor("#ffe066"))
        doc = self._browser.document()
        selections: list[QTextEdit.ExtraSelection] = []
        cursor = doc.find(self._search_query, 0)
        while not cursor.isNull():
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)
            cursor = doc.find(self._search_query, cursor)
        self._browser.setExtraSelections(selections)

    # ----- rendering ---------------------------------------------------

    def _render(self, index: int, *, preserve_scroll: bool = False) -> None:
        """Render section ``index`` with the current theme's CSS.

        ``preserve_scroll`` keeps the reader's place — used when re-rendering
        only to re-apply theme colours (tab activation / theme switch), not
        when the user navigates to a different section.
        """
        if self._browser is None or not (0 <= index < len(self._sections)):
            return
        self._current = index
        section = self._sections[index]
        try:
            html = section.index_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            html = f"<h1>Couldn't open this section</h1><p>{exc}</p>"

        scrollbar = self._browser.verticalScrollBar()
        pos = scrollbar.value() if preserve_scroll else 0

        # setDefaultStyleSheet must precede setHtml — it's applied as the
        # document is parsed. This is how the theme palette reaches the
        # otherwise colour-free section HTML.
        self._browser.document().setDefaultStyleSheet(self._theme_css())
        # Resolve <img src="foo.png"> first from the section's own folder, then
        # from the theme- and role-matched screenshot folder — so the guide
        # shows Light shots under Light, Dark under Dark, and the Server /
        # Client / Loopback capture that matches the running role. A re-render
        # on theme switch or tab activation swaps the folder, so screenshots
        # follow theme + role automatically (same filename lives under each
        # _shots/<theme>/<role>/<key>/).
        shots_dir = self._shots_dir(section)
        # Also the role-shots root, so a "<tab>/<file>" reference resolves to
        # another tab's capture (Quick Start reuses the Setup / Run shots).
        shots_root = self._shots_role_root()
        # And the theme root, so a "<role>/<tab>/<file>" reference resolves to a
        # specific role's capture (Quick Start pins each step's screenshot to
        # its card's role, matching the welcome tour — 2026-06-02).
        shots_theme_root = self._shots_theme_root()
        # Last path: theme-matched shared artwork (the Overview topology
        # diagrams). Searched after the section folder + screenshots so a
        # section can still override by name if it ever ships its own.
        assets_dir = self._assets_dir()
        self._browser.setSearchPaths(
            [str(section.directory), str(shots_dir),
             str(shots_root), str(shots_theme_root), str(assets_dir)]
        )
        # Wrap bare figures in zoom links, THEN swap each inline <img> for a
        # smooth-scaled in-document resource so it renders as crisply as the
        # "Zoom More" view (Round-24 LLL). The zoom anchors still carry the
        # original filename, so click-to-enlarge resolves full resolution.
        processed = _make_images_zoomable(html)
        processed = self._embed_inline_images(processed)
        self._browser.setHtml(processed)

        cursor = self._browser.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self._browser.setTextCursor(cursor)
        if preserve_scroll:
            scrollbar.setValue(pos)

        # setHtml dropped any previous highlights; re-paint them if a search is
        # active so every match on this page stays marked.
        self._highlight_all_matches()

        # Keep the sidebar selection in sync without re-entering the slot.
        if self._sidebar.currentRow() != index:
            blocked = self._sidebar.blockSignals(True)
            self._sidebar.setCurrentRow(index)
            self._sidebar.blockSignals(blocked)

    def _embed_inline_images(self, html: str) -> str:
        """Replace each inline ``<img src="file">`` with a smooth-scaled resource.

        Resolves the filename the same way the renderer does (section folder →
        theme/role ``_shots`` → theme ``_assets``), pre-scales it to the live
        viewport width with a high-quality sampler, and registers it on the
        document — so the inline figure is as sharp as the source allows rather
        than relying on ``QTextBrowser``'s fast scaler (Round-24 LLL, 2E/2F).
        A figure that can't be resolved/loaded is left untouched (the search
        paths still let the browser find it). Resource keys are scoped to the
        section + sequence, so re-rendering a section reuses (not leaks) them.
        """
        if self._browser is None:
            return html
        target = max(640, self._browser.viewport().width() - 48)
        dpr = self._browser.devicePixelRatioF()
        doc = self._browser.document()
        seq = 0

        def _repl(m: re.Match[str]) -> str:
            nonlocal seq
            src = m.group("src")
            abs_path = self._resolve_shot_path(src)
            if abs_path is None:
                return m.group(0)
            key = f"mem://help/{self._current}/{seq}"
            seq += 1
            embedded = embed_scaled_image(
                doc, str(abs_path),
                max_logical_width=target, device_pixel_ratio=dpr, key=key,
                cache=self._scaled_cache,
            )
            if embedded is None:
                return m.group(0)
            url, width = embedded
            return f'<img src="{url}" width="{width}">'

        return re.sub(r'<img\b[^>]*\bsrc="(?P<src>[^"]+)"[^>]*>', _repl, html)

    def _is_dark(self) -> bool:
        """Whether the active palette is dark — drives CSS, sidebar styling,
        match-highlight colour, and which screenshot folder is searched."""
        return self.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _theme_name(self) -> str:
        """``"dark"`` / ``"light"`` for the active palette."""
        return "dark" if self._is_dark() else "light"

    def _palette(self) -> dict:
        """The active theme's colour palette."""
        return theme.PALETTES[self._theme_name()]

    def _theme_css(self) -> str:
        """Build the document stylesheet from the active theme palette.

        Decides Light vs. Dark from the live ``QPalette`` (so it follows a
        runtime theme switch), then pulls the matching rich colour set from
        :data:`theme.PALETTES` — the same source of truth the rest of the UI
        uses, so the guide stays on-brand. Semantic callout accents
        (green / amber) are hardcoded: they read on both themes.
        """
        s = self._palette()
        return f"""
        body {{ font-family: 'Inter','Segoe UI',Arial,sans-serif; font-size: 10.5pt;
                color: {s['text']}; }}
        h1 {{ color: {s['accent']}; font-size: 17pt; margin: 0 0 8px 0; }}
        h2 {{ color: {s['accent']}; font-size: 13pt; margin: 18px 0 6px 0; }}
        h3 {{ color: {s['text']}; font-size: 11pt; margin: 14px 0 4px 0; }}
        p {{ line-height: 142%; }}
        a {{ color: {s['link']}; }}
        .lead {{ color: {s['subtext']}; font-size: 11.5pt; }}
        ul {{ margin-left: 4px; }}
        li {{ margin: 3px 0; }}
        code, pre, tt {{ font-family: Consolas,'Courier New',monospace;
                         background: {s['surface']}; color: {s['text']}; }}
        code, tt {{ padding: 1px 4px; }}
        pre {{ padding: 8px 10px; background: {s['surface']}; }}
        table {{ border-collapse: collapse; margin: 10px 0; }}
        th, td {{ border: 1px solid {s['border_strong']}; padding: 5px 9px;
                  text-align: left; vertical-align: top; }}
        th {{ background: {s['header_bg']}; color: {s['text']}; }}
        .btn {{ background: {s['alt_base']}; color: {s['accent']};
                font-family: 'Inter','Segoe UI',sans-serif; }}
        .callout {{ background: {s['alt_base']}; padding: 8px 12px; margin: 12px 0;
                    border-left: 4px solid {s['accent']}; }}
        .ctitle {{ font-weight: bold; margin: 0 0 4px 0; color: {s['accent']}; }}
        .tip {{ border-left-color: #2e9e5b; }}
        .tip .ctitle {{ color: #2e9e5b; }}
        .warn {{ border-left-color: #cc7a00; }}
        .warn .ctitle {{ color: #cc7a00; }}
        .note {{ border-left-color: {s['accent']}; }}
        .note .ctitle {{ color: {s['accent']}; }}
        .shot {{ background: {s['surface']}; border: 1px dashed {s['border_strong']};
                 color: {s['subtext']}; padding: 16px; margin: 12px 0;
                 font-style: italic; }}
        .figure {{ margin: 14px 0; }}
        .figure a {{ text-decoration: none; }}
        .figure img {{ border: 1px solid {s['border_strong']}; }}
        .caption {{ color: {s['subtext']}; font-style: italic; font-size: 9.5pt;
                    margin: 4px 0 0 0; }}
        .zoom {{ color: {s['accent']}; font-style: normal; }}
        """

    def _style_sidebar(self) -> None:
        """Style the section list as separated, button-like cards.

        Theme-aware (rebuilt from :data:`theme.PALETTES` so it tracks a runtime
        switch). Bigger text + padding + per-item borders make each section an
        obvious, tappable target — re-applied alongside the browser CSS on
        theme change.
        """
        if self._browser is None:  # fallback path has no sidebar
            return
        s = self._palette()
        self._sidebar.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; outline: 0; }}
            QListWidget::item {{
                background: {s['alt_base']};
                color: {s['text']};
                border: 1px solid {s['border_strong']};
                border-radius: 8px;
                padding: 12px 14px;
                margin: 4px 2px;
                font-size: 11pt;
                font-weight: 600;
            }}
            QListWidget::item:hover {{ border-color: {s['accent']}; }}
            QListWidget::item:selected {{
                background: {s['accent']};
                color: {s['window']};
                border-color: {s['accent']};
            }}
        """)

    # ----- theme / activation hooks ------------------------------------

    def refresh(self) -> None:
        """Re-apply theme styling on tab activation (backstop for switches)."""
        if self._sections and self._browser is not None:
            self._style_sidebar()
            self._render(self._current, preserve_scroll=True)

    def changeEvent(self, event: QEvent) -> None:  # Qt override (camelCase)
        """Re-render with fresh palette colours when the theme changes."""
        if event.type() in (
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        ) and self._sections and self._browser is not None:
            self._style_sidebar()
            self._render(self._current, preserve_scroll=True)
        super().changeEvent(event)


def _make_images_zoomable(html: str) -> str:
    """Wrap every bare ``<img src="X">`` in a ``zoom:X`` link.

    So clicking *any* figure — screenshot OR rendered diagram — opens the
    full-screen viewer (Round-23 point 15), not just the screenshots that
    already ship a ``<a href="zoom:...">`` wrapper. An ``<img>`` already
    inside a ``zoom:`` anchor is matched together with that anchor and left
    untouched, so there's no double-wrapping.
    """
    def _maybe_wrap(m: re.Match[str]) -> str:
        if m.group("lead"):
            return m.group(0)  # already inside a zoom anchor
        return f'<a href="zoom:{m.group("src")}">{m.group("img")}</a>'

    return re.sub(
        r'(?P<lead><a href="zoom:[^"]*">\s*)?'
        r'(?P<img><img\b[^>]*\bsrc="(?P<src>[^"]+)"[^>]*>)',
        _maybe_wrap,
        html,
    )
