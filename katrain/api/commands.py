"""Command implementations for the local API. Each runs inside the GUI process.

Commands registered with main=True run on the Kivy main thread (like a user click). Commands that wait for
KataGo (main=False) do their bookkeeping on the main thread via `on_main` and wait in the HTTP worker thread,
so the GUI never blocks.
"""

import copy
import itertools
import os
import threading
import time
import weakref
from datetime import datetime

from kivy.clock import Clock
from pysgf import Move

from katrain.api.model import (
    analysis_json,
    board_json,
    branch_json,
    check_moves_legal,
    gtp,
    line_nodes,
    main_line,
    node_id,
    node_json,
    node_ref,
    on_main_line,
    parse_color,
    parse_coords,
    parse_moves,
    parse_region,
    region_json,
    resolve_node,
    tree_json,
)
from katrain.api.overlay import MARK_TYPES
from katrain.api.overlay import parse_color as parse_mark_color
from katrain.api.protocol import DATA_FOLDER, ApiError
from katrain.core.constants import MODE_ANALYZE, MODE_PLAY, PRIORITY_EXTRA_ANALYSIS, REPORT_DT, STATUS_INFO
from katrain.core.game import KaTrainSGF

COMMANDS = {}
ENGINE_SETTINGS = {"max_visits": int, "fast_visits": int, "max_time": float, "wide_root_noise": float}
VIEW_TOGGLES = {
    "hints": "hints",
    "ownership": "ownership",
    "policy": "policy",
    "eval": "eval",
    "children": "show_children",
}


def command(name, main=True):
    def deco(fn):
        COMMANDS[name] = (fn, main, (fn.__doc__ or "").strip().split("\n")[0])
        return fn

    return deco


