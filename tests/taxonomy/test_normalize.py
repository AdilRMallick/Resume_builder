"""Unit tests for text normalization. No database."""

from __future__ import annotations

import pytest

from jme.taxonomy.normalize import normalize, tokenize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # basic folding
        ("Python", "python"),
        ("  PYTHON  ", "python"),
        ("Python\t\nDeveloper", "python developer"),
        ("Machine   Learning", "machine learning"),
        ("", ""),
        ("   ", ""),
        (None, ""),
        # meaningful characters survive
        ("C++", "c++"),
        ("C#", "c#"),
        (".NET", ".net"),
        (".NET Core", ".net core"),
        ("ASP.NET", "asp.net"),
        ("Node.js", "node.js"),
        ("Next.js", "next.js"),
        ("Vue.js", "vue.js"),
        ("F#", "f#"),
        ("C++17", "c++17"),
        ("3+ years", "3+ years"),
        # punctuation that must not survive
        ("Go, Rust, and C++", "go rust and c++"),
        ("Python (3.11)", "python 3.11"),
        ("Kubernetes / Docker", "kubernetes docker"),
        ("front-end", "front end"),
        ("micro-services", "micro services"),
        ("Objective-C", "objective c"),
        ("scikit-learn", "scikit learn"),
        ("CI/CD", "ci cd"),
        ("A/B testing", "a b testing"),
        ("[Required] Java", "required java"),
        ("SQL;", "sql"),
        ("*Go*", "go"),
        # trailing periods are not part of the token
        ("We use Go.", "we use go"),
        ("Experience with Node.js.", "experience with node.js"),
        ("Java. Python. Go.", "java python go"),
        # ampersand becomes a word
        ("Data Structures & Algorithms", "data structures and algorithms"),
        ("R&D", "r and d"),
        ("Authentication & Authorization", "authentication and authorization"),
        # apostrophes disappear rather than split
        ("Bachelor's degree", "bachelors degree"),
        ("Bachelor’s degree", "bachelors degree"),
        ("developer's toolkit", "developers toolkit"),
        # unicode
        ("Go – Golang", "go golang"),
        ("Python Developer", "python developer"),
        ("“React”", "react"),
        ("Ruby‐on‐Rails", "ruby on rails"),
        # case and digits
        ("HTML5", "html5"),
        ("5G", "5g"),
        ("K8s", "k8s"),
        ("gRPC", "grpc"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["C++", "Node.js", "Data Structures & Algorithms", "Bachelor's degree", "Go, Rust", ".NET"],
)
def test_normalize_is_idempotent(raw):
    once = normalize(raw)
    assert normalize(once) == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Django", ("django",)),
        ("MongoDB", ("mongodb",)),
        ("ongoing", ("ongoing",)),
        ("Go-to-market", ("go", "to", "market")),
        ("C/C++", ("c", "c++")),
        ("experience with Go and Kubernetes", ("experience", "with", "go", "and", "kubernetes")),
        ("", ()),
        ("   ", ()),
    ],
)
def test_tokenize(raw, expected):
    assert tokenize(raw) == expected


@pytest.mark.parametrize("word", ["django", "mongodb", "ongoing", "gopher", "lego", "cargo"])
def test_go_is_not_a_token_inside_other_words(word):
    """The word-boundary guarantee, stated as a property of tokenization."""
    assert "go" not in tokenize(word)


@pytest.mark.parametrize("text", ["c++", "c#", ".net", "csharp"])
def test_c_is_not_a_token_inside_language_names(text):
    assert "c" not in tokenize(text)
