"""Local non-secret configuration. CLI options override these defaults."""
import json
import os
from pathlib import Path

def load_settings():
    path = Path(os.environ.get("PR_TOOLS_CONFIG", str(Path(__file__).with_name("config.json"))))
    config = {}
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config.json must contain an object")
        allowed = {"base_url", "months", "output", "ca_bundle", "cookie_file"}
        if set(config) - allowed:
            raise ValueError("Unknown config fields; do not store credentials in config.json")
        for key in ("base_url", "output", "ca_bundle", "cookie_file"):
            if key in config and not isinstance(config[key], str):
                raise ValueError(key + " must be a string")
        if "months" in config and (not isinstance(config["months"], list) or not all(isinstance(x, str) for x in config["months"])):
            raise ValueError("months must be a list of YYYY-MM strings")
        for key in ("output", "ca_bundle", "cookie_file"):
            if config.get(key):
                value = Path(config[key]).expanduser()
                config[key] = str(value if value.is_absolute() else path.resolve().parent / value)
    if os.environ.get("BITBUCKET_BASE_URL"):
        config["base_url"] = os.environ["BITBUCKET_BASE_URL"]
    return config