def on_main(fn, timeout=20):
    """Run fn on the Kivy main thread and return its result (re-raising its exception)."""
    if threading.current_thread() is threading.main_thread():
        return fn()
    done, box = threading.Event(), {}

    def task(_dt):
        try:
            box["result"] = fn()
        except BaseException as e:
            box["error"] = e
        finally:
            done.set()

    Clock.schedule_once(task, 0)
    if not done.wait(timeout):
        raise ApiError("gui_busy", f"GUI did not respond within {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["result"]


def _bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _int(a, key, default=None, minimum=None):
    v = a.get(key)
    if v is None or v == "":
        return default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ApiError("bad_argument", f"'{key}' must be an integer, got {v!r}")
    if minimum is not None and v < minimum:
        raise ApiError("bad_argument", f"'{key}' must be >= {minimum}")
    return v


class ApiState:
    """API-side state kept on the KaTrainGui object (marks, snapshots, demo, registered variations)."""

    def __init__(self):
        self.marks = []
        self.mark_ids = itertools.count(1)
        self.snapshots = {}
        self.demo = None
        self.created = weakref.WeakSet()  # nodes created through the API
        self.variations = {}
        self.variation_ids = itertools.count(1)
        self.tsumego = None


class CommandHandler:
    def __init__(self, katrain):
        self.katrain = katrain
        if not hasattr(katrain, "api_state"):
            katrain.api_state = ApiState()
        self.state = katrain.api_state

    # -- plumbing ---------------------------------------------------------

    def dispatch(self, name, args):
        entry = COMMANDS.get(name)
        if entry is None:
            raise ApiError("unknown_command", f"unknown command '{name}'", available=sorted(COMMANDS))
        fn, main, _ = entry
        if main:
            return on_main(lambda: fn(self, args))
        return fn(self, args)

    @property
    def game(self):
        game = self.katrain.game
        if game is None or not hasattr(game, "current_node"):
            raise ApiError("no_game", "no game loaded")
        if self.katrain.contributing:
            raise ApiError("contributing", "KaTrain is in distributed-training contribution mode")
        return game

    def node(self, a, key="node"):
        return resolve_node(self.game, a.get(key))

    def engine_for(self, node):
        engine = self.game.engines.get(node.next_player) if hasattr(self.game, "engines") else None
        if engine is None or not engine.check_alive():
            raise ApiError("engine_unavailable", "KataGo engine is not running", hint="check the engine status dot")
        return engine

    def refresh(self, redraw=True):
        self.katrain.update_state(redraw_board=redraw)
        self.katrain.board_gui.redraw_hover_contents_trigger()

    def goto(self, node):
        game = self.game
        if game.insert_mode:
            raise ApiError("insert_mode", "finish KaTrain's insert mode before navigating")
        self.katrain.board_gui.animating_pv = None
        game.set_current_node(node)
        self.refresh()

    def current_json(self, a=None):
        out = {"current": node_json(self.game.current_node)}
        if a and _bool(a.get("board")):
            out["board"] = board_json(self.game.current_node)
        return out

    def set_mode(self, mode):
        play_mode = self.katrain.play_mode
        if play_mode.mode != mode:
            button = play_mode.analyze if mode == MODE_ANALYZE else play_mode.play
            button.trigger_action(duration=0)

    def add_nodes(self, start, moves, analyze="fast"):
        """Create (or reuse) nodes for moves below `start`. Returns (nodes, created_count)."""
        check_moves_legal(start, moves)
        node, nodes, created = start, [], 0
        for move in moves:
            existing = next((c for c in node.children if c.move == move), None)
            node = existing or node.play(move)
            if existing is None:
                node_id(node)  # number new nodes in move order
                created += 1
                self.state.created.add(node)
                if analyze != "none":
                    engine = self.game.engines[node.next_player]
                    node.analyze(engine, analyze_fast=analyze == "fast")
            nodes.append(node)
        return nodes, created

    def edit_notice(self, created):
        if created and self.state.demo is None:
            return {
                "game_modified": True,
                "hint": "moves were added to the game record; use 'demo start' for throwaway lines",
            }
        return {"game_modified": bool(created), "in_demo": self.state.demo is not None}

    # -- snapshots --------------------------------------------------------

    def capture(self):
        game, k = self.game, self.katrain
        return {
            "root": game.root,
            "node": game.current_node,
            "mode": k.play_analyze_mode,
            "region": copy.copy(game.region_of_interest),
            "marks": copy.deepcopy(self.state.marks),
            "time": time.time(),
        }

    def restore(self, snap):
        game = self.game
        if game.root is not snap["root"]:
            raise ApiError("game_changed", "the snapshot belongs to a different game than the one loaded")
        node = snap["node"]
        tree = set(game.root.nodes_in_tree)
        while node not in tree and node.parent is not None:
            node = node.parent
        self.state.marks = copy.deepcopy(snap["marks"])
        self.goto(node)
        game.region_of_interest = copy.copy(snap["region"])
        self.set_mode(snap["mode"])
        self.refresh()

    @staticmethod
    def snapshot_json(name, snap):
        return {
            "name": name,
            "node": node_ref(snap["node"]),
            "move_number": snap["node"].depth,
            "mode": snap["mode"],
            "marks": len(snap["marks"]),
            "saved": datetime.fromtimestamp(snap["time"]).isoformat(timespec="seconds"),
        }

    # -- analysis plumbing (worker thread) --------------------------------

    def search(self, node, engine, visits, region=None, refine_move=None, timeout=300, max_time=None, ownership=None):
        """Send one KataGo query, feed results into the node like the GUI does, and wait for the final one."""
        if any(n.clear_placements for n in node.nodes_from_root):
            raise ApiError("unsupported_position", "positions with AE (cleared stones) cannot be analyzed")
        done, box = threading.Event(), {}

        def callback(result, partial):
            try:
                node.set_analysis(result, refine_move, False, region, partial)
            finally:
                if not partial:
                    box["result"] = result
                    done.set()

        def error_callback(result):
            box["error"] = result.get("error", str(result))
            done.set()

        engine.request_analysis(
            node,
            callback=callback,
            error_callback=error_callback,
            visits=visits,
            time_limit=False,
            next_move=refine_move,
            region_of_interest=region,
            priority=PRIORITY_EXTRA_ANALYSIS,
            report_every=None if refine_move else REPORT_DT,
            extra_settings={"maxTime": max_time} if max_time else None,
            ownership=ownership,
        )
        if not done.wait(timeout):
            raise ApiError(
                "timeout", f"analysis did not finish within {timeout}s", hint="raise --timeout or lower --visits"
            )
        if "error" in box:
            raise ApiError("engine_error", str(box["error"]))
        self.katrain.update_state()
        return box["result"]


# ---------------------------------------------------------------------------
# meta / reading
# ---------------------------------------------------------------------------


@command("commands")
def cmd_commands(h, a):
    """List available API commands."""
    return {name: {"help": doc, "waits_for_engine": not main} for name, (_, main, doc) in sorted(COMMANDS.items())}


@command("status")
def cmd_status(h, a):
    """Game info, current node, mode, engine state, demo state."""
    k, game = h.katrain, h.game
    root, cn = game.root, game.current_node
    engine = getattr(k, "engine", None)
    players = {}
    for bw in "BW":
        info = k.players_info[bw]
        players[bw] = {
            "name": root.get_property("P" + bw) or info.name or None,
            "rank": root.get_property(bw + "R"),
            "type": info.player_type,
            "subtype": info.player_subtype,
        }
    engine_info = None
    if engine is not None:
        engine_info = {
            "alive": engine.check_alive(),
            "idle": engine.is_idle(),
            "queries_remaining": engine.queries_remaining(),
            "model": os.path.basename(str(engine.config.get("model", ""))),
            **{key: engine.config.get(key) for key in ENGINE_SETTINGS},
        }
    demo = h.state.demo
    return {
        "game": {
            "board_size": list(game.board_size),
            "komi": game.komi,
            "rules": root.ruleset,
            "handicap": root.handicap,
            "players": players,
            "result": root.get_property("RE"),
            "sgf_filename": game.sgf_filename,
            "game_id": game.game_id,
            "nodes": len(root.nodes_in_tree),
            "main_line_moves": main_line(root)[-1].depth,
        },
        "current": node_json(cn, detail=False),
        "current_line_moves": line_nodes(cn)[-1].depth,
        "on_main_line": on_main_line(cn),
        "mode": k.play_analyze_mode,
        "pondering": k.pondering,
        "insert_mode": game.insert_mode,
        "region_of_interest": region_json(game.region_of_interest),
        "engine": engine_info,
        "demo": None if demo is None else {"started_at": node_ref(demo["snapshot"]["node"]), "label": demo["label"]},
        "pv_shown": k.board_gui.animating_pv is not None,
        "marks": len(h.state.marks),
        "tsumego": None if h.state.tsumego is None else h.state.tsumego.summary(),
    }


@command("board")
def cmd_board(h, a):
    """Stones on the board at a node (default: current)."""
    return board_json(h.node(a), ascii_board=_bool(a.get("ascii"), True))


@command("node")
def cmd_node(h, a):
    """Details of one node: move, children, comments, properties, evaluation."""
    return node_json(h.node(a), detail=True)


@command("history")
def cmd_history(h, a):
    """Moves from the root to a node; with future=true continue to the end of the line."""
    node = h.node(a)
    nodes = line_nodes(node) if _bool(a.get("future")) else node.nodes_from_root
    with_eval = _bool(a.get("eval"), True)
    cur = h.game.current_node
    out = []
    for n in nodes:
        entry = node_json(n, with_eval=with_eval)
        if n is cur:
            entry["current"] = True
        if len(n.children) > 1:
            entry["variations"] = len(n.children)
        out.append(entry)
    return {"node": node_ref(node), "nodes": out}


@command("tree")
def cmd_tree(h, a):
    """Game tree with variations (linear stretches collapsed)."""
    start = resolve_node(h.game, a.get("node") or "root")
    return {"current": node_ref(h.game.current_node), "tree": tree_json(start, _int(a, "max_depth"))}


@command("variations")
def cmd_variations(h, a):
    """Alternatives at a node (its children) and the branch points on its line."""
    node = h.node(a)
    branch_points = []
    for n in line_nodes(node):
        if len(n.children) > 1:
            branch_points.append(
                {
                    "node": node_ref(n),
                    "move_number": n.depth,
                    "branches": [branch_json(c, 8) for c in n.ordered_children],
                }
            )
    siblings = node.parent.ordered_children if node.parent else [node]
    return {
        "node": node_ref(node),
        "children": [branch_json(c, _int(a, "max_len", 20)) for c in node.ordered_children],
        "siblings": [node_ref(s) for s in siblings],
        "sibling_index": siblings.index(node),
        "branch_points_on_line": branch_points,
    }


# ---------------------------------------------------------------------------
# SGF
# ---------------------------------------------------------------------------


def _backup_game(h):
    """Save the current game before replacing it, so nothing is lost by an API import."""
    game = h.game
    if len(game.root.nodes_in_tree) <= 1:
        return None
    folder = os.path.join(DATA_FOLDER, "api_backups")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"backup_{datetime.now():%Y%m%d_%H%M%S_%f}.sgf")
    with open(path, "w", encoding="utf-8") as f:
        f.write(game.root.sgf())
    return path


