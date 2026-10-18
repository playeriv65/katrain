"""Background analysis sandboxes: private copies of a position that the user never sees.

The agent can play out and analyze variations here while the user keeps using the board. Nothing in a sandbox
touches the GUI: no navigation, no nodes in the user's game tree, no overlays, and its KataGo queries run below
the GUI's own priority. `sandbox.show` is the one explicit step that brings a line to the user's board.
"""

import copy
import itertools

from pysgf import Move

from katrain.api.commands import _bool, _int, _show_pv, cmd_demo_start, command, on_main
from katrain.api.model import (
    analysis_json,
    board_json,
    check_moves_legal,
    node_ref,
    parse_color,
    parse_coords,
    parse_moves,
)
from katrain.api.protocol import ApiError
from katrain.core.game_node import GameNode

_ids = itertools.count(1)
DROPPED_PROPERTIES = ("C", "KT")  # comments and embedded analysis are not needed in a private copy


class Sandbox:
    def __init__(self, sid, origin):
        self.id = sid
        self.origin = origin  # node in the user's game this sandbox branched from
        self.user_root = origin.root
        self.root, self.base = clone_path(origin)
        self.cursor = self.base

    def line(self):
        """Moves played in the sandbox after the origin, up to the cursor."""
        nodes = self.cursor.nodes_from_root[len(self.base.nodes_from_root) :]
        return [n.move for n in nodes if n.move]

    def summary(self):
        moves = self.line()
        return {
            "id": self.id,
            "from": node_ref(self.origin),
            "from_move_number": self.origin.depth,
            "moves": [f"{m.player}:{m.gtp()}" for m in moves],
            "move_number": self.cursor.depth,
            "to_move": self.cursor.next_player,
        }


def clone_path(origin):
    """Copy root -> origin into a detached tree. Returns (root, copy of origin)."""
    path = origin.nodes_from_root
    clones = []
    for src in path:
        props = {k: copy.deepcopy(v) for k, v in src.properties.items() if k not in DROPPED_PROPERTIES}
        clones.append(GameNode(parent=clones[-1] if clones else None, properties=props))
    base = clones[-1]
    base.analysis = copy.deepcopy(origin.analysis)  # reuse what KaTrain already computed
    base.analysis_visits_requested = origin.analysis_visits_requested
    return clones[0], base


def _sandboxes(h):
    if not hasattr(h.state, "sandboxes"):
        h.state.sandboxes = {}
    return h.state.sandboxes


def _get(h, a):
    sb = _sandboxes(h).get(str(a.get("id") or ""))
    if sb is None:
        raise ApiError("not_found", f"no sandbox '{a.get('id')}'", available=sorted(_sandboxes(h)))
    return sb


@command("sandbox.new")
def cmd_sandbox_new(h, a):
    """Copy the position at a node of the user's game (default: current) into a hidden sandbox."""
    origin = h.node(a, "from")
    sid = str(a.get("name") or f"s{next(_ids)}")
    if sid in _sandboxes(h):
        raise ApiError("exists", f"sandbox '{sid}' already exists")
    sb = Sandbox(sid, origin)
    _sandboxes(h)[sid] = sb
    return {**sb.summary(), "board": board_json(sb.cursor)["ascii"]}


@command("sandbox.play")
def cmd_sandbox_play(h, a):
    """Play moves in a sandbox (never touches the user's board)."""
    sb = _get(h, a)
    cur = sb.cursor
    start_player = parse_color(a.get("color")) or cur.next_player
    moves = parse_moves(a.get("moves") or [], cur.board_size, start_player)
    if not moves:
        raise ApiError("bad_argument", "no moves given")
    check_moves_legal(cur, moves)
    for move in moves:
        cur = cur.play(move)
    sb.cursor = cur
    return {**sb.summary(), "board": board_json(cur)["ascii"]}


@command("sandbox.back")
def cmd_sandbox_back(h, a):
    """Take back n moves in a sandbox (not past the position it was copied from)."""
    sb = _get(h, a)
    for _ in range(_int(a, "n", 1, minimum=1)):
        if sb.cursor is sb.base:
            break
        sb.cursor = sb.cursor.parent
    return {**sb.summary(), "board": board_json(sb.cursor)["ascii"]}


