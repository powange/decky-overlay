#!/usr/bin/env python3
"""Shared in-game overlay renderer for the Steam Deck (gamescope).

Started by the Decky Overlay backend (main.py) with the *system* python3. It
opens a transparent, full-screen X11 window on gamescope's Xwayland
(DISPLAY=:0) tagged GAMESCOPE_EXTERNAL_OVERLAY, so gamescope composites it on
top of the running game - the same technique mangoapp and OverLaid use.
gamescope shows a single external overlay window at a time: this one process
draws the blocks of every client plugin, so they never fight over it.

Only libX11, libXext (optional) and libcairo are needed, all present on SteamOS.

Protocol: the backend writes one JSON scene per line on stdin:
  {"blocks": [{"zone": "top-right", "items": [ITEM, ...]}, ...],
   "settings": {"size": "medium", "opacity": 0.75}}
ITEM is one card:
  {"label": "Pâtes", "countdown": {"end_at": 1700000000.0}}      running
  {"label": "Boss", "countdown": {"remaining": 95, "paused": true}}
  {"label": "FPS", "text": "60", "style": "normal|warn|alert|muted"}
  optional "show_under": seconds - a countdown is only shown below that.
The scene may also carry one interactive list, drawn in the middle:
  "menu": {"title": "...", "items": ["A", "B"], "selected": 0, "hint": "..."}
Countdowns are computed here, so the backend only writes when something
changes. EOF on stdin (backend gone) makes the renderer exit.
"""


import ctypes
import ctypes.util
import json
import math
import os
import select
import sys
import time
from ctypes import (POINTER, Structure, byref, c_char_p, c_double, c_int,
                    c_long, c_uint, c_ulong, c_void_p)


# --------------------------------------------------------------------------- #
# Native libraries
# --------------------------------------------------------------------------- #

def _load(*names):
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


x11 = _load("libX11.so.6", ctypes.util.find_library("X11") or "libX11.so")
cairo = _load("libcairo.so.2", ctypes.util.find_library("cairo") or "libcairo.so")
xext = _load("libXext.so.6", ctypes.util.find_library("Xext") or "libXext.so")

if x11 is None or cairo is None:
    sys.stderr.write("renderer: libX11 or libcairo not found\n")
    sys.exit(2)


class XVisualInfo(Structure):
    _fields_ = [
        ("visual", c_void_p), ("visualid", c_ulong), ("screen", c_int),
        ("depth", c_int), ("c_class", c_int), ("red_mask", c_ulong),
        ("green_mask", c_ulong), ("blue_mask", c_ulong),
        ("colormap_size", c_int), ("bits_per_rgb", c_int),
    ]


class XSetWindowAttributes(Structure):
    _fields_ = [
        ("background_pixmap", c_ulong), ("background_pixel", c_ulong),
        ("border_pixmap", c_ulong), ("border_pixel", c_ulong),
        ("bit_gravity", c_int), ("win_gravity", c_int),
        ("backing_store", c_int), ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong), ("save_under", c_int),
        ("event_mask", c_long), ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int), ("colormap", c_ulong),
        ("cursor", c_ulong),
    ]


class CairoTextExtents(Structure):
    _fields_ = [
        ("x_bearing", c_double), ("y_bearing", c_double),
        ("width", c_double), ("height", c_double),
        ("x_advance", c_double), ("y_advance", c_double),
    ]


