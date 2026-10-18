"""Node addressing, coordinate parsing and JSON serialization for the API. Pure functions, no GUI access."""

import itertools
import re
import threading

from pysgf import Move

from katrain.api.protocol import ApiError
from katrain.core.game import BaseGame, IllegalMoveException

_node_ids = itertools.count(1)


# -- node identity ---------------------------------------------------------


def node_id(node):
    """Stable (per GUI session) integer id, assigned lazily."""
    nid = getattr(node, "_api_id", None)
    if nid is None:
        nid = node._api_id = next(_node_ids)
    return nid


def node_ref(node):
    return f"n{node_id(node)}"


def find_node_by_id(root, nid):
    for node in root.nodes_in_tree:
        if node_id(node) == nid:
            return node
    return None


def line_nodes(node):
    """Root -> node, then onward along the first child to the end of the line."""
    nodes = node.nodes_from_root
    while nodes[-1].children:
        nodes.append(nodes[-1].ordered_children[0])
    return nodes


def main_line(root):
    return line_nodes(root)


def on_main_line(node):
    return all(n.parent is None or n.parent.ordered_children[0] is n for n in node.nodes_from_root)


def _node_at_move_number(nodes, number, what):
    if number == 0:
        return nodes[0]
    for n in nodes:
        if n.move and n.depth == number:
            return n
    last = max((n.depth for n in nodes), default=0)
    raise ApiError("node_not_found", f"move {number} not on the {what} (it has {last} moves)")


def resolve_node(game, ref):
    """Turn a node reference into a node.

    current | root | end | <N> (move N on the current line) | main:<N> | n<ID> | #<ID>
    """
    if ref is None or ref == "" or ref == "current":
        return game.current_node
    ref = str(ref).strip()
    if ref == "root":
        return game.root
    if ref == "end":
        return line_nodes(game.current_node)[-1]
    m = re.fullmatch(r"(?:n|#)(\d+)", ref)
    if m:
        node = find_node_by_id(game.root, int(m[1]))
        if node is None:
            raise ApiError("node_not_found", f"no node with id {ref} in the current game")
        return node
    m = re.fullmatch(r"main:(\d+)", ref)
    if m:
        return _node_at_move_number(main_line(game.root), int(m[1]), "main line")
    if re.fullmatch(r"\d+", ref):
        return _node_at_move_number(line_nodes(game.current_node), int(ref), "current line")
    raise ApiError(
        "bad_node_ref", f"cannot parse node reference '{ref}'", hint="use current|root|end|<N>|main:<N>|n<ID>"
    )


# -- coordinates -----------------------------------------------------------

COLOR_ALIASES = {"B": "B", "BLACK": "B", "X": "B", "W": "W", "WHITE": "W", "O": "W"}


def parse_color(text):
    if text is None:
        return None
    color = COLOR_ALIASES.get(str(text).strip().upper())
    if color is None:
        raise ApiError("bad_color", f"unknown color '{text}', use B or W")
    return color


def parse_coords(text, board_size):
    """GTP coordinate (D4, Q16, pass) -> (x, y) or None for pass."""
    t = str(text).strip().upper()
    if t == "PASS":
        return None
    m = re.fullmatch(r"([A-HJ-Z]{1,2})(\d{1,2})", t)
    if not m or m[1] not in Move.GTP_COORD:
        raise ApiError("bad_coordinate", f"cannot parse coordinate '{text}' (use GTP style like D4, Q16 or pass)")
    x, y = Move.GTP_COORD.index(m[1]), int(m[2]) - 1
    if not (0 <= x < board_size[0] and 0 <= y < board_size[1]):
        raise ApiError("bad_coordinate", f"'{text}' is outside the {board_size[0]}x{board_size[1]} board")
    return x, y


def parse_move(text, board_size, default_player):
    """'D4', 'B:D4', 'W Q16', 'pass' -> Move."""
    t = str(text).strip()
    m = re.fullmatch(r"([BbWw])[:\s]\s*(\S+)", t)
    player = default_player
    if m:
        player, t = m[1].upper(), m[2]
    return Move(parse_coords(t, board_size), player=player)


def parse_moves(items, board_size, start_player):
    """Parse a move sequence; colors alternate from start_player unless given explicitly."""
    if isinstance(items, str):
        items = items.replace(",", " ").split()
    moves, player = [], start_player
    for item in items:
        move = parse_move(item, board_size, player)
        moves.append(move)
        player = Move.opponent_player(move.player)
    return moves


