"""Configuration module: pydantic schema and JSON defaults."""

from .config_io import (
    ConfigIOError,
    TEMPLATE_FILENAME,
    apply_config,
    dump_config_file,
    list_known_configs,
    load_config_file,
    write_template,
)
from .schema import AppConfig, load_default_config

__all__ = [
    "AppConfig",
    "ConfigIOError",
    "TEMPLATE_FILENAME",
    "apply_config",
    "dump_config_file",
    "list_known_configs",
    "load_config_file",
    "load_default_config",
    "write_template",
]
