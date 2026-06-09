"""In-app update check + download against GitHub Releases (Feature 6).

Pure logic only — **no Qt, no QSettings, no globals**. The view layer
(:mod:`pingpair.views.about_view`) drives :class:`QThread` workers that call
into this module off the GUI thread and render the returned
:class:`UpdateCheckResult`.

Version model: a GitHub **Release** is anchored to a git **tag**, so the
release's ``tag_name`` is the version signal (strip the leading ``v`` and
compare to :data:`pingpair.__version__`). The release also carries the
**built bundle** as an attached asset — a packaged build's user has no
Python, so a real self-update must download a *built* artifact, not the
source zip GitHub auto-serves per tag. We therefore poll ``releases/latest``
(it returns both the tag and the assets) and download the bundle asset.

Network model: the release *check* is itself a reachability probe — any
failure (no internet, DNS, timeout, Wi-Fi disabled for testing, a static IP
with no gateway) surfaces as :class:`UpdateStatus.ERROR` with a human-readable
``detail``; the caller adds state-specific context. Before the *download* (the
long operation) the caller additionally runs :func:`preflight_check` — a fast,
short-timeout GET — so an offline machine gets a clear "no internet route"
message in seconds instead of waiting out the full 60 s download timeout. This
matters for PingPair specifically: during a sweep it deliberately disables
Wi-Fi and uses a point-to-point Ethernet link with no gateway, so the update
path is legitimately offline much of the time. PingPair's own firewall rules
are inbound-only, so they never block this outbound call.

Release repo: **production points at the public repo
``mhmd2520/PingPair``** (:data:`RELEASE_REPO`). A missing repo / no releases
yet returns HTTP 404, handled gracefully as :class:`UpdateStatus.NO_RELEASE`
("no release channel yet"), **not** an error.
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Public release coordinates. Production: the updater checks the clean public
# repo for releases. Changing the two constants here is the single edit needed
# to repoint the whole updater + the About-tab GitHub link (which derives its
# URL from these).
RELEASE_OWNER = "mhmd2520"
RELEASE_REPO = "PingPair"

GITHUB_REPO_URL = f"https://github.com/{RELEASE_OWNER}/{RELEASE_REPO}"
RELEASES_API_URL = (
    f"https://api.github.com/repos/{RELEASE_OWNER}/{RELEASE_REPO}/releases/latest"
)
# Human-facing "open the release page" fallback when the API response omits
# an ``html_url`` (it always includes one in practice, but be defensive).
RELEASES_PAGE_URL = f"{GITHUB_REPO_URL}/releases/latest"

# GitHub rejects API requests without a User-Agent (HTTP 403). Identify
# ourselves; the version is appended by the caller via check_for_update.
_USER_AGENT = "PingPair-update-check"

_DOWNLOAD_CHUNK = 1024 * 1024  # 1 MiB streamed-read block — fewer syscalls /
# Python-loop iterations than the old 256 KiB on a weak VM vCPU.

# Opener signature: (request, *, timeout) -> a context-manager file-like
# response (.read(), .headers). Matches urllib.request.urlopen so tests can
# pass a fake without touching the network.
Opener = Callable[..., Any]
# Download progress callback: (bytes_so_far, total_bytes_or_0).
ProgressFn = Callable[[int, int], None]
# Cancel predicate: download_file polls it after each chunk and aborts when True.
CancelFn = Callable[[], bool]


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject any redirect that downgrades to a non-HTTPS scheme.

    urllib's default opener silently follows 3xx redirects — *including* an
    ``https://`` → ``http://`` downgrade. GitHub's ``browser_download_url``
    is itself a redirect to a CDN, so without this guard an active MITM could
    steer the **elevated** self-update download onto plaintext HTTP, defeating
    the HTTPS gate the UI thinks it enforced. https→https is fine; anything
    else is refused.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not str(newurl).lower().startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing insecure redirect to non-HTTPS URL: {newurl}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Built once: an opener identical to urlopen's default chain but with the
# strict-HTTPS redirect handler swapped in. Used as the default for every
# network call below; tests still inject their own fake opener.
_HTTPS_OPENER = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())


def _default_open(request: Any, *, timeout: float) -> Any:
    """Strict-HTTPS replacement for ``urllib.request.urlopen``.

    Refuses a non-HTTPS *initial* URL outright (defence in depth — the API /
    asset URLs are HTTPS by construction) and follows redirects only while
    they stay HTTPS via :class:`_HTTPSOnlyRedirectHandler`.
    """
    full_url = (
        request.full_url
        if isinstance(request, urllib.request.Request)
        else str(request)
    )
    if not full_url.lower().startswith("https://"):
        raise UpdateCheckError(f"refusing to fetch a non-HTTPS URL: {full_url}")
    return _HTTPS_OPENER.open(request, timeout=timeout)


class UpdateCheckError(Exception):
    """The release check/download could not complete (network/HTTP/parse/IO).

    Carries a short, user-readable message in ``str(exc)`` suitable for a
    status line or dialog — never a raw traceback.
    """


class ReleaseNotFound(UpdateCheckError):
    """The repo or its ``latest`` release returned HTTP 404.

    Distinct from a generic error so the caller can render the friendlier
    "no release published yet" state instead of a red failure — expected
    until the public repo + first Release exist.
    """


class DownloadCancelled(UpdateCheckError):
    """The user cancelled an in-flight download.

    Distinct so the caller can reset quietly (no red error dialog) instead
    of treating it as a failure.
    """


class UpdateStatus(enum.Enum):
    """Outcome of an update check."""

    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    NO_RELEASE = "no_release"  # repo/release 404 or no usable tag yet
    ERROR = "error"  # network / HTTP / parse failure


@dataclass(frozen=True)
class ReleaseAsset:
    """A downloadable file attached to a GitHub Release."""

    name: str
    url: str  # browser_download_url
    size: int = 0


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of the pre-download reachability probe (:func:`preflight_check`).

    ``ok`` True means GitHub answered (any HTTP status counts — we reached it).
    ``detail`` carries a short, user-readable reason when ``ok`` is False, ready
    for the caller to enrich with state-specific hints (Wi-Fi off, Loopback).
    """

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class UpdateCheckResult:
    """Structured result of a single update check.

    ``latest_version`` / ``release_url`` are populated whenever the API
    answered with a usable release (UP_TO_DATE or UPDATE_AVAILABLE).
    ``asset`` / ``sha256_url`` are populated only when UPDATE_AVAILABLE *and*
    the release carries a downloadable bundle. ``detail`` carries the human
    message for NO_RELEASE / ERROR.
    """

    status: UpdateStatus
    current_version: str
    latest_version: str = ""
    release_url: str = ""
    asset: ReleaseAsset | None = None
    sha256_url: str = ""
    detail: str = ""
    # The release ``body`` (Markdown "what's new"), shown in the update modal.
    # Empty when the release has no notes or for non-UPDATE_AVAILABLE results.
    release_notes: str = ""

    @property
    def update_available(self) -> bool:
        return self.status is UpdateStatus.UPDATE_AVAILABLE

    @property
    def can_self_install(self) -> bool:
        """True when there's a concrete bundle to download and install."""
        return self.update_available and self.asset is not None


def parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted numeric version into a comparable int tuple.

    Tolerant of the shapes GitHub tags take in the wild:

    * Strips a leading ``v`` / ``V`` (``"v0.2.0"`` -> ``(0, 2, 0)``).
    * Reads the leading integer of each dot-separated chunk, so a
      pre-release suffix is ignored at the chunk where it appears
      (``"0.2.0-rc1"`` -> ``(0, 2, 0)``, ``"1.2-beta"`` -> ``(1, 2)``).
    * Returns ``()`` for anything with no leading numeric component
      (``""``, ``"latest"``) so callers can treat it as "unparseable".
    """
    core = text.strip().lstrip("vV").strip()
    parts: list[int] = []
    for chunk in core.split("."):
        chunk = chunk.strip()
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True iff ``latest`` is a strictly higher version than ``current``.

    Both are parsed via :func:`parse_version` and zero-padded to equal
    length so ``"0.2"`` and ``"0.2.0"`` compare equal (neither is newer).
    An unparseable ``latest`` is never "newer" — the caller surfaces that
    as an error rather than a phantom update.
    """
    lv = parse_version(latest)
    if not lv:
        return False
    cv = parse_version(current)
    width = max(len(lv), len(cv))
    lv += (0,) * (width - len(lv))
    cv += (0,) * (width - len(cv))
    return lv > cv


