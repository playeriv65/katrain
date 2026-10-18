"""Zoomed board view: show only a rectangle of the board (e.g. a life-and-death problem), like a book diagram.

The game itself is untouched. Grid positions are recomputed so the zoom region fills the board widget; points
outside it land outside the widget and every drawing layer is clipped to the visible rectangle. Real board edges
are drawn as edges, cut sides let the grid lines run on a little to show the board continues. Stones listed in
`board.hidden_coords` (e.g. the stones of KaTrain's tsumego frame) are not drawn but still count for KataGo.
"""

import math

from kivy.graphics import Color, Line, Rectangle
from kivy.graphics.scissor_instructions import ScissorPop, ScissorPush

from katrain.gui.theme import Theme
from katrain.gui.widgets.drawing import cached_texture, draw_circle, draw_text

LINE_RUN_ON = 0.5  # grid spaces the lines extend past a cut (non-edge) side


def _star_points(size):
    pos = 3 if size <= 11 else 4
    if size < 7:
        return []
    return [pos - 1, size - pos] + ([size // 2] if size % 2 == 1 and size > 7 else [])


def is_visible(board, coords):
    region = getattr(board, "zoom_region", None)
    if not region or coords is None:
        return True
    xmin, xmax, ymin, ymax = region
    return xmin <= coords[0] <= xmax and ymin <= coords[1] <= ymax


def view_center(board):
    """Pixel center of the zoomed view, or None when not zoomed."""
    region = getattr(board, "zoom_region", None)
    if not region or not board.gridpos:
        return None
    xmin, xmax, ymin, ymax = region
    (x1, y1), (x2, y2) = board.gridpos[ymin][xmin], board.gridpos[ymax][xmax]
    return (x1 + x2) / 2, (y1 + y2) / 2


def push_view_clip(board):
    if getattr(board, "zoom_region", None) and getattr(board, "zoom_clip", None):
        x, y, width, height = board.zoom_clip
        ScissorPush(x=x, y=y, width=width, height=height)


def pop_view_clip(board):
    if getattr(board, "zoom_region", None) and getattr(board, "zoom_clip", None):
        ScissorPop()


def draw_zoomed_board(board):
    """Replacement for BadukPanWidget.draw_board while a zoom region is set."""
    game = board.katrain.game
    szx, szy = game.board_size
    xmin, xmax, ymin, ymax = board.zoom_region
    cols, rows = xmax - xmin + 1, ymax - ymin + 1

    margin_lb = 1.5 if board.draw_coords_enabled else 0.75  # left/bottom hold the coordinates
    margin_rt = 0.75
    x_spaces = cols - 1 + margin_lb + margin_rt
    y_spaces = rows - 1 + margin_lb + margin_rt
    grid = math.floor(min(board.width / x_spaces, board.height / y_spaces) + 0.1)
    board.grid_size = grid
    board.stone_size = board.calculate_stone_size(grid)

    left = board.pos[0] + (board.width - x_spaces * grid) / 2
    bottom = board.pos[1] + (board.height - y_spaces * grid) / 2
    x0, y0 = left + margin_lb * grid, bottom + margin_lb * grid  # pixel position of (xmin, ymin)
    gx = [round(x0 + (x - xmin) * grid, 4) for x in range(szx)]
    gy = [round(y0 + (y - ymin) * grid, 4) for y in range(szy)]
    board.initial_gridpos_x, board.initial_gridpos_y = gx, gy
    board.gridpos = [[[gx[x], gy[y]] for x in range(szx)] for y in range(szy)]

    width, height = x_spaces * grid, y_spaces * grid
    # clip to the visible points plus half a grid (a stone radius), not the whole margin, so nothing
    # belonging to the hidden row/column just outside the view leaks in
    half = grid / 2
    clip_left, clip_bottom = gx[xmin] - half, gy[ymin] - half
    win_x, win_y = board.to_window(clip_left, clip_bottom)
    board.zoom_clip = (int(win_x), int(win_y), int(math.ceil(cols * grid)), int(math.ceil(rows * grid)))

    run_on = LINE_RUN_ON * grid
    x_lo = gx[xmin] - (0 if xmin == 0 else run_on)
    x_hi = gx[xmax] + (0 if xmax == szx - 1 else run_on)
    y_lo = gy[ymin] - (0 if ymin == 0 else run_on)
    y_hi = gy[ymax] + (0 if ymax == szy - 1 else run_on)

    with board.canvas.before:
        board.canvas.before.clear()
        Color(*(Theme.INSERT_BOARD_COLOR_TINT if game.insert_mode else Theme.BOARD_COLOR_TINT))
        Rectangle(pos=(left, bottom), size=(width, height), texture=cached_texture(Theme.BOARD_TEXTURE))
        Color(*Theme.LINE_COLOR)
        for x in range(xmin, xmax + 1):
            Line(points=[gx[x], y_lo, gx[x], y_hi])
        for y in range(ymin, ymax + 1):
            Line(points=[x_lo, gy[y], x_hi, gy[y]])
        starpt_size = grid * Theme.STARPOINT_SIZE
        for x in _star_points(szx):
            for y in _star_points(szy):
                if xmin <= x <= xmax and ymin <= y <= ymax:
                    draw_circle((gx[x], gy[y]), starpt_size, Theme.LINE_COLOR)
        if board.draw_coords_enabled:
            Color(0.25, 0.25, 0.25)
            offset = round(grid * 1.5 / 2, 12)
            for x in range(xmin, xmax + 1):
                draw_text(
                    pos=(gx[x], gy[ymin] - offset),
                    text=board.get_x_coordinate_text(x, szx),
                    font_size=grid / 1.5,
                    font_name="Roboto",
                )
            for y in range(ymin, ymax + 1):
                draw_text(
                    pos=(gx[xmin] - offset, gy[y]),
                    text=board.get_y_coordinate_text(y, szy),
                    font_size=grid / 1.5,
                    font_name="Roboto",
                )


def set_zoom(board, region, hidden_coords=None):
    """Set (or with region=None clear) the zoomed view and the stones to hide, then redraw."""
    if region:
        board.reset_rotation()
    board.zoom_region = list(region) if region else None
    board.hidden_coords = set(hidden_coords or ())
    board.zoom_clip = None
    board.gridpos = None  # force a full grid rebuild on the next normal draw_board
    board.redraw_trigger()
