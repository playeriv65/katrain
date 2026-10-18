"""`katrain` command line entry point.

Without a subcommand (or with an .sgf / config.json path) it starts the GUI exactly as before.
With a subcommand it talks to a running GUI over the local API and prints JSON.
Must not import Kivy or katrain.core at module level: the CLI has to start fast.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from katrain.api.protocol import COMMAND_PATH, DATA_FOLDER, HEALTH_PATH, TOKEN_HEADER, read_discovery

EXIT_OK, EXIT_API_ERROR, EXIT_USAGE, EXIT_NOT_RUNNING = 0, 1, 2, 3
ANALYSIS_TIMEOUT = 300


class NotRunning(Exception):
    pass


# -- transport ----------------------------------------------------------------

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost via a proxy


def _connection(port=None):
    info = read_discovery()
    if not info:
        raise NotRunning("KaTrain GUI is not running (no ~/.katrain/api.json). Start it with: katrain gui")
    return f"http://127.0.0.1:{port or info['port']}", info["token"]


def _request(path, payload=None, port=None, timeout=60):
    base, token = _connection(port)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={TOKEN_HEADER: token, "Content-Type": "application/json"}
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": {"code": f"http_{e.code}", "message": str(e)}}
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return {"ok": False, "error": {"code": "client_timeout", "message": f"no answer within {timeout}s"}}
        raise NotRunning(f"KaTrain GUI is not reachable ({reason}). Start it with: katrain gui")


def call(command, args=None, port=None, timeout=60):
    """Execute one API command; returns the response envelope {'ok': ..., 'result'|'error': ...}."""
    args = {k: v for k, v in (args or {}).items() if v is not None}
    return _request(COMMAND_PATH, {"command": command, "args": args}, port=port, timeout=timeout)


def health(port=None, timeout=3):
    return _request(HEALTH_PATH, port=port, timeout=timeout)


# -- launching the GUI ---------------------------------------------------------


def _older_user_config():
    """Error dict if ~/.katrain/config.json belongs to an older KaTrain (e.g. an installed release next to this
    source build): KaTrain would replace that file with its package defaults on startup."""
    from katrain.core.constants import CONFIG_MIN_VERSION

    path = os.path.join(DATA_FOLDER, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            version = json.load(f)["general"]["version"]
        older = [int(v) for v in version.split(".")] < [int(v) for v in CONFIG_MIN_VERSION.split(".")]
    except (OSError, ValueError, KeyError, TypeError):
        return None  # missing or unreadable: KaTrain creates or repairs it, nothing to lose
    if not older:
        return None
    return {
        "code": "config_conflict",
        "message": f"{path} is a KaTrain {version} config; this build ({CONFIG_MIN_VERSION}) would overwrite it",
        "hint": "pass --config <file> or set KATRAIN_CONFIG to a separate config.json for this build",
    }


def launch_gui(config=None, sgf=None, wait=True, timeout=90):
    try:
        if health().get("ok"):
            return {"ok": True, "result": {"already_running": True, **read_discovery()}}
    except NotRunning:
        pass
    config = config or os.environ.get("KATRAIN_CONFIG")
    if not config:
        conflict = _older_user_config()
        if conflict:
            return {"ok": False, "error": conflict}
    python = sys.executable
    if os.name == "nt":
        # pythonw has no console: launching python.exe detached makes the venv launcher's child open a new
        # console window (a Windows Terminal window that stays open for the whole GUI session).
        pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.isfile(pythonw):
            python = pythonw
    cmd = [python, "-m", "katrain"]
    if config:
        cmd.append(os.path.abspath(os.path.expanduser(config)))
    if sgf:
        cmd.append(os.path.abspath(os.path.expanduser(sgf)))
    os.makedirs(DATA_FOLDER, exist_ok=True)
    log_path = os.path.join(DATA_FOLDER, "gui.log")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    launched = time.time()
    with open(log_path, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
    if not wait:
        return {"ok": True, "result": {"started": True, "pid": proc.pid, "log": log_path}}
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return {
                "ok": False,
                "error": {"code": "gui_exited", "message": f"GUI exited with code {proc.returncode}", "log": log_path},
            }
        info = read_discovery()
        # venv launchers re-exec the interpreter, so match on start time rather than pid
        if info and info.get("started", 0) >= launched - 1:
            try:
                if health().get("ok"):
                    return {
                        "ok": True,
                        "result": {"started": True, "pid": proc.pid, "port": info["port"], "log": log_path},
                    }
            except NotRunning:
                pass
        time.sleep(0.5)
    return {
        "ok": False,
        "error": {"code": "timeout", "message": f"GUI API not ready after {timeout}s", "log": log_path},
    }


# -- katrain:// links ----------------------------------------------------------


def link_to_command(url):
    """katrain://state/<name>, katrain://goto/45, katrain://goto?node=n12, katrain://variation/v3?step=2&as=branch,
    katrain://pv?from=45&moves=D4,Q16&step=1, katrain://analysis?node=45"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "katrain":
        raise ValueError("links must start with katrain://")
    action = parsed.netloc
    target = parsed.path.strip("/") or None
    q = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    if action == "goto":
        return "nav.goto", {"node": target or q.get("node")}
    if action == "state":
        return "state.restore", {"name": urllib.parse.unquote(target or q.get("name") or "")}
    if action == "variation":
        return "variation.show", {"id": target or q.get("id"), "step": q.get("step"), "as": q.get("as")}
    if action == "pv":
        return "pv.show", {
            "from": q.get("from"),
            "moves": q.get("moves"),
            "candidate": q.get("candidate"),
            "step": q.get("step"),
        }
    if action == "analysis":
        return "analysis.get", {"node": target or q.get("node")}
    raise ValueError(f"unknown link action '{action}'")


