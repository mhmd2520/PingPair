# Changelog

All notable changes to PingPair are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-09

### Added
- Automated 20-case Server/Client LAN sweep (fping + iperf3) with Word /
  Excel / PDF / TXT reports and a JSON sidecar per run.
- Setup prerequisite checker with one-click fixes, role auto-detect, Config
  test-plan editor, Analysis comparison/diff tab, Help guide, and theming.
- **In-app updater (Feature 6).** About → Updates: "Check for updates"
  (manual), an every-launch update reminder with a "Don't remind me again"
  opt-out, and "Download & install" that downloads the new build from the
  GitHub release, verifies its SHA-256, swaps the install, and relaunches.
  Cancel button for an in-flight download; live speed/ETA readout.
- About tab reorganized into four cards (About / Updates / Credits & License /
  Diagnostics).
- The PingPair logo now appears in the header of every Word and PDF report —
  single-sweep, multi-segment, and the Analysis comparison exports.
- Packaging: top-level `LICENSE`, expanded `THIRD_PARTY_LICENSES.md`, and this
  changelog.

### Changed
- Download streamed in 1 MiB chunks (was 256 KiB) for faster transfer.
- The Analysis tab's **Export comparison report** now writes a single
  self-contained folder (`<name>/` plus `<name>/Analysis_Images/`) with
  consistently sized wide charts.
- A sweep is now blocked while Wi-Fi is connected to the test subnet (it would
  otherwise corrupt the measurement), and the Ethernet adapter is reverted to
  DHCP when the app closes.

### Security
- Update download now refuses an `https`→`http` redirect downgrade (strict-HTTPS
  opener), in addition to the existing mandatory SHA-256 check. The release
  bundle remains **unsigned** for v1 — HTTPS + SHA-256 is the integrity story;
  code-signing is a documented post-v1 item.
- All elevated system-tool calls (`netsh` / `ping` / `icacls` / `cmd`) are now
  invoked by absolute `%SystemRoot%\System32` path, removing a PATH/cwd
  binary-planting privilege-escalation surface.
- The Setup-tab NIC override is validated as an IPv4 literal before it reaches
  `netsh`; the report filename pattern is sanitized so it can't write outside
  the Reports folder.

### Fixed
- Self-update now actually applies and relaunches: the swap helper no longer
  uses `timeout` (which fails with no console); it uses `ping`-based waits,
  logs to a file, checks robocopy's exit code, and runs in its own console.
- Closing the window mid-download no longer freezes the app.
- A 100%-loss test case no longer renders the literal text `nan` in the latency
  columns (Word / PDF / TXT), writes a malformed `NaN` token into the JSON
  sidecar, or skews the summary statistics — those latencies now show as "—".
- The Analysis tab's **Export comparison report** now works in every format (it
  previously failed with an internal error for all formats).
- iperf3 results that report a connection error are now recorded as a failed
  case instead of a misleading 0 Mbps measurement.
- The server no longer stalls ~10 seconds (leaving an orphaned iperf3 process)
  when a client disconnects mid-case.
- "Reset to defaults" on the Save Options tab now restores Word + Excel only
  (the documented default), instead of also turning PDF and TXT on.
- The Analysis Diff tab's hint banner is now readable on the Light theme.

[Unreleased]: https://github.com/mhmd2520/PingPair/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mhmd2520/PingPair/releases/tag/v0.1.0
