"""Render tailored data into the canonical Jake Gutierrez LaTeX structure."""

from __future__ import annotations

from typing import Any


def escape_latex(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in value)


def _contact_line(items: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for item in items:
        value = escape_latex(item["value"])
        if item.get("url"):
            rendered.append(
                rf"\href{{{escape_latex(item['url'])}}}{{\underline{{{value}}}}}"
            )
        else:
            rendered.append(value)
    return r" $|$ ".join(rendered)


def _bullet_list(entry: dict[str, Any]) -> str:
    bullets = "\n".join(
        rf"        \resumeItem{{{escape_latex(bullet['text'])}}}"
        for bullet in entry.get("bullets", [])
    )
    if not bullets:
        return ""
    return (
        "      \\resumeItemListStart\n"
        f"{bullets}\n"
        "      \\resumeItemListEnd\n"
    )


def _subheading(entry: dict[str, Any]) -> str:
    return (
        "    \\resumeSubheading\n"
        f"      {{{escape_latex(entry['organization'])}}}"
        f"{{{escape_latex(entry.get('location', ''))}}}\n"
        f"      {{{escape_latex(entry['title'])}}}"
        f"{{{escape_latex(entry['dates'])}}}\n"
        f"{_bullet_list(entry)}"
    )


def _project_heading(entry: dict[str, Any]) -> str:
    name = escape_latex(entry["organization"])
    if entry.get("url"):
        name = rf"\href{{{escape_latex(entry['url'])}}}{{\underline{{{name}}}}}"
    heading = rf"\textbf{{{name}}} $|$ \emph{{{escape_latex(entry['title'])}}}"
    return (
        "      \\resumeProjectHeading\n"
        f"          {{{heading}}}{{{escape_latex(entry['dates'])}}}\n"
        f"{_bullet_list(entry)}"
    )


def _section(name: str, entries: list[dict[str, Any]], *, projects: bool = False) -> str:
    renderer = _project_heading if projects else _subheading
    body = "\n".join(renderer(entry) for entry in entries)
    return (
        f"%-----------{name.upper().replace(' ', '-')}-----------\n"
        f"\\section{{{name}}}\n"
        "  \\resumeSubHeadingListStart\n"
        f"{body}"
        "  \\resumeSubHeadingListEnd\n"
    )


def _skills(data: dict[str, Any]) -> str:
    lines = [
        rf"     \textbf{{{escape_latex(category)}}}{{: {escape_latex(', '.join(values))}}} \\"
        for category, values in data["skills"].items()
    ]
    if data.get("certifications"):
        lines.append(
            rf"     \textbf{{Certifications}}{{: {escape_latex(', '.join(data['certifications']))}}} \\"
        )
    return "\n".join(lines)


def render_jake_latex(data: dict[str, Any]) -> str:
    """Return a complete, one-page-oriented Jake template document.

    Target company/title metadata is deliberately ignored. It is useful to the browser
    workflow, but Jake's header contains identity and contact information only.
    """
    education = _section("Education", data["education"])
    experience = _section("Experience", data["experience"])
    projects = _section("Projects", data["projects"], projects=True)
    leadership = _section("Leadership", data["leadership"])
    skills = _skills(data)
    return rf"""%-------------------------
% Resume in LaTeX
% Template: Jake Gutierrez (MIT License)
% Tailoring policy: verified bullet selection and ordering only
%------------------------
\documentclass[letterpaper,11pt]{{article}}

\usepackage{{latexsym}}
\usepackage[empty]{{fullpage}}
\usepackage{{titlesec}}
\usepackage{{marvosym}}
\usepackage[usenames,dvipsnames]{{color}}
\usepackage{{verbatim}}
\usepackage{{enumitem}}
\usepackage[hidelinks]{{hyperref}}
\usepackage{{fancyhdr}}
\usepackage[english]{{babel}}
\usepackage{{tabularx}}
\input{{glyphtounicode}}

\pagestyle{{fancy}}
\fancyhf{{}}
\fancyfoot{{}}
\renewcommand{{\headrulewidth}}{{0pt}}
\renewcommand{{\footrulewidth}}{{0pt}}

\addtolength{{\oddsidemargin}}{{-0.6in}}
\addtolength{{\evensidemargin}}{{-0.5in}}
\addtolength{{\textwidth}}{{1.19in}}
\addtolength{{\topmargin}}{{-.8in}}
\addtolength{{\textheight}}{{1.5in}}

\urlstyle{{same}}
\raggedbottom
\raggedright
\setlength{{\tabcolsep}}{{0in}}

\titleformat{{\section}}{{
  \vspace{{-6pt}}\scshape\raggedright\large
}}{{}}{{0em}}{{}}[\color{{black}}\titlerule \vspace{{-7pt}}]

\pdfgentounicode=1

\newcommand{{\resumeItem}}[1]{{\item\small{{#1}}\vspace{{-4pt}}}}
\newcommand{{\resumeSubheading}}[4]{{
  \vspace{{-2pt}}\item
    \begin{{tabular*}}{{0.97\textwidth}}[t]{{l@{{\extracolsep{{\fill}}}}r}}
      \textbf{{#1}} & #2 \\
      \textit{{\small#3}} & \textit{{\small #4}} \\
    \end{{tabular*}}\vspace{{-7pt}}
}}
\newcommand{{\resumeProjectHeading}}[2]{{
  \item
    \begin{{tabular*}}{{0.97\textwidth}}{{l@{{\extracolsep{{\fill}}}}r}}
      \small#1 & #2 \\
    \end{{tabular*}}\vspace{{-7pt}}
}}
\renewcommand\labelitemii{{$\vcenter{{\hbox{{\tiny$\bullet$}}}}$}}
\newcommand{{\resumeSubHeadingListStart}}{{\begin{{itemize}}[leftmargin=0.15in, label={{}}]}}
\newcommand{{\resumeSubHeadingListEnd}}{{\end{{itemize}}}}
\newcommand{{\resumeItemListStart}}{{\begin{{itemize}}}}
\newcommand{{\resumeItemListEnd}}{{\end{{itemize}}\vspace{{-5pt}}}}

\begin{{document}}

\begin{{center}}
    \textbf{{\LARGE \scshape {escape_latex(data['name'])}}} \\ \vspace{{2pt}}
    \small {_contact_line(data['contact'])}
\end{{center}}

{education}
{experience}
{projects}
{leadership}
%-----------TECHNICAL-SKILLS-----------
\section{{Technical Skills}}
 \begin{{itemize}}[leftmargin=0.15in, label={{}}]
    \small{{\item{{
{skills}
    }}}}
 \end{{itemize}}

\end{{document}}
"""
