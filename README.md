# PingPair

**Automated point-to-point LAN characterization for Windows.** PingPair
drives a hands-free 20-case test sweep between two Windows machines using
[`fping`](https://github.com/schweikert/fping) (latency / loss) and
[`iperf3`](https://github.com/esnet/iperf) (throughput / jitter / loss), then
produces a Word + Excel report (PDF / TXT optional) plus the JSON config that
produced it.

The two endpoints can be physical laptops, Windows VMs on a private LAN
segment, or embedded computers on a train's on-board network — the tool
treats them the same as long as both sides share a layer-2 broadcast domain.

---

## What it does

For every `(payload, bandwidth)` pair in a 4 × 5 grid — payloads
`200 / 600 / 1000 / 1300 B`, bandwidths `10 / 30 / 50 / 70 / 90 Mbps` —
PingPair runs one ~30 s test case, then moves to the next. All **20 cases**
run in sequence with no manual intervention.

```
Laptop A — Server  192.168.1.1   ──  Ethernet  ──  Laptop B — Client  192.168.1.2
   iperf3 -s                                          iperf3 -c  +  fping  (per case)
```

Each run produces a per-sweep folder under `Reports/` containing a Word
document, an Excel workbook, and a `.json` sidecar — with the 8-column
results table (throughput, jitter, loss, min/avg/max latency).

## Features

- **Setup tab** — one-glance prerequisite checker (Python, admin, tools,
  NIC IP, firewall rules, Wi-Fi) with copy-paste fixes and opt-in
  "Fix this for me" buttons. Never modifies the host without confirmation.
- **Config tab** — full test-plan editor; keep a library of `.json`
  profiles, edit via form or raw JSON with live two-way sync.
- **Run tab** — drives the sweep. Per-case subset selection, continuous
  multi-segment mode, a real-time progress bar with a duration-aware ETA,
  and live iperf3/fping log + charts. Runs against a remote Server or
  entirely on `127.0.0.1` in Loopback mode.
- **Save Options tab** — Word / PDF / Excel / TXT output, auto-save or
  prompt-per-run, test-record metadata.
- **Analysis tab** — load past runs and overlay / compare / diff their
  metrics; export a comparison report.
- **In-app updater** — checks GitHub Releases, verifies the download's
  SHA-256, and self-updates.

## Download & install

Grab the latest packaged build from the
[**Releases**](https://github.com/mhmd2520/PingPair/releases) page, unzip it,
and run `PingPair.exe`. No Python install required — the bundle ships its own
runtime plus `fping` and `iperf3`. The app updates itself from this same
Releases feed.

## Run from source

```cmd
cd Software
pip install -e ".[dev]"
python -m pytest -v
python -m pingpair
```

Requires Python 3.11+. Full setup — static IPs, firewall, the two-machine
run, every tab — is in **[`Software/README.md`](Software/README.md)**.

## Tech stack

Python 3.11+ · PySide6 (Qt 6) · pyqtgraph · python-docx · reportlab ·
openpyxl · pydantic. Bundled `fping 5.5` + `iperf3 3.21` (Windows/Cygwin).

## Repository layout

```
Software/        Python package (GUI + core + reporting), tests, bundled binaries
CHANGELOG.md     release history (Keep a Changelog)
```

## License

PingPair is **proprietary** — © 2026 Mohamed Khaled, all rights reserved.
See [`LICENSE`](LICENSE). The free-of-charge use grant, restrictions, and
warranty disclaimer are stated there.

Bundled third-party components (`fping`, `iperf3`, the Cygwin runtime,
Qt/PySide6, Python libraries) remain under their own licenses — see
[`Software/THIRD_PARTY_LICENSES.md`](Software/THIRD_PARTY_LICENSES.md) for the
full notices and the corresponding-source offer for the LGPL/Cygwin parts.