def _load_tree(h, move_tree, fast, at_end, sgf_filename=None):
    backup = _backup_game(h)
    h.state.demo = None
    h.state.marks = []
    h.state.tsumego = None
    h.katrain._do_new_game(move_tree=move_tree, analyze_fast=fast, sgf_filename=sgf_filename)
    if at_end:
        h.katrain.game.redo(9999)
    h.refresh()
    return backup


@command("sgf.export")
def cmd_sgf_export(h, a):
    """Export the game as SGF text, or write it to 'path' (refuses to overwrite unless overwrite=true)."""
    game = h.game
    trainer = h.katrain.config("trainer", {})
    comments = _bool(a.get("comments"))
    sgf = game.root.sgf(
        save_comments_player={"B": True, "W": True} if comments else None,
        save_comments_class=trainer.get("save_feedback") if comments else None,
        eval_thresholds=trainer.get("eval_thresholds"),
        save_analysis=_bool(a.get("analysis")),
        save_marks=_bool(a.get("marks")),
    )
    path = a.get("path")
    if not path:
        return {"sgf": sgf}
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.exists(path) and not _bool(a.get("overwrite")):
        raise ApiError("file_exists", f"{path} exists", hint="pass overwrite=true to replace it")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(sgf)
    return {"path": path, "bytes": len(sgf.encode("utf-8"))}


@command("sgf.import")
def cmd_sgf_import(h, a):
    """Load an SGF (path or content) into the GUI. The previous game is backed up first."""
    fast, at_end = _bool(a.get("fast")), _bool(a.get("at_end"))
    try:
        if a.get("content"):
            tree, filename = KaTrainSGF.parse_sgf(a["content"]), None
        elif a.get("path"):
            filename = os.path.abspath(os.path.expanduser(a["path"]))
            tree = KaTrainSGF.parse_file(filename)
        else:
            raise ApiError("bad_argument", "give 'path' or 'content'")
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("sgf_parse_error", f"could not read SGF: {e}")
    backup = _load_tree(h, tree, fast, at_end, filename)
    return {"loaded": filename or "<content>", "previous_game_backup": backup, **cmd_status(h, {})}


