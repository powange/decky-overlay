"""Decky Overlay - one in-game overlay shared by several Decky plugins.

gamescope only shows ONE external overlay window at a time. This plugin owns
it: client plugins (Decky Timer, ...) connect to a local Unix socket and send
the blocks they want displayed; a single renderer process draws them all.

Protocol (version 1): newline-delimited JSON over /tmp/decky-overlay/overlay.sock
  -> {"op": "hello", "client": "decky-timer", "name": "Decky Timer", "protocol": 1}
  -> {"op": "set", "id": "timers", "zone": "top-right", "order": 0, "items": [ITEM...]}
  -> {"op": "menu", "id": "picker", "title": "...", "items": ["A", "B"], "selected": 0,
      "hint": "..."}                                      (interactive list, centred)
  -> {"op": "clear", "id": "timers"}
  <- {"op": "welcome", "protocol": 1, "zones": [...], "features": ["menu"]}   (after hello)
  <- {"op": "error", "error": "..."}                     (invalid message)
A client's blocks disappear when its connection closes, so a crashed or
uninstalled plugin never leaves stale cards on screen. ITEM is described in
renderer/renderer.py.
A menu is drawn in the middle of the screen even when the overlay is disabled
or the client hidden in the settings: the client opened it on a user action
and drives it (input stays with the client; Decky Overlay only draws it).
Only the most recently updated menu is shown.
"""
import asyncio
import glob
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import time

import decky

PROTOCOL = 1
SOCKET_DIR = "/tmp/decky-overlay"
SOCKET_PATH = os.path.join(SOCKET_DIR, "overlay.sock")
ZONES = ("top-left", "top-center", "top-right", "bottom-left", "bottom-right")

MAX_CLIENTS = 16
MAX_BLOCKS_PER_CLIENT = 8
MAX_ITEMS_PER_BLOCK = 12
MAX_MENU_ITEMS = 64
FEATURES = ["menu"]
MAX_LINE = 64 * 1024          # bytes per message
LOG_MAX_BYTES = 256 * 1024
RENDERER_MAX_FAILURES = 3
RENDERER_HEALTHY_AFTER = 60
PREVIEW_SECONDS = 10

DEFAULT_SETTINGS = {
    "enabled": True,
    "size": "medium",          # small | medium | large | xlarge
    "opacity": 0.75,
    "display": ":0",           # gamescope's Xwayland used by Steam
    "hidden_clients": [],      # client ids whose blocks are not drawn
}

# Environment variables of Decky's bundled (PyInstaller) python that would
# break the system python3 used for the renderer.
_STRIP_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH")


def _now():
    return time.time()


# ---- Validation ------------------------------------------------------------ #

def _valid_settings(raw):
    out = {}
    if not isinstance(raw, dict):
        return out
    checks = {
        "enabled": lambda v: v if isinstance(v, bool) else None,
        "size": lambda v: v if v in ("small", "medium", "large", "xlarge") else None,
        "opacity": lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
        and 0 <= v <= 1 else None,
        "display": lambda v: v if isinstance(v, str) and re.fullmatch(r":\d{1,2}", v) else None,
        "hidden_clients": lambda v: [c for c in v if isinstance(c, str) and _ID.fullmatch(c)][:64]
        if isinstance(v, list) else None,
    }
    for key, value in raw.items():
        check = checks.get(key)
        if check is None:
            continue
        cleaned = check(value)
        if cleaned is None:
            decky.logger.warning(f"Ignoring setting {key}={value!r}")
        else:
            out[key] = cleaned
    return out


_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")


def _text(v, limit):
    return " ".join(str(v).split())[:limit] if v is not None else ""


def _valid_item(item):
    """Cleans one card; returns None if unusable."""
    if not isinstance(item, dict):
        return None
    out = {"label": _text(item.get("label"), 48)}
    cd = item.get("countdown")
    if isinstance(cd, dict):
        if cd.get("paused"):
            rem = cd.get("remaining")
            if not isinstance(rem, (int, float)) or isinstance(rem, bool):
                return None
            out["countdown"] = {"paused": True, "remaining": float(rem)}
        else:
            end_at = cd.get("end_at")
            if not isinstance(end_at, (int, float)) or isinstance(end_at, bool):
                return None
            out["countdown"] = {"end_at": float(end_at)}
        su = item.get("show_under")
        if isinstance(su, (int, float)) and not isinstance(su, bool) and su > 0:
            out["show_under"] = float(su)
    else:
        out["text"] = _text(item.get("text"), 32)
        if item.get("style") in ("normal", "warn", "alert", "muted"):
            out["style"] = item["style"]
    return out


