"""Compile generated Jake-template LaTeX into a local PDF with Tectonic."""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jme.resume.latex import render_jake_latex


class PDFRenderError(RuntimeError):
    """Raised when the local LaTeX engine cannot produce a PDF."""


@dataclass(frozen=True)
class CompiledPDF:
    data: bytes
    pages: int


def resolve_tectonic(configured_path: str | None = None) -> Path | None:
    """Find an explicitly configured, project-local, or PATH Tectonic binary."""
    candidates: list[Path] = []
    if configured_path:
        candidates.append(Path(configured_path).expanduser())
    executable = "tectonic.exe" if sys.platform == "win32" else "tectonic"
    candidates.append(Path(__file__).resolve().parents[2] / ".tools" / "tectonic" / executable)
    if discovered := shutil.which("tectonic"):
        candidates.append(Path(discovered))
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    return None


def compile_latex_pdf(
    latex: str,
    *,
    configured_path: str | None = None,
    timeout_sec: int = 120,
) -> CompiledPDF:
    """Compile server-generated LaTeX without accepting arbitrary browser input."""
    compiler = resolve_tectonic(configured_path)
    if compiler is None:
        raise PDFRenderError(
            "Tectonic is not installed; run scripts/install-tectonic.ps1 and restart JME"
        )

    with tempfile.TemporaryDirectory(prefix="jme-resume-") as temp_name:
        workdir = Path(temp_name)
        source = workdir / "resume.tex"
        output = workdir / "resume.pdf"
        source.write_text(latex, encoding="utf-8")
        command = [
            str(compiler),
            "--untrusted",
            "--keep-logs",
            "--outdir",
            str(workdir),
            str(source),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_sec,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise PDFRenderError(f"LaTeX compilation exceeded {timeout_sec} seconds") from exc
        except OSError as exc:
            raise PDFRenderError(f"Tectonic could not start: {exc}") from exc

        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout or "unknown Tectonic error").strip()
            detail = " ".join(detail.split())[-800:]
            raise PDFRenderError(f"LaTeX compilation failed: {detail}")
        log_path = workdir / "resume.log"
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        matches = re.findall(r"Output written on .*?\((\d+) pages?[,)]", log)
        if not matches:
            raise PDFRenderError("Tectonic produced a PDF but its page count could not be verified")
        pages = int(matches[-1])
        return CompiledPDF(data=output.read_bytes(), pages=pages)


def _prune_lowest_priority_content(result: dict[str, Any]) -> int:
    education = result.get("education", [])
    for entry in reversed(education):
        if entry.get("bullets"):
            entry["bullets"].pop()
            return 1

    projects = result.get("projects", [])
    if len(projects) > 2:
        removed = len(projects[-1].get("bullets", []))
        projects.pop()
        return max(removed, 1)

    for entry in reversed(result.get("experience", [])):
        if len(entry.get("bullets", [])) > 2:
            entry["bullets"].pop()
            return 1

    for entry in reversed(projects):
        if len(entry.get("bullets", [])) > 1:
            entry["bullets"].pop()
            return 1

    for entry in reversed(result.get("leadership", [])):
        if len(entry.get("bullets", [])) > 1:
            entry["bullets"].pop()
            return 1

    for entry in reversed(result.get("experience", [])):
        if len(entry.get("bullets", [])) > 1:
            entry["bullets"].pop()
            return 1

    if len(projects) > 1:
        removed = len(projects[-1].get("bullets", []))
        projects.pop()
        return max(removed, 1)
    return 0


def compile_one_page_resume(
    result: dict[str, Any],
    *,
    configured_path: str | None = None,
    timeout_sec: int = 120,
) -> tuple[dict[str, Any], CompiledPDF, int]:
    """Compile a copy of a tailored result, pruning low-priority content to one page."""
    fitted = copy.deepcopy(result)
    omitted = 0
    for _attempt in range(16):
        fitted["latex"] = render_jake_latex(fitted)
        compiled = compile_latex_pdf(
            fitted["latex"],
            configured_path=configured_path,
            timeout_sec=timeout_sec,
        )
        if compiled.pages == 1:
            return fitted, compiled, omitted
        removed = _prune_lowest_priority_content(fitted)
        if removed == 0:
            break
        omitted += removed
    raise PDFRenderError("Jake-template resume could not be reduced to one page")