@command("game.new")
def cmd_game_new(h, a):
    """Start a new empty game (size, komi, rules, handicap). The previous game is backed up first."""
    k = h.katrain
    size = str(a.get("size") or k.config("game/size"))
    komi = a.get("komi", k.config("game/komi"))
    rules = a.get("rules") or k.config("game/rules")
    tree = KaTrainSGF.parse_sgf(f"(;GM[1]FF[4]SZ[{size}]KM[{komi}]RU[{rules}])")
    handicap = _int(a, "handicap", 0)
    if handicap:
        tree.set_property("HA", handicap)
        tree.place_handicap_stones(handicap)
    backup = _load_tree(h, tree, fast=False, at_end=False)
    return {"previous_game_backup": backup, **cmd_status(h, {})}


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------


def _analysis_args(a):
    return dict(
        top=_int(a, "top", 10),
        include_pv=_bool(a.get("pv"), True),
        pv_limit=_int(a, "pv_limit"),
        ownership=_bool(a.get("ownership")),
        policy=_bool(a.get("policy")),
    )


@command("analysis.get")
def cmd_analysis_get(h, a):
    """Analysis KaTrain already has for a node (no new search)."""
    return analysis_json(h.node(a), moves=a.get("moves") or None, **_analysis_args(a))


@command("analysis.run", main=False)
def cmd_analysis_run(h, a):
    """Search a node to `visits` (reuses existing results if already deep enough) and return the analysis."""
    timeout = _int(a, "timeout", 300, minimum=1)

    def prepare():
        node = h.node(a)
        engine = h.engine_for(node)
        visits = _int(a, "visits", engine.config["max_visits"], minimum=1)
        region = a.get("region")
        if region == "gui":
            region = h.game.region_of_interest
        elif region:
            region = parse_region(region, node.board_size)
        cached = (
            not _bool(a.get("force"))
            and not region
            and node.analysis_complete
            and node.root_visits >= 0.95 * visits  # requested visits may have been cut short by max_time
        )
        return node, engine, visits, region, cached

    node, engine, visits, region, cached = on_main(prepare)
    if not cached:
        if not _bool(a.get("wait"), True):
            node.analyze(
                engine, visits=visits, priority=PRIORITY_EXTRA_ANALYSIS, time_limit=False, region_of_interest=region
            )
            return {"queued": True, "node": node_ref(node), "visits": visits}
        if region and not node.analysis_exists:  # region results don't carry a root evaluation
            h.search(node, engine, engine.config.get("fast_visits") or 25, timeout=timeout)
        max_time = float(a["max_time"]) if a.get("max_time") else None
        h.search(node, engine, visits, region=region, timeout=timeout, max_time=max_time)
    result = on_main(lambda: analysis_json(node, **_analysis_args(a)))
    result.update({"reused_existing": cached, "requested_visits": visits, "region": region_json(region)})
    return result


@command("analysis.moves", main=False)
def cmd_analysis_moves(h, a):
    """Search specific candidate moves at a node and compare them (points lost, PV)."""
    timeout = _int(a, "timeout", 300, minimum=1)

    def prepare():
        node = h.node(a)
        engine = h.engine_for(node)
        visits = _int(a, "visits", engine.config["max_visits"], minimum=1)
        raw = a.get("moves") or []
        moves = [m for m in (raw.replace(",", " ").split() if isinstance(raw, str) else raw)]
        if not moves:
            raise ApiError("bad_argument", "give at least one move to compare")
        parsed = [Move(parse_coords(m, node.board_size), player=node.next_player) for m in moves]
        for m in parsed:
            check_moves_legal(node, [m])
        return node, engine, visits, parsed

    node, engine, visits, moves = on_main(prepare)
    if not node.analysis_exists:
        h.search(node, engine, visits, timeout=timeout)
    errors = {}

    def run(move):
        try:
            h.search(node, engine, visits, refine_move=move, timeout=timeout)
        except ApiError as e:
            errors[move.gtp()] = e.to_json()

    threads = [threading.Thread(target=run, args=(m,), daemon=True) for m in moves]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result = on_main(lambda: analysis_json(node, moves=[m.gtp() for m in moves], **{**_analysis_args(a), "top": None}))
    result["candidates"].sort(key=lambda c: c["points_lost"] if c["points_lost"] is not None else 1e9)
    result["compared_visits"] = visits
    if errors:
        result["errors"] = errors
    return result


@command("analysis.game")
def cmd_analysis_game(h, a):
    """Queue whole-game (re)analysis in the background; poll with analysis.wait or status."""
    kwargs = {"mistakes_only": _bool(a.get("mistakes_only"))}
    if a.get("visits"):
        kwargs["visits"] = _int(a, "visits", minimum=1)
    if a.get("move_range"):
        lo, hi = [int(x) for x in str(a["move_range"]).replace(":", "-").split("-")]
        kwargs["move_range"] = [lo, hi]
    h.engine_for(h.game.current_node)
    h.game.analyze_extra("game", **kwargs)
    return {"queued": True, **kwargs, "queries_remaining": h.katrain.engine.queries_remaining()}


@command("analysis.wait", main=False)
def cmd_analysis_wait(h, a):
    """Wait until the engine has no pending queries (not while pondering)."""
    timeout = _int(a, "timeout", 300, minimum=1)
    engine = on_main(lambda: h.engine_for(h.game.current_node))
    if h.katrain.pondering:
        raise ApiError(
            "pondering", "continuous analysis is on, the engine never becomes idle", hint="analysis ponder off"
        )
    deadline = time.time() + timeout
    while not engine.is_idle():
        if time.time() > deadline:
            raise ApiError(
                "timeout", f"engine still busy after {timeout}s", queries_remaining=engine.queries_remaining()
            )
        time.sleep(0.25)
    return {"idle": True}