def _log_link(url, response):
    try:
        os.makedirs(DATA_FOLDER, exist_ok=True)
        with open(os.path.join(DATA_FOLDER, "links.log"), "a", encoding="utf-8") as f:
            error = None if response.get("ok") else response.get("error")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {url} -> {'ok' if error is None else error}\n")
    except OSError:
        pass


# -- argument parsing ------------------------------------------------------------


def _on_off(v):
    v = v.lower()
    if v in ("on", "true", "1", "yes"):
        return True
    if v in ("off", "false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError("use on/off")


def _add_node(p, default_help="current node"):
    p.add_argument("--node", "-n", help=f"node: current|root|end|<move N>|main:<N>|n<ID> (default: {default_help})")


def _add_analysis_view(p):
    p.add_argument("--top", type=int, default=None, help="number of candidate moves (default 10)")
    p.add_argument("--no-pv", action="store_true", help="omit principal variations")
    p.add_argument("--pv-limit", type=int, help="truncate PVs to this many moves")
    p.add_argument("--ownership", action="store_true", help="include the ownership grid")
    p.add_argument("--policy", action="store_true", help="include the top policy moves")


def _view_args(a):
    return {"top": a.top, "pv": not a.no_pv, "pv_limit": a.pv_limit, "ownership": a.ownership, "policy": a.policy}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="katrain",
        description="Control a running KaTrain GUI. Run without arguments to start the GUI. "
        "Read commands print JSON. Exit codes: 0 ok, 1 command error, 2 usage error, 3 GUI not running.",
    )
    parser.add_argument("--port", type=int, help="API port (default: from ~/.katrain/api.json)")
    parser.add_argument("--timeout", type=int, help="seconds to wait for the GUI / engine")
    parser.add_argument("--compact", action="store_true", help="single-line JSON output")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    def cmd(name, help_text, **kw):
        return sub.add_parser(name, help=help_text, description=help_text, **kw)

    p = cmd("gui", "start the KaTrain GUI in the background and wait for its API")
    p.add_argument("sgf", nargs="?", help="SGF file to open")
    p.add_argument("--config", help="config.json to use (default: $KATRAIN_CONFIG or ~/.katrain/config.json)")
    p.add_argument("--no-wait", action="store_true")

    cmd("ping", "check that the GUI API is reachable")
    cmd("guide", "print the agent usage guide (markdown)")
    cmd("commands", "list all API commands")
    p = cmd("call", "call any API command with JSON arguments")
    p.add_argument("command")
    p.add_argument("json_args", nargs="?", default="{}", help='e.g. \'{"node": "45"}\'')
    p = cmd("open", "follow a katrain:// link (goto / variation / pv / analysis)")
    p.add_argument("url")

    # reading
    cmd("status", "game info, current node, mode, engine state")
    p = cmd("board", "stones on the board")
    _add_node(p)
    p.add_argument("--ascii", action="store_true", help="print only the text diagram")
    p = cmd("node", "details of a node")
    p.add_argument("ref", nargs="?", help="node reference (default current)")
    p = cmd("history", "moves from the root to a node")
    _add_node(p)
    p.add_argument("--future", action="store_true", help="continue to the end of the line")
    p.add_argument("--no-eval", action="store_true", help="omit per-move evaluations")
    p = cmd("tree", "game tree with all variations")
    _add_node(p, "root")
    p.add_argument("--max-depth", type=int)
    p = cmd("variations", "alternatives at a node and branch points on its line")
    _add_node(p)

    # sgf / game
    p = cmd("sgf", "import or export SGF")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("export", help="print SGF or write it to a file")
    q.add_argument("--out", "-o", help="file to write (refuses to overwrite without --overwrite)")
    q.add_argument("--overwrite", action="store_true")
    q.add_argument("--analysis", action="store_true", help="embed KaTrain analysis")
    q.add_argument("--comments", action="store_true", help="add KaTrain move comments")
    q.add_argument("--marks", action="store_true", help="add best-move marks")
    q = s.add_parser("import", help="load an SGF file into the GUI (current game is backed up)")
    q.add_argument("path", help="SGF path, or - for stdin")
    q.add_argument("--fast", action="store_true", help="quick initial analysis")
    q.add_argument("--end", action="store_true", help="go to the last move")
    p = cmd("game", "game-level actions")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("new", help="start a new game (current game is backed up)")
    q.add_argument("--size", help="19, 13, 9 or WxH")
    q.add_argument("--komi", type=float)
    q.add_argument("--rules", help="japanese, chinese, korean, aga, tromp-taylor, new zealand")
    q.add_argument("--handicap", type=int)

    # analysis
    p = cmd("analysis", "KataGo analysis")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("get", help="analysis KaTrain already has (no new search)")
    _add_node(q)
    _add_analysis_view(q)
    q.add_argument("--moves", nargs="+", help="only these candidate moves")
    q = s.add_parser("run", help="search a position to N visits and return the analysis")
    _add_node(q)
    _add_analysis_view(q)
    q.add_argument("--visits", "-v", type=int, help="visits (default: engine max_visits)")
    q.add_argument("--region", help="restrict candidates to a region like C3:G7, or 'gui' for KaTrain's region")
    q.add_argument("--max-time", type=float, help="time cap in seconds")
    q.add_argument("--force", action="store_true", help="search even if existing analysis is deep enough")
    q.add_argument("--no-wait", action="store_true", help="queue the search and return immediately")
    q = s.add_parser("moves", help="search specific moves and compare them")
    q.add_argument("moves", nargs="+", help="moves for the player to move, e.g. D4 Q16")
    _add_node(q)
    _add_analysis_view(q)
    q.add_argument("--visits", "-v", type=int)
    q = s.add_parser("game", help="queue whole-game analysis in the background")
    q.add_argument("--visits", "-v", type=int)
    q.add_argument("--mistakes-only", action="store_true")
    q.add_argument("--range", help="move range like 20-60")
    s.add_parser("wait", help="wait until the engine is idle")
    q = s.add_parser("ponder", help="continuous analysis on/off")
    q.add_argument("state", type=_on_off)
    s.add_parser("stop", help="stop pondering and pending queries")

    p = cmd("engine", "search settings")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    s.add_parser("get", help="show settings")
    q = s.add_parser("set", help="change settings")
    q.add_argument("--max-visits", type=int)
    q.add_argument("--fast-visits", type=int)
    q.add_argument("--max-time", type=float)
    q.add_argument("--wide-root-noise", type=float)
    q.add_argument("--save", action="store_true", help="persist to the config file")

    # navigation
    p = cmd("goto", "jump to a node")
    p.add_argument("ref", help="<move N> | main:<N> | n<ID> | root | end")
    p.add_argument("--board", action="store_true", help="include the board")
    for name, help_text in (("next", "step forward"), ("prev", "step back")):
        p = cmd(name, help_text)
        p.add_argument("n", nargs="?", type=int, default=1)
        p.add_argument("--board", action="store_true")
    p = cmd("branch", "switch between sibling variations")
    p.add_argument("which", help="next | prev | <index>")
    p.add_argument("--board", action="store_true")
    p = cmd("play", "play moves from the current position (adds them to the game)")
    p.add_argument("moves", nargs="+", help="D4 Q16 ..., colors alternate; prefix B:/W: to force")
    p.add_argument("--color", help="color of the first move")
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--board", action="store_true")

    p = cmd("variation", "create, delete, promote, register and show variations")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("add", help="add a variation below a node")
    q.add_argument("moves", nargs="+")
    q.add_argument("--from", dest="from_", help="start node (default current)")
    q.add_argument("--color")
    q.add_argument("--goto", action="store_true", help="jump to the end of the new variation")
    q.add_argument("--analyze", choices=["fast", "full", "none"], default="fast")
    q = s.add_parser("delete", help="delete a node and its subtree")
    q.add_argument("ref")
    q.add_argument("--force", action="store_true", help="allow deleting original game nodes")
    q = s.add_parser("promote", help="make the line through a node the main line")
    q.add_argument("ref")
    q = s.add_parser("register", help="register a variation and get an id (v1, v2, ...)")
    q.add_argument("moves", nargs="+")
    q.add_argument("--from", dest="from_")
    q.add_argument("--color")
    q.add_argument("--label")
    s.add_parser("list", help="registered variations")
    q = s.add_parser("show", help="show a registered variation")
    q.add_argument("id")
    q.add_argument("--as", dest="as_", choices=["pv", "branch"], default="pv")
    q.add_argument("--step", type=int, help="show only the first N moves")

    # display
    p = cmd("pv", "overlay a variation with numbered stones (does not change the game)")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("show", help="show moves, or KataGo's PV for --candidate")
    q.add_argument("moves", nargs="*")
    q.add_argument("--from", dest="from_", help="start node (default current)")
    q.add_argument("--candidate", help="use the analysed PV of this candidate move")
    q.add_argument("--step", type=int, help="show only the first N moves (omit to animate)")
    q = s.add_parser("step", help="show one more / fewer move")
    q.add_argument("delta", nargs="?", type=int, default=1)
    q.add_argument("--index", type=int, help="show exactly N moves")
    s.add_parser("hide", help="remove the overlay")

    p = cmd("mark", "marks and highlights on the board")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("add", help="mark points")
    q.add_argument("points", nargs="+")
    q.add_argument("--type", "-t", default="circle", choices=["circle", "square", "triangle", "cross", "label", "dot"])
    q.add_argument("--text", nargs="+", help="label texts (default A, B, C...)")
    q.add_argument("--color", "-c", help="red, blue, green, yellow, orange, purple, cyan, white, black, #rrggbb")
    q.add_argument(
        "--global", dest="global_", action="store_true", help="keep showing on every move (default: this node only)"
    )
    q = s.add_parser("area", help="highlight a rectangle")
    q.add_argument("region", help="C3:G7")
    q.add_argument("--text")
    q.add_argument("--color", "-c")
    q.add_argument("--global", dest="global_", action="store_true")
    s.add_parser("list", help="list marks")
    q = s.add_parser("clear", help="remove marks (all, or given points / ids)")
    q.add_argument("points", nargs="*")
    q.add_argument("--ids", nargs="+")

    p = cmd("region", "KataGo region of interest")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("set")
    q.add_argument("region", help="C3:G7")
    s.add_parser("clear")

    p = cmd("view", "toggle display layers")
    for name in ("hints", "ownership", "policy", "eval", "children", "move-numbers"):
        p.add_argument(f"--{name}", type=_on_off, metavar="on|off")
    p.add_argument("--zoom", metavar="C3:G7|auto|off", help="show only part of the board (auto: the tsumego problem)")
    p.add_argument("--hide-frame", type=_on_off, metavar="on|off", help="hide the tsumego frame stones")
    p = cmd("mode", "switch play / analyze mode")
    p.add_argument("mode", choices=["play", "analyze"])
    p = cmd("message", "show a line in the status bar")
    p.add_argument("text")

    p = cmd("state", "save / restore board states")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    for name in ("save", "restore"):
        q = s.add_parser(name)
        q.add_argument("name", nargs="?", default="default")
    s.add_parser("list")

    p = cmd("demo", "temporary demonstrations that leave the game record untouched")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("start", help="snapshot the position; later additions are temporary")
    q.add_argument("--label")
    q = s.add_parser("end", help="remove demo moves and restore the snapshot")
    q.add_argument("--keep", action="store_true", help="keep the moves added during the demo")
    s.add_parser("status")

    p = cmd("tsumego", "life-and-death problems")
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("list", help="list problems in an SGF file or folder")
    q.add_argument("source", help="SGF file or folder (searched recursively)")
    q.add_argument("--set", dest="set_", help="only problems whose collection/folder contains this text")
    q.add_argument("--difficulty", help="only problems with this difficulty label")
    q.add_argument("--limit", type=int, default=200)
    q = s.add_parser("load", help="load a problem: frame + local search, zoomed view with the frame hidden")
    q.add_argument("source", help="SGF file or folder")
    q.add_argument("--no-zoom", action="store_true", help="keep the full-board view")
    q.add_argument("--index", "-i", type=int, default=0, help="problem index from 'tsumego list'")
    q.add_argument("--set", dest="set_")
    q.add_argument("--difficulty")
    q.add_argument(
        "--frame",
        type=_on_off,
        metavar="on|off",
        help="KaTrain's tsumego frame (default: on, off for center problems)",
    )
    q.add_argument("--margin", type=int, default=4)
    q.add_argument("--ko", action="store_true", help="frame with ko threats")
    q = s.add_parser("target", help="set goal and target group")
    q.add_argument("--goal", choices=["kill", "live"], help="from the perspective of the player to move")
    q.add_argument("--group", help="a stone of the target group, e.g. C3")
    q.add_argument("--region", help="local search area, e.g. A1:F6")
    q = s.add_parser("try", help="play a move; KataGo answers locally")
    q.add_argument("move")
    q.add_argument("--visits", "-v", type=int)
    q.add_argument("--no-reply", action="store_true", help="do not let KataGo answer")
    q = s.add_parser("eval", help="evaluate the current problem position")
    q.add_argument("--visits", "-v", type=int)
    s.add_parser("reset", help="back to the starting position")
    q = s.add_parser("solution", help="reference solution from the SGF, or KataGo's line")
    q.add_argument("--show", action="store_true", help="overlay it on the board")
    q.add_argument("--visits", "-v", type=int)
    s.add_parser("status")

    p = cmd(
        "sandbox", "background exploration: lines and analysis are kept in the user's game, the board does not move"
    )
    s = p.add_subparsers(dest="sub", metavar="ACTION", required=True)
    q = s.add_parser("new", help="start a background cursor at a position of the user's game")
    q.add_argument("--from", dest="from_", help="node to copy (default: the user's current position)")
    q.add_argument("--name", help="sandbox id (default s1, s2, ...)")
    q = s.add_parser("play", help="play moves at the cursor (kept as a variation, board does not move)")
    q.add_argument("id")
    q.add_argument("moves", nargs="+")
    q.add_argument("--color", help="color of the first move")
    q = s.add_parser("back", help="take back moves in a sandbox")
    q.add_argument("id")
    q.add_argument("n", nargs="?", type=int, default=1)
    q = s.add_parser("board", help="show a sandbox board")
    q.add_argument("id")
    q.add_argument("--ascii", action="store_true", help="print only the text diagram")
    q = s.add_parser("analyze", help="analyze a sandbox position at low priority")
    q.add_argument("id")
    q.add_argument("--visits", "-v", type=int)
    q.add_argument("--moves", nargs="+", help="compare these candidate moves")
    q.add_argument("--top", type=int)
    q.add_argument("--no-pv", action="store_true")
    q.add_argument("--pv-limit", type=int)
    q.add_argument("--ownership", action="store_true")
    q.add_argument("--force", action="store_true")
    q = s.add_parser("show", help="bring a sandbox line to the user's board")
    q.add_argument("id")
    q.add_argument(
        "--as", dest="as_", choices=["pv", "goto"], default="pv", help="pv overlay, or move the user's board there"
    )
    q.add_argument("--step", type=int, help="show only the first N moves")
    s.add_parser("list", help="list sandboxes")
    q = s.add_parser("drop", help="forget a sandbox (or 'all'); --prune also deletes the variations it added")
    q.add_argument("id")
    q.add_argument("--prune", action="store_true", help="delete the sandbox's API-created variations")
    _accept_global_options_everywhere(parser)
    return parser


