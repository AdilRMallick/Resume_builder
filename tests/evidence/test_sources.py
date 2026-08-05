"""Source loading: markdown directories and GitHub READMEs (respx, never live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from jme.evidence.sources import (
    GITHUB_API,
    SOURCE_MARKDOWN,
    SOURCE_REPO_README,
    fetch_repo_readmes,
    iter_markdown_dir,
    load_sources,
)


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------------------
# markdown directory
# --------------------------------------------------------------------------------------


def test_walks_recursively_and_uses_relative_posix_refs(tmp_path):
    _write(tmp_path, "resume.md", "# Resume\n\nbody\n")
    _write(tmp_path, "projects/redis.md", "# Redis\n\nbody\n")
    _write(tmp_path, "projects/deep/nested.markdown", "# Nested\n\nbody\n")

    records = list(iter_markdown_dir(tmp_path))
    assert [r.source_ref for r in records] == [
        "projects/deep/nested.markdown",
        "projects/redis.md",
        "resume.md",
    ]
    assert {r.source_type for r in records} == {SOURCE_MARKDOWN}


def test_source_ref_is_independent_of_the_absolute_path(tmp_path):
    _write(tmp_path, "a/b.md", "# B\n\nbody\n")
    other = tmp_path / "copy"
    other.mkdir()
    _write(other, "a/b.md", "# B\n\nbody\n")
    assert list(iter_markdown_dir(tmp_path / "a"))[0].source_ref == "b.md"
    assert list(iter_markdown_dir(other))[0].source_ref == "a/b.md"


def test_skips_non_markdown_hidden_and_vendored_files(tmp_path):
    _write(tmp_path, "keep.md", "# Keep\n\nbody\n")
    _write(tmp_path, "notes.txt", "ignored")
    _write(tmp_path, ".hidden.md", "ignored")
    _write(tmp_path, ".git/config.md", "ignored")
    _write(tmp_path, "node_modules/pkg/README.md", "ignored")
    _write(tmp_path, "empty.md", "   \n")

    assert [r.source_ref for r in iter_markdown_dir(tmp_path)] == ["keep.md"]


def test_missing_directory_is_a_warning_not_a_crash(tmp_path):
    assert list(iter_markdown_dir(tmp_path / "nope")) == []


# --------------------------------------------------------------------------------------
# github readmes
# --------------------------------------------------------------------------------------


@respx.mock
def test_fetches_readmes_for_configured_repos():
    route = respx.get(f"{GITHUB_API}/repos/octo/pipeline/readme").mock(
        return_value=httpx.Response(200, text="# Pipeline\n\nA Redis pipeline.\n")
    )
    records = fetch_repo_readmes(["octo/pipeline"])

    assert route.called
    assert route.calls[0].request.headers["accept"] == "application/vnd.github.raw"
    assert "authorization" not in route.calls[0].request.headers
    assert len(records) == 1
    assert records[0].source_type == SOURCE_REPO_README
    assert records[0].source_ref == "octo/pipeline"
    assert "A Redis pipeline." in records[0].text


@respx.mock
def test_token_is_sent_when_configured():
    route = respx.get(f"{GITHUB_API}/repos/octo/private/readme").mock(
        return_value=httpx.Response(200, text="# Private\n\nbody\n")
    )
    fetch_repo_readmes(["octo/private"], token="ghp_secret")
    assert route.calls[0].request.headers["authorization"] == "Bearer ghp_secret"


@respx.mock
def test_a_404_repo_is_skipped_and_the_rest_still_load():
    respx.get(f"{GITHUB_API}/repos/octo/gone/readme").mock(return_value=httpx.Response(404))
    respx.get(f"{GITHUB_API}/repos/octo/here/readme").mock(
        return_value=httpx.Response(200, text="# Here\n\nbody\n")
    )
    records = fetch_repo_readmes(["octo/gone", "octo/here"])
    assert [r.source_ref for r in records] == ["octo/here"]


@respx.mock
def test_rate_limited_repo_is_skipped():
    respx.get(f"{GITHUB_API}/repos/octo/limited/readme").mock(
        return_value=httpx.Response(403, text="rate limit exceeded")
    )
    assert fetch_repo_readmes(["octo/limited"]) == []


@respx.mock
def test_transport_error_is_skipped():
    respx.get(f"{GITHUB_API}/repos/octo/flaky/readme").mock(
        side_effect=httpx.ConnectError("boom")
    )
    assert fetch_repo_readmes(["octo/flaky"]) == []


@respx.mock
def test_empty_readme_is_skipped():
    respx.get(f"{GITHUB_API}/repos/octo/blank/readme").mock(
        return_value=httpx.Response(200, text="   \n")
    )
    assert fetch_repo_readmes(["octo/blank"]) == []


def test_malformed_repo_spec_is_rejected_loudly():
    with pytest.raises(ValueError, match="owner/repo"):
        fetch_repo_readmes(["not-a-repo"])


def test_no_repos_makes_no_requests():
    with respx.mock:
        assert fetch_repo_readmes([]) == []


# --------------------------------------------------------------------------------------
# combined loader
# --------------------------------------------------------------------------------------


@respx.mock
def test_load_sources_combines_both_and_reports_scanned_types(tmp_path):
    _write(tmp_path, "resume.md", "# Resume\n\nbody\n")
    respx.get(f"{GITHUB_API}/repos/octo/pipeline/readme").mock(
        return_value=httpx.Response(200, text="# Pipeline\n\nbody\n")
    )

    records, scanned = load_sources(directory=tmp_path, repos=["octo/pipeline"], token=None)
    assert scanned == {SOURCE_MARKDOWN, SOURCE_REPO_README}
    assert {r.source_type for r in records} == {SOURCE_MARKDOWN, SOURCE_REPO_README}


def test_load_sources_without_repos_does_not_claim_to_have_scanned_them(tmp_path):
    _write(tmp_path, "resume.md", "# Resume\n\nbody\n")
    records, scanned = load_sources(directory=tmp_path, repos=[], token=None)
    assert scanned == {SOURCE_MARKDOWN}
    assert len(records) == 1