def _valid_menu(msg):
    """Cleans a menu message; returns (menu, error)."""
    items = msg.get("items")
    if not isinstance(items, list) or not items:
        return None, "items must be a non-empty list"
    labels = []
    for it in items[:MAX_MENU_ITEMS]:
        if isinstance(it, dict):
            it = it.get("label")
        if isinstance(it, (str, int, float)) and not isinstance(it, bool):
            labels.append(_text(it, 48) or "?")
    if not labels:
        return None, "no usable menu item"
    sel = msg.get("selected", 0)
    if not isinstance(sel, int) or isinstance(sel, bool):
        sel = 0
    return {"kind": "menu", "title": _text(msg.get("title"), 48), "hint": _text(msg.get("hint"), 80),
            "items": labels, "selected": max(0, min(sel, len(labels) - 1)), "updated": _now()}, None


# ---- Game Mode detection ------------------------------------------------------ #

def _gamescope_running():
    """True when the Game Mode session (gamescope) is up: no overlay on the desktop."""
    for comm in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm) as f:
                if f.read().startswith("gamescope"):
                    return True
        except OSError:
            continue
    return False


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.id = None
        self.name = ""
        self.blocks = {}       # block id -> {"zone", "order", "items"}
        self.connected_at = _now()


class Plugin:
    def __init__(self):
        self.state_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        self.renderer_script = os.path.join(decky.DECKY_PLUGIN_DIR, "renderer", "renderer.py")
        self.renderer_log = os.path.join(decky.DECKY_PLUGIN_LOG_DIR, "renderer.log")
        self.settings = dict(DEFAULT_SETTINGS)
        self.clients = []
        self.server = None
        self.socket_error = ""
        self.proc = None
        self.proc_started = 0.0
        self.dying = []
        self.failures = 0
        self.renderer_error = ""
        self.preview_until = 0.0
        self.game_mode = True
        self.game_mode_checked = 0.0
        self.watch_task = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def _main(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        os.makedirs(decky.DECKY_PLUGIN_LOG_DIR, exist_ok=True)
        self._load()
        await self._start_server()
        self.watch_task = asyncio.get_event_loop().create_task(self._watch())
        decky.logger.info("Decky Overlay loaded")

    async def _unload(self):
        if self.watch_task:
            self.watch_task.cancel()
        await self._stop_server()
        self._stop_renderer()
        self._reap(force=True)
        decky.logger.info("Decky Overlay unloaded")

    async def _uninstall(self):
        await self._unload()

    def _load(self):
        try:
            with open(self.state_path) as f:
                self.settings = {**DEFAULT_SETTINGS, **_valid_settings(json.load(f))}
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.error(f"Could not read settings: {e}")

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ #
    # Socket server
    # ------------------------------------------------------------------ #

    def _prepare_socket_dir(self):
        """Private directory for the socket; refuses one owned by someone else."""
        try:
            os.mkdir(SOCKET_DIR, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(SOCKET_DIR)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise RuntimeError(f"{SOCKET_DIR} n'appartient pas à ce plugin")
        os.chmod(SOCKET_DIR, 0o700)
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)  # left over by a previous run

    async def _start_server(self):
        try:
            self._prepare_socket_dir()
            self.server = await asyncio.start_unix_server(self._handle_client, path=SOCKET_PATH,
                                                          limit=MAX_LINE)
            os.chmod(SOCKET_PATH, 0o600)
            self.socket_error = ""
        except Exception as e:
            self.socket_error = f"Socket indisponible : {e}"
            decky.logger.error(self.socket_error)

    async def _stop_server(self):
        if self.server:
            self.server.close()
            for c in list(self.clients):
                c.writer.close()
            self.clients = []
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass

    async def _send(self, client, msg):
        try:
            client.writer.write((json.dumps(msg) + "\n").encode())
            await client.writer.drain()
        except Exception:
            pass

    async def _handle_client(self, reader, writer):
        if len(self.clients) >= MAX_CLIENTS:
            writer.close()
            return
        client = Client(writer)
        self.clients.append(client)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await self._send(client, {"op": "error", "error": "message too long"})
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    await self._send(client, {"op": "error", "error": "invalid JSON"})
                    continue
                error = self._apply(client, msg)
                if error:
                    await self._send(client, {"op": "error", "error": error})
                elif msg.get("op") == "hello":
                    await self._send(client, {"op": "welcome", "protocol": PROTOCOL, "zones": list(ZONES),
                                              "features": FEATURES})
        except (ConnectionError, OSError):
            pass
        finally:
            if client in self.clients:
                self.clients.remove(client)
            writer.close()
            self._sync_renderer()

    def _apply(self, client, msg):
        """Applies one message; returns an error string or None."""
        if not isinstance(msg, dict):
            return "message must be an object"
        op = msg.get("op")
        if op == "hello":
            cid = msg.get("client")
            if not isinstance(cid, str) or not _ID.fullmatch(cid):
                return "invalid client id"
            client.id = cid
            client.name = _text(msg.get("name") or cid, 48)
            return None
        if client.id is None:
            return "send hello first"
        bid = msg.get("id")
        if not isinstance(bid, str) or not _ID.fullmatch(bid):
            return "invalid block id"
        if op == "clear":
            client.blocks.pop(bid, None)
        elif op == "menu":
            if bid not in client.blocks and len(client.blocks) >= MAX_BLOCKS_PER_CLIENT:
                return "too many blocks"
            menu, error = _valid_menu(msg)
            if error:
                return error
            client.blocks[bid] = menu
        elif op == "set":
            if bid not in client.blocks and len(client.blocks) >= MAX_BLOCKS_PER_CLIENT:
                return "too many blocks"
            items = msg.get("items")
            if not isinstance(items, list):
                return "items must be a list"
            cleaned = [i for i in (_valid_item(x) for x in items[:MAX_ITEMS_PER_BLOCK]) if i]
            order = msg.get("order", 0)
            client.blocks[bid] = {
                "zone": msg.get("zone") if msg.get("zone") in ZONES else "top-center",
                "order": order if isinstance(order, int) and not isinstance(order, bool) else 0,
                "items": cleaned,
            }
        else:
            return f"unknown op {op!r}"
        self._sync_renderer()
        return None

    # ------------------------------------------------------------------ #
    # Scene and renderer process
    # ------------------------------------------------------------------ #

    def _scene(self):
        hidden = set(self.settings.get("hidden_clients", []))
        blocks = []
        menu = None
        for c in self.clients:
            if c.id is None:
                continue
            for b in c.blocks.values():
                if b.get("kind") == "menu" and (menu is None or b["updated"] >= menu["updated"]):
                    menu = b
            if c.id in hidden:
                continue
            for bid, b in c.blocks.items():
                if b.get("kind") != "menu" and b["items"]:
                    blocks.append({"zone": b["zone"], "order": b["order"], "items": b["items"],
                                   "_key": (b["order"], c.connected_at, bid)})
        if self._preview_active():
            blocks.append({"zone": "top-center", "order": -1, "_key": (-1, 0, "preview"), "items": [
                {"label": "Aperçu Decky Overlay", "countdown": {"end_at": self.preview_until + 290}},
                {"label": "Exemple", "text": "42", "style": "warn"},
            ]})
        blocks.sort(key=lambda b: b.pop("_key"))
        if not self.settings.get("enabled", True) and not self._preview_active():
            blocks = []  # cards are off; a menu is still shown
        scene = {"blocks": blocks, "settings": {k: self.settings[k] for k in ("size", "opacity")}}
        if menu is not None:
            scene["menu"] = {k: menu[k] for k in ("title", "hint", "items", "selected")}
        return scene

    def _in_game_mode(self):
        if _now() - self.game_mode_checked > 5:
            self.game_mode = _gamescope_running()
            self.game_mode_checked = _now()
        return self.game_mode

    def _preview_active(self):
        return _now() < self.preview_until

    def _renderer_wanted(self):
        if not self._in_game_mode():
            return False
        if self._preview_active():
            return True
        scene = self._scene()
        return bool(scene["blocks"] or scene.get("menu"))

    def _as_desktop_user(self, env):
        if os.geteuid() != 0:
            return {}
        try:
            pw = pwd.getpwnam(decky.DECKY_USER)
        except Exception as e:
            decky.logger.warning(f"Cannot drop privileges: {e}")
            return {}
        env.update({"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
                    "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}"})
        return {"user": pw.pw_uid, "group": pw.pw_gid}

    def _start_renderer(self):
        python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else shutil.which("python3")
        if not python:
            self.renderer_error = "python3 introuvable sur le système"
            return
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env["DISPLAY"] = self.settings.get("display") or ":0"
        user_kwargs = self._as_desktop_user(env)
        try:
            if os.path.exists(self.renderer_log) and os.path.getsize(self.renderer_log) > LOG_MAX_BYTES:
                os.replace(self.renderer_log, self.renderer_log + ".1")
            with open(self.renderer_log, "a") as log:
                log.write(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')} DISPLAY={env['DISPLAY']}\n")
                log.flush()
                self.proc = subprocess.Popen(
                    [python, "-u", self.renderer_script],
                    stdin=subprocess.PIPE, stdout=log, stderr=log, env=env,
                    cwd=os.path.dirname(self.renderer_script), **user_kwargs,
                )
            os.set_blocking(self.proc.stdin.fileno(), False)  # never block on a stuck renderer
            self.proc_started = _now()
        except Exception as e:
            self.proc = None
            self.renderer_error = f"Lancement impossible : {e}"
            decky.logger.error(self.renderer_error)

    def _stop_renderer(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.dying.append((self.proc, _now() + 2))
        self.proc = None

    def _reap(self, force=False):
        still = []
        for proc, deadline in self.dying:
            if proc.poll() is not None:
                continue
            if force or _now() >= deadline:
                try:
                    proc.kill()
                    proc.wait(timeout=0.2)
                except Exception:
                    pass
                continue
            still.append((proc, deadline))
        self.dying = still

    def _renderer_died(self):
        code = self.proc.poll() if self.proc else None
        if self.proc is not None:
            self.dying.append((self.proc, _now() + 2))
        self.proc = None
        if _now() - self.proc_started > RENDERER_HEALTHY_AFTER:
            self.failures = 0
        self.failures += 1
        tail = ""
        try:
            with open(self.renderer_log) as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            tail = lines[-1] if lines else ""
        except Exception:
            pass
        self.renderer_error = f"Le rendu s'est arrêté (code {code}). {tail}".strip()
        if self.failures >= RENDERER_MAX_FAILURES:
            self.renderer_error += " Relances abandonnées après plusieurs échecs."
        decky.logger.error(self.renderer_error)

    def _sync_renderer(self):
        if not self._renderer_wanted():
            self._stop_renderer()
            return
        if self.proc is not None and self.proc.poll() is not None:
            self._renderer_died()
        if self.proc is None:
            if self.failures >= RENDERER_MAX_FAILURES:
                return
            self._start_renderer()
        if self.proc is not None:
            data = (json.dumps(self._scene()) + "\n").encode()
            try:
                if os.write(self.proc.stdin.fileno(), data) != len(data):
                    raise BlockingIOError("partial write")
            except BlockingIOError:
                decky.logger.error("Renderer not responding, restarting it")
                self._stop_renderer()
                self.failures += 1
            except (BrokenPipeError, OSError, ValueError):
                self._renderer_died()

    async def _watch(self):
        while True:
            try:
                await asyncio.sleep(0.5)
                self._reap()
                if self.preview_until and _now() >= self.preview_until:
                    self.preview_until = 0.0
                    self._sync_renderer()
                wanted = self._renderer_wanted()
                if wanted and (self.proc is None or self.proc.poll() is not None):
                    self._sync_renderer()
                elif not wanted and self.proc is not None:
                    self._stop_renderer()  # e.g. left Game Mode
                if self.server is None and self.socket_error:
                    await self._start_server()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"watch loop: {e}")

    # ------------------------------------------------------------------ #
    # Frontend API
    # ------------------------------------------------------------------ #

    def _state(self):
        hidden = set(self.settings.get("hidden_clients", []))
        return {
            "settings": self.settings,
            "clients": [{"id": c.id, "name": c.name,
                         "blocks": sum(len(b["items"]) for b in c.blocks.values() if b.get("kind") != "menu"),
                         "hidden": c.id in hidden} for c in self.clients if c.id],
            "renderer_running": self.proc is not None and self.proc.poll() is None,
            "renderer_error": self.renderer_error,
            "socket_error": self.socket_error,
            "socket_path": SOCKET_PATH,
            "game_mode": self.game_mode,
            "log_dir": decky.DECKY_PLUGIN_LOG_DIR,
        }

    async def get_state(self):
        return self._state()

    async def get_version(self):
        return os.environ.get("DECKY_PLUGIN_VERSION") or getattr(decky, "DECKY_PLUGIN_VERSION", "?")

    async def save_settings(self, settings: dict):
        old_display = self.settings.get("display")
        self.settings.update(_valid_settings(settings))
        self.failures = 0
        self.renderer_error = ""
        if self.settings.get("display") != old_display:
            self._stop_renderer()
        self._save()
        self._sync_renderer()
        return self._state()

    async def preview(self):
        self.preview_until = _now() + PREVIEW_SECONDS
        self.failures = 0
        self.renderer_error = ""
        self.game_mode_checked = 0.0
        self._sync_renderer()
        return self._state()