@command("analysis.ponder")
def cmd_analysis_ponder(h, a):
    """Turn continuous analysis of the current position on or off."""
    on = _bool(a.get("on"), True)
    if on != h.katrain.pondering:
        h.katrain.toggle_continuous_analysis(quiet=True)
    return {"pondering": h.katrain.pondering}


@command("analysis.stop")
def cmd_analysis_stop(h, a):
    """Stop pondering and cancel all pending KataGo queries."""
    h.game.analyze_extra("stop")
    h.refresh(False)
    return {"stopped": True}


@command("engine.get")
def cmd_engine_get(h, a):
    """Current search settings."""
    engine_config = h.katrain.config("engine")
    return {key: engine_config.get(key) for key in ENGINE_SETTINGS}


@command("engine.set")
def cmd_engine_set(h, a):
    """Change search settings (max_visits, fast_visits, max_time, wide_root_noise); save=true persists them."""
    engine_config = h.katrain.config("engine")
    changed = {}
    for key, typ in ENGINE_SETTINGS.items():
        if a.get(key) is not None:
            try:
                value = typ(a[key])
            except (TypeError, ValueError):
                raise ApiError("bad_argument", f"{key} must be {typ.__name__}")
            if value < 0:
                raise ApiError("bad_argument", f"{key} must be >= 0")
            engine_config[key] = value
            engine = h.katrain.engine
            if engine is not None and engine.config is not engine_config:
                engine.config[key] = value
            changed[key] = value
    if not changed:
        raise ApiError("bad_argument", f"nothing to change, use one of {sorted(ENGINE_SETTINGS)}")
    if _bool(a.get("save")):
        h.katrain.save_config("engine")
    return {"changed": changed, "saved": _bool(a.get("save")), **cmd_engine_get(h, {})}


# ---------------------------------------------------------------------------
# navigation and editing
# ---------------------------------------------------------------------------


@command("nav.goto")
def cmd_goto(h, a):
    """Jump to a node (move number, n<ID>, main:<N>, root, end)."""
    h.goto(h.node(a))
    return h.current_json(a)


@command("nav.next")
def cmd_next(h, a):
    """Step forward n moves along the current line."""
    if h.game.insert_mode:
        raise ApiError("insert_mode", "finish KaTrain's insert mode before navigating")
    h.katrain.board_gui.animating_pv = None
    h.game.redo(_int(a, "n", 1, minimum=1))
    h.refresh()
    return h.current_json(a)


@command("nav.prev")
def cmd_prev(h, a):
    """Step back n moves."""
    if h.game.insert_mode:
        raise ApiError("insert_mode", "finish KaTrain's insert mode before navigating")
    h.katrain.board_gui.animating_pv = None
    h.game.undo(_int(a, "n", 1, minimum=1))
    h.refresh()
    return h.current_json(a)


@command("nav.branch")
def cmd_branch(h, a):
    """Switch to the next (direction=1) or previous (-1) sibling variation, or a given index."""
    cn = h.game.current_node
    if cn.parent is None:
        raise ApiError("no_variation", "the root has no siblings")
    siblings = cn.parent.ordered_children
    if a.get("index") is not None:
        idx = _int(a, "index")
    else:
        idx = siblings.index(cn) + _int(a, "direction", 1)
    if not 0 <= idx < len(siblings):
        raise ApiError("no_variation", f"no sibling at index {idx}", siblings=len(siblings))
    h.goto(siblings[idx])
    return {**h.current_json(a), "sibling_index": idx, "siblings": len(siblings)}


@command("play")
def cmd_play(h, a):
    """Play moves from the current node (adds them to the game tree, analyzed like GUI moves)."""
    game, cn = h.game, h.game.current_node
    if game.insert_mode:
        raise ApiError("insert_mode", "finish KaTrain's insert mode first")
    start_player = parse_color(a.get("color")) or cn.next_player
    moves = parse_moves(a.get("moves") or [], cn.board_size, start_player)
    if not moves:
        raise ApiError("bad_argument", "no moves given")
    nodes, created = h.add_nodes(cn, moves, analyze="full" if _bool(a.get("analyze"), True) else "none")
    h.katrain.board_gui.animating_pv = None
    game.set_current_node(nodes[-1])
    h.refresh()
    return {
        **h.current_json(a),
        "played": [m.gtp() for m in moves],
        "nodes": [node_ref(n) for n in nodes],
        **h.edit_notice(created),
    }


@command("variation.add")
def cmd_variation_add(h, a):
    """Add a variation below a node without validating against the GUI state; goto=true jumps to its end."""
    start = h.node(a, "from")
    start_player = parse_color(a.get("color")) or start.next_player
    moves = parse_moves(a.get("moves") or [], start.board_size, start_player)
    if not moves:
        raise ApiError("bad_argument", "no moves given")
    nodes, created = h.add_nodes(start, moves, analyze=a.get("analyze", "fast"))
    if _bool(a.get("goto")):
        h.goto(nodes[-1])
    else:
        h.refresh()
    return {
        "from": node_ref(start),
        "nodes": [node_ref(n) for n in nodes],
        "end": node_ref(nodes[-1]),
        **h.edit_notice(created),
    }


