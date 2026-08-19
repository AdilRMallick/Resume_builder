from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2] / "extension"

AUTOFILL_SCRIPTS = [
    "profile/schema.js",
    "autofill/dom.js",
    "autofill/widgets.js",
    "autofill/fields.js",
    "autofill/runner.js",
    "autofill/content.js",
]


def manifest() -> dict:
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_is_a_side_panel_extension_with_a_profile_editor() -> None:
    data = manifest()
    assert data["manifest_version"] == 3
    assert data["side_panel"]["default_path"] == "sidepanel.html"
    assert data["background"]["service_worker"] == "service-worker.js"
    assert data["options_ui"]["page"] == "profile/editor.html"
    assert set(data["permissions"]) == {
        "activeTab",
        "scripting",
        "sidePanel",
        "storage",
        "tabs",
    }


def test_autofill_is_scoped_to_workday_and_the_local_backend() -> None:
    """The extension may reach exactly two places: Workday, and JME on localhost.

    Any other host in either list would be a way for application data to leave the
    device, which is the one thing this design promises it cannot do.
    """
    data = manifest()
    workday = {
        "https://*.myworkdayjobs.com/*",
        "https://*.myworkdaysite.com/*",
        "https://*.workday.com/*",
    }
    local = {"http://127.0.0.1:8002/*", "http://localhost:8002/*"}
    assert set(data["host_permissions"]) == workday | local

    [content_script] = data["content_scripts"]
    assert set(content_script["matches"]) == workday
    assert content_script["js"] == AUTOFILL_SCRIPTS


def test_extension_assets_are_local_and_present() -> None:
    for page in ("sidepanel.html", "profile/editor.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert not re.findall(r'(?:src|href)=["\']https?://', html), page
    assert '<script src="sidepanel.js"></script>' in (ROOT / "sidepanel.html").read_text(
        encoding="utf-8"
    )
    for name in [
        "sidepanel.js",
        "sidepanel.css",
        "service-worker.js",
        "profile/editor.js",
        "profile/editor.css",
        *AUTOFILL_SCRIPTS,
    ]:
        assert (ROOT / name).is_file(), name


def test_page_capture_is_user_triggered_and_tailoring_stays_local() -> None:
    panel = (ROOT / "sidepanel.js").read_text(encoding="utf-8")
    worker = (ROOT / "service-worker.js").read_text(encoding="utf-8")
    assert 'const API = "http://127.0.0.1:8002"' in panel
    assert "fetch(`${API}/resume/tailor`" in panel
    assert 'sendMessage({ type: "extract-active-job-page" })' in panel
    assert 'message.type !== "extract-active-job-page"' in worker
    assert "chrome.scripting.executeScript" in worker


def test_the_profile_never_leaves_the_device() -> None:
    """No autofill file may contain a network call.

    The engine reads `chrome.storage.local` and writes into the page it is already
    running in. A `fetch` or `XMLHttpRequest` anywhere in this directory would mean
    profile data had somewhere else to go.
    """
    for name in AUTOFILL_SCRIPTS:
        source = (ROOT / name).read_text(encoding="utf-8")
        for forbidden in ("fetch(", "XMLHttpRequest", "navigator.sendBeacon", "WebSocket"):
            assert forbidden not in source, f"{name} must not make network calls ({forbidden})"


def test_the_engine_never_submits_the_application() -> None:
    runner = (ROOT / "autofill" / "runner.js").read_text(encoding="utf-8")
    content = (ROOT / "autofill" / "content.js").read_text(encoding="utf-8")
    assert "requestSubmit" not in runner and "requestSubmit" not in content
    assert ".submit()" not in runner and ".submit()" not in content


def run_node(script: str) -> None:
    """Run one of the JavaScript suites and surface its output on failure.

    `check=False` with an explicit assert rather than `check=True`: a CalledProcessError
    hides the runner's own report, and which assertion failed is the whole message.
    """
    result = subprocess.run(
        ["node", str(Path(__file__).with_name(script))],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_autofill_logic_checks_pass() -> None:
    """The engine's decision logic: normalisation, seeding, dates, option matching."""
    run_node("autofill_checks.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(
    not (Path(__file__).parent / "node_modules" / "jsdom").is_dir(),
    reason="run `npm install` in tests/extension to enable the DOM suite",
)
def test_autofill_dom_checks_pass() -> None:
    """The engine end to end against synthetic Workday forms.

    Opt-in because it needs jsdom, which this repository otherwise has no use for. The
    logic suite above runs everywhere and covers what the engine decides to type; this
    one covers whether it succeeds in typing it.
    """
    run_node("dom_checks.js")
