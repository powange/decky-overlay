import json
import os
import shutil
import subprocess
import sys
import time

import pytest

renderer = pytest.importorskip("renderer")  # needs libX11 + libcairo


def test_format_seconds():
    assert renderer.format_seconds(61.2) == "01:02"
    assert renderer.format_seconds(-83.5) == "-01:23"
    assert renderer.format_seconds(3725) == "1:02:05"


def test_build_zones():
    now = 1000.0
    scene = {"blocks": [
        {"zone": "top-right", "items": [{"label": "x" * 40, "countdown": {"end_at": now + 30}},
                                        {"label": "Hidden", "countdown": {"end_at": now + 3600}, "show_under": 300},
                                        {"label": "P", "countdown": {"paused": True, "remaining": 5}}]},
        {"zone": "nowhere", "items": [{"label": "FPS", "text": "60", "style": "warn"}]}]}
    zones = renderer.build_zones(scene, now)
    assert [r[2] for r in zones["top-right"]] == ["normal", "pause"]
    assert zones["top-right"][0][0] == ["x" * 24, "x" * 16]   # long label wraps
    assert zones["top-center"] == [(["FPS"], ["60"], "warn")]


def test_wrap():
    assert renderer.wrap("Pluie, fin dans 12 min sur Shark Bait Cove", 16, 3) == [
        "Pluie, fin dans", "12 min sur Shark", "Bait Cove"]
    assert renderer.wrap("The Damned Sea Dog Tavern", 24, 2) == ["The Damned Sea Dog", "Tavern"]
    lines = renderer.wrap("un deux trois quatre cinq six", 9, 2)
    assert len(lines) == 2 and lines[-1].endswith("…") and all(len(l) <= 9 for l in lines)
    assert renderer.wrap("", 16, 3) == [""]


def test_countdown_style():
    now = 1000.0
    scene = {"blocks": [{"items": [
        {"label": "Pluie", "countdown": {"end_at": now + 60}, "style": "alert"},
        {"label": "Fini", "countdown": {"end_at": now - 5}, "style": "muted"}]}]}
    rows = renderer.build_zones(scene, now)["top-center"]
    assert [r[2] for r in rows] == ["alert", "over"]


def test_sleeps_until_next_change():
    now = 1000.0
    run = {"blocks": [{"items": [{"countdown": {"end_at": now + 10.3}}]}]}
    assert 0.3 <= renderer.seconds_until_change(run, now) < 0.32
    static = {"blocks": [{"items": [{"label": "FPS", "text": "60"}]}]}
    assert renderer.seconds_until_change(static, now) is None


def test_menu_window_keeps_selection_visible():
    assert renderer.menu_window(5, 4, 9) == (0, 5)
    assert renderer.menu_window(20, 0, 9) == (0, 9)
    assert renderer.menu_window(20, 10, 9) == (6, 15)
    assert renderer.menu_window(20, 19, 9) == (11, 20)


@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb not installed")
def test_renders_on_a_virtual_display():
    xvfb = subprocess.Popen(["Xvfb", ":92", "-screen", "0", "1280x800x24"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        proc = subprocess.Popen([sys.executable, renderer.__file__], stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=dict(os.environ, DISPLAY=":92"))
        scene = {"blocks": [{"zone": "top-left", "items": [{"label": "T", "text": "1"}]}], "settings": {},
                 "menu": {"title": "Choose", "items": [f"Macro {i}" for i in range(15)], "selected": 7,
                          "hint": "A: play"}}
        proc.stdin.write((json.dumps(scene) + "\n").encode())
        proc.stdin.flush()
        time.sleep(1.5)
        assert proc.poll() is None, proc.stderr.read().decode()
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
    finally:
        xvfb.kill()
