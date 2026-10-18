"""Life-and-death problem mode built on KaTrain's tsumego frame and region-of-interest search.

The solver may play any legal move; KataGo answers inside the problem region instead of following a fixed answer
tree. The SGF's own solution tree, when present, is kept aside as a reference and never shown unless asked for.
"""

import copy
import os
import re

from pysgf import Move

from katrain.api.commands import _bool, _int, command, on_main
from katrain.api.model import (
    board_at,
    check_moves_legal,
    gtp,
    node_ref,
    ownership_at,
    parse_coords,
    parse_region,
    region_json,
)
from katrain.api.protocol import ApiError
from katrain.core.game import KaTrainSGF
from katrain.core.game_node import GameNode

LEVEL_WORDS = (
    "elementary",
    "beginner",
    "easy",
    "intermediate",
    "medium",
    "advanced",
    "hard",
    "expert",
    "入门",
    "初级",
    "中级",
    "高级",
    "初段",
    "有段",
)
ALIVE, DEAD = 0.5, -0.5


# -- problem collections --------------------------------------------------------


def split_collection(text):
    """Split an SGF collection '(;...)(;...)' into its top-level game trees."""
    trees, depth, start, in_value, escape = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_value:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "]":
                in_value = False
        elif ch == "[":
            in_value = True
        elif ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                trees.append(text[start : i + 1])
                start = None
    return trees


def _prop(text, name):
    m = re.search(rf"(?<![A-Z]){name}\[((?:[^\]\\]|\\.)*)\]", text)
    return m[1] if m else None


