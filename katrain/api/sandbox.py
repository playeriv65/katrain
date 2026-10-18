"""Background exploration cursors ("sandboxes") inside the user's own game tree.

A sandbox is a cursor of the agent's, separate from the position the GUI shows. Moves the agent plays become
ordinary variations in the user's game, and every analysis is stored in those nodes, so nothing computed in the
background is thrown away: the user can browse the lines and their evaluations later without re-analysis.
What the sandbox never does is move the user's board, draw overlays, or outrank the GUI's own queries.
`sandbox.show` is the explicit step that brings a line to the user's attention.
"""

import itertools

from pysgf import Move

from katrain.api.commands import _bool, _int, _show_pv, command, on_main
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

_ids = itertools.count(1)


class Sandbox:
    def __init__(self, sid, origin):
        self.id = sid
        self.origin = origin  # node of the user's game the exploration starts from
        self.root = origin.root
        self.cursor = origin

    def line(self):
        """Nodes after the origin up to the cursor."""
        return self.cursor.nodes_from_root[len(self.origin.nodes_from_root) :]

    def summary(self):
        nodes = self.line()
        return {
            "id": self.id,
            "from": node_ref(self.origin),
            "from_move_number": self.origin.depth,
            "moves": [f"{n.move.player}:{n.move.gtp()}" for n in nodes if n.move],
            "nodes": [node_ref(n) for n in nodes],
            "at": node_ref(self.cursor),
            "move_number": self.cursor.depth,
            "to_move": self.cursor.next_player,
        }


def _sandboxes(h):
    if not hasattr(h.state, "sandboxes"):
        h.state.sandboxes = {}
    return h.state.sandboxes


def _get(h, a):
    sb = _sandboxes(h).get(str(a.get("id") or ""))
    if sb is None:
        raise ApiError("not_found", f"no sandbox '{a.get('id')}'", available=sorted(_sandboxes(h)))
    if h.game.root is not sb.root:
        raise ApiError("game_changed", "the user's board shows a different game than this sandbox belongs to")
    if sb.cursor not in sb.root.nodes_in_tree:
        raise ApiError("not_found", "the sandbox position was deleted from the game")
    return sb


def _redraw_tree(h):
    h.katrain.controls.move_tree.redraw_tree_trigger()


@command("sandbox.new")
def cmd_sandbox_new(h, a):
    """Start a background cursor at a node of the user's game (default: the position the user is viewing)."""
    origin = h.node(a, "from")
    sid = str(a.get("name") or f"s{next(_ids)}")
    if sid in _sandboxes(h):
        raise ApiError("exists", f"sandbox '{sid}' already exists")
    sb = Sandbox(sid, origin)
    _sandboxes(h)[sid] = sb
    return {**sb.summary(), "board": board_json(sb.cursor)["ascii"]}


@command("sandbox.play")
def cmd_sandbox_play(h, a):
    """Play moves at the sandbox cursor. They are kept as variations in the user's game; the board does not move."""
    sb = _get(h, a)
    cur = sb.cursor
    start_player = parse_color(a.get("color")) or cur.next_player
    moves = parse_moves(a.get("moves") or [], cur.board_size, start_player)
    if not moves:
        raise ApiError("bad_argument", "no moves given")
    nodes, created = h.add_nodes(cur, moves, analyze="none")  # validates legality first
    sb.cursor = nodes[-1]
    _redraw_tree(h)
    return {**sb.summary(), "created_nodes": created, "board": board_json(sb.cursor)["ascii"]}


@command("sandbox.back")
def cmd_sandbox_back(h, a):
    """Move the sandbox cursor back n moves (not past where it started)."""
    sb = _get(h, a)
    for _ in range(_int(a, "n", 1, minimum=1)):
        if sb.cursor is sb.origin:
            break
        sb.cursor = sb.cursor.parent
    return {**sb.summary(), "board": board_json(sb.cursor)["ascii"]}


@command("sandbox.board")
def cmd_sandbox_board(h, a):
    """Board at the sandbox cursor."""
    sb = _get(h, a)
    return {**sb.summary(), **board_json(sb.cursor)}


@command("sandbox.analyze", main=False)
def cmd_sandbox_analyze(h, a):
    """Analyze the sandbox position at low priority; results are stored in the user's game. moves=[...] compares
    candidate moves (stored in the node's candidate list like KaTrain's own analysis)."""
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
    return {"sandbox": sb.summary(), "reused_existing": cached and not moves, "requested_visits": visits, **result}


@command("sandbox.show")
def cmd_sandbox_show(h, a):
    """Bring a sandbox line to the user's board: as=pv (numbered-stone overlay from the start, default) or as=goto
    (move the user's board to the sandbox position). step=N limits the PV to its first N moves."""
    sb = _get(h, a)
    nodes = sb.line()
    if not nodes:
        raise ApiError("bad_argument", "the sandbox has no moves yet")
    if a.get("as", "pv") == "goto":
        h.goto(sb.cursor)
        return {"shown": "goto", **h.current_json()}
    moves = [n.move for n in nodes if n.move]
    return {"shown": "pv", **_show_pv(h, sb.origin, moves, _int(a, "step", minimum=1))}


@command("sandbox.list")
def cmd_sandbox_list(h, a):
    """All sandboxes of the current game."""
    root = h.game.root
    return [sb.summary() for sb in _sandboxes(h).values() if sb.root is root]


@command("sandbox.drop")
def cmd_sandbox_drop(h, a):
    """Forget a sandbox (id=all for every one). prune=true also deletes the variations it added, as long as they
    contain only API-created nodes and nobody is looking at them."""
    boxes = _sandboxes(h)
    targets = list(boxes.values()) if a.get("id") == "all" else [_get(h, a)]
    pruned = 0
    if _bool(a.get("prune")):
        current = h.game.current_node
        for sb in targets:
            if sb.root is not h.game.root:
                continue
            first_added = next((n for n in sb.line() if n in h.state.created), None)
            if first_added is None or first_added.parent is None:
                continue
            subtree = first_added.nodes_in_tree
            if all(n in h.state.created for n in subtree) and current not in subtree:
                first_added.parent.children.remove(first_added)
                pruned += len(subtree)
        _redraw_tree(h)
    for sb in targets:
        boxes.pop(sb.id, None)
    return {"dropped": len(targets), "pruned_nodes": pruned}
