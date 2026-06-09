# PingPair — user guide

Automated LAN characterization between two Windows machines using
**fping 5.5** (latency / loss) and **iperf3 3.21** (throughput / jitter /
loss). PingPair runs a fixed 20-case test grid hands-free and produces a
Word + Excel report (PDF / TXT optional) plus the JSON config that produced
it.

The two endpoints can be physical laptops, Windows VMs on a private LAN
segment, or embedded computers on a train's on-board network. The tool
treats them the same as long as both sides share a layer-2 broadcast domain.

> **Just want to run it?** Download the packaged build from the
> [Releases](https://github.com/mhmd2520/PingPair/releases) page, unzip, and
> run `PingPair.exe` — no Python needed. This guide also covers running from
> source (§1).

## The test methodology

Each run sweeps a **4 × 5 grid** — every payload size against every
bandwidth target:

| Payload (bytes) | × | Bandwidth (Mbps) |
|---|---|---|
| 200, 600, 1000, 1300 | × | 10, 30, 50, 70, 90 |

= **20 cases**, run back-to-back with no manual intervention. For each case
the Server side runs `iperf3 -s` and the Client side fires `iperf3 -c` (UDP,
~30 s at that case's payload + bandwidth) alongside `fping` for latency and
loss; then the next case starts. Each case's results — throughput, jitter,
loss, and min / avg / max latency — fill one row of the 8-column results
table that becomes the report.

```
Server  192.168.1.1   ──  Ethernet / LAN segment  ──  Client  192.168.1.2
  iperf3 -s                                            iperf3 -c  +  fping
```

(All 20 cases, the payload/bandwidth values, and the per-case duration are
fully editable on the **Config** tab — see §2.4c.)

---

## 1. First-time setup on a fresh machine

Follow these steps once per machine (Server side and Client side).
Estimated time: ~10 minutes.

### 1.1 Install Python 3.11 or newer

**Recommended — installer from python.org:**

1. Browse to <https://www.python.org/downloads/windows/>.
2. Download "Windows installer (64-bit)" for 3.12.x or newer.
3. Run the installer and **tick "Add python.exe to PATH"** before
   pressing Install.

This is the path validated on the project's two test VMs (Python 3.14.4).

> **Note on `winget`.** `winget install --id Python.Python.3.12 -e` works
> in theory but has been known to leave PATH and `py.exe` launcher
> mappings in inconsistent states on this project's VMs (the launcher
> reported "No suitable Python runtime found" even after a successful
> install). If you try it and hit problems, uninstall and switch to the
> python.org installer above — that's what's known-good.

Verify:
```
py --list
python --version
```
You should see at least `3.11`. The tool is tested with 3.14 and ships
with `requires-python = ">=3.11"`.

### 1.2 (Optional) Visual C++ runtime

Some Python wheels (PySide6, numpy) need the Microsoft Visual C++
2015–2022 redistributable. Modern Windows 10/11 already has it.
If `pip install` later complains about missing DLLs:
<https://aka.ms/vs/17/release/vc_redist.x64.exe>.

### 1.3 Get PingPair onto the machine

Two ways, depending on whether you want the ready-to-run app or the source.

**a) Packaged build (recommended — no Python needed).** Download the latest
`PingPair-<version>-win64.zip` from the
[Releases](https://github.com/mhmd2520/PingPair/releases) page and unzip it
anywhere (e.g. `C:\PingPair\`). You'll get `PingPair.exe` next to its
`_internal\` runtime folder — that's the whole app. It bundles its own
Python, `fping`, and `iperf3`, so you can **skip §1.1, §1.5, and §1.6** and
go straight to §1.4 (the network).

**b) Run from source.** Clone the repository (or download the *source* ZIP
from the same Releases page) to a local path:

```
git clone https://github.com/mhmd2520/PingPair.git C:\PingPair
```

The Python package lives under `Software\` — that's the folder the
source commands in this guide run from.

> **On a VM**, the install steps are identical to a physical machine — put
> the build (or source) inside the guest. The only VM-specific part is the
> *network* (§1.4): attach both VMs to the same **isolated/internal virtual
> switch**, not NAT or Bridged. Copying the unzipped folder into the guest, a
> shared folder, or a guest-side `git clone` all work equally well.

### 1.4 Configure the network

PingPair's canonical point-to-point addresses are:

| Role | IPv4 | Subnet |
|---|---|---|
| Server (Laptop A / VM-1) | `192.168.1.1` | `255.255.255.0` |
| Client (Laptop B / VM-2) | `192.168.1.2` | `255.255.255.0` |

**Set a static IP on each machine:**

1. Open `Settings → Network & Internet → Ethernet` and click your
   adapter, **or** open `ncpa.cpl` from Win+R.
2. Right-click the adapter → Properties → "Internet Protocol Version 4
   (TCP/IPv4)" → Properties.
3. Pick "Use the following IP address". Enter the IP and subnet from
   the table. Leave the gateway blank (this is a point-to-point LAN).
4. OK out of all dialogs.

**Topology check:**

- Two physical machines: connect them with an Ethernet cable
  (crossover not required on modern NICs) or via a small switch.
- Two VMs (VMware Workstation / Hyper-V): attach both to the same
  **internal / LAN-Segment** virtual network. In VMware: VM Settings →
  Network Adapter → "LAN segment" → pick (or create) "Internal-Network".
  Do **not** use NAT or Bridged for this purpose.

**Verify reachability** from the Client side (VM-2):
```
ping 192.168.1.1
```
Four `Reply from 192.168.1.1` lines = LAN is good. If pings fail, the
problem is the firewall or the virtual switch, not PingPair — fix it
before continuing.

### 1.5 Install PingPair's dependencies (source only)

*Skip this if you downloaded the packaged build — it ships its own
dependencies.*

From an **Administrator** Command Prompt (so the GUI can add firewall rules
later), in the `Software\` folder of your checkout:

```
cd Software
pip install -e .
```

`-e` makes it an editable install — edits under `src\pingpair\` take effect
without re-installing. The download is ~150 MB (PySide6 is the big one). Use
`pip install -e ".[dev]"` instead if you also want the test / lint tooling
(pytest, ruff, mypy).

### 1.6 Verify the installation

Run the headless prerequisite checker — a one-glance status of
Python / fping / iperf3 / NIC IP / firewall rules. It exits non-zero on any
FAIL, so it doubles as a smoke check:
```
python -m pingpair --check-prereqs
```

(Packaged build: run `PingPair.exe --check-prereqs` from the unzip folder
instead.) You should see nine rows — Python, Administrator, fping, iperf3,
Local NIC IP, three firewall rules, and Wi-Fi disabled. Anything that's red
or yellow has a "Fix this for me" button waiting in the GUI's Setup tab —
see §2.3.

---

## 2. First two-VM run (the "real" test)

Once both machines have completed §1, you're ready to do an end-to-end
sweep across the LAN.

### 2.1 Launch the GUI on both sides

Open an **Administrator** Command Prompt (so the Setup tab's Fix
buttons can call `netsh advfirewall`).

On **VM-1 (Server, 192.168.1.1)** — packaged build: double-click
`PingPair.exe` (or launch it from an elevated CMD). From source:
```
cd Software
python -m pingpair
```

On **VM-2 (Client, 192.168.1.2)**: the same.

**On first launch the app auto-detects the role from your local IP**:

- If this machine is bound to **`192.168.1.1`** (the canonical Server IP
  from §1.4), it starts in **Server** role.
- If this machine is bound to **`192.168.1.2`** (the canonical Client IP),
  it starts in **Client** role.
- If neither matches (you're on a different LAN, or the static IP isn't
  set yet), the app falls back to **Client** and shows a yellow banner
  on the Setup tab telling you to verify the role before pressing Run.

The choice is persisted in QSettings — subsequent launches reuse it
without auto-detecting again, so the first run on each VM is the only
one that ever inspects the IP.

**Per-launch consistency check.** Even on subsequent launches, PingPair
re-sniffs the local IPs and compares them to the saved role. If the IP
no longer matches (e.g. you reset the NIC, the LAN changed, DHCP
overrode the static address), the Setup tab shows an orange banner:

> *This PC's IP doesn't match the saved Client role — expected
> 192.168.1.2 to be bound, but it isn't. Currently bound: 192.168.10.50.
> Either fix the NIC IP below or change the role above.*

The role itself is **never auto-flipped behind your back** — the banner
is purely a heads-up. You can resolve it two ways:

1. Click the **Set the correct IP** button on the Local NIC IP FAIL
   row of the prereq table (see §2.3) — assigns the canonical IP for
   the current role via `netsh`, takes ~1 second, drops your existing
   connection on that adapter briefly while it re-binds.
2. Or change the role on the Setup tab (see §2.2) if this PC is now
   meant to play the other side.

The banner clears automatically the moment the prereq table re-runs
and finds the IP now matches the role — no app restart needed.

### 2.2 Verify (or change) the role

The role banner at the top of the window is colour-coded:

- **Green** — *Server role — listening on 192.168.1.1:5202 (control) and
  5201 (iperf3).*
- **Blue** — *Client role — drives sweeps against Server at 192.168.1.1.*
- **Orange** — *Loopback dev mode — both roles on 127.0.0.1.*

If the auto-detect picked wrong, or you're physically swapping which
laptop plays which role, open the **Setup** tab → **Role** box at the top.
Since Group F (2026-05-16) the role group is laid out as a single
horizontal row of three radios — **Server | Client | Loopback** — with
auto-apply on click. No Apply button:

1. Click the radio you want. The Run tab rebuilds in place, role
   persists to QSettings, no restart needed.
2. For **Client**, type the other machine's IP into **Server host**
   (defaults to the canonical `192.168.1.1`); changes auto-apply on
   focus-out.

Role switching is **blocked while a sweep is running** — the click is
refused with a QMessageBox and the radio reverts to the previously-active
role.

**Per-PC NIC override** (Group F).  Below the radios is a checkbox
**Use a custom IP configuration  (overrides the defaults below)** and
three input fields: **IP**, **Subnet**, **Gateway**.  Tick the checkbox
to enable the fields and type the values you want; the override auto-
applies ~500 ms after you stop typing.  Each field's placeholder shows
the role-defaults so you know what's used when the field is empty
(`Server 192.168.1.1 / Client 192.168.1.2`, `255.255.255.0`,
`(blank = no gateway)`).  The override is per-machine — persisted in
QSettings — so both PCs can still share the same Config tab profile
while each binds its NIC to whatever the local network actually uses
(e.g. when the LAN is on `10.x.x.x` instead of the canonical
`192.168.1.x`, or when the Server and Client live on different subnets
with a router between them).

**External IP-change detection.**  After every prereq pass PingPair
compares the bound NIC IP against the last value it successfully
applied via the "Set the correct IP" fix.  If a change is detected
outside the app (DHCP renewed, manual `netsh`, NIC reset), a dialog
pops with three options — **Keep new IP** (accept the change as the
new baseline), **Restore previous** (re-run netsh to put back the
last-applied value), **Cancel** (apply a 30 s cooldown so the dialog
doesn't keep popping).

**Input validation across the form** (Group G, 2026-05-18). Every
validated input across the app has four layers of feedback:

- **Keypress filter** — IP / Subnet / Gateway / Server host fields
  only accept digits and dots; letters and other characters are
  silently rejected at the keystroke (and on paste). Payloads and
  Bandwidths on the Config tab accept only digits, commas, and
  spaces. Broad-charset fields (Filename pattern, Destination
  folder, fping Extra args) skip the keypress filter because their
  allowed character set is too wide to restrict.
- **Red border + tint** on the field when the input is structurally
  invalid (e.g. `192.168.1.999` — out of range, not a typing error).
- **Diagnostic hover tooltip** explains *what* is wrong. Hover for
  ~700 ms on `192.168.1.999` and the tooltip reads
  `Octet 4 (999) exceeds 255 — max is 255.`. On a valid field the
  tooltip is the base format hint (`IPv4 only - four octets 0-255,
  e.g. 192.168.1.10.`). Tooltips appear on every validated field
  regardless of valid/invalid state.
- **Pydantic on Apply** is the final safety net. The first three
  layers are the friendly UX; pydantic still rejects anything
  structurally wrong if it sneaks past.

Unticking the **Use a custom IP configuration** checkbox clears
the IP / Subnet / Gateway fields entirely so a stale typo doesn't
linger while the fields are greyed out. Re-ticking gives a clean
starting state.

### 2.3 Resolve any Setup tab warnings on each side

Open the **Setup** tab on each VM. Anything in yellow or red has a Fix
button. Each Fix button pops a confirmation dialog showing the exact
shell command before running it.

**Firewall rules** (yellow WARN rows): one button each for ICMP echo,
iperf3 TCP/UDP 5201, control TCP 5202. Each adds an inbound
`netsh advfirewall` rule.

**Wi-Fi disabled** (yellow WARN row, when a Wi-Fi adapter is carrying
IPv4 traffic): the **Disable Wi-Fi** button. With both Wi-Fi (typically
Public profile, gateway → corp LAN) and the dedicated Ethernet
(Private, point-to-point) up, Windows can route 192.168.1.x packets
out the wrong NIC and you'll see phantom packet loss in the report.
At click-time PingPair finds the actual Wi-Fi adapter name and runs:

```
netsh interface set interface name="<adapter>" admin=disable
```

Re-enable it from the Windows tray or with `admin=enable` when you're
done.

**Local NIC IP** (red FAIL row, when the NIC isn't on the canonical
192.168.1.x scheme): the **Set the correct IP** button. At click-time
PingPair detects your primary Ethernet adapter (skipping virtual /
loopback / Wi-Fi adapters) and runs:

```
netsh interface ipv4 set address name="<adapter>" static <role-IP> 255.255.255.0
```

The IP comes from the role this PC is currently playing — Server gets
`192.168.1.1`, Client gets `192.168.1.2`. Requires Administrator
(launch the app from an elevated CMD); the button is disabled
otherwise. Your existing connection on that adapter drops for ~1 second
while netsh re-binds, then the FAIL row turns PASS on the auto re-check
and the orange role-mismatch banner from §2.1 (if it was showing)
clears too. If your role is Loopback, or no Ethernet adapter can be
detected, this fix falls back to opening the Windows adapter settings
panel (`ncpa.cpl`) so you can set the IP manually.

After every Fix button has run, every row should turn green.

### 2.4 Run the 20-case sweep

Both windows up, both Setup tabs all green? You're ready.

**On VM-1 (Server):** the Run tab automatically started the
ControlServer when the app launched in Server role. You'll see
"Listening on 192.168.1.1:5202" and the Connected client field reads
`(no client yet)`. Leave it open.

> The Server panel's bottom row has three buttons: **Start server**,
> **Stop server**, and **Restart server**. Start is only enabled when
> the listener isn't running (after a Stop, or if it failed to bind);
> Stop and Restart are enabled when it is. Every click writes a line
> to the Event log so you can confirm the action took effect.

**On VM-2 (Client):** open the Run tab. You'll see a 20-row sweep
table pre-filled with case # / payload / bandwidth, all rows showing
`Pending`. Each row has a **Run** checkbox in the leftmost column —
keep them all ticked for the full canonical sweep, or uncheck rows
you want to skip. The **Sweep subset** group above the table has
quick-select helpers:

- **Select all / Select none** — toggle the whole grid.
- **Payload row** (200/600/1000/1300 B) — each button toggles the 5
  cases for that payload size.
- **Bandwidth row** (10/30/50/70/90 Mbps) — each button toggles the
  4 cases for that bandwidth.
- A counter chip shows `N of 20 selected · est. Mm Ss` (blue at full,
  orange when partial, grey at zero). The estimate is **duration-aware**
  — it scales with the configured case duration (a 30 s case is ~48 s
  of wall time on Windows because fping runs at the ~15.6 ms timer
  granularity; a 5 s case is ~9 s). Once a sweep is running, the ETA
  switches to a measured average of completed cases so it self-corrects.

Your selection survives an app restart (persisted via QSettings). The
**Run** button label tracks the subset: `Run full sweep` at 20/20,
`Run subset (N cases)` otherwise, disabled at 0. Press it when you're
ready.

What happens next:

1. The Client connects to the Server over TCP/5202 (you'll see
   `Connected (server v<version>)` on the Client and
   `client_connected: …` on the Server's event log).
2. For each of the 20 cases, the Server spawns a fresh `iperf3 -s -1`
   bound to 192.168.1.1:5201, the Client fires `iperf3 -c` plus `fping`
   in parallel, both finish in ~50 s (Windows clock granularity), and
   the row in the sweep table fills in with throughput / jitter / loss /
   min / avg / max latency.
3. After all (selected) rows are green/red, the bottom status reads
   `Sweep finished in 15m 50s — 20/20 cases ok · saved 3 report file(s) to Reports\PingPair_2026-05-10_HHMMSS`.
   The file count is 3 (`.docx` + `.xlsx` + `.json` sidecar) by default —
   add `.pdf` / `.txt` by ticking the boxes on the Save Options tab.

Total time: ~16 minutes for a clean run. You can press **Stop** at any
time; the Client stops cleanly and the server returns to `(idle)`.

When the sweep finishes the new **save dialog** pops (or, if you've
turned on **Auto save**, the existing result popup with saved-files
info — see §2.5).

### 2.4a Continuous (multi-segment) mode — the train walk-through

The Client panel has a **Continuous (multi-segment) mode** checkbox
just above the Sweep subset group. Tick it when you're characterising
a LAN across multiple physical segments in one walk-through — the
canonical example is a train operator hopping cab-to-cab and running
the same metric grid at each car-pair. Each segment writes one
"sweep" worth of data; the whole multi-segment run rolls up into a
single consolidated report.

Workflow:

1. Tick **Continuous (multi-segment) mode**. A **First segment label**
   input appears below it — type a descriptive name (e.g.
   `Cab M2 ↔ M4`) or leave blank for the default `Segment 1`.
2. Pick the **Sweep subset** that will apply to every segment. Per
   the 2026-05-11 design call, the subset is **shared across all
   segments** so the cross-segment comparison table in the report
   has uniform columns.
3. Press **Run subset (N cases)**. The first segment runs as normal.
4. When the segment finishes, the **Segment complete — next?**
   dialog pops. Three buttons:
   - **Continue with next segment** (default) — type the next
     segment's label, click. Plug into the next car-pair if you're
     physically moving; the Client reconnects to the Server
     automatically and the next sweep starts.
   - **Retry this segment** — only enabled if the previous segment
     ended with errors or a TCP drop. Drops the bad segment from
     the report and re-runs the same plan against the same label.
   - **Save and finish** — wraps the multi-segment run, writes the
     consolidated report (see §2.5), and exits multi-segment mode.

The dialog has a running tally at the top showing the segments
completed so far with their status and duration, so you can keep
track without leaving the Run tab.

The between-segments dialog has four buttons (added 2026-05-13):

- **Save and finish** — end the multi-segment run and write a
  consolidated report.
- **Retry this segment** — discard the just-finished segment and
  re-run with the same label (only enabled when that segment
  didn't end OK).
- **Skip this segment** — record the just-finished segment in the
  report (so the failure is auditable) and advance to the next
  segment without re-running. Useful when one car-pair is broken
  but you want to keep going through the train.
- **Continue with next segment** — the standard happy path: store
  the result, prompt for a label, connect to the next pairing.

Press **Stop** at any time to abort. If the Server is unreachable
while reconnecting, the Stop now interrupts the connection retry
immediately (no more "Stopping…" for 7 seconds) and shows a
"Continuous-mode sweep stopped" info popup. Any segments already
completed stay in memory — click "Save report now" on the Report
tab if you want to keep them.

### 2.4b The Analysis tab — overlay & compare past sweeps

The **Analysis** tab (between Save Options and Help) loads every
sweep `.json` sidecar in your Reports folder into a checkable
list, then plots their metrics on the right side. Tick one or
more runs to overlay them. The list **auto-refreshes** from your
Save Options destination folder, so newly-saved sweeps appear on
their own (no Refresh button). The tab only walks the canonical
per-sweep layout (`Reports/<basename>/<basename>.json`).

Seven sub-tabs on the right:

- **Throughput · Avg latency · Packet loss · Jitter** — one
  pyqtgraph line per ticked run (or per segment for multi-segment
  runs), x = case #, y = the matching metric.
- **Stats** — one row per ticked run, columns: cases ok · per-metric
  min / avg / max with median + stdev in cell tooltips.
- **Trend** — chronological scatter, one marker per run by
  `started_at`. Combo box at the top picks which metric to plot.
- **Diff** — activates when exactly two runs are ticked. Per-case
  A/B/Δ delta table with `higher_is_better` colouring (positive
  throughput delta = green, positive loss delta = red, etc.).

Left-side filter group narrows the data: case # range, payload
checkboxes, bandwidth checkboxes, metadata substring matches
(technician / customer / record ID). Charts and Stats refresh
live as you tick filters.

Toolbar has two export buttons:

- **Export comparison report…** — bundles the ticked runs (plus
  the active filter snapshot) into a docx/xlsx/pdf/txt report
  saved under `Reports/Comparison_<ts>/`. PNG charts embedded in
  docx + pdf; native LineCharts in xlsx; the diff section appears
  only when exactly two runs were ticked.
- **Save current chart as PNG…** — rasterises whichever metric
  chart sub-tab is currently visible.

Every saved per-sweep report ALSO bundles an **Analysis
appendix** automatically: per-metric line charts (docx + pdf),
native xlsx LineCharts, ASCII sparklines (txt), plus per-payload
and per-bandwidth breakdown tables. The same charts also land as
standalone PNGs in an `Analysis_Images/` subfolder next to each
report (tick **"Charts (.png)"** in Save settings —
default ON).

### 2.4c The Config tab — custom test-plan profiles

The **Config tab** (third from the left) is a full editor for the
test plan that the Run tab sweeps across. The default plan is the
standard 20-case grid (4 payloads × 5 bandwidths; see "The test
methodology" at the top), but you can override it with any payload
list, bandwidth list,
duration, protocol, network IPs, ports, or fping flags — and save
the result as a named `.json` profile you can re-load any time.

Files live in `Software\Configs\` (created automatically the first
time you click Download Template).

**Five toolbar buttons:**

1. **Download Template…** — writes `Configs\Template.json`, a
   fully-commented profile file matching the shipped defaults. The
   first key is `_comment_IMPORTANT` telling you to rename the file
   before editing so the next Download Template can't clobber it.
   If the template already exists, you get a confirmation dialog
   with three choices: **Overwrite template** (destructive),
   **Save fresh template as…** (defaults to `Template_2.json`),
   or **Cancel**.
2. **Import config…** — pick any `.json` file (Open dialog filters
   to `*.json`) and PingPair parses + validates it, populating
   the form. Schema errors land in the red status banner — no
   pydantic stack trace in your face. Partial files (e.g. one that
   only overrides `test_plan.duration_s`) load cleanly with all
   other sections inheriting from `defaults.json`.
3. **Save As…** — write the current form values to a new `.json`
   file under `Configs\`. Use this to fork a profile after edits.
4. **Apply to current session** — push the form's values into the
   live AppConfig. The Run tab's grid rebuilds to match (e.g.
   if you changed payloads to `[100, 500, 900]` and bandwidths
   to `[25, 75]`, the Run tab now shows a 6-case table). The
   Setup tab also re-runs its prereqs against the new IPs. While
   a sweep is in flight this button is greyed out — apply mid-sweep
   would clash with the running plan.
5. **Reset to defaults** — wipe the form back to `defaults.json`
   (confirmation dialog first). Doesn't apply automatically;
   you still need to click Apply to push.

**The form** is a single `Test plan` group box containing all 12
parameters in four side-by-side columns with thin sunken separators:

| Col 1 | Col 2 | Col 3 | Col 4 |
|---|---|---|---|
| Payloads (B) | Interval (iperf3 -i) | Client IP | iperf3 port |
| Bandwidths (Mbps) | Protocol (UDP / TCP) | Subnet mask | fping Interval (-p) |
| Duration per case | Server IP | Control port | fping Extra args |

Every parameter is visible at once — no scrolling. Mouse-wheel
scrolling over spinboxes is disabled, so you can't accidentally
change Duration / Interval / Ports while reviewing an imported
profile.

**The Raw JSON pane** below the form holds the same data in
canonical pretty-printed JSON. A left-side grey gutter shows
line numbers (so parse errors that say "line N, column N" are
actionable). Auto-sync goes both directions:

- Edits in the form push to the JSON pane instantly.
- Edits in the JSON pane push back to the form ~1 second after
  you stop typing (debounced so partial mid-typing edits don't
  flash errors). Parse / schema errors land in the status banner
  but the form is **not** touched, so you can fix the JSON in
  place without losing your form state.

**The Live CLI preview** at the bottom shows the three command
strings (iperf3 server / iperf3 client / fping) for the first
case of the current plan — a quick sanity check that the form's
values produce the iperf3 invocation you expect.

**Typical workflow for a new customer / hardware pair:**

1. Open Config tab → click Download Template.
2. Open `Configs\Template.json` in your editor of choice.
3. Edit the values you want to override. Save as e.g.
   `CarPair-M2M4.json` so the next template download can't
   clobber your work.
4. Back in PingPair → Import config… → pick your file.
5. Click Apply to current session.
6. Switch to Run tab → press Run sweep. The sweep walks your
   custom grid.

Sweep report sidecars in `Reports\` use the same plain `.json`
naming as profile files (they're outputs of sweeps, consumed by
the Analysis tab). The on-disk schema is unchanged — only the
file extension was unified.

### 2.5 Reports — save flow

There are two save flows, picked by the **Auto save** master toggle
on the Save Options tab.

**Auto save OFF (the default)** — after every test finishes, a
**Sweep complete — save report?** dialog pops with the result summary
at the top, then a Destination + Filename pattern form, then a
**Don't ask me in the future** checkbox, then **Save** / **Skip**
buttons. Edit the Destination or Pattern per-run if you want; click
**Save** to write the files (or **Skip** to discard the run — the
result is still in memory and can be saved later from the Report
tab's **Save report now** button). Ticking **Don't ask me in the
future** before clicking Save flips Auto save back on with whatever
Destination + Pattern you typed as the new defaults.

**Auto save ON** — same hands-free behaviour as the older releases:
PingPair writes the report set to the configured Destination +
Filename pattern from the Save Options tab without prompting, then shows a
result popup with the saved-file paths. The Destination + Filename
pattern fields are only editable in this state — when Auto save is
off they're greyed-out *defaults* for the dialog above.

Either way, PingPair writes a **per-sweep subfolder** under the
chosen Destination, named after the resolved filename pattern (e.g.
`PingPair_<YYYY-MM-DD>_<HHMMSS>\` with the default, or whatever you
typed). Inside the subfolder go all artefacts for the run: a Word
document (`.docx`), an Excel workbook (`.xlsx`), and a matching
`.json` sidecar with the full provenance for that run. PDF
and TXT are available too — tick the boxes in the Save Options tab.
The Word and PDF reports are headed with the PingPair logo above
the title.

> **Auto-suffix on custom filenames.** If your filename pattern
> doesn't include a `{date}` / `{time}` token (e.g. you typed `test`),
> the next run with the same pattern auto-suffixes the folder to
> `test_2`, `test_3`, etc. instead of silently overwriting. The
> default pattern is naturally unique because of the timestamp tokens
> so the suffix never kicks in unless you've customised.

Each single-sweep report carries:

- A title block with run ID, timestamps, both IPs, software versions
  + any populated **Test-record metadata** (technician, customer,
  hardware S/N, environment, record ID) — these stay populated
  across launches via QSettings.
- The **Performance Metrics** table — the 8-column results shape
  (Payload / BW Pushed / Throughput Received / Jitter / Loss /
  Min / Avg / Max latency).
- A **Per-case detail** section with status, return codes, error
  notes, and the exact iperf3/fping CLI strings used for that case.

**Multi-segment** reports (saved when you use Continuous mode —
§2.4a) replace the single Performance Metrics + Per-case detail
sections with:

- A **Segments summary** table listing every segment (label,
  duration, cases ok / total, status).
- One **Performance Metrics** table per segment.
- Three **Cross-segment comparison** tables (Throughput / Avg
  Latency / Packet Loss) — rows = case, columns = segment — so you
  can spot regressions between car-pairs at a glance.
- Folder name gets a `_multisegment` suffix to make multi-segment
  runs distinguishable from single sweeps in a flat listing of
  `Reports/`.

The Excel workbook adds a few extras:

- **Summary** sheet — the segments table + run-level info.
- **Throughput / Avg Latency / Packet Loss** sheets — pivoted
  comparison views, one per metric.
- **One Detail sheet per segment** — same shape as the single-sweep
  Detail sheet, listing every case with return codes + CLI strings.
- **Run info** sheet — run-level metadata + the full AppConfig
  snapshot used.

The `.json` sidecar uses **schema v5** — additive top-level
`gateway` + `nic_override` keys on top of v3 (single-sweep) /
v4 (multi-segment, with a `segments` block). Older v3 / v4 files
on disk still load via the same loader; only the file extension
was renamed away from the legacy `.config.json`.

Pre-existing flat-layout `.config.json` reports from before the
per-sweep-folder migration don't appear in the Recent reports
list, and the Analysis tab now auto-loads **only** the canonical
per-sweep layout (`Reports/<name>/<name>.json`) — the old manual
**Add file…** button was removed. To revisit a legacy flat report,
place it in a per-sweep subfolder so the scanner finds it.

### 2.5a Tuning the save defaults

Open the Save Options tab. The **Save settings** group at the top has:

- **Auto save** — master toggle (default off). When ticked, the
  Destination + Pattern fields below become editable. Unticking
  resets Destination back to `Reports/` and Pattern back to
  `PingPair_{date}_{time}` (factory defaults), which also clears
  any stale red-border state from a previous typo.
- **Destination folder** + **Browse…** — where reports land. Only
  editable when Auto save is on; otherwise it's the pre-filled
  default for the post-test save dialog.
- **Filename pattern** — supports `{date}` (YYYY-MM-DD) and `{time}`
  (HHMM) tokens. Same Auto-save-only edit rule. Validated against
  Windows-illegal filename characters; `<` `>` `|` `"` `*` `?`
  `:` `/` `\` all flag a red border + diagnostic tooltip pinpointing
  the bad character and its position.
- **Formats** — `.docx` / `.xlsx` / `.pdf` / `.txt` checkboxes.
  Always editable regardless of Auto save state — they apply to both
  flows.
- **Reset to defaults** — confirms then wipes Auto save / Destination
  / Pattern / Formats back to factory values. Test-record metadata
  and the Recent reports list are *not* touched.

The **Test-record metadata (optional)** group below holds
technician / customer / hardware S/N / environment / record-ID
fields. Populated values appear on every report's title page and
the xlsx Run-info sheet; blank fields are omitted. Persists across
launches via QSettings.

---

## 3. Day-to-day commands

From source, run these from the `Software\` folder of your checkout. With the
packaged build, double-click `PingPair.exe` — or append the same flags to it
(e.g. `PingPair.exe --loopback`).

```cmd
cd Software

:: launch the GUI
python -m pingpair

:: headless prereq check (CI-friendly; non-zero exit on any FAIL)
python -m pingpair --check-prereqs

:: loopback mode (skip the role picker, run both sides on 127.0.0.1)
python -m pingpair --loopback
```

---

## 4. Folder layout

```
Software\
├── bin\
│   ├── fping\          fping.exe + cygwin1.dll (2020 build)
│   └── iperf3\         iperf3.exe (3.21) + cygwin1.dll (2026-03 build) + cygz.dll + cygcrypto-3.dll
├── src\pingpair\       the application (GUI + core + reporting)
├── Reports\            run output — auto-created, not in source control
├── Configs\            saved test-plan profiles — auto-created on first use
├── requirements.txt
├── pyproject.toml
├── THIRD_PARTY_LICENSES.md
└── README.md           (you are here)
```

In the **packaged build** the same `bin\`, `Reports\`, and `Configs\` folders
sit next to `PingPair.exe` (the runtime lives in the adjacent `_internal\`
folder) — so your reports and saved profiles are right beside the app.

The two binaries each ship a different `cygwin1.dll`, which is why they
live in separate sibling folders under `bin\` instead of being merged.
Windows DLL search order resolves each binary's matching runtime from
its own folder.

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `py -3.11 ... No suitable Python runtime found` | Python not installed, or `winget` install left an inconsistent state | Use the python.org installer (§1.1), tick "Add python.exe to PATH", then close and reopen the terminal. (Source install only — the packaged build needs no Python.) |
| Setup tab: "Local NIC IP" is FAIL | Static IP not set, or NIC is on the wrong subnet | Click the row's **Set the correct IP** button (auto-detects the primary Ethernet adapter and runs `netsh interface ipv4 set address` for the role's canonical IP — see §2.3). Falls back to opening `ncpa.cpl` if the adapter can't be detected or the role is Loopback. |
| Top banner shows "Server role" / "Client role" but the orange Setup tab banner says "expected 192.168.1.X to be bound, but it isn't" | The saved role doesn't match the currently bound NIC IP — typical after a NIC reset, an IP change, or moving the laptop to a different LAN. The role itself is **never auto-flipped** to avoid silent surprises. | Click **Set the correct IP** on the Local NIC IP FAIL row to fix the IP for the saved role, or change the role on the Setup tab (Apply role) if this PC is now meant to play the other side. |
| Setup tab: firewall rows stay yellow after pressing Fix | Not running as Administrator | Close the app, relaunch from an elevated CMD: `python -m pingpair`. |
| iperf3 client: "unable to receive control message" on second run | Stale iperf3 listener from a forced kill | Wait 30 s, retry. The case orchestrator avoids this by letting `-1` exit cleanly between cases. |
| Live latency chart only shows last few seconds | (Old bug — fixed.) Chart cap was 300 points. Current cap is 10 000 points so the full run is visible. | — |
| fping run takes ~48 s for a 30 s case | Windows timer granularity (~15.6 ms) makes `-p 10` deliver one packet every ~16 ms instead of 10 ms. Expected — just wait. | The progress bar and ETA are duration-aware (`core.runner.estimate_case_wall_s`), so the per-case `Ns/Ms` text matches reality at any configured duration. |
| Client sat at "Running" for ~25 s after Server was killed | iperf3 (UDP) doesn't notice a dead peer — only CASE_DONE write fails after iperf3 completes naturally. (Old bug — fixed in Round-7.) The Client now spawns a background socket monitor that aborts iperf3 within ~1 s of a peer disconnect via `select`+`MSG_PEEK`. | — |
| Orange banner reads "Server disconnected during case (monitor)" right after you pressed Stop on the Client | (Old bug — fixed in Round-8.) The Round-7 monitor thread saw the user-closed socket as a peer disconnect and misattributed it. | The monitor + run_sweep error paths now check `self._stop` first and bail cleanly without emitting an error event; `_on_stop` also clears the persistent banner immediately. No action required on a current build. |
| Setup tab: "Wi-Fi disabled" is WARN with no fix needed | A Wi-Fi adapter is enabled but disconnected from any AP. Counts as "off" for this check — only Wi-Fi adapters with a non-loopback IPv4 trigger the WARN. | No action required. |
| Subset Run button is disabled | All Run checkboxes are unchecked — there's nothing to run | Click **Select all** in the Sweep subset group, or pick the cases you want via the per-payload / per-bandwidth toggle buttons. The Run button re-enables as soon as at least one case is ticked. |
| Apply (on Config tab) is greyed out | A sweep is currently active — applying a new test plan mid-sweep would clash with the running plan | Wait for the sweep to finish, or press **Stop** on the Run tab, then re-click Apply. |
| `Test.config.config.json` appears in `Configs\` after Save As | (Old bug — fixed during Group E.) Qt's Windows file-dialog auto-append produced doubled suffixes when the user typed `.config`. | Profiles now use plain `.json`; rename any legacy `.config.json` profile to `.json` and re-import. |
| Sweep finished but no files appeared in `Reports\` | **Auto save** is off (the new default) and you clicked **Skip** on the save dialog | Click **Save report now** on the Save Options tab — the in-memory result is still there until the next sweep starts. Or tick **Don't ask me in the future** the next time you save to flip Auto save back on. |
| Report shows fewer than 20 rows | A Sweep subset was active for that run (Group B / Sweep subset group on the Run tab) | Expected — reports only contain the cases that actually ran. The `.json` sidecar's top-level `selected_case_indexes` field records the subset for audit. |
| Setup tab shows the override fields greyed out but the placeholders are wrong for my LAN | The Config tab profile defaults still show through — empty override fields fall back to the profile per field | Either tick the **Use a custom IP configuration** checkbox and type the right values (per-PC override), or open the Config tab and edit the profile defaults (shared across PCs). |
| "External IP change detected" dialog pops every time I open the Setup tab | The currently-bound IP genuinely doesn't match what PingPair last applied | Click **Keep new IP** to accept the new value as the baseline, or **Restore previous** to put back the last-applied. Clicking **Cancel** applies a 30 s cooldown before the dialog can re-appear. |

---

For the release history see [`../CHANGELOG.md`](../CHANGELOG.md). Questions,
bug reports, and feature requests:
<https://github.com/mhmd2520/PingPair/issues>.
