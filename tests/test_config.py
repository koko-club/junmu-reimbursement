import json
from pathlib import Path
import unittest

import sys

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app


APP_DIR = Path(__file__).resolve().parents[1]


class ConfigContractTest(unittest.TestCase):
    def test_config_contains_application_defaults(self):
        config_path = APP_DIR / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["template_path"],
            "resources/差旅报销单模板.xlsx",
        )
        self.assertEqual(config["output_dir"], "generated")
        self.assertEqual(config["host"], "127.0.0.1")
        self.assertEqual(config["port"], 0)
        self.assertEqual(config["soffice_path"], "")
        self.assertEqual((APP_DIR / config["output_dir"]).parent, APP_DIR)

    def test_relative_template_path_resolves_from_config_directory(self):
        config = app._load_config(APP_DIR / "config.json")
        self.assertEqual(config["template_path"], APP_DIR / "resources/差旅报销单模板.xlsx")
        self.assertTrue(config["template_path"].is_file())


if __name__ == "__main__":
    unittest.main()