@command("variation.delete")
def cmd_variation_delete(h, a):
    """Delete a node and everything below it. Nodes not created via the API need force=true."""
    game = h.game
    node = h.node(a)
    if node.parent is None:
        raise ApiError("bad_argument", "cannot delete the root")
    subtree = node.nodes_in_tree
    foreign = [n for n in subtree if n not in h.state.created]
    if foreign and not _bool(a.get("force")):
        raise ApiError(
            "protected",
            f"{len(foreign)} of {len(subtree)} nodes were not created through the API",
            hint="pass force=true to delete original game moves",
        )
    for n in subtree:
        n.remove_shortcut()
    if node in node.parent.children:
        node.parent.children.remove(node)
    if game.current_node in subtree:
        h.goto(node.parent)
    else:
        h.refresh()
    return {"deleted_nodes": len(subtree), **h.current_json()}


@command("variation.promote")
def cmd_variation_promote(h, a):
    """Make the line through a node the main line."""
    node = h.node(a)
    n = node
    while n.parent is not None:
        n.parent.children.remove(n)
        n.parent.children.insert(0, n)
        n = n.parent
    h.refresh()
    return {"promoted": node_ref(node), "on_main_line": on_main_line(node)}


@command("variation.register")
def cmd_variation_register(h, a):
    """Register a variation (moves from a node) under a short id like v1, without touching the board."""
    start = h.node(a, "from")
    start_player = parse_color(a.get("color")) or start.next_player
    moves = parse_moves(a.get("moves") or [], start.board_size, start_player)
    if not moves:
        raise ApiError("bad_argument", "no moves given")
    check_moves_legal(start, moves)
    vid = f"v{next(h.state.variation_ids)}"
    h.state.variations[vid] = {"from": start, "moves": moves, "label": a.get("label"), "root": h.game.root}
    return {
        "id": vid,
        "from": node_ref(start),
        "moves": [f"{m.player}:{m.gtp()}" for m in moves],
        "label": a.get("label"),
    }


@command("variation.list")
def cmd_variation_list(h, a):
    """Registered variations."""
    root = h.game.root
    return [
        {
            "id": vid,
            "from": node_ref(v["from"]),
            "moves": [f"{m.player}:{m.gtp()}" for m in v["moves"]],
            "label": v["label"],
            "current_game": v["root"] is root,
        }
        for vid, v in h.state.variations.items()
    ]


@command("variation.show")
def cmd_variation_show(h, a):
    """Show a registered variation: as=pv (overlay, default) or as=branch (creates nodes, goes to step)."""
    v = h.state.variations.get(str(a.get("id")))
    if v is None:
        raise ApiError("not_found", f"no registered variation '{a.get('id')}'")
    if v["root"] is not h.game.root:
        raise ApiError("game_changed", "this variation belongs to a different game")
    step = _int(a, "step")
    if a.get("as", "pv") == "branch":
        nodes, created = h.add_nodes(v["from"], v["moves"])
        target = nodes[min(step, len(nodes)) - 1] if step else nodes[-1]
        h.goto(target)
        return {**h.current_json(a), "nodes": [node_ref(n) for n in nodes], **h.edit_notice(created)}
    return _show_pv(h, v["from"], v["moves"], step)


# ---------------------------------------------------------------------------
# board display: PV overlays, marks, region, view toggles
# ---------------------------------------------------------------------------


def _show_pv(h, node, moves, step=None):
    """Overlay a sequence with numbered stones, without changing the game tree."""
    cn = h.game.current_node
    expected = node.next_player
    for m in moves:
        if m.player != expected:
            raise ApiError(
                "bad_argument",
                "PV display needs alternating colors starting with the player to move",
                hint="use 'variation add' for sequences with tenuki/passes by one side",
            )
        expected = Move.opponent_player(expected)
    check_moves_legal(node, moves)
    if node is not cn and node.parent is not cn and node not in cn.nodes_from_root:
        h.goto(node)  # draw_pv can only hide stones of descendants of the PV start
    board = h.katrain.board_gui
    pv = [m.gtp() for m in moves]
    board.animating_pv = (pv, node, time.time(), board.last_mouse_pos)
    board.animating_pv_index = None if step is None else max(0, min(step, len(pv)) - 1)
    board.pv_pinned = True
    board.redraw_hover_contents_trigger()
    return {
        "from": node_ref(node),
        "pv": pv,
        "shown_moves": len(pv) if step is None else board.animating_pv_index + 1,
        "animated": step is None,
    }


@command("pv.show")
def cmd_pv_show(h, a):
    """Overlay a variation as numbered stones (moves=..., or candidate=<move> to use KataGo's PV). step=N shows N moves."""
    node = h.node(a, "from")
    if a.get("candidate"):
        want = str(a["candidate"]).upper()
        cand = next((c for c in node.candidate_moves if c["move"].upper() == want), None)
        if cand is None or not cand.get("pv"):
            raise ApiError(
                "not_found",
                f"no analysed candidate {want} with a PV at {node_ref(node)}",
                hint="run analysis.moves first",
            )
        raw = cand["pv"]
    else:
        raw = a.get("moves") or []
    moves = parse_moves(raw, node.board_size, node.next_player)
    if not moves:
        raise ApiError("bad_argument", "give moves or candidate")
    return _show_pv(h, node, moves, _int(a, "step", minimum=1))


