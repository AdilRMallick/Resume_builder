"""Fetch-path tests. respx intercepts httpx, so nothing here touches the network."""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx
from _feedfixtures import MALFORMED, RUN1

from jme.config import get_settings
from jme.ingestor.feed import (
    FeedNotFoundError,
    FeedParseError,
    FeedUnavailableError,
    fetch_feed,
    load_feed_file,
    parse_feed,
)

FEED_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"


def test_configured_feed_url_is_the_confirmed_upstream_path() -> None:
    """Guards the one thing that silently breaks the whole pipeline.

    Confirmed against the repository tree: `listings.json` lives at
    `.github/scripts/listings.json` on the `dev` branch, and that is the file the
    upstream GitHub Action rewrites.
    """
    url = get_settings().feed_url
    assert url.startswith("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/")
    assert url.endswith("/.github/scripts/listings.json")


@respx.mock
def test_fetch_feed_returns_body_bytes() -> None:
    body = RUN1.read_bytes()
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=body))

    fetched = fetch_feed(FEED_URL)

    assert route.called
    assert fetched == body
    assert parse_feed(fetched).sha256 == hashlib.sha256(body).hexdigest()


@respx.mock
def test_fetch_feed_sends_the_user_agent() -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=b"[]"))

    fetch_feed(FEED_URL, user_agent="job-match-engine/0.1 (+https://example.com)")

    request = respx.calls.last.request
    assert request.headers["user-agent"] == "job-match-engine/0.1 (+https://example.com)"


@respx.mock
def test_fetch_feed_follows_redirects() -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/l.json"})
    )
    respx.get("https://cdn.example.com/l.json").mock(
        return_value=httpx.Response(200, content=b"[]")
    )

    assert fetch_feed(FEED_URL) == b"[]"


@respx.mock
def test_fetch_feed_raises_typed_error_on_404() -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(404, text="404: Not Found"))

    with pytest.raises(FeedNotFoundError) as exc:
        fetch_feed(FEED_URL)

    assert exc.value.status_code == 404
    assert exc.value.url == FEED_URL
    assert "404" in str(exc.value)


@respx.mock
@pytest.mark.parametrize("status", [400, 403, 429, 500, 502, 503])
def test_fetch_feed_raises_unavailable_on_other_error_statuses(status: int) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(FeedUnavailableError) as exc:
        fetch_feed(FEED_URL)

    assert exc.value.status_code == status
    assert not isinstance(exc.value, FeedNotFoundError)


@respx.mock
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("timed out"),
    ],
)
def test_fetch_feed_wraps_transport_errors(error: Exception) -> None:
    respx.get(FEED_URL).mock(side_effect=error)

    with pytest.raises(FeedUnavailableError) as exc:
        fetch_feed(FEED_URL)

    assert type(error).__name__ in str(exc.value)


@respx.mock
def test_malformed_json_body_raises_feed_parse_error() -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=MALFORMED.read_bytes()))

    body = fetch_feed(FEED_URL)
    with pytest.raises(FeedParseError):
        parse_feed(body, source=FEED_URL)


@respx.mock
def test_html_error_page_served_with_200_raises_parse_error() -> None:
    # GitHub occasionally serves a rate-limit HTML page with a 200
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=b"<html><body>rate limited</body></html>")
    )

    with pytest.raises(FeedParseError):
        parse_feed(fetch_feed(FEED_URL), source=FEED_URL)


@respx.mock
def test_fetch_feed_reuses_a_passed_client() -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=b"[]"))

    with httpx.Client() as client:
        assert fetch_feed(FEED_URL, client=client) == b"[]"
        # a caller-owned client must survive the call
        assert not client.is_closed


def test_load_feed_file_reads_bytes() -> None:
    assert load_feed_file(RUN1) == RUN1.read_bytes()


def test_load_feed_file_missing_path_raises_not_found(tmp_path) -> None:
    with pytest.raises(FeedNotFoundError):
        load_feed_file(tmp_path / "nope.json")
