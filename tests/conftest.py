"""Test bootstrap: add the bundled `agora` submodule to sys.path so tests
can import ``shared.models`` etc. (cms_client runtime deps live there).
"""

from __future__ import annotations

import sys
from pathlib import Path

_AGORA_ROOT = Path(__file__).resolve().parent.parent / "agora"
if _AGORA_ROOT.is_dir():
    p = str(_AGORA_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)
