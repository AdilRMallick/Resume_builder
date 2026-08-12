from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2] / "extension"


def test_manifest_is_a_minimal_side_panel_extension() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["side_panel"]["default_path"] == "sidepanel.html"
    assert manifest["background"]["service_worker"] == "service-worker.js"
    assert set(manifest["permissions"]) == {"activeTab", "scripting", "sidePanel"}
    assert manifest["host_permissions"] == [
        "http://127.0.0.1:8002/*",
        "http://localhost:8002/*",
    ]


def test_extension_assets_are_local_and_present() -> None:
    html = (ROOT / "sidepanel.html").read_text(encoding="utf-8")
    assert not re.findall(r'(?:src|href)=["\']https?://', html)
    assert '<script src="sidepanel.js"></script>' in html
    assert '<link rel="stylesheet" href="sidepanel.css">' in html
    for name in ("sidepanel.js", "sidepanel.css", "service-worker.js"):
        assert (ROOT / name).is_file()


def test_page_capture_is_user_triggered_and_tailoring_stays_local() -> None:
    panel = (ROOT / "sidepanel.js").read_text(encoding="utf-8")
    worker = (ROOT / "service-worker.js").read_text(encoding="utf-8")
    assert 'const API = "http://127.0.0.1:8002"' in panel
    assert 'fetch(`${API}/resume/tailor`' in panel
    assert 'sendMessage({ type: "extract-active-job-page" })' in panel
    assert 'message.type !== "extract-active-job-page"' in worker
    assert "chrome.scripting.executeScript" in worker


def test_resume_preview_never_renders_a_target_job_banner() -> None:
    panel = (ROOT / "sidepanel.js").read_text(encoding="utf-8")
    html = (ROOT / "sidepanel.html").read_text(encoding="utf-8")
    assert "Tailored for:" not in panel
    assert 'class="target"' not in panel
    assert "Selected Projects" not in panel
    assert "Download .tex" in html
    assert "Download exact PDF" in html
    assert 'render_pdf: true' in panel
    assert "data.pdf_base64" in panel
    assert "currentTailoredResume.latex" in panel


def test_ai_provider_selection_keeps_secrets_in_the_local_backend() -> None:
    panel = (ROOT / "sidepanel.js").read_text(encoding="utf-8")
    html = (ROOT / "sidepanel.html").read_text(encoding="utf-8")
    manifest = (ROOT / "manifest.json").read_text(encoding="utf-8")
    assert 'fetch(`${API}/resume/providers`)' in panel
    assert 'fetch(`${API}/health`)' not in panel
    assert 'window.addEventListener("focus", checkServer)' in panel
    assert 'customization_mode: byId("customization-mode").value' in panel
    assert "authorization" not in panel.lower()
    assert "x-api-key" not in panel.lower()
    assert "sk-" not in panel
    assert 'value="verified"' in html
    assert 'value="openai"' in html
    assert 'value="anthropic"' in html
    assert 'value="gemini"' in html
    assert 'value="kimi"' in html
    assert '"version": "0.2.0"' in manifest
