import json
from pathlib import Path
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config import AppConfig, load_config


class ConfigContractTest(unittest.TestCase):
    def write_config(self, directory: Path, values: dict) -> Path:
        path = directory / "config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        return path

    def test_config_contains_application_defaults(self):
        config_path = APP_DIR / "config.json"
        raw = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(raw["template_path"], "resources/差旅报销单模板.xlsx")
        self.assertEqual(raw["data_dir"], "data")
        self.assertEqual(raw["host"], "0.0.0.0")
        self.assertEqual(raw["port"], 8800)
        self.assertEqual(raw["soffice_path"], "")
        self.assertEqual(raw["max_body_bytes"], 10485760)
        self.assertEqual(raw["max_concurrent_generations"], 2)
        self.assertFalse(raw["cookie_secure"])

    def test_relative_paths_resolve_from_config_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config_path = self.write_config(root, {
                "template_path": "resources/template.xlsx",
                "data_dir": "state",
                "templates_dir": "views",
                "static_dir": "assets",
            })

            config = load_config(config_path)

        self.assertIsInstance(config, AppConfig)
        self.assertEqual(config.template_path, root / "resources/template.xlsx")
        self.assertEqual(config.data_dir, root / "state")
        self.assertEqual(config.templates_dir, root / "views")
        self.assertEqual(config.static_dir, root / "assets")

    def test_environment_overrides_supported_values(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config_path = self.write_config(root, {
                "template_path": "template.xlsx",
                "data_dir": "data",
                "host": "127.0.0.1",
                "port": 8800,
                "soffice_path": "",
                "max_body_bytes": 1,
                "max_concurrent_generations": 1,
                "cookie_secure": False,
            })
            config = load_config(config_path, {
                "APP_HOST": "0.0.0.0",
                "APP_PORT": "9000",
                "APP_DATA_DIR": "runtime-data",
                "APP_SOFFICE_PATH": "/opt/soffice",
                "APP_MAX_BODY_BYTES": "2048",
                "APP_MAX_CONCURRENT_GENERATIONS": "3",
                "APP_COOKIE_SECURE": "YES",
            })

        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.data_dir, root / "runtime-data")
        self.assertEqual(config.soffice_path, "/opt/soffice")
        self.assertEqual(config.max_body_bytes, 2048)
        self.assertEqual(config.max_concurrent_generations, 3)
        self.assertTrue(config.cookie_secure)

    def test_port_zero_is_rejected_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as name:
            config_path = self.write_config(Path(name), {
                "template_path": "template.xlsx",
                "data_dir": "data",
                "port": 0,
            })

            with self.assertRaisesRegex(ValueError, "port"):
                load_config(config_path)

    def test_environment_port_zero_is_rejected_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as name:
            config_path = self.write_config(Path(name), {
                "template_path": "template.xlsx",
                "data_dir": "data",
                "port": 8800,
            })

            with self.assertRaisesRegex(ValueError, "port"):
                load_config(config_path, {"APP_PORT": "0"})

    def test_port_zero_and_supported_boolean_forms_are_accepted_with_opt_in(self):
        with tempfile.TemporaryDirectory() as name:
            config_path = self.write_config(Path(name), {
                "template_path": "template.xlsx",
                "data_dir": "data",
                "port": 0,
                "cookie_secure": "no",
            })
            config = load_config(
                config_path,
                {"APP_COOKIE_SECURE": "1"},
                allow_ephemeral_port=True,
            )

        self.assertEqual(config.port, 0)
        self.assertTrue(config.cookie_secure)

    def test_non_string_path_and_string_fields_are_rejected(self):
        fields = (
            "template_path",
            "data_dir",
            "templates_dir",
            "static_dir",
            "host",
            "soffice_path",
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for field in fields:
                for invalid_value in (None, False, {}):
                    values = {
                        "template_path": "template.xlsx",
                        "data_dir": "data",
                        "templates_dir": "templates",
                        "static_dir": "static",
                        "host": "127.0.0.1",
                        "soffice_path": "",
                    }
                    values[field] = invalid_value
                    with self.subTest(field=field, invalid_value=invalid_value):
                        with self.assertRaisesRegex(ValueError, field):
                            load_config(self.write_config(root, values))

    def test_invalid_values_are_rejected(self):
        cases = (
            ({"port": 65536}, "port"),
            ({"port": -1}, "port"),
            ({"port": 1.5}, "port"),
            ({"max_body_bytes": 0}, "max_body_bytes"),
            ({"max_concurrent_generations": 0}, "max_concurrent_generations"),
            ({"cookie_secure": "maybe"}, "cookie_secure"),
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for overrides, expected in cases:
                values = {
                    "template_path": "template.xlsx",
                    "data_dir": "data",
                    "port": 8800,
                    "max_body_bytes": 1,
                    "max_concurrent_generations": 1,
                    "cookie_secure": False,
                }
                values.update(overrides)
                with self.subTest(values=overrides):
                    with self.assertRaisesRegex(ValueError, expected):
                        load_config(self.write_config(root, values))


if __name__ == "__main__":
    unittest.main()