def parse_region(spec, board_size):
    """'C3:G7' or ['C3', 'G7'] -> [xmin, xmax, ymin, ymax]."""
    parts = spec.replace(",", ":").split(":") if isinstance(spec, str) else list(spec)
    if len(parts) != 2:
        raise ApiError("bad_region", f"region must be two corners like C3:G7, got '{spec}'")
    (x1, y1), (x2, y2) = [parse_coords(p, board_size) or (None, None) for p in parts]
    if x1 is None or x2 is None:
        raise ApiError("bad_region", "region corners cannot be pass")
    return [min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)]


def region_json(region):
    if not region:
        return None
    xmin, xmax, ymin, ymax = region
    return {"from": Move((xmin, ymin)).gtp(), "to": Move((xmax, ymax)).gtp()}


def gtp(coords):
    return Move(coords).gtp()


# -- board positions -------------------------------------------------------


def board_at(node):
    """A throwaway BaseGame holding the position at `node`, without touching the live game."""
    g = BaseGame.__new__(BaseGame)
    g._lock = threading.RLock()
    g.root = node.root
    g.current_node = node
    g._calculate_groups()
    return g


def check_moves_legal(node, moves):
    """Raise ApiError if the sequence cannot be played from `node`."""
    g = board_at(node)
    for i, move in enumerate(moves):
        try:
            g._validate_move_and_update_chains(move, ignore_ko=False)
        except IllegalMoveException as e:
            raise ApiError("illegal_move", f"{move.player} {move.gtp()} is illegal: {e}", index=i)


def board_json(node, ascii_board=True):
    g = board_at(node)
    szx, szy = node.board_size
    grid = [["." for _ in range(szx)] for _ in range(szy)]
    stones = {"B": [], "W": []}
    for m in g.stones:
        grid[m.coords[1]][m.coords[0]] = "X" if m.player == "B" else "O"
        stones[m.player].append(m.gtp())
    captured = {p: sum(1 for m in g.prisoners if m.player == p) for p in "BW"}
    result = {
        "node": node_ref(node),
        "size": [szx, szy],
        "next_player": node.next_player,
        "move_number": node.depth,
        "last_move": node.move.gtp() if node.move else None,
        "black_stones": sorted(stones["B"]),
        "white_stones": sorted(stones["W"]),
        "captures": {"B": captured["W"], "W": captured["B"]},  # stones each player has taken
        "rows": ["".join(grid[y]) for y in range(szy - 1, -1, -1)],  # top row first, X=black O=white
    }
    if ascii_board:
        cols = [Move.GTP_COORD[x] for x in range(szx)]
        lines = ["    " + " ".join(cols)]
        last = node.move.coords if node.move else None
        for y in range(szy - 1, -1, -1):
            cells = []
            for x in range(szx):
                c = grid[y][x]
                cells.append(c.lower() if last == (x, y) else c)
            lines.append(f"{y + 1:>3} " + " ".join(cells))
        result["ascii"] = "\n".join(lines)
    return result


# -- nodes and trees ------------------------------------------------------


def move_json(node):
    move = node.move
    return None if move is None else {"player": move.player, "move": move.gtp()}


def eval_json(node):
    """Short evaluation summary of a node, all numbers from Black's perspective."""
    if not node.analysis_exists:
        return None
    root = node.analysis["root"]
    out = {
        "visits": root.get("visits"),
        "black_winrate": _round(root.get("winrate"), 4),
        "black_score_lead": _round(root.get("scoreLead"), 2),
        "complete": node.analysis_complete,
    }
    if node.points_lost is not None:
        out["points_lost"] = _round(node.points_lost, 2)
    return out


def node_json(node, detail=False, with_eval=True):
    out = {
        "id": node_ref(node),
        "move_number": node.depth,
        "move": move_json(node),
        "next_player": node.next_player,
        "children": len(node.children),
    }
    if node.placements:
        out["placements"] = {
            "B": [m.gtp() for m in node.placements if m.player == "B"],
            "W": [m.gtp() for m in node.placements if m.player == "W"],
        }
    if with_eval:
        out["eval"] = eval_json(node)
    if detail:
        out["is_root"] = node.is_root
        out["on_main_line"] = on_main_line(node)
        out["parent"] = node_ref(node.parent) if node.parent else None
        out["child_ids"] = [node_ref(c) for c in node.ordered_children]
        out["note"] = node.note or None
        comments = node.properties.get("C")
        out["sgf_comment"] = "\n".join(comments) if comments else None
        out["properties"] = {k: list(v) for k, v in node.properties.items() if k not in ("KT",)}
    return out


def branch_json(node, max_len=None):
    """A variation starting at `node`: its moves along first children."""
    moves, n = [], node
    while True:
        if n.move:
            moves.append(n.move.gtp())
        if not n.children or (max_len and len(moves) >= max_len):
            break
        n = n.ordered_children[0]
    return {"start": node_ref(node), "first_move": move_json(node), "length": len(moves), "moves": moves}


