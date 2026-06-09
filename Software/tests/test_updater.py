"""Tests for the Feature-6 in-app update checker (pure logic, no Qt)."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

from pingpair.core import updater
from pingpair.core.updater import (
    RELEASES_PAGE_URL,
    ReleaseAsset,
    ReleaseNotFound,
    UpdateCheckError,
    UpdateStatus,
    check_for_update,
    download_file,
    fetch_latest_release,
    is_newer,
    parse_sha256_text,
    parse_version,
    select_bundle_asset,
    sha256_file,
    verify_sha256,
)

# --------------------------------------------------------------------------
# Fake HTTP opener — matches urllib.request.urlopen's (request, *, timeout).
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        self._buf = BytesIO(body)
        # urllib responses expose .headers.get(...); a plain dict is close
        # enough for the one key (Content-Length) our code reads.
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _json_opener(payload: dict, *, capture: dict | None = None):
    body = json.dumps(payload).encode("utf-8")

    def _open(request, *, timeout=None):
        if capture is not None:
            capture["url"] = request.full_url
            capture["headers"] = dict(request.header_items())
            capture["timeout"] = timeout
        return _FakeResponse(body)

    return _open


def _bytes_opener(body: bytes, *, total: int | None = None):
    headers = {} if total is None else {"Content-Length": str(total)}

    def _open(request, *, timeout=None):
        return _FakeResponse(body, headers)

    return _open


def _raising_opener(exc: Exception):
    def _open(request, *, timeout=None):
        raise exc

    return _open


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.github.com/x",
        code=code,
        msg="boom",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# --------------------------------------------------------------------------
# parse_version
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0.1.0", (0, 1, 0)),
        ("v0.2.0", (0, 2, 0)),
        ("V1.2.3", (1, 2, 3)),
        ("  v3.4  ", (3, 4)),
        ("1", (1,)),
        ("0.2.0-rc1", (0, 2, 0)),
        ("1.2-beta", (1, 2)),
        ("10.20.30", (10, 20, 30)),
    ],
)
def test_parse_version_shapes(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "latest", "v", "release-x"])
def test_parse_version_unparseable_is_empty(text):
    assert parse_version(text) == ()


# --------------------------------------------------------------------------
# is_newer
# --------------------------------------------------------------------------


def test_is_newer_true_when_higher():
    assert is_newer("0.2.0", "0.1.0") is True
    assert is_newer("v1.0.0", "0.9.9") is True
    assert is_newer("0.1.1", "0.1.0") is True


def test_is_newer_false_when_same_or_lower():
    assert is_newer("0.1.0", "0.1.0") is False
    assert is_newer("0.1.0", "0.2.0") is False


def test_is_newer_pads_unequal_lengths():
    # 0.2 and 0.2.0 are equal — neither is newer in either direction.
    assert is_newer("0.2", "0.2.0") is False
    assert is_newer("0.2.0", "0.2") is False


def test_is_newer_unparseable_latest_is_never_newer():
    assert is_newer("latest", "0.1.0") is False
    assert is_newer("", "0.1.0") is False


# --------------------------------------------------------------------------
# fetch_latest_release
# --------------------------------------------------------------------------


def test_fetch_returns_decoded_json():
    data = fetch_latest_release(
        "https://x", opener=_json_opener({"tag_name": "v9.9.9"})
    )
    assert data["tag_name"] == "v9.9.9"


def test_fetch_sends_user_agent_and_accept_header():
    cap: dict = {}
    fetch_latest_release(
        "https://api.example/x",
        opener=_json_opener({"tag_name": "v1"}, capture=cap),
        user_agent="PingPair-test/1.2.3",
    )
    assert cap["url"] == "https://api.example/x"
    assert cap["headers"]["User-agent"] == "PingPair-test/1.2.3"
    assert cap["headers"]["Accept"] == "application/vnd.github+json"


def test_fetch_404_raises_release_not_found():
    with pytest.raises(ReleaseNotFound):
        fetch_latest_release("https://x", opener=_raising_opener(_http_error(404)))


def test_fetch_other_http_status_raises_update_error():
    with pytest.raises(UpdateCheckError) as ei:
        fetch_latest_release("https://x", opener=_raising_opener(_http_error(500)))
    assert "500" in str(ei.value)
    assert not isinstance(ei.value, ReleaseNotFound)


def test_fetch_urlerror_raises_update_error():
    err = urllib.error.URLError("name or service not known")
    with pytest.raises(UpdateCheckError) as ei:
        fetch_latest_release("https://x", opener=_raising_opener(err))
    assert "reach GitHub" in str(ei.value)


def test_fetch_timeout_raises_update_error():
    with pytest.raises(UpdateCheckError):
        fetch_latest_release(
            "https://x", opener=_raising_opener(TimeoutError("slow"))
        )


def test_fetch_bad_json_raises_update_error():
    def _open(request, *, timeout=None):
        return _FakeResponse(b"not json {")

    with pytest.raises(UpdateCheckError) as ei:
        fetch_latest_release("https://x", opener=_open)
    assert "unreadable" in str(ei.value).lower()


def test_fetch_non_object_json_raises_update_error():
    def _open(request, *, timeout=None):
        return _FakeResponse(b"[1, 2, 3]")

    with pytest.raises(UpdateCheckError):
        fetch_latest_release("https://x", opener=_open)


# --------------------------------------------------------------------------
# check_for_update — outcome mapping
# --------------------------------------------------------------------------


def test_check_update_available():
    res = check_for_update(
        "0.1.0",
        url="https://x",
        opener=_json_opener(
            {"tag_name": "v0.2.0", "html_url": "https://gh/releases/0.2.0"}
        ),
    )
    assert res.status is UpdateStatus.UPDATE_AVAILABLE
    assert res.update_available is True
    assert res.latest_version == "0.2.0"
    assert res.current_version == "0.1.0"
    assert res.release_url == "https://gh/releases/0.2.0"


def test_check_update_available_carries_release_notes():
    res = check_for_update(
        "0.1.0",
        url="https://x",
        opener=_json_opener(
            {"tag_name": "v0.2.0", "body": "- Faster downloads\n- Bug fixes"}
        ),
    )
    assert res.status is UpdateStatus.UPDATE_AVAILABLE
    assert "Faster downloads" in res.release_notes


def test_check_update_available_missing_body_is_empty_notes():
    res = check_for_update(
        "0.1.0", url="https://x", opener=_json_opener({"tag_name": "v0.2.0"})
    )
    assert res.release_notes == ""


def test_check_up_to_date_same_version():
    res = check_for_update(
        "0.2.0", url="https://x", opener=_json_opener({"tag_name": "v0.2.0"})
    )
    assert res.status is UpdateStatus.UP_TO_DATE
    assert res.update_available is False
    assert res.latest_version == "0.2.0"


def test_check_up_to_date_when_local_is_ahead():
    res = check_for_update(
        "0.3.0", url="https://x", opener=_json_opener({"tag_name": "v0.2.0"})
    )
    assert res.status is UpdateStatus.UP_TO_DATE


def test_check_release_url_falls_back_to_page_when_missing():
    res = check_for_update(
        "0.1.0", url="https://x", opener=_json_opener({"tag_name": "v0.2.0"})
    )
    assert res.release_url == RELEASES_PAGE_URL


def test_check_404_is_no_release_not_error():
    res = check_for_update(
        "0.1.0", url="https://x", opener=_raising_opener(_http_error(404))
    )
    assert res.status is UpdateStatus.NO_RELEASE
    assert res.detail


def test_check_empty_url_short_circuits_to_no_release():
    res = check_for_update("0.1.0", url="", opener=_raising_opener(RuntimeError()))
    assert res.status is UpdateStatus.NO_RELEASE


def test_check_tag_without_version_is_no_release():
    res = check_for_update(
        "0.1.0", url="https://x", opener=_json_opener({"tag_name": "latest"})
    )
    assert res.status is UpdateStatus.NO_RELEASE


def test_check_missing_tag_is_no_release():
    res = check_for_update(
        "0.1.0", url="https://x", opener=_json_opener({"name": "no tag here"})
    )
    assert res.status is UpdateStatus.NO_RELEASE


def test_check_network_failure_is_error_with_detail():
    res = check_for_update(
        "0.1.0",
        url="https://x",
        opener=_raising_opener(urllib.error.URLError("offline")),
    )
    assert res.status is UpdateStatus.ERROR
    assert res.detail
    assert res.update_available is False


def test_check_is_total_no_exceptions_escape():
    # Any opener blowing up in an unexpected way must still yield a result,
    # never propagate — the worker relies on this.
    res = check_for_update(
        "0.1.0", url="https://x", opener=_raising_opener(OSError("weird"))
    )
    assert res.status is UpdateStatus.ERROR


def test_release_api_url_is_derived_from_repo_constants():
    # Production points at the public "PingPair" repo. Assert the URL is
    # *derived* from the constants rather than pinning the current repo name,
    # so a one-line repoint doesn't break this test.
    expected_api = (
        f"https://api.github.com/repos/{updater.RELEASE_OWNER}/"
        f"{updater.RELEASE_REPO}/releases/latest"
    )
    assert updater.RELEASE_OWNER == "mhmd2520"
    assert expected_api == updater.RELEASES_API_URL
    assert updater.GITHUB_REPO_URL.endswith(
        f"{updater.RELEASE_OWNER}/{updater.RELEASE_REPO}"
    )


# --------------------------------------------------------------------------
# select_bundle_asset
# --------------------------------------------------------------------------


def _asset(name: str, url: str, size: int = 0) -> dict:
    return {"name": name, "browser_download_url": url, "size": size}


def test_select_bundle_picks_zip_and_sha_sidecar():
    assets = [
        _asset("notes.txt", "https://x/notes.txt"),
        _asset("PingPair-0.2.0-win64.zip", "https://x/bundle.zip", 1234),
        _asset("PingPair-0.2.0-win64.zip.sha256", "https://x/bundle.sha256"),
    ]
    bundle, sha_url = select_bundle_asset(assets)
    assert bundle == ReleaseAsset(
        name="PingPair-0.2.0-win64.zip", url="https://x/bundle.zip", size=1234
    )
    assert sha_url == "https://x/bundle.sha256"


def test_select_bundle_none_when_no_zip():
    bundle, sha_url = select_bundle_asset([_asset("readme.md", "https://x/r.md")])
    assert bundle is None
    assert sha_url == ""


def test_select_bundle_first_zip_wins_and_sha_optional():
    assets = [
        _asset("a.zip", "https://x/a.zip", 10),
        _asset("b.zip", "https://x/b.zip", 20),
    ]
    bundle, sha_url = select_bundle_asset(assets)
    assert bundle.url == "https://x/a.zip"
    assert sha_url == ""


def test_check_update_available_populates_asset():
    res = check_for_update(
        "0.1.0",
        url="https://x",
        opener=_json_opener(
            {
                "tag_name": "v0.2.0",
                "html_url": "https://gh/r/0.2.0",
                "assets": [
                    _asset("PingPair-0.2.0-win64.zip", "https://x/b.zip", 999),
                    _asset("PingPair-0.2.0-win64.zip.sha256", "https://x/b.sha256"),
                ],
            }
        ),
    )
    assert res.status is UpdateStatus.UPDATE_AVAILABLE
    assert res.can_self_install is True
    assert res.asset is not None
    assert res.asset.url == "https://x/b.zip"
    assert res.sha256_url == "https://x/b.sha256"


def test_check_update_available_without_asset_cannot_self_install():
    res = check_for_update(
        "0.1.0", url="https://x", opener=_json_opener({"tag_name": "v0.2.0"})
    )
    assert res.status is UpdateStatus.UPDATE_AVAILABLE
    assert res.asset is None
    assert res.can_self_install is False


# --------------------------------------------------------------------------
# download_file + integrity
# --------------------------------------------------------------------------


def test_download_writes_file_and_reports_progress(tmp_path):
    payload = b"x" * (3 * 1024 * 1024)  # > 2 of the 1 MiB chunks
    seen: list[tuple[int, int]] = []
    dest = tmp_path / "bundle.zip"
    out = download_file(
        "https://x/b.zip",
        dest,
        opener=_bytes_opener(payload, total=len(payload)),
        progress=lambda got, total: seen.append((got, total)),
    )
    assert out == dest
    assert dest.read_bytes() == payload
    assert seen[-1] == (len(payload), len(payload))  # finished at 100%
    assert seen[0][0] < seen[-1][0]  # progressed in steps


def test_download_failure_removes_partial_file(tmp_path):
    dest = tmp_path / "bundle.zip"
    dest.write_bytes(b"stale")
    with pytest.raises(UpdateCheckError):
        download_file(
            "https://x/b.zip",
            dest,
            opener=_raising_opener(urllib.error.URLError("dropped")),
        )
    assert not dest.exists()


def test_download_http_error_is_update_error(tmp_path):
    with pytest.raises(UpdateCheckError) as ei:
        download_file(
            "https://x/b.zip",
            tmp_path / "b.zip",
            opener=_raising_opener(_http_error(503)),
        )
    assert "503" in str(ei.value)


def test_sha256_file_and_verify(tmp_path):
    import hashlib

    blob = b"PingPair update bundle"
    f = tmp_path / "b.zip"
    f.write_bytes(blob)
    expected = hashlib.sha256(blob).hexdigest()
    assert sha256_file(f) == expected
    assert verify_sha256(f, expected) is True
    assert verify_sha256(f, expected.upper()) is True  # case-insensitive
    assert verify_sha256(f, "deadbeef") is False  # wrong length
    assert verify_sha256(f, "0" * 64) is False  # right length, wrong digest


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a" * 64, "a" * 64),
        (("A" * 64), "a" * 64),
        (f"{'b' * 64}  PingPair-0.2.0-win64.zip", "b" * 64),
        ("not-a-hash", ""),
        ("", ""),
        ("xyz" * 30, ""),  # 90 chars, not hex-64
    ],
)
def test_parse_sha256_text(text, expected):
    assert parse_sha256_text(text) == expected
