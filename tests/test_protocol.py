import asyncio
import json
import os
import time

import main


async def _connect():
    r, w = await asyncio.open_unix_connection(main.SOCKET_PATH)
    return r, w


async def _send(w, msg):
    w.write((json.dumps(msg) + "\n").encode())
    await w.drain()


async def _recv(r):
    return json.loads(await asyncio.wait_for(r.readline(), 2))


def test_hello_set_clear_and_disconnect(plugin):
    async def scenario():
        await plugin._start_server()
        assert oct(os.stat(main.SOCKET_DIR).st_mode & 0o777) == "0o700"
        r, w = await _connect()
        await _send(w, {"op": "hello", "client": "decky-timer", "name": "Decky Timer", "protocol": 1})
        assert (await _recv(r))["op"] == "welcome"
        await _send(w, {"op": "set", "id": "timers", "zone": "top-right",
                        "items": [{"label": "Pâtes", "countdown": {"end_at": time.time() + 60}}]})
        await asyncio.sleep(0.1)
        scene = plugin._scene()
        assert scene["blocks"][0]["zone"] == "top-right" and scene["blocks"][0]["items"][0]["label"] == "Pâtes"
        assert plugin._state()["clients"][0]["name"] == "Decky Timer"
        await _send(w, {"op": "clear", "id": "timers"})
        await asyncio.sleep(0.1)
        assert plugin._scene()["blocks"] == []
        await _send(w, {"op": "set", "id": "timers", "items": [{"label": "X", "text": "1"}]})
        await asyncio.sleep(0.1)
        w.close()
        await asyncio.sleep(0.2)
        assert plugin._scene()["blocks"] == [] and plugin.clients == []  # gone with its connection
        await plugin._stop_server()
    asyncio.run(scenario())


def test_rejects_bad_messages(plugin):
    async def scenario():
        await plugin._start_server()
        r, w = await _connect()
        await _send(w, {"op": "set", "id": "x", "items": []})
        assert "hello" in (await _recv(r))["error"]
        await _send(w, {"op": "hello", "client": "Bad Id!"})
        assert "client id" in (await _recv(r))["error"]
        await _send(w, {"op": "hello", "client": "ok"})
        await _recv(r)
        await _send(w, {"op": "set", "id": "../etc", "items": []})
        assert "block id" in (await _recv(r))["error"]
        w.write(b"not json\n")
        await w.drain()
        assert "JSON" in (await _recv(r))["error"]
        for i in range(main.MAX_BLOCKS_PER_CLIENT):
            await _send(w, {"op": "set", "id": f"b{i}", "items": [{"label": "x", "text": "1"}]})
        await _send(w, {"op": "set", "id": "one-too-many", "items": []})
        assert "too many" in (await _recv(r))["error"]
        w.close()
        await plugin._stop_server()
    asyncio.run(scenario())


def test_items_are_sanitised():
    assert main._valid_item({"label": "a" * 100, "countdown": {"end_at": True}}) is None
    item = main._valid_item({"label": "x" * 100, "text": "y" * 100, "style": "evil"})
    assert len(item["label"]) == 48 and len(item["text"]) == 32 and "style" not in item
    cd = main._valid_item({"label": "t", "countdown": {"paused": True, "remaining": 5}, "show_under": 300})
    assert cd == {"label": "t", "countdown": {"paused": True, "remaining": 5.0}, "show_under": 300.0}


def test_hidden_clients_and_order(plugin):
    a, b = main.Client(None), main.Client(None)
    a.id, b.id = "a", "b"
    a.blocks = {"x": {"zone": "top-left", "order": 5, "items": [{"label": "A", "text": "1"}]}}
    b.blocks = {"y": {"zone": "top-left", "order": 1, "items": [{"label": "B", "text": "2"}]}}
    plugin.clients = [a, b]
    assert [blk["items"][0]["label"] for blk in plugin._scene()["blocks"]] == ["B", "A"]
    plugin.settings["hidden_clients"] = ["b"]
    assert [blk["items"][0]["label"] for blk in plugin._scene()["blocks"]] == ["A"]


def test_settings_whitelist():
    assert main._valid_settings({"opacity": 0.4, "size": "huge", "display": ":0;x", "evil": 1,
                                 "hidden_clients": ["ok", "Bad!"]}) == {"opacity": 0.4, "hidden_clients": ["ok"]}


def test_socket_dir_owned_by_someone_else_is_refused(plugin, monkeypatch):
    os.mkdir(main.SOCKET_DIR)
    monkeypatch.setattr(os, "geteuid", lambda: 12345)
    asyncio.run(plugin._start_server())
    assert "n'appartient pas" in plugin.socket_error


def test_no_renderer_outside_game_mode(plugin):
    c = main.Client(None)
    c.id = "a"
    c.blocks = {"x": {"zone": "top-left", "order": 0, "items": [{"label": "A", "text": "1"}]}}
    plugin.clients = [c]
    plugin.game_mode_checked = 0
    assert plugin._renderer_wanted() is False
