"""Verify launcher options reach the server without starting model inference."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import run_service


class LauncherTest(unittest.TestCase):
    def launch(self, arguments):
        forwarded = []
        with patch.object(sys, "argv", ["run_service.py", *arguments]), \
                patch.object(run_service, "check_models"), \
                patch.object(run_service, "check_port", return_value=True), \
                patch("builtins.print"), \
                patch("app.server.main", side_effect=lambda: forwarded.extend(sys.argv)):
            self.assertEqual(run_service.main(), 0)
        return forwarded

    def test_no_preload_and_english_overrides(self):
        args = self.launch(["--no-preload", "--en-threads", "2", "--en-partial-step", "0.9"])
        self.assertIn("--no-preload", args)
        self.assertEqual(args[args.index("--en-threads") + 1], "2")
        self.assertEqual(args[args.index("--en-partial-step") + 1], "0.9")

    def test_defaults_leave_language_tuning_to_server(self):
        args = self.launch([])
        for option in ("--no-preload", "--threads", "--partial-step", "--en-threads", "--en-partial-step"):
            self.assertNotIn(option, args)


if __name__ == "__main__":
    unittest.main()
