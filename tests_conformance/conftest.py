"""Conformance-suite conftest — REAL molecule_runtime + molecule_plugin.

This test tree is DELIBERATELY separate from ``tests/``. The ``tests/conftest.py``
there installs STUB ``molecule_runtime`` / ``a2a`` modules into ``sys.modules`` so
the unit suite can import ``adapter`` without the heavy runtime installed. The
ADR-004 conformance suite needs the OPPOSITE: it drives the adapter through the
REAL ``molecule_runtime.adapter_base.BaseAdapter`` + the real boot-safe MCP probe,
and inherits ``molecule_plugin.adapter_conformance.AdapterConformance`` from the
SDK. A stub ``molecule_runtime`` would make ``pytest.importorskip("molecule_runtime")``
resolve to the stub and the socket round-trip would test nothing.

So this directory has its own conftest (which pytest applies per-directory) that
adds the template root to ``sys.path`` (for ``from adapter import Adapter``) and
does NO stubbing — the real packages are expected on ``PYTHONPATH`` (the SDK's own
CI and each template's unit job both install ``molecule-ai-workspace-runtime`` +
``molecule-ai-sdk``; see adapter-socket.contract.md §8).
"""

import os
import sys

# Template root (parent of this tests_conformance/ dir) — so `from adapter
# import Adapter` resolves the real adapter.py.
_TEMPLATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TEMPLATE_ROOT not in sys.path:
    sys.path.insert(0, _TEMPLATE_ROOT)