@command("pv.step")
def cmd_pv_step(h, a):
    """Step the shown PV: delta=+1/-1, or index=N (number of moves shown)."""
    board = h.katrain.board_gui
    if board.animating_pv is None:
        raise ApiError("no_pv", "no PV is being shown")
    pv = board.animating_pv[0]
    if a.get("index") is not None:
        shown = _int(a, "index")
    else:
        current = board.animating_pv_index if board.animating_pv_index is not None else len(pv) - 1
        shown = current + 1 + _int(a, "delta", 1)
    shown = max(1, min(shown, len(pv)))
    board.animating_pv_index = shown - 1
    board.pv_pinned = True
    board.redraw_hover_contents_trigger()
    return {"pv": pv, "shown_moves": shown, "last_shown": pv[shown - 1]}


@command("pv.hide")
def cmd_pv_hide(h, a):
    """Remove the PV overlay."""
    board = h.katrain.board_gui
    board.animating_pv = None
    board.pv_pinned = False
    board.redraw_hover_contents_trigger()
    return {"hidden": True}


def _mark_json(m):
    out = {k: v for k, v in m.items() if k not in ("rgb", "coords", "region", "node")}
    if "coords" in m:
        out["point"] = gtp(m["coords"])
    if "region" in m:
        out["region"] = region_json(m["region"])
    out["node"] = None if m.get("node") is None else f"n{m['node']}"
    return out


@command("mark.add")
def cmd_mark_add(h, a):
    """Mark points: type=circle|square|triangle|cross|label|dot, text for labels, color, scope=node|global."""
    game = h.game
    kind = a.get("type", "circle")
    if kind not in MARK_TYPES or kind == "area":
        raise ApiError("bad_argument", f"type must be one of {[t for t in MARK_TYPES if t != 'area']}")
    rgb = parse_mark_color(a.get("color"))
    if rgb is None:
        raise ApiError("bad_argument", f"unknown color {a.get('color')!r} (named color or #rrggbb)")
    points = a.get("points") or []
    if isinstance(points, str):
        points = points.replace(",", " ").split()
    if not points:
        raise ApiError("bad_argument", "no points given")
    texts = a.get("text")
    if isinstance(texts, str):
        texts = [texts] if len(points) == 1 else texts.split()
    scope_node = None if a.get("scope") == "global" else node_id(game.current_node)
    added = []
    for i, p in enumerate(points):
        coords = parse_coords(p, game.board_size)
        if coords is None:
            raise ApiError("bad_coordinate", "cannot mark pass")
        text = None
        if kind == "label":
            text = texts[i] if texts and i < len(texts) else chr(ord("A") + i % 26)
        # replace an existing mark at the same point and scope
        h.state.marks = [m for m in h.state.marks if not (m.get("coords") == coords and m.get("node") == scope_node)]
        mark = {
            "id": next(h.state.mark_ids),
            "type": kind,
            "coords": coords,
            "rgb": rgb,
            "color": a.get("color", "red"),
            "text": text,
            "node": scope_node,
        }
        h.state.marks.append(mark)
        added.append(_mark_json(mark))
    h.katrain.board_gui.redraw_hover_contents_trigger()
    return {"added": added}


@command("mark.area")
def cmd_mark_area(h, a):
    """Highlight a rectangular area, e.g. region=C3:G7, with optional label text and color."""
    game = h.game
    rgb = parse_mark_color(a.get("color") or "yellow")
    if rgb is None:
        raise ApiError("bad_argument", f"unknown color {a.get('color')!r}")
    region = parse_region(a.get("region") or "", game.board_size)
    scope_node = None if a.get("scope") == "global" else node_id(game.current_node)
    mark = {
        "id": next(h.state.mark_ids),
        "type": "area",
        "region": region,
        "rgb": rgb,
        "color": a.get("color") or "yellow",
        "text": a.get("text"),
        "node": scope_node,
    }
    h.state.marks.append(mark)
    h.katrain.board_gui.redraw_hover_contents_trigger()
    return {"added": [_mark_json(mark)]}


@command("mark.list")
def cmd_mark_list(h, a):
    """All API marks."""
    return [_mark_json(m) for m in h.state.marks]


@command("mark.clear")
def cmd_mark_clear(h, a):
    """Remove marks: all (default), ids=[...], or points=[...]."""
    before = len(h.state.marks)
    ids, points = a.get("ids"), a.get("points")
    if ids:
        ids = {int(str(i).lstrip("#")) for i in (ids if isinstance(ids, list) else str(ids).split())}
        h.state.marks = [m for m in h.state.marks if m["id"] not in ids]
    elif points:
        points = points.replace(",", " ").split() if isinstance(points, str) else points
        coords = {parse_coords(p, h.game.board_size) for p in points}
        h.state.marks = [m for m in h.state.marks if m.get("coords") not in coords]
    else:
        h.state.marks = []
    h.katrain.board_gui.redraw_hover_contents_trigger()
    return {"removed": before - len(h.state.marks), "remaining": len(h.state.marks)}