def _sig(lib, name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


XOpenDisplay = _sig(x11, "XOpenDisplay", c_void_p, c_char_p)
XDefaultScreen = _sig(x11, "XDefaultScreen", c_int, c_void_p)
XRootWindow = _sig(x11, "XRootWindow", c_ulong, c_void_p, c_int)
XDisplayWidth = _sig(x11, "XDisplayWidth", c_int, c_void_p, c_int)
XDisplayHeight = _sig(x11, "XDisplayHeight", c_int, c_void_p, c_int)
XMatchVisualInfo = _sig(x11, "XMatchVisualInfo", c_int, c_void_p, c_int, c_int, c_int, POINTER(XVisualInfo))
XCreateColormap = _sig(x11, "XCreateColormap", c_ulong, c_void_p, c_ulong, c_void_p, c_int)
XCreateWindow = _sig(x11, "XCreateWindow", c_ulong, c_void_p, c_ulong, c_int, c_int, c_uint, c_uint,
                     c_uint, c_int, c_uint, c_void_p, c_ulong, POINTER(XSetWindowAttributes))
XInternAtom = _sig(x11, "XInternAtom", c_ulong, c_void_p, c_char_p, c_int)
XChangeProperty = _sig(x11, "XChangeProperty", c_int, c_void_p, c_ulong, c_ulong, c_ulong, c_int, c_int,
                       c_void_p, c_int)
XStoreName = _sig(x11, "XStoreName", c_int, c_void_p, c_ulong, c_char_p)
XMapWindow = _sig(x11, "XMapWindow", c_int, c_void_p, c_ulong)
XUnmapWindow = _sig(x11, "XUnmapWindow", c_int, c_void_p, c_ulong)
XFlush = _sig(x11, "XFlush", c_int, c_void_p)
XPending = _sig(x11, "XPending", c_int, c_void_p)
XNextEvent = _sig(x11, "XNextEvent", c_int, c_void_p, c_void_p)
XConnectionNumber = _sig(x11, "XConnectionNumber", c_int, c_void_p)
XCloseDisplay = _sig(x11, "XCloseDisplay", c_int, c_void_p)

c_surface_create = _sig(cairo, "cairo_xlib_surface_create", c_void_p, c_void_p, c_ulong, c_void_p, c_int, c_int)
c_create = _sig(cairo, "cairo_create", c_void_p, c_void_p)
c_push_group = _sig(cairo, "cairo_push_group", None, c_void_p)
c_pop_group_to_source = _sig(cairo, "cairo_pop_group_to_source", None, c_void_p)
c_set_operator = _sig(cairo, "cairo_set_operator", None, c_void_p, c_int)
c_paint = _sig(cairo, "cairo_paint", None, c_void_p)
c_set_rgba = _sig(cairo, "cairo_set_source_rgba", None, c_void_p, c_double, c_double, c_double, c_double)
c_new_path = _sig(cairo, "cairo_new_path", None, c_void_p)
c_new_sub_path = _sig(cairo, "cairo_new_sub_path", None, c_void_p)
c_close_path = _sig(cairo, "cairo_close_path", None, c_void_p)
c_arc = _sig(cairo, "cairo_arc", None, c_void_p, c_double, c_double, c_double, c_double, c_double)
c_rectangle = _sig(cairo, "cairo_rectangle", None, c_void_p, c_double, c_double, c_double, c_double)
c_fill = _sig(cairo, "cairo_fill", None, c_void_p)
c_clip = _sig(cairo, "cairo_clip", None, c_void_p)
c_reset_clip = _sig(cairo, "cairo_reset_clip", None, c_void_p)
c_fill_preserve = _sig(cairo, "cairo_fill_preserve", None, c_void_p)
c_stroke = _sig(cairo, "cairo_stroke", None, c_void_p)
c_set_line_width = _sig(cairo, "cairo_set_line_width", None, c_void_p, c_double)
c_select_font = _sig(cairo, "cairo_select_font_face", None, c_void_p, c_char_p, c_int, c_int)
c_set_font_size = _sig(cairo, "cairo_set_font_size", None, c_void_p, c_double)
c_text_extents = _sig(cairo, "cairo_text_extents", None, c_void_p, c_char_p, POINTER(CairoTextExtents))
c_move_to = _sig(cairo, "cairo_move_to", None, c_void_p, c_double, c_double)
c_show_text = _sig(cairo, "cairo_show_text", None, c_void_p, c_char_p)
c_surface_flush = _sig(cairo, "cairo_surface_flush", None, c_void_p)

CAIRO_OPERATOR_SOURCE = 1
CAIRO_OPERATOR_OVER = 2
CAIRO_FONT_SLANT_NORMAL = 0
CAIRO_FONT_WEIGHT_NORMAL = 0
CAIRO_FONT_WEIGHT_BOLD = 1

TRUE_COLOR = 4
INPUT_OUTPUT = 1
CW_BACK_PIXEL = 1 << 1
CW_BORDER_PIXEL = 1 << 3
CW_OVERRIDE_REDIRECT = 1 << 9
CW_COLORMAP = 1 << 13
XA_CARDINAL = 6
PROP_MODE_REPLACE = 0


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #

class Overlay:
    def __init__(self):
        display_name = os.environ.get("DISPLAY", ":0").encode()
        self.dpy = XOpenDisplay(display_name)
        if not self.dpy:
            raise RuntimeError(f"cannot open display {display_name.decode()}")
        screen = XDefaultScreen(self.dpy)
        root = XRootWindow(self.dpy, screen)
        self.width = XDisplayWidth(self.dpy, screen)
        self.height = XDisplayHeight(self.dpy, screen)

        vinfo = XVisualInfo()
        if not XMatchVisualInfo(self.dpy, screen, 32, TRUE_COLOR, byref(vinfo)):
            raise RuntimeError("no 32-bit ARGB visual")

        attrs = XSetWindowAttributes()
        attrs.colormap = XCreateColormap(self.dpy, root, vinfo.visual, 0)
        attrs.background_pixel = 0
        attrs.border_pixel = 0
        attrs.override_redirect = 1
        mask = CW_COLORMAP | CW_BACK_PIXEL | CW_BORDER_PIXEL | CW_OVERRIDE_REDIRECT

        self.win = XCreateWindow(self.dpy, root, 0, 0, self.width, self.height, 0, 32,
                                 INPUT_OUTPUT, vinfo.visual, mask, byref(attrs))
        XStoreName(self.dpy, self.win, b"Decky Overlay")

        # Ask gamescope to composite this window on top of everything.
        one = c_long(1)
        for atom_name in (b"GAMESCOPE_EXTERNAL_OVERLAY",):
            atom = XInternAtom(self.dpy, atom_name, 0)
            XChangeProperty(self.dpy, self.win, atom, XA_CARDINAL, 32,
                            PROP_MODE_REPLACE, byref(one), 1)

        # Empty input shape: the overlay never steals touches or clicks.
        if xext is not None:
            try:
                shape = _sig(xext, "XShapeCombineRectangles", None, c_void_p, c_ulong, c_int, c_int,
                             c_int, c_void_p, c_int, c_int, c_int)
                shape(self.dpy, self.win, 2, 0, 0, None, 0, 0, 0)  # ShapeInput, ShapeSet
            except AttributeError:
                pass

        # Not mapped yet: render() maps the window only while cards are shown,
        # so an empty overlay costs gamescope nothing.
        self.mapped = False
        self.last_bbox = None
        XFlush(self.dpy)

        self.surface = c_surface_create(self.dpy, self.win, vinfo.visual, self.width, self.height)
        self.cr = c_create(self.surface)
        self.fd = XConnectionNumber(self.dpy)

    def drain_events(self):
        ev = ctypes.create_string_buffer(256)  # sizeof(XEvent) is 192
        while XPending(self.dpy):
            XNextEvent(self.dpy, ev)

    # ----------------------------------------------------------------- draw

    def _text_width(self, text, size, bold, font=None):
        cr = self.cr
        c_select_font(cr, font or (FONT_MONO if bold else FONT_SANS), CAIRO_FONT_SLANT_NORMAL,
                      CAIRO_FONT_WEIGHT_BOLD if bold else CAIRO_FONT_WEIGHT_NORMAL)
        c_set_font_size(cr, size)
        ext = CairoTextExtents()
        c_text_extents(cr, text.encode(), byref(ext))
        return ext.x_advance

    def _text(self, text, x, y, size, bold, rgba, font=None):
        cr = self.cr
        c_select_font(cr, font or (FONT_MONO if bold else FONT_SANS), CAIRO_FONT_SLANT_NORMAL,
                      CAIRO_FONT_WEIGHT_BOLD if bold else CAIRO_FONT_WEIGHT_NORMAL)
        c_set_font_size(cr, size)
        c_set_rgba(cr, *rgba)
        c_move_to(cr, x, y)
        c_show_text(cr, text.encode())

    def _rounded(self, x, y, w, h, r):
        cr = self.cr
        c_new_path(cr)
        c_new_sub_path(cr)
        c_arc(cr, x + w - r, y + r, r, -math.pi / 2, 0)
        c_arc(cr, x + w - r, y + h - r, r, 0, math.pi / 2)
        c_arc(cr, x + r, y + h - r, r, math.pi / 2, math.pi)
        c_arc(cr, x + r, y + r, r, math.pi, 3 * math.pi / 2)
        c_close_path(cr)


    # ----------------------------------------------------------------- draw

    def _metrics(self, settings):
        scale = SIZES.get(settings.get("size", "medium"), 1.0)
        m = {
            "scale": scale, "name_size": 13 * scale, "value_size": 26 * scale,
            "pad_x": 12 * scale, "pad_y": 7 * scale, "gap": 6 * scale,
            "margin": 12 * scale, "radius": 10 * scale,
        }
        m["box_h"] = m["pad_y"] * 2 + m["name_size"] + 4 * scale + m["value_size"]
        return m

    def _layout(self, zones, settings):
        """zones: {zone: [row, ...]} -> ([(zone, x, y, w, rows)], bbox or None)."""
        m = self._metrics(settings)
        placed = []
        bbox = None
        for zone in ZONES:
            rows = zones.get(zone)
            if not rows:
                continue
            widths = []
            for label, value, state in rows:
                widths.append(max(self._text_width(_label(label, state), m["name_size"], False),
                                  self._text_width(value, m["value_size"], True)) + m["pad_x"] * 2)
            w = max(max(widths), 110 * m["scale"])
            total_h = len(rows) * m["box_h"] + (len(rows) - 1) * m["gap"]
            y = m["margin"] if zone.startswith("top") else self.height - m["margin"] - total_h
            if zone.endswith("left"):
                x = m["margin"]
            elif zone.endswith("center"):
                x = (self.width - w) / 2
            else:
                x = self.width - m["margin"] - w
            placed.append((zone, x, y, w, rows))
            box = (x - 4, y - 4, x + w + 4, y + total_h + 4)
            bbox = box if bbox is None else (min(bbox[0], box[0]), min(bbox[1], box[1]),
                                             max(bbox[2], box[2]), max(bbox[3], box[3]))
        return placed, bbox

    def _menu_layout(self, menu, settings):
        """Menu box geometry and visible rows, or None."""
        if not menu or not menu.get("items"):
            return None
        m = self._metrics(settings)
        s = m["scale"]
        row_size = 20 * s
        row_h = row_size * 1.7
        title_size = 15 * s
        hint_size = 12 * s
        pad = 18 * s
        items = [_cut(i, MENU_LABEL_MAX) for i in menu["items"]]
        sel = max(0, min(int(menu.get("selected") or 0), len(items) - 1))
        first, last = menu_window(len(items), sel, MENU_VISIBLE)
        title = _cut(menu.get("title") or "", MENU_LABEL_MAX)
        hint = _cut(menu.get("hint") or "", 80)
        w = max([self._text_width(t, row_size, False) for t in items[first:last]] + [0]) + pad * 2 + 24 * s
        w = max(w, self._text_width(title, title_size, True, FONT_SANS) + pad * 2,
                self._text_width(hint, hint_size, False) + pad * 2, 320 * s)
        w = min(w, self.width - 40)
        h = pad * 2 + (last - first) * row_h
        if title:
            h += title_size + 12 * s
        if hint:
            h += hint_size + 12 * s
        x = (self.width - w) / 2
        y = (self.height - h) / 2
        return {"x": x, "y": y, "w": w, "h": h, "pad": pad, "row_size": row_size, "row_h": row_h,
                "title": title, "title_size": title_size, "hint": hint, "hint_size": hint_size,
                "items": items, "sel": sel, "first": first, "last": last, "scale": s}

    def _draw_menu(self, L, opacity):
        cr = self.cr
        s = L["scale"]
        self._rounded(L["x"], L["y"], L["w"], L["h"], 14 * s)
        c_set_rgba(cr, 0.05, 0.05, 0.07, max(0.85, opacity))
        c_fill_preserve(cr)
        c_set_line_width(cr, 1.5 * s)
        c_set_rgba(cr, 1, 1, 1, 0.18)
        c_stroke(cr)
        x = L["x"] + L["pad"]
        y = L["y"] + L["pad"]
        if L["title"]:
            self._text(L["title"], x, y + L["title_size"], L["title_size"], True, (1.0, 0.78, 0.30, 1), FONT_SANS)
            y += L["title_size"] + 12 * s
        for i in range(L["first"], L["last"]):
            if i == L["sel"]:
                self._rounded(L["x"] + 8 * s, y, L["w"] - 16 * s, L["row_h"], 8 * s)
                c_set_rgba(cr, 0.20, 0.45, 0.85, 0.9)
                c_fill(cr)
            more = (i == L["first"] and L["first"] > 0) or (i == L["last"] - 1 and L["last"] < len(L["items"]))
            fg = (1, 1, 1, 1) if i == L["sel"] else (0.85, 0.85, 0.88, 0.6 if more else 0.95)
            self._text(L["items"][i], x + 4 * s, y + L["row_h"] / 2 + L["row_size"] * 0.35,
                       L["row_size"], False, fg)
            y += L["row_h"]
        if L["hint"]:
            y += 12 * s
            self._text(L["hint"], x, y + L["hint_size"], L["hint_size"], False, (0.7, 0.7, 0.75, 1))

    def render(self, zones, settings, menu=None):
        """Only the area covered by cards (now and before) is repainted, and the
        window is unmapped when there is nothing to show."""
        cr = self.cr
        placed, bbox = self._layout(zones, settings)
        menu_l = self._menu_layout(menu, settings)
        if menu_l is not None:
            box = (menu_l["x"] - 4, menu_l["y"] - 4, menu_l["x"] + menu_l["w"] + 4, menu_l["y"] + menu_l["h"] + 4)
            bbox = box if bbox is None else (min(bbox[0], box[0]), min(bbox[1], box[1]),
                                             max(bbox[2], box[2]), max(bbox[3], box[3]))
        if bbox is None:
            if self.mapped:
                XUnmapWindow(self.dpy, self.win)
                self.mapped = False
                XFlush(self.dpy)
            self.last_bbox = None
            return
        if not self.mapped:
            XMapWindow(self.dpy, self.win)
            self.mapped = True
            self.last_bbox = (0, 0, self.width, self.height)  # first paint: everything
        dirty = bbox if self.last_bbox is None else (
            min(bbox[0], self.last_bbox[0]), min(bbox[1], self.last_bbox[1]),
            max(bbox[2], self.last_bbox[2]), max(bbox[3], self.last_bbox[3]))
        self.last_bbox = bbox

        m = self._metrics(settings)
        opacity = max(0.0, min(1.0, float(settings.get("opacity", 0.75))))
        c_rectangle(cr, dirty[0], dirty[1], dirty[2] - dirty[0], dirty[3] - dirty[1])
        c_clip(cr)
        c_push_group(cr)
        c_set_operator(cr, CAIRO_OPERATOR_SOURCE)
        c_set_rgba(cr, 0, 0, 0, 0)
        c_paint(cr)
        c_set_operator(cr, CAIRO_OPERATOR_OVER)

        for _zone, x, y, w, rows in placed:
            for label, value, state in rows:
                bg, border, name_fg, value_fg = STYLES.get(state, STYLES["normal"])
                self._rounded(x, y, w, m["box_h"], m["radius"])
                c_set_rgba(cr, *bg[:3], bg[3] * opacity)
                c_fill_preserve(cr)
                c_set_line_width(cr, 1.5 * m["scale"])
                c_set_rgba(cr, *border)
                c_stroke(cr)
                self._text(_label(label, state), x + m["pad_x"], y + m["pad_y"] + m["name_size"],
                           m["name_size"], False, name_fg)
                self._text(value, x + m["pad_x"],
                           y + m["pad_y"] + m["name_size"] + 4 * m["scale"] + m["value_size"] * 0.92,
                           m["value_size"], True, value_fg)
                y += m["box_h"] + m["gap"]

        if menu_l is not None:
            self._draw_menu(menu_l, opacity)

        c_pop_group_to_source(cr)
        c_set_operator(cr, CAIRO_OPERATOR_SOURCE)
        c_paint(cr)
        c_reset_clip(cr)
        c_surface_flush(self.surface)
        XFlush(self.dpy)


FONT_SANS = b"sans-serif"
FONT_MONO = b"monospace"
SIZES = {"small": 0.8, "medium": 1.0, "large": 1.35, "xlarge": 1.75}
ZONES = ("top-left", "top-center", "top-right", "bottom-left", "bottom-right")
LABEL_MAX = 24   # longer labels are cut with an ellipsis (cards stay compact)
VALUE_MAX = 16
MENU_LABEL_MAX = 40
MENU_VISIBLE = 9  # rows shown at once; the list scrolls around the selection

# state -> (background rgba (alpha scaled by opacity), border rgba, label rgba, value rgba)
STYLES = {
    "normal": ((0.05, 0.05, 0.07, 1.0), (1, 1, 1, 0.12), (0.85, 0.85, 0.88, 0.95), (1, 1, 1, 1)),
    "over":   ((0.35, 0.02, 0.02, 1.0), (1.0, 0.3, 0.3, 0.9), (0.85, 0.85, 0.88, 0.95), (1.0, 0.36, 0.36, 1)),
    "alert":  ((0.35, 0.02, 0.02, 1.0), (1.0, 0.3, 0.3, 0.9), (0.85, 0.85, 0.88, 0.95), (1.0, 0.36, 0.36, 1)),
    "pause":  ((0.08, 0.08, 0.10, 0.85), (1, 1, 1, 0.12), (1.0, 0.78, 0.30, 0.9), (1.0, 0.78, 0.30, 0.9)),
    "warn":   ((0.10, 0.08, 0.02, 1.0), (1.0, 0.78, 0.30, 0.6), (0.85, 0.85, 0.88, 0.95), (1.0, 0.78, 0.30, 1)),
    "muted":  ((0.05, 0.05, 0.07, 0.7), (1, 1, 1, 0.08), (0.6, 0.6, 0.65, 0.9), (0.7, 0.7, 0.75, 1)),
}


def _label(label, state):
    return f"{label} · pause" if state == "pause" else label


def _cut(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Scene logic (pure functions, unit tested)
# --------------------------------------------------------------------------- #

def format_seconds(value):
    """Countdown text: ceil while positive, floor magnitude once overtime."""
    if value > 0:
        total = int(math.ceil(value))
        sign = ""
    else:
        total = int(math.floor(-value))
        sign = "-" if total > 0 else ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{sign}{h}:{m:02d}:{s:02d}"
    return f"{sign}{m:02d}:{s:02d}"


def _remaining(cd, now):
    if cd.get("paused"):
        return float(cd.get("remaining") or 0)
    return float(cd.get("end_at") or now) - now


def _hidden(item, remaining):
    limit = item.get("show_under")
    return isinstance(limit, (int, float)) and limit > 0 and remaining > limit


def build_zones(scene, now):
    """Scene -> {zone: [(label, value, state), ...]} for what is visible now."""
    zones = {}
    for block in scene.get("blocks", []):
        zone = block.get("zone") if block.get("zone") in ZONES else "top-center"
        for item in block.get("items", []):
            if not isinstance(item, dict):
                continue
            label = _cut(item.get("label") or "", LABEL_MAX)
            cd = item.get("countdown")
            if isinstance(cd, dict):
                r = _remaining(cd, now)
                if _hidden(item, r):
                    continue
                state = "pause" if cd.get("paused") else ("normal" if r > 0 else "over")
                value = format_seconds(r)
            else:
                value = _cut(item.get("text") or "", VALUE_MAX)
                state = item.get("style") if item.get("style") in STYLES else "normal"
            zones.setdefault(zone, []).append((label, value, state))
    return zones


def menu_window(count, selected, visible):
    """[first, last) of the rows shown so that `selected` stays in view."""
    if count <= visible:
        return 0, count
    first = max(0, min(selected - visible // 2, count - visible))
    return first, first + visible


def seconds_until_change(scene, now):
    """Time until a displayed countdown can change; None = only on new input."""
    waits = []
    for block in scene.get("blocks", []):
        for item in block.get("items", []):
            cd = item.get("countdown") if isinstance(item, dict) else None
            if not isinstance(cd, dict) or cd.get("paused"):
                continue
            r = _remaining(cd, now)
            if _hidden(item, r):
                waits.append(r - item["show_under"])  # wake up when it appears
                continue
            # Positive values show ceil(r), negative ones floor(-r): both
            # change when r crosses an integer.
            frac = r - math.floor(r)
            waits.append(frac if frac > 0 else 1.0)
    if not waits:
        return None
    return max(0.01, min(waits) + 0.005)


def main():
    overlay = Overlay()
    scene = {"blocks": [], "settings": {}}
    buf = b""
    last_key = None
    stdin_fd = sys.stdin.fileno()
    os.set_blocking(stdin_fd, False)

    while True:
        timeout = seconds_until_change(scene, time.time())
        readable, _, _ = select.select([stdin_fd, overlay.fd], [], [], timeout)
        if stdin_fd in readable:
            try:
                chunk = os.read(stdin_fd, 65536)
            except BlockingIOError:
                chunk = None
            if chunk == b"":
                break  # backend closed the pipe
            if chunk:
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        scene = json.loads(line)
                    except ValueError:
                        sys.stderr.write(f"renderer: bad message {line[:200]!r}\n")
        if overlay.fd in readable:
            overlay.drain_events()

        zones = build_zones(scene, time.time())
        menu = scene.get("menu") if isinstance(scene.get("menu"), dict) else None
        key = (json.dumps(zones, sort_keys=True), json.dumps(scene.get("settings", {}), sort_keys=True),
               json.dumps(menu, sort_keys=True))
        if key != last_key:
            overlay.render(zones, scene.get("settings", {}), menu)
            last_key = key

    XCloseDisplay(overlay.dpy)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # logged by the backend
        sys.stderr.write(f"renderer: fatal: {exc!r}\n")
        sys.exit(1)
