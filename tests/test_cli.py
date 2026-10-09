from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from trad.cli import main  # noqa: E402


class CliTests(unittest.TestCase):
    def test_valid_config_has_non_secret_summary(self) -> None:
        config_path = Path(__file__).parents[1] / "config" / "backtest.example.toml"
        output = StringIO()

        with redirect_stdout(output):
            result = main([str(config_path), "--json"])

        self.assertEqual(result, 0)
        self.assertIn('"status": "valid"', output.getvalue())
        self.assertIn('"simulation_only": true', output.getvalue())
        self.assertNotIn("api_key", output.getvalue())

    def test_invalid_config_returns_nonzero_without_starting_any_engine(self) -> None:
        config_path = Path(__file__).parents[1] / "config" / "does-not-exist.toml"
        output = StringIO()

        with redirect_stdout(output):
            result = main([str(config_path)])

        self.assertEqual(result, 2)
        self.assertIn("configuration error", output.getvalue())


if __name__ == "__main__":
    unittest.main()
