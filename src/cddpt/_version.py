"""Single source of truth for the package version.

Kept as its own tiny module (rather than living in ``__init__.py``) so that
``settings.py`` can import it without creating a circular import with the
package's ``__init__.py`` (which itself imports from ``settings.py``).

Keep this in sync with the ``version`` field in ``pyproject.toml``.
"""

from __future__ import annotations

__version__ = "0.0.1"