@command("region.set")
def cmd_region_set(h, a):
    """Restrict KataGo's candidate moves to a region (KaTrain's region of interest), e.g. C3:G7."""
    game = h.game
    region = parse_region(a.get("region") or "", game.board_size)
    game.set_region_of_interest([region[0], region[1], region[2], region[3]])
    h.refresh(False)
    return {"region_of_interest": region_json(game.region_of_interest)}


@command("region.clear")
def cmd_region_clear(h, a):
    """Remove the region of interest."""
    h.game.region_of_interest = None
    h.refresh(False)
    return {"region_of_interest": None}


@command("view.set")
def cmd_view_set(h, a):
    """Toggle display layers: hints, ownership, policy, eval, children, move_numbers (true/false)."""
    controls = h.katrain.analysis_controls
    changed = {}
    for key, widget_id in VIEW_TOGGLES.items():
        if a.get(key) is not None:
            controls.ids[widget_id].checkbox.active = _bool(a[key])
            changed[key] = _bool(a[key])
    if a.get("move_numbers") is not None:
        h.katrain.show_move_num = _bool(a["move_numbers"])
        changed["move_numbers"] = h.katrain.show_move_num
    h.refresh()
    state = {key: controls.ids[widget_id].active for key, widget_id in VIEW_TOGGLES.items()}
    state["move_numbers"] = h.katrain.show_move_num
    return {"changed": changed, "view": state}


@command("mode.set")
def cmd_mode_set(h, a):
    """Switch between play and analyze mode."""
    mode = a.get("mode")
    if mode not in (MODE_PLAY, MODE_ANALYZE):
        raise ApiError("bad_argument", f"mode must be '{MODE_PLAY}' or '{MODE_ANALYZE}'")
    h.set_mode(mode)
    return {"mode": h.katrain.play_analyze_mode}


@command("message")
def cmd_message(h, a):
    """Show a short line in KaTrain's status bar."""
    text = str(a.get("text") or "")[:300]
    h.katrain.controls.set_status(text, STATUS_INFO, check_level=False)
    return {"shown": text}


# ---------------------------------------------------------------------------
# snapshots and demo mode
# ---------------------------------------------------------------------------


@command("state.save")
def cmd_state_save(h, a):
    """Remember the current position, mode, region and marks under a name."""
    name = str(a.get("name") or "default")
    h.state.snapshots[name] = h.capture()
    return h.snapshot_json(name, h.state.snapshots[name])


@command("state.restore")
def cmd_state_restore(h, a):
    """Return to a saved state."""
    name = str(a.get("name") or "default")
    snap = h.state.snapshots.get(name)
    if snap is None:
        raise ApiError("not_found", f"no saved state '{name}'", available=sorted(h.state.snapshots))
    h.restore(snap)
    return {"restored": name, **h.current_json(a)}


@command("state.list")
def cmd_state_list(h, a):
    """Saved states."""
    return [h.snapshot_json(name, snap) for name, snap in h.state.snapshots.items()]


@command("demo.start")
def cmd_demo_start(h, a):
    """Begin a temporary demonstration: everything added until demo.end is removed again (unless keep=true)."""
    if h.state.demo is not None:
        raise ApiError("demo_active", "a demo is already running", hint="demo end first")
    game = h.game
    h.state.demo = {
        "snapshot": h.capture(),
        "existing": set(game.root.nodes_in_tree),
        "label": a.get("label"),
    }
    h.set_mode(MODE_ANALYZE)  # no AI replies or teaching undos while demonstrating
    return {"demo": "started", "label": a.get("label"), **h.current_json(a)}


@command("demo.end")
def cmd_demo_end(h, a):
    """End the demo: drop nodes added during it (keep=true keeps them) and restore the saved position."""
    demo = h.state.demo
    if demo is None:
        raise ApiError("no_demo", "no demo is running")
    h.state.demo = None
    game = h.game
    snap = demo["snapshot"]
    board = h.katrain.board_gui
    board.animating_pv, board.pv_pinned = None, False
    if game.root is not snap["root"]:
        return {"demo": "ended", "warning": "a different game was loaded during the demo; nothing restored"}
    removed = 0
    if not _bool(a.get("keep")):
        existing = demo["existing"]
        for node in [n for n in game.root.nodes_in_tree if n not in existing and n.parent in existing]:
            for n in node.nodes_in_tree:
                n.remove_shortcut()
                removed += 1
            node.parent.children.remove(node)
    h.restore(snap)
    return {"demo": "ended", "removed_nodes": removed, "kept": _bool(a.get("keep")), **h.current_json(a)}


@command("demo.status")
def cmd_demo_status(h, a):
    """Whether a demo is running and where it started."""
    demo = h.state.demo
    if demo is None:
        return {"active": False}
    added = [n for n in h.game.root.nodes_in_tree if n not in demo["existing"]]
    return {
        "active": True,
        "label": demo["label"],
        "started_at": node_ref(demo["snapshot"]["node"]),
        "added_nodes": len(added),
    }
