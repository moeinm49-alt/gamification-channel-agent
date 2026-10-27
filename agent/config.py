import os
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_config(path=None):
    p = pathlib.Path(path) if path else ROOT / "config.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["_root"] = str(p.parent)
    return cfg


def env(name, default=""):
    return os.environ.get(name, default).strip()
