"""Draws API marks (circles, labels, highlighted areas...) on top of the board. Called from BadukPanWidget."""

from kivy.graphics import Color, Ellipse, Line, Rectangle
from kivy.metrics import dp

from katrain.api.model import node_id
from katrain.gui.widgets.drawing import draw_text

NAMED_COLORS = {
    "red": (0.9, 0.15, 0.15),
    "blue": (0.15, 0.4, 0.95),
    "green": (0.1, 0.75, 0.25),
    "yellow": (0.95, 0.85, 0.1),
    "orange": (1.0, 0.55, 0.0),
    "purple": (0.65, 0.25, 0.85),
    "cyan": (0.1, 0.8, 0.85),
    "white": (1.0, 1.0, 1.0),
    "black": (0.0, 0.0, 0.0),
}
MARK_TYPES = ("circle", "square", "triangle", "cross", "label", "dot", "area")


def parse_color(spec):
    """Named color or #rrggbb -> rgb tuple. Returns None if not recognised."""
    if not spec:
        return NAMED_COLORS["red"]
    spec = str(spec).strip().lower()
    if spec in NAMED_COLORS:
        return NAMED_COLORS[spec]
    if spec.startswith("#") and len(spec) == 7:
        try:
            return tuple(int(spec[i : i + 2], 16) / 255 for i in (1, 3, 5))
        except ValueError:
            return None
    return None


def visible_marks(state, game):
    cur = node_id(game.current_node)
    return [m for m in state.marks if m.get("node") is None or m["node"] == cur]


def draw_api_overlay(board):
    katrain = board.katrain
    state = getattr(katrain, "api_state", None)
    if not state or not state.marks or not katrain.game or not board.gridpos:
        return
    szx, szy = katrain.game.board_size
    if len(board.gridpos) < szy or len(board.gridpos[0]) < szx:
        return
    r = board.stone_size
    width = dp(2)
    for mark in visible_marks(state, katrain.game):
        rgb = mark["rgb"]
        kind = mark["type"]
        if kind == "area":
            xmin, xmax, ymin, ymax = mark["region"]
            (x1, y1), (x2, y2) = board.gridpos[ymin][xmin], board.gridpos[ymax][xmax]
            left, right = min(x1, x2) - board.grid_size / 2, max(x1, x2) + board.grid_size / 2
            bottom, top = min(y1, y2) - board.grid_size / 2, max(y1, y2) + board.grid_size / 2
            Color(*rgb, 0.18)
            Rectangle(pos=(left, bottom), size=(right - left, top - bottom))
            Color(*rgb, 0.9)
            Line(rectangle=(left, bottom, right - left, top - bottom), width=width)
            if mark.get("text"):
                Color(*rgb, 1)  # text textures are white and get tinted by the current color
                draw_text(
                    pos=(left + board.grid_size / 2, top + board.grid_size / 3),
                    text=mark["text"],
                    font_size=board.grid_size / 2.2,
                )
            continue
        x, y = mark["coords"]
        cx, cy = board.gridpos[y][x]
        if kind == "circle":
            Color(*rgb, 1)
            Line(circle=(cx, cy, r * 0.62), width=width)
        elif kind == "square":
            Color(*rgb, 1)
            s = r * 0.55
            Line(rectangle=(cx - s, cy - s, 2 * s, 2 * s), width=width)
        elif kind == "triangle":
            Color(*rgb, 1)
            s = r * 0.7
            Line(points=[cx, cy + s, cx - s * 0.87, cy - s * 0.5, cx + s * 0.87, cy - s * 0.5], close=True, width=width)
        elif kind == "cross":
            Color(*rgb, 1)
            s = r * 0.5
            Line(points=[cx - s, cy - s, cx + s, cy + s], width=width)
            Line(points=[cx - s, cy + s, cx + s, cy - s], width=width)
        elif kind == "dot":
            Color(*rgb, 0.55)
            Ellipse(pos=(cx - r * 0.9, cy - r * 0.9), size=(r * 1.8, r * 1.8))
        elif kind == "label":
            Color(*rgb, 0.9)
            Ellipse(pos=(cx - r * 0.7, cy - r * 0.7), size=(r * 1.4, r * 1.4))
            Color(*((1, 1, 1) if sum(rgb) < 2.2 else (0, 0, 0)), 1)
            draw_text(pos=(cx, cy), text=str(mark.get("text") or "?")[:3], font_size=board.grid_size / 2.2)
