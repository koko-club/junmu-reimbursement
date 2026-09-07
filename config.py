"""Application configuration loaded from JSON and environment variables."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class AppConfig:
    template_path: Path
    data_dir: Path
    templates_dir: Path
    static_dir: Path
    host: str
    port: int
    soffice_path: str
    max_body_bytes: int
    max_concurrent_generations: int
    cookie_secure: bool


_DEFAULTS = {
    "template_path": "resources/差旅报销单模板.xlsx",
    "data_dir": "data",
    "templates_dir": "templates",
    "static_dir": "static",
    "host": "127.0.0.1",
    "port": 8800,
    "soffice_path": "",
    "max_body_bytes": 10 * 1024 * 1024,
    "max_concurrent_generations": 2,
    "cookie_secure": False,
}


def _path_from_config(value: object, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else base / path


def _integer(value: object, name: str) -> int:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _boolean(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> AppConfig:
    """Load configuration, resolving relative paths against the JSON file location."""
    config_path = Path(path)
    try:
        values = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("config must contain valid JSON") from exc
    if not isinstance(values, dict):
        raise ValueError("config must be an object")

    environment = os.environ if environ is None else environ
    merged = {**_DEFAULTS, **values}
    overrides = {
        "host": "APP_HOST",
        "port": "APP_PORT",
        "data_dir": "APP_DATA_DIR",
        "soffice_path": "APP_SOFFICE_PATH",
        "max_body_bytes": "APP_MAX_BODY_BYTES",
        "max_concurrent_generations": "APP_MAX_CONCURRENT_GENERATIONS",
        "cookie_secure": "APP_COOKIE_SECURE",
    }
    for key, environment_key in overrides.items():
        if environment_key in environment:
            merged[key] = environment[environment_key]

    base = config_path.parent
    port = _integer(merged["port"], "port")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    max_body_bytes = _integer(merged["max_body_bytes"], "max_body_bytes")
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")
    max_concurrent_generations = _integer(
        merged["max_concurrent_generations"], "max_concurrent_generations"
    )
    if max_concurrent_generations <= 0:
        raise ValueError("max_concurrent_generations must be positive")
    host = str(merged["host"]).strip()
    if not host:
        raise ValueError("host must not be empty")

    return AppConfig(
        template_path=_path_from_config(merged["template_path"], base),
        data_dir=_path_from_config(merged["data_dir"], base),
        templates_dir=_path_from_config(merged["templates_dir"], base),
        static_dir=_path_from_config(merged["static_dir"], base),
        host=host,
        port=port,
        soffice_path=str(merged["soffice_path"]),
        max_body_bytes=max_body_bytes,
        max_concurrent_generations=max_concurrent_generations,
        cookie_secure=_boolean(merged["cookie_secure"], "cookie_secure"),
    )
