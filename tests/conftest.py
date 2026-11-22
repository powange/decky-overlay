"""Fake `decky` module so main.py runs outside Decky Loader."""
import logging
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "renderer"))

decky = types.ModuleType("decky")
decky.logger = logging.getLogger("decky-test")
decky.DECKY_USER = "nobody-test"
sys.modules["decky"] = decky


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    import main

    for name in ("settings", "logs"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", str(tmp_path / "settings"), raising=False)
    monkeypatch.setattr(decky, "DECKY_PLUGIN_LOG_DIR", str(tmp_path / "logs"), raising=False)
    monkeypatch.setattr(decky, "DECKY_PLUGIN_DIR", ROOT, raising=False)
    sock_dir = tmp_path / "sock"
    monkeypatch.setattr(main, "SOCKET_DIR", str(sock_dir))
    monkeypatch.setattr(main, "SOCKET_PATH", str(sock_dir / "overlay.sock"))
    monkeypatch.setattr(main, "_gamescope_running", lambda: False)  # no renderer unless asked
    return main.Plugin()
