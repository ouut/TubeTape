"""Entry point for ``python -m tubetape`` and the PyInstaller build.

Uses an absolute import so it also works when bundled as a standalone
executable (where the relative import has no parent package).
"""

from __future__ import annotations

import sys

from tubetape.cli import main

if __name__ == "__main__":
    sys.exit(main())
