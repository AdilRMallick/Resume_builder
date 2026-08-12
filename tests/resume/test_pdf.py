from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jme.resume import pdf
from jme.resume.tailor import load_profile, tailor_profile


def test_compile_latex_pdf_uses_untrusted_local_tectonic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler = tmp_path / "tectonic.exe"
    compiler.write_bytes(b"test executable")
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        output_dir = Path(command[command.index("--outdir") + 1])
        (output_dir / "resume.pdf").write_bytes(b"%PDF-1.7\ncompiled")
        (output_dir / "resume.log").write_text(
            "Output written on resume.xdv (1 page, 123 bytes).", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pdf.subprocess, "run", fake_run)
    rendered = pdf.compile_latex_pdf(
        r"\documentclass{article}\begin{document}Hi\end{document}",
        configured_path=str(compiler),
    )

    assert rendered.data.startswith(b"%PDF-1.7")
    assert rendered.pages == 1
    assert "--untrusted" in seen["command"]
    assert "--keep-logs" in seen["command"]


def test_compile_latex_pdf_reports_a_missing_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "resolve_tectonic", lambda configured_path=None: None)
    with pytest.raises(pdf.PDFRenderError, match="install-tectonic.ps1"):
        pdf.compile_latex_pdf("valid generated latex")


def test_one_page_compile_prunes_a_copy_not_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = tailor_profile(
        load_profile(),
        job_description="Python FastAPI PostgreSQL Redis Docker REST API AWS " * 8,
    )
    source_education_bullets = len(result["education"][0]["bullets"])
    pages = iter((2, 1))
    monkeypatch.setattr(
        pdf,
        "compile_latex_pdf",
        lambda *args, **kwargs: pdf.CompiledPDF(b"%PDF", next(pages)),
    )

    fitted, compiled, omitted = pdf.compile_one_page_resume(result)

    assert compiled.pages == 1
    assert omitted == 1
    assert fitted["education"][0]["bullets"] == []
    assert len(result["education"][0]["bullets"]) == source_education_bullets
