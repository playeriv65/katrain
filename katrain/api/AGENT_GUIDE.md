# KaTrain CLI — guide for AI agents

`katrain <command>` controls the KaTrain GUI that is open on the user's screen. KataGo judges positions;
you explain them. The CLI never writes teaching text for you.

## Basics

- Start the GUI if needed: `katrain gui` (waits until ready; reuses a running one). `katrain ping` checks it.
- Output is JSON on stdout. Errors: `{"ok": false, "error": {"code", "message", "hint"?}}` and a non-zero exit
  code (1 command error, 2 usage error, 3 GUI not running).
- Coordinates are GTP style: columns `A`–`T` without `I`, rows from the bottom (`D4`, `Q16`, `pass`).
  Board rows in `board` output are listed top row first; `X` = black, `O` = white, lowercase = last move.
- Numbers: `winrate` / `score_lead` are for the **player to move** at that node; `black_winrate` /
  `black_score_lead` are from Black's view. `points_lost` is always positive for a loss.
- `--compact` gives single-line JSON. `--timeout N` raises waiting limits. Both work anywhere on the line.
- Run `katrain <command> --help` for every option. `katrain commands` lists the raw API;
  `katrain call <command> '<json>'` calls it directly.

## Node references (`--node`, `goto`, `--from`)

| Ref | Meaning |
| --- | --- |
| `current` (default) | the node shown in the GUI |
| `45` | move 45 on the current line (root -> current -> first children to the end) |
| `main:45` | move 45 on the main line |
| `n17` | node id from any previous output (stable while the GUI runs) |
| `root`, `end` | start of game, end of current line |

## Reading

```
katrain status                      # rules, komi, players, current node, engine, demo/tsumego state
katrain board [--node 45] [--ascii] # stones, captures, next player
katrain history [--future]          # moves to the current node (with per-move evaluation)
katrain tree [--max-depth 30]       # all variations; linear stretches collapsed
katrain variations                  # alternatives at this node + branch points on the line
katrain node n17                    # one node incl. comments and SGF properties
katrain sgf export [-o file.sgf]    # SGF text, or write a file (never overwrites without --overwrite)
katrain sgf import game.sgf [--end] # load a game; the previous game is backed up to ~/.katrain/api_backups
```

## Analysis (KataGo)

```
katrain analysis get [--node 45] [--top 5]       # what KaTrain already computed — free, no search
katrain analysis run [--node 45] -v 2000          # search to 2000 visits; reuses deeper existing results
katrain analysis moves D4 C3 E5 [--node 45] -v 800  # search these moves and compare (points lost, PV)
katrain analysis run --region C3:G7               # only consider moves inside a region
katrain analysis game -v 300 [--mistakes-only]    # background whole-game analysis; then `analysis wait`
katrain engine set --max-visits 1000 [--save]     # default search depth
katrain analysis ponder on|off                    # continuous analysis shown live in the GUI
```

Each candidate has `move`, `visits`, `winrate`, `score_lead`, `points_lost`, `prior` and `pv` (the predicted
line, starting with the candidate itself). Prefer `analysis get` first; only search when `available` is false
or visits are too low.

## Controlling the board

```
katrain goto 45 | next [n] | prev [n] | branch next|prev|<index>
katrain play D4 Q16            # adds moves to the game (colors alternate; B:/W: prefix forces a color)
katrain variation add C3 D2 --from 45 [--goto]   # add a side line without moving there
katrain variation delete n88   # only API-created nodes unless --force
katrain variation promote n88  # make it the main line
katrain mark add D4 E5 --type label --text A B   # also circle|square|triangle|cross|dot, --color, --global
katrain mark area C3:G7 --text "weak group" --color orange
katrain mark clear
katrain view --hints on --ownership off          # also policy, eval, children, move-numbers
katrain message "Look at the marked stones"      # one line in the status bar
```

Marks belong to the current node by default (they disappear when the user navigates away); `--global`
keeps them on every node.

## Demonstrating variations without touching the record

Two ways, use whichever fits:

1. **PV overlay** (no tree change at all): numbered stones drawn on the board.
   ```
   katrain pv show --candidate D4 --step 1   # KataGo's line for D4, first move only
   katrain pv step                           # one more move (pv step -1 goes back)
   katrain pv show C3 D2 C2 --step 3
   katrain pv hide
   ```
2. **Demo mode** (real moves the user can click through, removed afterwards):
   ```
   katrain demo start --label "why C3 fails"
   katrain play C3 D2 C2          # or variation add / goto / mark ... anything
   katrain demo end               # deletes everything added since demo start, restores position, mode and marks
   ```
   `demo end --keep` keeps the added moves. Demo mode switches the GUI to analysis mode so no AI replies.

`state save <name>` / `state restore <name>` bookmark and return to a position (plus marks and mode).

Outside demo mode, `play` and `variation add` change the game tree and report `"game_modified": true`.
Nothing is ever written to the user's SGF files unless you call `sgf export -o`.

## Two modes: explaining on the user's board vs. thinking in the background

The user is looking at, and may be playing on, the same KaTrain board you control.

* Foreground (the user sees it): `goto`, `play`, `pv show`, `mark`, `demo start/end`, `message`.
  Only use these when you are explaining something; the user's own navigation moves the same board.
* Background (the board does not move): a sandbox is your own cursor in the user's game tree. Moves you play
  become ordinary variations and every analysis is stored in those nodes, so the user can browse your work
  later without re-analysis. Background queries run below the GUI's own priority.

```
katrain sandbox new --from 36            # -> {"id": "s1", ...}; cursor at the position after move 36
katrain sandbox play s1 H5 B8 E8         # kept as a variation; the user's board stays where it is
katrain sandbox analyze s1 -v 2000       # stored in that node; the GUI's own analysis goes first
katrain sandbox analyze s1 --moves H6 H4 # compare candidates; stored in the node's candidate list
katrain sandbox back s1 2 | sandbox board s1 --ascii | sandbox list
katrain sandbox show s1 --step 3         # foreground: overlay the line on the user's board
katrain sandbox show s1 --as goto        # foreground: move the user's board to the sandbox position
katrain sandbox drop s1 [--prune]        # --prune deletes the variations you added (only API-created nodes)
```

Don't throw work away: analyze positions through sandboxes or `analysis run`, never in a detached copy.

Prefer reading (`status`, `analysis get`, `board`) and sandboxes while the user is working; switch to the
foreground only to show a conclusion.

## Registered variations and links

```
katrain variation register D4 C3 D3 --from 45 --label "main idea"   # -> {"id": "v1"}
katrain variation show v1 [--step 2] [--as branch]
katrain open "katrain://variation/v1?step=2"
katrain open "katrain://goto/45"
katrain open "katrain://pv?from=45&moves=D4,C3&step=1"
```

## Life-and-death problems

```
katrain tsumego list ~/problems [--set cho --difficulty elementary]
katrain tsumego load ~/problems --index 3          # hides the SGF answer, adds KaTrain's tsumego frame + region
katrain tsumego target --goal kill --group C2      # goal for the player to move; group = any stone of the target
katrain tsumego try B2                             # any legal move; KataGo evaluates and answers locally
katrain tsumego eval                               # target status (alive/dead/unsettled) + best local moves
katrain tsumego solution [--show]                  # SGF reference lines (correct/wrong) and KataGo's line
katrain tsumego reset                              # back to the start, attempt moves removed
```

`tsumego load` also zooms the board to the problem (like a book diagram) and hides the frame stones; KataGo still
uses them. `katrain view --zoom off|auto|C3:G7 --hide-frame on|off` changes this; loading any other game resets it.

`tsumego try` returns `winrate_lost`, `was_best_move`, `target.status`, `goal_met`, `better_moves` and
`refutation_pv` (KataGo's punishment line after a wrong move — show it with `pv show --from <node> ...`).

## Good habits

- Read `status` once at the start; keep node ids (`n17`) instead of re-deriving move numbers.
- For teaching, wrap explorations in `demo start` / `demo end` so the user's record stays clean.
- Show, then talk: `goto`, `mark`, `pv show --step N` before explaining each step.
- Long searches: pass `-v` explicitly and `--timeout`; the GUI stays responsive while waiting.