def tree_json(node, max_depth=None, depth=0):
    """Nested tree. Linear stretches are collapsed into a single entry with a move list."""
    seq, n = [], node
    while True:
        seq.append(n)
        if len(n.children) != 1 or (max_depth is not None and depth + len(seq) > max_depth):
            break
        n = n.children[0]
    out = {
        "start": node_ref(seq[0]),
        "end": node_ref(seq[-1]),
        "first_move_number": seq[0].depth,
        "moves": [f"{s.move.player}:{s.move.gtp()}" if s.move else "-" for s in seq],
    }
    if seq[-1].children:
        if max_depth is not None and depth + len(seq) > max_depth:
            out["truncated"] = True
        else:
            out["variations"] = [tree_json(c, max_depth, depth + len(seq)) for c in seq[-1].ordered_children]
    return out


# -- analysis -------------------------------------------------------------


def _round(v, digits):
    return None if v is None else round(v, digits)


def candidate_json(cand, to_move, include_pv=True, pv_limit=None):
    sign = 1 if to_move == "B" else -1
    black_wr = cand.get("winrate")
    out = {
        "move": cand["move"],
        "order": cand.get("order"),
        "visits": cand.get("visits"),
        "winrate": _round(black_wr if sign == 1 else (None if black_wr is None else 1 - black_wr), 4),
        "score_lead": _round(None if cand.get("scoreLead") is None else sign * cand["scoreLead"], 2),
        "black_winrate": _round(black_wr, 4),
        "black_score_lead": _round(cand.get("scoreLead"), 2),
        "points_lost": _round(cand.get("pointsLost"), 2),
        "winrate_lost": _round(cand.get("winrateLost"), 4),
        "prior": _round(cand.get("prior"), 4),
    }
    if "scoreStdev" in cand:
        out["score_stdev"] = _round(cand["scoreStdev"], 2)
    if include_pv and cand.get("pv"):
        pv = cand["pv"][:pv_limit] if pv_limit else cand["pv"]
        out["pv"] = list(pv)
    return out


def ownership_rows(ownership, board_size):
    """KataGo ownership arrays already start at the top row (see utils.var_to_grid)."""
    if not ownership:
        return None
    szx, szy = board_size
    return [[round(v, 2) for v in ownership[r * szx : (r + 1) * szx]] for r in range(szy)]


def ownership_at(ownership, coords, board_size):
    szx, szy = board_size
    x, y = coords
    return ownership[(szy - 1 - y) * szx + x]


def analysis_json(node, top=None, include_pv=True, pv_limit=None, ownership=False, policy=False, moves=None):
    """Everything KaTrain knows about a node's analysis (reused from the GUI, no new search)."""
    to_move = node.next_player
    out = {
        "node": node_ref(node),
        "move_number": node.depth,
        "to_move": to_move,
        "available": node.analysis_exists,
        "complete": node.analysis_complete,
        "visits_requested": node.analysis_visits_requested,
        "perspective": "winrate/score_lead are for the player to move; black_* fields are from Black's view",
    }
    if not node.analysis_exists:
        return out
    root = node.analysis["root"]
    sign = 1 if to_move == "B" else -1
    out["root"] = {
        "visits": root.get("visits"),
        "winrate": _round(root["winrate"] if sign == 1 else 1 - root["winrate"], 4),
        "score_lead": _round(sign * root["scoreLead"], 2),
        "black_winrate": _round(root.get("winrate"), 4),
        "black_score_lead": _round(root.get("scoreLead"), 2),
        "score_stdev": _round(root.get("scoreStdev"), 2),
    }
    cands = node.candidate_moves
    if moves:
        wanted = {m.upper() for m in moves}
        cands = [c for c in cands if c["move"].upper() in wanted]
    elif top:
        cands = cands[:top]
    out["candidates"] = [candidate_json(c, to_move, include_pv, pv_limit) for c in cands]
    if node.move and node.parent and node.parent.analysis_exists:
        out["played_move"] = {
            "move": node.move.gtp(),
            "player": node.move.player,
            "points_lost": _round(node.points_lost, 2),
        }
    if ownership:
        out["ownership"] = ownership_rows(node.ownership, node.board_size)
        out["ownership_note"] = "rows top->bottom, +1 = Black owns, -1 = White owns"
    if policy and node.policy:
        ranking = node.policy_ranking or []
        out["policy_top"] = [{"move": m.gtp(), "prior": round(p, 4)} for p, m in ranking[:10]]
    return out
