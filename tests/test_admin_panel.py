import importlib.util
import pathlib
import unittest


def _load_admin_panel():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "scripts" / "admin-panel.py"
    spec = importlib.util.spec_from_file_location("admin_panel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


admin_panel = _load_admin_panel()


class ParseListResponseTests(unittest.TestCase):
    def test_parses_zero_of_max_variant(self):
        parsed = admin_panel.parse_list_response(
            "There are 0 of a max of 20 players online:"
        )
        self.assertEqual(parsed["player_count"], 0)
        self.assertEqual(parsed["max_players"], 20)
        self.assertEqual(parsed["players"], [])

    def test_parses_legacy_wording_and_names(self):
        parsed = admin_panel.parse_list_response(
            "There are 2 of 20 max players online: Alice, Bob"
        )
        self.assertEqual(parsed["player_count"], 2)
        self.assertEqual(parsed["max_players"], 20)
        self.assertEqual(parsed["players"], ["Alice", "Bob"])

    def test_parses_slash_variant_and_singular(self):
        parsed = admin_panel.parse_list_response(
            "There is 1/20 player online: Steve"
        )
        self.assertEqual(parsed["player_count"], 1)
        self.assertEqual(parsed["max_players"], 20)
        self.assertEqual(parsed["players"], ["Steve"])

    def test_handles_whitespace_and_empty_names(self):
        parsed = admin_panel.parse_list_response(
            " There are   3 of a max of 20 players online:  Alice ,  Bob,Charlie  "
        )
        self.assertEqual(parsed["player_count"], 3)
        self.assertEqual(parsed["max_players"], 20)
        self.assertEqual(parsed["players"], ["Alice", "Bob", "Charlie"])

    def test_rejects_unrecognized_shape(self):
        self.assertIsNone(admin_panel.parse_list_response("players online right now: 0"))


class ServerStateTests(unittest.TestCase):
    def test_player_count_tracks_rcon_count(self):
        state = admin_panel.ServerState()
        state.set_players([], 3, 20)
        snap = state.snapshot()
        self.assertTrue(snap["online"])
        self.assertEqual(snap["player_count"], 3)
        self.assertEqual(snap["players"], [])
        self.assertEqual(snap["max_players"], 20)


if __name__ == "__main__":
    unittest.main()
