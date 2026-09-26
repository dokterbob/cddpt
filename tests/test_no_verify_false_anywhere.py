"""AST-based guard: cddpt must never contain a code path that disables TLS
certificate verification, anywhere under ``src/``.

This exists because the very problem cddpt was written to replace is a
community plugin that unconditionally disabled TLS verification (see
docs/PLAN.md, "Key findings"). ``cddpt.http`` is the single place TLS trust
is decided (the OS-native trust store via ``truststore``, or an explicit
``ca_bundle``) -- nothing else in the codebase should ever come close to
weakening it.

Detects, anywhere under ``src/``:

- ``verify=False`` as a call keyword argument (e.g. a ``requests.get(...,
  verify=False)`` call)
- ``check_hostname=False`` as a call keyword argument
- ``<expr>.verify = False`` / ``<expr>.check_hostname = False`` attribute
  assignment
- a reference to ``ssl.CERT_NONE`` (or a bare imported ``CERT_NONE``)
- a reference to ``ssl._create_unverified_context``
- a reference to ``urllib3.disable_warnings``
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

#: Bare names / attribute names that are inherently a TLS-verification
#: downgrade wherever they appear -- as a call, or as a plain reference.
_DANGEROUS_ATTR_OR_NAME = frozenset({"CERT_NONE", "_create_unverified_context", "disable_warnings"})
#: Keyword arguments that are a downgrade specifically when set to ``False``.
_DANGEROUS_FALSE_KEYWORDS = frozenset({"verify", "check_hostname"})

#: Fallback used only when a file cannot be parsed as Python (e.g. a syntax
#: error, or a non-UTF-8 encoding) -- so a broken file is never silently
#: skipped instead of flagged.
_TEXT_FALLBACK_PATTERNS = [
    re.compile(r"verify\s*=\s*False"),
    re.compile(r"check_hostname\s*=\s*False"),
    re.compile(r"\bCERT_NONE\b"),
    re.compile(r"_create_unverified_context"),
    re.compile(r"disable_warnings"),
]


def _is_false_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


class _TlsDowngradeVisitor(ast.NodeVisitor):
    """Walks a module's AST collecting TLS-downgrade red flags."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg in _DANGEROUS_FALSE_KEYWORDS and _is_false_constant(kw.value):
                self.violations.append(f"line {node.lineno}: {kw.arg}=False keyword argument")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_false_constant(node.value):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in _DANGEROUS_FALSE_KEYWORDS:
                    self.violations.append(f"line {node.lineno}: .{target.attr} = False assignment")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _DANGEROUS_ATTR_OR_NAME:
            self.violations.append(f"line {node.lineno}: reference to '{node.attr}'")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _DANGEROUS_ATTR_OR_NAME:
            self.violations.append(f"line {node.lineno}: reference to '{node.id}'")
        self.generic_visit(node)


def find_violations(source: str) -> list[str]:
    """Return human-readable TLS-downgrade violations found in ``source``."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [
            f"text fallback match: {pattern.pattern!r}"
            for pattern in _TEXT_FALLBACK_PATTERNS
            if pattern.search(source)
        ]

    visitor = _TlsDowngradeVisitor()
    visitor.visit(tree)
    return visitor.violations


def _iter_source_files() -> list[Path]:
    assert SRC_ROOT.is_dir(), f"expected a src/ directory at {SRC_ROOT}"
    return sorted(SRC_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "path",
    _iter_source_files(),
    ids=lambda p: str(p.relative_to(SRC_ROOT)),
)
def test_no_tls_downgrade_in_source_file(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    violations = find_violations(source)
    assert not violations, f"{path}: TLS-downgrade pattern(s) found: {violations}"


@pytest.mark.parametrize(
    "snippet",
    [
        "requests.get(url, verify=False)\n",
        "session.verify = False\n",
        "import ssl\nctx.check_hostname = False\n",
        "requests.post(url, check_hostname=False)\n",
        "import ssl\nssl.CERT_NONE\n",
        "from ssl import CERT_NONE\nx = CERT_NONE\n",
        "import ssl\nssl._create_unverified_context()\n",
        "from ssl import _create_unverified_context\n_create_unverified_context()\n",
        "import urllib3\nurllib3.disable_warnings()\n",
        "from urllib3 import disable_warnings\ndisable_warnings()\n",
    ],
)
def test_detector_catches_each_pattern(snippet: str) -> None:
    assert find_violations(snippet), f"detector failed to flag: {snippet!r}"


def test_detector_allows_clean_source() -> None:
    clean = "import requests\nrequests.get('https://example.com', verify=True)\n"
    assert find_violations(clean) == []


def test_detector_falls_back_to_text_on_syntax_error() -> None:
    broken = "def f(:\n    verify=False\n"
    assert find_violations(broken)
