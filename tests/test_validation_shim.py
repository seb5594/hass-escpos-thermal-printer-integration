"""The validation shim picks probatio on current Home Assistant and voluptuous on older ones."""

import importlib
import sys

import pytest

from custom_components.escpos_printer import _validation


def test_uses_probatio_when_available():
    import probatio

    assert _validation.vol is probatio


def test_falls_back_to_voluptuous_without_probatio(monkeypatch):
    voluptuous = pytest.importorskip("voluptuous")
    monkeypatch.setitem(sys.modules, "probatio", None)  # makes `import probatio` fail
    try:
        reloaded = importlib.reload(_validation)
        assert reloaded.vol is voluptuous
    finally:
        monkeypatch.undo()
        importlib.reload(_validation)
