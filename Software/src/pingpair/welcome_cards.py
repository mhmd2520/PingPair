"""Quick-Start flash-card content for the first-boot welcome screen.

Round-22 (EEE); reworked Round-23, then again Round-24. Qt-free so it
unit-tests without a ``QApplication``.
:class:`pingpair.views.welcome.WelcomeDialog` renders these one at a time as
flash cards (← Previous / Next → / Skip); Help → Quick Start presents the same
material as a normal scrollable section (it mirrors these cards — keep the two
in sync).

Round-24 changes (point 2C):

* The intro card is **minimal** — the dialog itself draws the logo + the
  two-tone "PingPair" wordmark + version chip + the two buttons. (2D) The
  intro carries a short 2-3 sentence brief in ``INTRO.body_html`` (restored
  in Round-26; it was briefly emptied in Round-24).
* The tour is now **7 cards** (2026-06-02: a Loopback Setup card was added so
  all three roles are walked): How it works · Setup (Server) · Setup (Client)
  · Setup (Loopback) · Run the sweep · Save the report · Where to find more.
* Setup is split into a **Server**, a **Client**, and a **Loopback** card,
  each pinned to that role's screenshot via :attr:`Card.role` (so the tour
  shows the right side regardless of which role the running PC happens to be).
  (2C2 / 2C3; Loopback added 2026-06-02.)
* Card 5 ("Save the report") shows the **sweep-finished popup**, not the Save
  Options tab. (2C5)
* Every card's body copy was rewritten to actually walk the screenshot and the
  step it shows, rather than a one-line gloss. (2C7)

Each card's ``image`` is resolved by the dialog against the theme-matched
``_assets/<theme>/`` (diagrams) and ``_shots/<theme>/<role>/`` (real
screenshots) folders — see
:meth:`pingpair.views.welcome.WelcomeDialog._resolve_image`. When ``Card.role``
is set it overrides the running role for that one card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A card's "… click the image to enlarge" affordance line. Both the runtime
# tour (views/welcome.py) and the Quick-Start generator (tools/build_quick_start_help.py)
# strip it when a card has no resolvable image, so the regex lives here in the
# Qt-free card module — the single source both import (no copy to drift).
HINT_RE = re.compile(r"<p class='hint'>.*?</p>", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Card:
    """One welcome / Quick-Start card."""

    title: str
    body_html: str            # Qt-rich-text fragment (HTML 4 subset)
    image: str | None = None  # _assets filename, or "<section>/<file>" in _shots
    role: str | None = None   # pin the screenshot to this role (else running role)


# The intro card — the literal "Welcome screen". The dialog draws the logo +
# wordmark + version chip and the Skip / Quick Start buttons; ``body_html`` is a
# short 2-3 sentence brief on what the app is, who it's for, and what sets it
# apart (Round-26 point 4), rendered as a centred, wrapped paragraph under the
# version chip.
INTRO = Card(
    title="Welcome to PingPair",
    body_html=(
        "Welcome to PingPair — a Windows desktop tool that fully automates a "
        "20-case LAN characterization test between two laptops, measuring "
        "latency, loss, throughput and jitter and saving a ready-to-share "
        "Word + Excel report on every run. "
        "It's built for field and lab engineers who need repeatable, "
        "two-machine link tests without hand-driving raw iperf3 and fping "
        "commands or stitching the numbers together by hand. "
        "What makes it different: a single app drives both ends over a private "
        "control channel, runs the whole 20-case grid hands-free, and hands you "
        "the documented, side-by-side results automatically."
    ),
    image=None,
)


# The Quick-Start tour. Mirrored by Help → Quick Start.
QUICK_START_CARDS: tuple[Card, ...] = (
    Card(
        title="1 · How it works",
        body_html=(
            "<p>PingPair runs a fixed <b>20-case</b> LAN test — four payload "
            "sizes &times; five bandwidths — and writes one report per run. "
            "Two laptops are wired <b>back-to-back over a single Ethernet "
            "cable</b>, no switch or router in between.</p>"
            "<p>One laptop is the <b>Server</b> (<tt>192.168.1.1</tt>) and just "
            "listens; the other is the <b>Client</b> (<tt>192.168.1.2</tt>) and "
            "drives the schedule. A private control channel on TCP&nbsp;5202 "
            "keeps the two in step while <tt>iperf3</tt> and <tt>fping</tt> "
            "measure each case. Both ends save a matching report.</p>"
            "<p>Only one PC? Pick <b>Loopback</b> in Setup to play both roles "
            "over <tt>127.0.0.1</tt> — same grid, same report, no cable.</p>"
        ),
        image="topology.png",
    ),
    Card(
        title="2 · Setup — go green (Server)",
        body_html=(
            "<p>Start on the <b>Server</b> laptop — set it up first so it's "
            "listening before the Client drives anything. Open <b>Setup</b>: "
            "each row is a prerequisite — the right IP on the wired NIC, the "
            "firewall rules <tt>iperf3</tt> and <tt>fping</tt> need, and Wi-Fi "
            "taken offline so test traffic can't leak onto the wrong adapter.</p>"
            "<p>Click <b>Fix all</b> to clear them in one pass, then confirm the "
            "coloured <b>role banner</b> reads <b>Server</b> and the IP shows "
            "<tt>192.168.1.1</tt> — the address the Client will sweep against. "
            "Leave the Server on the <b>Run</b> tab; it just listens and obeys, "
            "you don't start anything on this side.</p>"
            "<p class='hint'>This is the real Server Setup tab — click the "
            "image to enlarge.</p>"
        ),
        image="setup/01-checks-overview.png",
        role="server",
    ),
    Card(
        title="3 · Setup — go green (Client)",
        body_html=(
            "<p>Now do the same on the <b>Client</b> laptop. The Setup tab looks "
            "identical, but the role banner reads <b>Client</b> and the IP is "
            "<tt>192.168.1.2</tt>. This is the side that <b>drives</b> the "
            "schedule.</p>"
            "<p>Run <b>Fix all</b> here too and get every row green. Yellow rows "
            "are warnings, not blockers — but green on both laptops means a "
            "clean run. If a check can't auto-fix, its row spells out the exact "
            "command to run by hand.</p>"
            "<p class='hint'>This is the real Client Setup tab — click the "
            "image to enlarge.</p>"
        ),
        image="setup/01-checks-overview.png",
        role="client",
    ),
    Card(
        title="4 · Setup — go green (Loopback)",
        body_html=(
            "<p>Only have <b>one PC</b>? Pick <b>Loopback</b> in Setup and "
            "PingPair plays <b>both roles on <tt>127.0.0.1</tt></b> — the full "
            "20-case grid runs on this single machine, with no Ethernet cable "
            "and no second laptop to wire up.</p>"
            "<p>The role banner turns amber and reads <b>Loopback dev mode</b>. "
            "The prereq list is shorter here — the firewall and Wi-Fi rows show "
            "<b>SKIP</b> because loopback traffic never leaves the machine, so "
            "there's usually nothing to fix. You still get the same report at "
            "the end, which makes Loopback the quickest way to try PingPair "
            "before you set up two laptops.</p>"
            "<p class='hint'>This is the real Loopback Setup tab — click the "
            "image to enlarge.</p>"
        ),
        image="setup/01-checks-overview.png",
        role="loopback",
    ),
    Card(
        title="5 · Run the sweep",
        body_html=(
            "<p>Still on the <b>Client</b>, open <b>Run</b> and press <b>Run "
            "full sweep</b>. All 20 cases run unattended — roughly "
            "<b>16 minutes</b> end to end — and the whole-sweep ETA counts down "
            "as it goes.</p>"
            "<p>The results table fills in case by case and the live charts "
            "track throughput, latency and loss in real time. Need to stop "
            "early? <b>Abort</b> ends cleanly and still offers to save what "
            "completed.</p>"
            "<p class='hint'>This is the real Run tab — click the image to "
            "enlarge.</p>"
        ),
        image="run/01-overview.png",
        role="client",  # always show the Client Run panel, never the running role
    ),
    Card(
        title="6 · Save the report",
        body_html=(
            "<p>When the sweep finishes, a popup reports how many cases "
            "passed, the total run time, and a <b>Details</b> list of every "
            "file written.</p>"
            "<p>Each run gets its own folder containing a <tt>.docx</tt> + "
            "<tt>.xlsx</tt> + a <tt>.json</tt> sidecar (and <tt>.pdf</tt> / "
            "<tt>.txt</tt> if you tick them). Choose the formats, the "
            "destination, and the test-record metadata on the <b>Save "
            "Options</b> tab <i>before</i> you run.</p>"
            "<p class='hint'>This is the finish popup — click the image to "
            "enlarge.</p>"
        ),
        image="save-options/02-finish-popup.png",
        role="client",  # the Client "Sweep complete - save report?" popup
    ),
    Card(
        title="7 · Where to find more",
        body_html=(
            "<p>That's the whole loop: wire up, go green, run, collect the "
            "report. This tour is written up any time under "
            "<b>Help → Quick Start</b>.</p>"
            "<p>For the bigger picture, <b>Help → Overview</b> walks the "
            "architecture diagrams — the topology, the control sequence, the "
            "case grid and the report artifacts. Every tab also has its own "
            "Help section, plus a <b>Troubleshooting</b> guide and the full "
            "<tt>fping</tt> / <tt>iperf3</tt> flag references.</p>"
            "<p>You're all set — press <b>Got it</b> to start.</p>"
        ),
        image=None,
    ),
)