def _subparsers(parser):
    return parser._subparsers._group_actions[0].choices if parser._subparsers else {}


def _accept_global_options_everywhere(parser):
    """Let --port/--timeout/--compact also appear after the subcommand (agents put them anywhere)."""
    for child in _subparsers(parser).values():
        child.add_argument("--port", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        child.add_argument("--timeout", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        child.add_argument("--compact", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        _accept_global_options_everywhere(child)


# -- argparse -> API command -------------------------------------------------------


def to_api(a):
    """Map parsed arguments to (command, args, timeout_hint)."""
    c = a.cmd
    sub = getattr(a, "sub", None)
    if c == "status":
        return "status", {}, None
    if c == "board":
        return "board", {"node": a.node}, None
    if c == "node":
        return "node", {"node": a.ref}, None
    if c == "history":
        return "history", {"node": a.node, "future": a.future, "eval": not a.no_eval}, None
    if c == "tree":
        return "tree", {"node": a.node, "max_depth": a.max_depth}, None
    if c == "variations":
        return "variations", {"node": a.node}, None
    if c == "sgf" and sub == "export":
        out = a.out and os.path.abspath(a.out)
        return (
            "sgf.export",
            {"path": out, "overwrite": a.overwrite, "analysis": a.analysis, "comments": a.comments, "marks": a.marks},
            None,
        )
    if c == "sgf" and sub == "import":
        if a.path == "-":
            return "sgf.import", {"content": sys.stdin.read(), "fast": a.fast, "at_end": a.end}, None
        return "sgf.import", {"path": os.path.abspath(a.path), "fast": a.fast, "at_end": a.end}, None
    if c == "game" and sub == "new":
        return "game.new", {"size": a.size, "komi": a.komi, "rules": a.rules, "handicap": a.handicap}, None
    if c == "analysis":
        if sub == "get":
            return "analysis.get", {"node": a.node, "moves": a.moves, **_view_args(a)}, None
        if sub == "run":
            t = a.timeout or ANALYSIS_TIMEOUT
            return (
                "analysis.run",
                {
                    "node": a.node,
                    "visits": a.visits,
                    "region": a.region,
                    "max_time": a.max_time,
                    "force": a.force,
                    "wait": not a.no_wait,
                    "timeout": t,
                    **_view_args(a),
                },
                t,
            )
        if sub == "moves":
            t = a.timeout or ANALYSIS_TIMEOUT
            return (
                "analysis.moves",
                {"node": a.node, "moves": a.moves, "visits": a.visits, "timeout": t, **_view_args(a)},
                t,
            )
        if sub == "game":
            return "analysis.game", {"visits": a.visits, "mistakes_only": a.mistakes_only, "move_range": a.range}, None
        if sub == "wait":
            t = a.timeout or ANALYSIS_TIMEOUT
            return "analysis.wait", {"timeout": t}, t
        if sub == "ponder":
            return "analysis.ponder", {"on": a.state}, None
        if sub == "stop":
            return "analysis.stop", {}, None
    if c == "engine":
        if sub == "get":
            return "engine.get", {}, None
        return (
            "engine.set",
            {
                "max_visits": a.max_visits,
                "fast_visits": a.fast_visits,
                "max_time": a.max_time,
                "wide_root_noise": a.wide_root_noise,
                "save": a.save,
            },
            None,
        )
    if c == "goto":
        return "nav.goto", {"node": a.ref, "board": a.board}, None
    if c == "next":
        return "nav.next", {"n": a.n, "board": a.board}, None
    if c == "prev":
        return "nav.prev", {"n": a.n, "board": a.board}, None
    if c == "branch":
        if a.which in ("next", "prev"):
            return "nav.branch", {"direction": 1 if a.which == "next" else -1, "board": a.board}, None
        return "nav.branch", {"index": a.which, "board": a.board}, None
    if c == "play":
        return "play", {"moves": a.moves, "color": a.color, "analyze": not a.no_analyze, "board": a.board}, None
    if c == "variation":
        if sub == "add":
            return (
                "variation.add",
                {"moves": a.moves, "from": a.from_, "color": a.color, "goto": a.goto, "analyze": a.analyze},
                None,
            )
        if sub == "delete":
            return "variation.delete", {"node": a.ref, "force": a.force}, None
        if sub == "promote":
            return "variation.promote", {"node": a.ref}, None
        if sub == "register":
            return "variation.register", {"moves": a.moves, "from": a.from_, "color": a.color, "label": a.label}, None
        if sub == "list":
            return "variation.list", {}, None
        if sub == "show":
            return "variation.show", {"id": a.id, "as": a.as_, "step": a.step}, None
    if c == "pv":
        if sub == "show":
            return (
                "pv.show",
                {"moves": a.moves or None, "from": a.from_, "candidate": a.candidate, "step": a.step},
                None,
            )
        if sub == "step":
            return "pv.step", {"delta": a.delta, "index": a.index}, None
        return "pv.hide", {}, None
    if c == "mark":
        if sub == "add":
            return (
                "mark.add",
                {
                    "points": a.points,
                    "type": a.type,
                    "text": a.text,
                    "color": a.color,
                    "scope": "global" if a.global_ else "node",
                },
                None,
            )
        if sub == "area":
            return (
                "mark.area",
                {"region": a.region, "text": a.text, "color": a.color, "scope": "global" if a.global_ else "node"},
                None,
            )
        if sub == "list":
            return "mark.list", {}, None
        return "mark.clear", {"points": a.points or None, "ids": a.ids}, None
    if c == "region":
        return ("region.set", {"region": a.region}, None) if sub == "set" else ("region.clear", {}, None)
    if c == "view":
        return (
            "view.set",
            {
                "hints": a.hints,
                "ownership": a.ownership,
                "policy": a.policy,
                "eval": a.eval,
                "children": a.children,
                "move_numbers": a.move_numbers,
                "zoom": a.zoom,
                "hide_frame": a.hide_frame,
            },
            None,
        )
    if c == "mode":
        return "mode.set", {"mode": a.mode}, None
    if c == "message":
        return "message", {"text": a.text}, None
    if c == "state":
        return f"state.{sub}", {"name": getattr(a, "name", None)}, None
    if c == "demo":
        if sub == "start":
            return "demo.start", {"label": a.label}, None
        if sub == "end":
            return "demo.end", {"keep": a.keep}, None
        return "demo.status", {}, None
    if c == "tsumego":
        t = a.timeout or ANALYSIS_TIMEOUT
        if sub == "list":
            return (
                "tsumego.list",
                {"source": os.path.abspath(a.source), "set": a.set_, "difficulty": a.difficulty, "limit": a.limit},
                None,
            )
        if sub == "load":
            return (
                "tsumego.load",
                {
                    "source": os.path.abspath(a.source),
                    "index": a.index,
                    "set": a.set_,
                    "difficulty": a.difficulty,
                    "frame": a.frame,
                    "margin": a.margin,
                    "ko": a.ko,
                    "zoom": not a.no_zoom,
                },
                None,
            )
        if sub == "target":
            return "tsumego.target", {"goal": a.goal, "group": a.group, "region": a.region}, None
        if sub == "try":
            return "tsumego.try", {"move": a.move, "visits": a.visits, "reply": not a.no_reply, "timeout": t}, t
        if sub == "eval":
            return "tsumego.eval", {"visits": a.visits, "timeout": t}, t
        if sub == "solution":
            return "tsumego.solution", {"show": a.show, "visits": a.visits, "timeout": t}, t
        return f"tsumego.{sub}", {}, None
    if c == "sandbox":
        if sub == "new":
            return "sandbox.new", {"from": a.from_, "name": a.name}, None
        if sub == "play":
            return "sandbox.play", {"id": a.id, "moves": a.moves, "color": a.color}, None
        if sub == "back":
            return "sandbox.back", {"id": a.id, "n": a.n}, None
        if sub == "board":
            return "sandbox.board", {"id": a.id}, None
        if sub == "analyze":
            t = a.timeout or ANALYSIS_TIMEOUT
            args = {
                "id": a.id,
                "visits": a.visits,
                "moves": a.moves,
                "top": a.top,
                "pv": not a.no_pv,
                "pv_limit": a.pv_limit,
                "ownership": a.ownership,
                "force": a.force,
                "timeout": t,
            }
            return "sandbox.analyze", args, t
        if sub == "show":
            return "sandbox.show", {"id": a.id, "as": a.as_, "step": a.step}, None
        if sub == "drop":
            return "sandbox.drop", {"id": a.id, "prune": a.prune}, None
        return "sandbox.list", {}, None
    raise ValueError(f"unhandled command {c} {sub}")


# -- output ------------------------------------------------------------------------


def _print_json(obj, compact):
    if compact:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _finish(response, compact, text_key=None):
    if response.get("ok"):
        result = response.get("result")
        if text_key and isinstance(result, dict) and text_key in result:
            print(result[text_key])
        else:
            _print_json(result, compact)
        return EXIT_OK
    _print_json({"ok": False, "error": response.get("error")}, compact)
    return EXIT_API_ERROR


def run_gui():
    from katrain.__main__ import run_app

    run_app()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    subcommands = set(_subparsers(parser))
    gui_file = (
        argv
        and argv[0] not in subcommands
        and (os.path.isfile(argv[0]) or argv[0].lower().endswith((".sgf", ".ngf", ".gib", "config.json")))
    )
    if not argv or gui_file:
        return run_gui()  # plain `katrain`, `katrain game.sgf`, `katrain my_config.json`

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    a = parser.parse_args(argv)
    if a.cmd is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        if a.cmd == "gui":
            return _finish(launch_gui(a.config, a.sgf, wait=not a.no_wait, timeout=a.timeout or 90), a.compact)
        if a.cmd == "guide":
            path = os.path.join(os.path.dirname(__file__), "api", "AGENT_GUIDE.md")
            with open(path, encoding="utf-8") as f:
                print(f.read())
            return EXIT_OK
        if a.cmd == "ping":
            return _finish(health(port=a.port, timeout=a.timeout or 3), a.compact)
        if a.cmd == "commands":
            return _finish(call("commands", port=a.port), a.compact)
        if a.cmd == "call":
            try:
                args = json.loads(a.json_args)
            except ValueError as e:
                print(f"invalid JSON arguments: {e}", file=sys.stderr)
                return EXIT_USAGE
            if not isinstance(args, dict):
                print("JSON arguments must be an object", file=sys.stderr)
                return EXIT_USAGE
            t = a.timeout or int(args.get("timeout") or 0) + 60
            return _finish(call(a.command, args, port=a.port, timeout=t), a.compact)
        if a.cmd == "open":
            try:
                command, args = link_to_command(a.url)
                response = call(command, args, port=a.port)
            except (ValueError, NotRunning) as e:
                response = {"ok": False, "error": {"code": "bad_link", "message": str(e)}}
            if sys.stdout is None:  # launched by the katrain:// protocol handler via pythonw: no console
                _log_link(a.url, response)
            return _finish(response, a.compact)
        command, args, wait = to_api(a)
        timeout = (wait + 30) if wait else (a.timeout or 60)
        wants_ascii = a.cmd == "board" or (a.cmd == "sandbox" and getattr(a, "sub", None) == "board")
        text_key = "ascii" if wants_ascii and a.ascii else None
        return _finish(call(command, args, port=a.port, timeout=timeout), a.compact, text_key)
    except NotRunning as e:
        _print_json({"ok": False, "error": {"code": "gui_not_running", "message": str(e)}}, a.compact)
        return EXIT_NOT_RUNNING


if __name__ == "__main__":
    sys.exit(main())