@command("sandbox.board")
def cmd_sandbox_board(h, a):
    """Board of a sandbox at its current move."""
    sb = _get(h, a)
    return {**sb.summary(), **board_json(sb.cursor)}


@command("sandbox.analyze", main=False)
def cmd_sandbox_analyze(h, a):
    """Analyze a sandbox position (low priority, invisible); moves=[...] compares specific candidates."""
    timeout = _int(a, "timeout", 300, minimum=1)

    def prepare():
        sb = _get(h, a)
        node = sb.cursor
        engine = h.engine_for(node)
        visits = _int(a, "visits", engine.config["max_visits"], minimum=1)
        raw = a.get("moves") or []
        names = raw.replace(",", " ").split() if isinstance(raw, str) else list(raw)
        moves = [Move(parse_coords(m, node.board_size), player=node.next_player) for m in names]
        for m in moves:
            check_moves_legal(node, [m])
        return sb, node, engine, visits, moves

    sb, node, engine, visits, moves = on_main(prepare)
    cached = not moves and node.analysis_complete and node.root_visits >= 0.95 * visits and not _bool(a.get("force"))
    if not cached or (moves and not node.analysis_exists):
        h.search(node, engine, visits, timeout=timeout, background=True)
    for m in moves:  # sequential: each comparison already fills the GPU
        h.search(node, engine, visits, refine_move=m, timeout=timeout, background=True)
    view = dict(
        top=None if moves else _int(a, "top", 10),
        include_pv=_bool(a.get("pv"), True),
        pv_limit=_int(a, "pv_limit"),
        ownership=_bool(a.get("ownership")),
        moves=[m.gtp() for m in moves] or None,
    )
    result = on_main(lambda: analysis_json(node, **view))
    if moves:
        result["candidates"].sort(key=lambda c: c["points_lost"] if c["points_lost"] is not None else 1e9)
    result.pop("node", None)  # sandbox node ids are not addressable in the user's game
    return {"sandbox": sb.summary(), "reused_existing": cached and not moves, "requested_visits": visits, **result}


@command("sandbox.show")
def cmd_sandbox_show(h, a):
    """Bring a sandbox line to the user's board: as=pv (numbered-stone overlay, default) or as=demo (real moves
    inside demo mode, removed again by demo.end). step=N shows only the first N moves."""
    sb = _get(h, a)
    if h.game.root is not sb.user_root:
        raise ApiError("game_changed", "the user's board shows a different game than this sandbox came from")
    if sb.origin not in h.game.root.nodes_in_tree:
        raise ApiError("not_found", "the position this sandbox came from was deleted from the game")
    moves = sb.line()
    if not moves:
        raise ApiError("bad_argument", "the sandbox has no moves yet")
    step = _int(a, "step", minimum=1)
    if a.get("as", "pv") == "demo":
        started = h.state.demo is None
        if started:
            cmd_demo_start(h, {"label": f"sandbox {sb.id}"})
        nodes, _ = h.add_nodes(sb.origin, moves)
        h.goto(nodes[min(step, len(nodes)) - 1] if step else nodes[-1])
        return {
            "shown": "demo",
            "demo_started": started,
            "moves": [f"{m.player}:{m.gtp()}" for m in moves],
            "hint": "katrain demo end removes these moves again",
            **h.current_json(),
        }
    return {"shown": "pv", **_show_pv(h, sb.origin, moves, step)}


@command("sandbox.list")
def cmd_sandbox_list(h, a):
    """All sandboxes."""
    root = h.game.root
    return [{**sb.summary(), "same_game": sb.user_root is root} for sb in _sandboxes(h).values()]


@command("sandbox.drop")
def cmd_sandbox_drop(h, a):
    """Delete a sandbox (or all with id=all)."""
    boxes = _sandboxes(h)
    if a.get("id") == "all":
        n = len(boxes)
        boxes.clear()
        return {"dropped": n}
    _get(h, a)
    del boxes[str(a["id"])]
    return {"dropped": 1}