def _read(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue


def _difficulty(text, path):
    di = _prop(text, "DI")
    if di:
        return di
    hay = f"{path} {_prop(text, 'GN') or ''} {_prop(text, 'GC') or ''}".lower()
    m = re.search(r"(\d{1,2}\s?[kd])(?![a-z])", hay)
    if m:
        return m[1].replace(" ", "")
    return next((w for w in LEVEL_WORDS if w in hay), None)


def scan_problems(source):
    """All problems in a file or folder, as metadata dicts (in a stable order)."""
    source = os.path.abspath(os.path.expanduser(source))
    if os.path.isdir(source):
        files = sorted(os.path.join(d, f) for d, _, fs in os.walk(source) for f in fs if f.lower().endswith(".sgf"))
        base = source
    elif os.path.isfile(source):
        files, base = [source], os.path.dirname(source)
    else:
        raise ApiError("not_found", f"{source} does not exist")
    problems = []
    for path in files:
        text = _read(path)
        if text is None:
            continue
        rel = os.path.relpath(path, base)
        trees = split_collection(text) or [text]
        for j, tree in enumerate(trees):
            problems.append(
                {
                    "file": path,
                    "tree_index": j,
                    "set": os.path.dirname(rel) or os.path.splitext(os.path.basename(path))[0],
                    "name": _prop(tree, "GN")
                    or (os.path.splitext(os.path.basename(path))[0] + (f"#{j + 1}" if len(trees) > 1 else "")),
                    "difficulty": _difficulty(tree, rel),
                    "to_play": _prop(tree, "PL"),
                }
            )
    return problems


def filter_problems(problems, set_filter=None, difficulty=None):
    out = problems
    if set_filter:
        out = [p for p in out if set_filter.lower() in (p["set"] + "/" + p["name"]).lower()]
    if difficulty:
        out = [p for p in out if p["difficulty"] and p["difficulty"].lower() == difficulty.lower()]
    return out


def _goal_from_text(text):
    t = (text or "").lower()
    if any(w in t for w in ("kill", "杀", "净死")):
        return "kill"
    if any(w in t for w in ("live", "活", "make life")):
        return "live"
    return None


def _comment(node):
    """KaTrain moves SGF C[] comments into node.note, so read both."""
    return " ".join([node.note or ""] + node.properties.get("C", []))


def _line_label(node):
    """RIGHT/WRONG label of a solution line, from common SGF conventions."""
    for n in [node] + _first_line(node):
        text = _comment(n).lower()
        if "BM" in n.properties or any(w in text for w in ("wrong", "incorrect", "失败", "错误")):
            return "wrong"
        if "TE" in n.properties or any(w in text for w in ("right", "correct", "正解", "正确")):
            return "correct"
    return None


def local_pv(pv, region):
    """Cut a PV at the first move outside the problem region (later moves are usually frame noise)."""
    if not region:
        return list(pv)
    xmin, xmax, ymin, ymax = region
    out = []
    for move in pv:
        coords = Move.from_gtp(move).coords
        if coords is not None and not (xmin <= coords[0] <= xmax and ymin <= coords[1] <= ymax):
            break
        out.append(move)
    while len(out) >= 2 and out[-1] == "pass" and out[-2] == "pass":
        out.pop()
    return out


def _first_line(node):
    line = []
    while node.children:
        node = node.children[0]
        line.append(node)
    return line


class TsumegoState:
    def __init__(self, meta, solution_root, problem_root, start_node, region, to_play):
        self.meta = meta
        self.solution_root = solution_root  # full SGF tree incl. answer branches (hidden)
        self.problem_root = problem_root
        self.start = start_node
        self.region = region
        self.to_play = to_play
        self.goal = _goal_from_text(
            " ".join(
                [_comment(solution_root)]
                + solution_root.properties.get("GC", [])
                + solution_root.properties.get("GN", [])
            )
        )
        self.group = None  # coords of a stone in the target group
        self.evals = {}  # node -> summarised raw KataGo result
        self.attempts = 0

    def summary(self):
        return {
            "problem": {k: v for k, v in self.meta.items() if k != "tree_index"},
            "start": node_ref(self.start),
            "to_play": self.to_play,
            "goal": self.goal,
            "target_group": gtp(self.group) if self.group else None,
            "region": region_json(self.region),
            "has_reference_solution": bool(self.solution_root.children),
            "attempts": self.attempts,
        }


def _ts(h):
    ts = h.state.tsumego
    if ts is None:
        raise ApiError("no_tsumego", "no problem loaded", hint="katrain tsumego load <file-or-folder>")
    if h.game.root is not ts.problem_root:
        h.state.tsumego = None
        raise ApiError("no_tsumego", "the problem was replaced by another game")
    return ts


# -- evaluation --------------------------------------------------------------------


def _target_chain(node, coords):
    g = board_at(node)
    cid = g.board[coords[1]][coords[0]]
    return None if cid < 0 else g.chains[cid]


def _default_group(ts, node):
    """Largest chain inside the region that the goal is about."""
    if not ts.goal:
        return None
    color = ts.to_play if ts.goal == "live" else Move.opponent_player(ts.to_play)
    g = board_at(node)
    xmin, xmax, ymin, ymax = ts.region or [0, node.board_size[0] - 1, 0, node.board_size[1] - 1]
    chains = [
        c
        for c in g.chains
        if c and c[0].player == color and any(xmin <= m.coords[0] <= xmax and ymin <= m.coords[1] <= ymax for m in c)
    ]
    return max(chains, key=len)[0].coords if chains else None


def _summarise(result, node):
    root = result["rootInfo"]
    moves = sorted(result.get("moveInfos", []), key=lambda m: m.get("order", 99))
    return {
        "black_winrate": root["winrate"],
        "black_score": root["scoreLead"],
        "visits": root["visits"],
        "ownership": result.get("ownership"),
        "moves": [
            {
                "move": m["move"],
                "visits": m["visits"],
                "black_winrate": m["winrate"],
                "black_score": m["scoreLead"],
                "pv": m.get("pv", []),
            }
            for m in moves
        ],
        "to_move": node.next_player,
    }


def _evaluate(h, ts, node, visits, timeout):
    if node in ts.evals and ts.evals[node]["visits"] >= visits * 0.9:
        return ts.evals[node]
    engine = on_main(lambda: h.engine_for(node))
    raw = h.search(node, engine, visits, region=ts.region, timeout=timeout, ownership=True)
    ts.evals[node] = _summarise(raw, node)
    return ts.evals[node]


def _group_status(ts, node, ev):
    if not ts.group:
        return None
    chain = _target_chain(node, ts.group)
    if chain is None:
        return {"status": "captured", "stones": 0}
    color = chain[0].player
    sign = 1 if color == "B" else -1
    own = ev.get("ownership")
    if not own:
        return {"status": "unknown", "stones": len(chain)}
    avg = sum(sign * ownership_at(own, m.coords, node.board_size) for m in chain) / len(chain)
    status = "alive" if avg >= ALIVE else "dead" if avg <= DEAD else "unsettled"
    return {"status": status, "color": color, "stones": len(chain), "ownership": round(avg, 2)}


def _solver_view(ts, ev):
    sign = 1 if ts.to_play == "B" else -1
    wr = ev["black_winrate"] if sign == 1 else 1 - ev["black_winrate"]
    return round(wr, 4), round(sign * ev["black_score"], 2)


def _goal_met(ts, status):
    if not status or not ts.goal:
        return None
    if ts.goal == "kill":
        return status["status"] in ("dead", "captured")
    return status["status"] == "alive"


def _candidates(ts, ev, n=5):
    sign = 1 if ev["to_move"] == "B" else -1
    return [
        {
            "move": m["move"],
            "visits": m["visits"],
            "winrate": round(m["black_winrate"] if sign == 1 else 1 - m["black_winrate"], 4),
            "pv": local_pv(m["pv"], ts.region)[:12],
        }
        for m in ev["moves"][:n]
    ]


# -- commands ----------------------------------------------------------------------


@command("tsumego.list", main=False)
def cmd_tsumego_list(h, a):
    """List problems in an SGF file/folder, optionally filtered by set and difficulty."""
    problems = filter_problems(scan_problems(a.get("source") or ""), a.get("set"), a.get("difficulty"))
    limit = _int(a, "limit", 200, minimum=1)
    sets, levels = {}, {}
    for p in problems:
        sets[p["set"]] = sets.get(p["set"], 0) + 1
        levels[str(p["difficulty"])] = levels.get(str(p["difficulty"]), 0) + 1
    return {
        "total": len(problems),
        "sets": sets,
        "difficulties": levels,
        "problems": [
            {"index": i, **{k: v for k, v in p.items() if k != "tree_index"}} for i, p in enumerate(problems[:limit])
        ],
    }


@command("tsumego.load")
def cmd_tsumego_load(h, a):
    """Load problem #index (after filters), hide its answer tree, add KaTrain's tsumego frame and local region."""
    from katrain.api.commands import _load_tree

    problems = filter_problems(scan_problems(a.get("source") or ""), a.get("set"), a.get("difficulty"))
    index = _int(a, "index", 0, minimum=0)
    if index >= len(problems):
        raise ApiError("not_found", f"problem index {index} out of range ({len(problems)} problems)")
    meta = problems[index]
    text = _read(meta["file"])
    trees = split_collection(text) or [text]
    try:
        solution_root = KaTrainSGF.parse_sgf(trees[meta["tree_index"]])
    except Exception as e:
        raise ApiError("sgf_parse_error", f"{meta['file']}: {e}")
    to_play = solution_root.initial_player
    props = copy.deepcopy(dict(solution_root.properties))
    props.pop("C", None)  # comments often spoil the answer
    props["PL"] = [to_play]
    problem_root = GameNode(properties=props)
    if not problem_root.placements:
        raise ApiError("bad_problem", "problem has no setup stones")
    backup = _load_tree(h, problem_root, fast=False, at_end=False)
    game = h.katrain.game
    if _bool(a.get("frame"), True):
        h.katrain._do_tsumego_frame(_bool(a.get("ko")), _int(a, "margin", 4, minimum=1))
    start = game.current_node
    region = game.region_of_interest
    ts = TsumegoState(
        {**meta, "index": index, "source": a.get("source")}, solution_root, game.root, start, region, to_play
    )
    h.state.tsumego = ts
    if ts.goal:
        ts.group = _default_group(ts, start)
    h.refresh()
    return {**ts.summary(), "previous_game_backup": backup}


@command("tsumego.target")
def cmd_tsumego_target(h, a):
    """Set goal (kill/live, for the player to move), target group stone and local search region."""
    ts = _ts(h)
    size = ts.start.board_size
    if a.get("goal"):
        if a["goal"] not in ("kill", "live"):
            raise ApiError("bad_argument", "goal must be kill or live")
        ts.goal = a["goal"]
    if a.get("region"):
        ts.region = parse_region(a["region"], size)
        h.game.set_region_of_interest(list(ts.region))
        ts.evals.clear()
    if a.get("group"):
        coords = parse_coords(a["group"], size)
        if coords is None or _target_chain(ts.start, coords) is None:
            raise ApiError("bad_argument", f"no stone at {a['group']} in the starting position")
        ts.group = coords
    elif a.get("goal") and not ts.group:
        ts.group = _default_group(ts, ts.start)
    h.refresh(False)
    return ts.summary()


@command("tsumego.eval", main=False)
def cmd_tsumego_eval(h, a):
    """Local KataGo evaluation of the current problem position: target status and best local moves."""
    ts = on_main(lambda: _ts(h))
    node = h.game.current_node
    visits = _int(a, "visits", h.katrain.config("engine").get("max_visits", 500), minimum=1)
    ev = _evaluate(h, ts, node, visits, _int(a, "timeout", 300))
    status = _group_status(ts, node, ev)
    wr, score = _solver_view(ts, ev)
    return {
        "node": node_ref(node),
        "to_move": node.next_player,
        "solver": ts.to_play,
        "solver_winrate": wr,
        "solver_score": score,
        "target": status,
        "goal_met": _goal_met(ts, status),
        "best_moves": _candidates(ts, ev),
    }


@command("tsumego.try", main=False)
def cmd_tsumego_try(h, a):
    """Play a move in the problem; KataGo evaluates it and (reply=true) answers with its best local move."""
    timeout = _int(a, "timeout", 300)

    def prepare():
        ts = _ts(h)
        cn = h.game.current_node
        if ts.start is not cn and ts.start not in cn.nodes_from_root:
            raise ApiError("outside_problem", "current position is not part of the problem", hint="tsumego reset")
        move = Move(parse_coords(a.get("move") or "", cn.board_size), player=cn.next_player)
        check_moves_legal(cn, [move])
        return ts, cn, move

    ts, cn, move = on_main(prepare)
    visits = _int(a, "visits", h.katrain.config("engine").get("max_visits", 500), minimum=1)
    before = _evaluate(h, ts, cn, visits, timeout)
    node = on_main(lambda: _play(h, cn, move))
    after = _evaluate(h, ts, node, visits, timeout)
    ts.attempts += 1

    mover_sign = 1 if move.player == "B" else -1
    wr_before = before["black_winrate"] if mover_sign == 1 else 1 - before["black_winrate"]
    wr_after = after["black_winrate"] if mover_sign == 1 else 1 - after["black_winrate"]
    best = before["moves"][0] if before["moves"] else None
    status = _group_status(ts, node, after)
    result = {
        "move": f"{move.player}:{move.gtp()}",
        "node": node_ref(node),
        "mover_winrate_before": round(wr_before, 4),
        "mover_winrate_after": round(wr_after, 4),
        "winrate_lost": round(wr_before - wr_after, 4),
        "score_lost": round(mover_sign * (before["black_score"] - after["black_score"]), 2),
        "best_move": best and best["move"],
        "was_best_move": bool(best and best["move"] == move.gtp()),
        "target": status,
        "goal_met": _goal_met(ts, status) if move.player == ts.to_play else None,
        "better_moves": _candidates(ts, before, 3),
        "refutation_pv": local_pv(after["moves"][0]["pv"], ts.region)[:12] if after["moves"] else [],
    }
    if _bool(a.get("reply"), True) and after["moves"]:
        reply = Move.from_gtp(after["moves"][0]["move"], player=node.next_player)
        if not reply.is_pass:
            reply_node = on_main(lambda: _play(h, node, reply, analyze=True))
            result["reply"] = {"move": f"{reply.player}:{reply.gtp()}", "node": node_ref(reply_node)}
        else:
            result["reply"] = {"move": f"{reply.player}:pass", "node": None}
    return result


def _play(h, parent, move, analyze=False):
    nodes, _ = h.add_nodes(parent, [move], analyze="none")
    node = nodes[0]
    if analyze:
        ts = h.state.tsumego
        node.analyze(h.game.engines[node.next_player], region_of_interest=ts.region if ts else None)
    h.goto(node)
    return node


@command("tsumego.reset")
def cmd_tsumego_reset(h, a):
    """Back to the starting position; attempt moves are removed."""
    ts = _ts(h)
    for child in list(ts.start.children):
        if all(n in h.state.created for n in child.nodes_in_tree):
            ts.start.children.remove(child)
    h.goto(ts.start)
    ts.evals = {n: v for n, v in ts.evals.items() if n is ts.start}
    ts.attempts = 0
    h.state.marks = []
    return ts.summary()


@command("tsumego.solution", main=False)
def cmd_tsumego_solution(h, a):
    """Reference answer lines from the SGF (labelled correct/wrong when marked) plus KataGo's local line."""
    ts = on_main(lambda: _ts(h))
    lines = []
    for child in ts.solution_root.children:
        nodes = [child] + _first_line(child)
        lines.append(
            {"moves": [f"{n.move.player}:{n.move.gtp()}" for n in nodes if n.move], "label": _line_label(child)}
        )
    visits = _int(a, "visits", h.katrain.config("engine").get("max_visits", 500), minimum=1)
    ev = _evaluate(h, ts, ts.start, visits, _int(a, "timeout", 300))
    katago_line = local_pv(ev["moves"][0]["pv"], ts.region)[:16] if ev["moves"] else []
    out = {"reference_lines": lines, "katago_line": katago_line, "start": node_ref(ts.start)}
    if _bool(a.get("show")):
        preferred = next((ln for ln in lines if ln["label"] == "correct"), lines[0] if lines else None)
        moves = [m.split(":")[1] for m in preferred["moves"]] if preferred else katago_line

        def show():
            from katrain.api.commands import _show_pv
            from katrain.api.model import parse_moves

            h.goto(ts.start)
            parsed = parse_moves(moves, ts.start.board_size, ts.start.next_player)
            return _show_pv(h, ts.start, parsed)

        try:
            out["shown"] = on_main(show)
        except ApiError as e:
            out["show_error"] = e.to_json()
    return out


@command("tsumego.status")
def cmd_tsumego_status(h, a):
    """Current problem, goal, target and attempts."""
    ts = h.state.tsumego
    if ts is None:
        return {"active": False}
    return {"active": True, **_ts(h).summary(), "current": node_ref(h.game.current_node)}