def select_bundle_asset(
    assets: list[dict[str, Any]],
) -> tuple[ReleaseAsset | None, str]:
    """Pick the Windows bundle ``.zip`` and its ``.sha256`` sidecar URL.

    Returns ``(asset, sha256_url)``. ``asset`` is the first ``.zip`` that
    isn't itself a checksum file; ``sha256_url`` is the download URL of a
    ``<something>.sha256`` asset if one was attached (else ``""``). An empty
    ``sha256_url`` is a hard error at install time — the bundle ships unsigned,
    so the download worker refuses to install an unverified update rather than
    skipping the check. Both are ``None``/``""`` when the release carries no
    usable bundle.
    """
    bundle: ReleaseAsset | None = None
    sha_url = ""
    for raw in assets:
        name = str(raw.get("name", ""))
        url = str(raw.get("browser_download_url") or "")
        if not name or not url:
            continue
        lower = name.lower()
        if lower.endswith(".sha256"):
            sha_url = url
        elif lower.endswith(".zip") and bundle is None:
            try:
                size = int(raw.get("size", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            bundle = ReleaseAsset(name=name, url=url, size=size)
    return bundle, sha_url


def fetch_latest_release(
    url: str = RELEASES_API_URL,
    *,
    opener: Opener | None = None,
    timeout: float = 10.0,
    user_agent: str = _USER_AGENT,
) -> dict[str, Any]:
    """GET the GitHub ``releases/latest`` JSON for ``url``.

    Returns the decoded JSON object. Raises :class:`ReleaseNotFound` on
    HTTP 404 (repo/release absent) and :class:`UpdateCheckError` on any
    other HTTP status, a transport failure, or an unparseable body.

    ``opener`` defaults to :func:`urllib.request.urlopen`; tests inject a
    fake with the same ``(request, *, timeout)`` signature.
    """
    open_url = opener or _default_open
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
        },
    )
    try:
        with open_url(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseNotFound("No release has been published yet.") from exc
        raise UpdateCheckError(f"GitHub returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateCheckError(f"Could not reach GitHub ({reason}).") from exc

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpdateCheckError("GitHub sent an unreadable response.") from exc
    if not isinstance(data, dict):
        raise UpdateCheckError("GitHub sent an unexpected response shape.")
    return data


def check_for_update(
    current_version: str,
    *,
    url: str = RELEASES_API_URL,
    opener: Opener | None = None,
    timeout: float = 10.0,
) -> UpdateCheckResult:
    """Check whether a newer PingPair release is published.

    Orchestrates :func:`fetch_latest_release` + :func:`is_newer` +
    :func:`select_bundle_asset` and maps every outcome onto an
    :class:`UpdateCheckResult` so the UI never has to interpret raw
    exceptions:

    * empty ``url`` or a 404 -> ``NO_RELEASE`` (no channel yet);
    * a release whose ``tag_name`` has no numeric version -> ``NO_RELEASE``;
    * a strictly higher tag -> ``UPDATE_AVAILABLE`` (with version, URL,
      and the bundle asset when one is attached);
    * same or older -> ``UP_TO_DATE``;
    * any transport / HTTP / parse failure -> ``ERROR`` (with ``detail``).
    """
    if not url:
        return UpdateCheckResult(
            status=UpdateStatus.NO_RELEASE,
            current_version=current_version,
            detail="No update channel is configured.",
        )
    try:
        data = fetch_latest_release(
            url,
            opener=opener,
            timeout=timeout,
            user_agent=f"{_USER_AGENT}/{current_version}",
        )
    except ReleaseNotFound as exc:
        return UpdateCheckResult(
            status=UpdateStatus.NO_RELEASE,
            current_version=current_version,
            detail=str(exc),
        )
    except UpdateCheckError as exc:
        return UpdateCheckResult(
            status=UpdateStatus.ERROR,
            current_version=current_version,
            detail=str(exc),
        )

    tag = str(data.get("tag_name", "")).strip()
    latest_version = tag.lstrip("vV").strip()
    release_url = str(data.get("html_url") or "").strip() or RELEASES_PAGE_URL

    if not parse_version(tag):
        return UpdateCheckResult(
            status=UpdateStatus.NO_RELEASE,
            current_version=current_version,
            detail=(
                f"The latest release has no recognisable version number ({tag!r})."
                if tag
                else "The latest release has no version tag."
            ),
        )

    if not is_newer(tag, current_version):
        return UpdateCheckResult(
            status=UpdateStatus.UP_TO_DATE,
            current_version=current_version,
            latest_version=latest_version,
            release_url=release_url,
        )

    assets = data.get("assets")
    asset, sha_url = (
        select_bundle_asset(assets) if isinstance(assets, list) else (None, "")
    )
    return UpdateCheckResult(
        status=UpdateStatus.UPDATE_AVAILABLE,
        current_version=current_version,
        latest_version=latest_version,
        release_url=release_url,
        asset=asset,
        sha256_url=sha_url,
        release_notes=str(data.get("body") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Pre-flight reachability probe (Round-6 #4)
# ---------------------------------------------------------------------------


def preflight_check(
    *,
    url: str = "https://api.github.com/",
    opener: Opener | None = None,
    timeout: float = 5.0,
    user_agent: str = _USER_AGENT,
) -> PreflightResult:
    """Fast "is there a route out?" probe, run right before a download.

    A short-timeout HTTPS GET to GitHub. The point is *speed*: it confirms a
    working internet route in a few seconds so the UI can show a clear
    "no connection" message instead of letting the full 60 s download timeout
    elapse on an offline machine (PingPair often runs offline by design — Wi-Fi
    disabled, point-to-point Ethernet with no gateway during a sweep).

    Any HTTP *status* — even 403/404 — counts as reachable (we got a response,
    so the route is fine; ``check_for_update`` interprets the actual release
    JSON separately). Only a transport failure (DNS, timeout, no route, refused)
    is treated as offline. Never raises — always returns a
    :class:`PreflightResult`.
    """
    open_url = opener or _default_open
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
        },
    )
    try:
        with open_url(request, timeout=timeout):
            pass
    except urllib.error.HTTPError:
        # Reached GitHub — it answered with an HTTP error status. Route is fine.
        return PreflightResult(ok=True)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return PreflightResult(
            ok=False,
            detail=f"No internet route to GitHub ({reason}).",
        )
    return PreflightResult(ok=True)


# ---------------------------------------------------------------------------
# Download + integrity
# ---------------------------------------------------------------------------


def download_file(
    url: str,
    dest: Path,
    *,
    opener: Opener | None = None,
    timeout: float = 60.0,
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
    user_agent: str = _USER_AGENT,
) -> Path:
    """Stream ``url`` to ``dest``, reporting progress as it goes.

    ``progress(bytes_so_far, total_or_0)`` is called after each chunk
    (``total`` is 0 when the server omits ``Content-Length``). ``cancelled``,
    if given, is polled after each chunk; when it returns True the partial
    file is removed and :class:`DownloadCancelled` is raised.

    Raises :class:`UpdateCheckError` on any transport / HTTP / IO failure,
    and removes a partial file so a failed (or cancelled) download never
    leaves a corrupt artifact behind. Returns ``dest``.
    """
    open_url = opener or _default_open
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": user_agent,
        },
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open_url(request, timeout=timeout) as response:
            total = _content_length(response)
            got = 0
            with open(dest, "wb") as fh:
                while True:
                    if cancelled is not None and cancelled():
                        raise DownloadCancelled("Download cancelled.")
                    block = response.read(_DOWNLOAD_CHUNK)
                    if not block:
                        break
                    fh.write(block)
                    got += len(block)
                    if progress is not None:
                        progress(got, total)
    except DownloadCancelled:
        _unlink_quietly(dest)
        raise
    except urllib.error.HTTPError as exc:
        _unlink_quietly(dest)
        raise UpdateCheckError(f"Download failed (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _unlink_quietly(dest)
        reason = getattr(exc, "reason", exc)
        raise UpdateCheckError(f"Download failed ({reason}).") from exc
    return dest


def fetch_text(
    url: str,
    *,
    opener: Opener | None = None,
    timeout: float = 15.0,
    user_agent: str = _USER_AGENT,
) -> str:
    """GET a small text resource (e.g. a ``.sha256`` sidecar). Stripped."""
    open_url = opener or _default_open
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with open_url(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateCheckError(f"Could not fetch checksum ({reason}).") from exc
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw.strip()


def sha256_file(path: Path, *, chunk: int = _DOWNLOAD_CHUNK) -> str:
    """Return the lowercase hex SHA-256 of ``path``, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_sha256_text(text: str) -> str:
    """Extract the hex digest from a ``.sha256`` file's contents.

    Accepts both a bare 64-char hex string and the ``<hex>  <filename>``
    shape ``sha256sum`` emits. Tolerates a leading UTF-8 BOM (Windows tools
    routinely prepend one). Returns lowercase hex, or ``""`` if no plausible
    digest is present.
    """
    cleaned = text.lstrip("﻿").strip()
    token = cleaned.split()[0].lower() if cleaned else ""
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
        return token
    return ""


def verify_sha256(path: Path, expected_hex: str) -> bool:
    """True iff ``path``'s SHA-256 matches ``expected_hex`` (case-insensitive)."""
    expected = expected_hex.strip().lower()
    if len(expected) != 64:
        return False
    return sha256_file(path) == expected


def _content_length(response: Any) -> int:
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0
    try:
        return int(headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
