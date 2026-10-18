import threading

from katrain.core.base_katrain import KaTrainBase
from katrain.core.game import Game, Move
from katrain.core.game_node import GameNode
from katrain.core.tsumego_frame import tsumego_view_region


class MockKaTrain(KaTrainBase):
    pass


class RecordingEngine:
    """Keeps every analysis request so a test can answer it."""

    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def request_analysis(self, node, callback, **kwargs):
        with self.lock:
            self.requests.append((node, callback, kwargs))

    def for_node(self, node):
        with self.lock:
            return [(cb, kw) for n, cb, kw in self.requests if n is node]


def analysis_result(score):
    return {
        "rootInfo": {"visits": 100, "winrate": 0.5, "scoreLead": score},
        "moveInfos": [
            {"move": "D4", "order": 0, "visits": 90, "winrate": 0.5, "scoreLead": score, "prior": 0.5, "pv": ["D4"]}
        ],
    }


class TestViewRegion:
    def test_corner_problem_reaches_the_edges(self):
        # stones in the lower left corner: A1..C3
        coords = [(0, 0), (2, 2), (1, 2)]
        xmin, xmax, ymin, ymax = tsumego_view_region(coords, (19, 19))
        assert (xmin, ymin) == (0, 0)
        assert xmax - xmin + 1 == 7 and ymax - ymin + 1 == 7  # widened to the minimum span

    def test_margin_around_the_stones(self):
        coords = [(16, 10), (18, 18)]
        assert tsumego_view_region(coords, (19, 19)) == [12, 18, 8, 18]

    def test_center_problem_grows_both_ways(self):
        xmin, xmax, ymin, ymax = tsumego_view_region([(9, 9)], (19, 19))
        assert (xmin, xmax, ymin, ymax) == (6, 12, 6, 12)

    def test_small_board_and_no_stones(self):
        assert tsumego_view_region([(2, 2)], (5, 5)) == [0, 4, 0, 4]
        assert tsumego_view_region([], (9, 9)) == [0, 8, 0, 8]


class TestRegionRoot:
    region = [0, 4, 0, 4]

    def answer(self, engine, node, score):
        for callback, _ in engine.for_node(node):
            callback(analysis_result(score), False)

    def test_region_result_is_not_the_evaluation_by_default(self):
        engine = RecordingEngine()
        node = GameNode(properties={"SZ": 19})
        node.analyze(engine, region_of_interest=self.region)
        self.answer(engine, node, 3.0)
        assert node.analysis["root"] is None
        assert "D4" in node.analysis["moves"]

    def test_region_root_sets_the_evaluation(self):
        engine = RecordingEngine()
        node = GameNode(properties={"SZ": 19})
        node.analyze(engine, region_of_interest=self.region, region_root=True)
        assert engine.for_node(node)[0][1]["region_of_interest"] == self.region  # still a local search
        self.answer(engine, node, 3.0)
        assert node.analysis["root"]["scoreLead"] == 3.0
        assert node.candidate_moves[0]["pointsLost"] == 0  # the local best move is the reference

    def test_play_in_tsumego_mode_sends_one_local_query(self):
        engine = RecordingEngine()
        game = Game(MockKaTrain(force_package_config=True), engine, move_tree=GameNode(properties={"SZ": 19}))
        game.region_of_interest = self.region  # set_region_of_interest also updates the GUI status bar
        game.region_root = True
        node = game.play(Move.from_gtp("B2", player="B"))
        queries = [kw for _, kw in engine.for_node(node)]
        assert len(queries) == 1
        assert queries[0]["region_of_interest"] == self.region

    def test_play_with_region_of_interest_keeps_whole_board_evaluation(self):
        engine = RecordingEngine()
        game = Game(MockKaTrain(force_package_config=True), engine, move_tree=GameNode(properties={"SZ": 19}))
        game.region_of_interest = self.region  # set_region_of_interest also updates the GUI status bar
        node = game.play(Move.from_gtp("B2", player="B"))
        queries = [kw for _, kw in engine.for_node(node)]
        assert [q.get("region_of_interest") for q in queries] == [None, self.region]
