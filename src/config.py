"""Config + environment loading shared by the CLI entrypoints."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "searches.yaml"


def load_config(path=CONFIG_PATH):
    """Parse config/searches.yaml into a dict."""
    with open(path) as f:
        return yaml.safe_load(f)


def load_env():
    """Load a local .env if present (no-op in CI, where secrets are real env vars)."""
    load_dotenv(ROOT / ".env")


def require_env(*names):
    """Return the named env vars, raising a clear error listing any that are missing."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nSet them in .env (local) or as GitHub Actions secrets (CI). "
            "See .env.example."
        )
    return {n: os.environ[n] for n in names}
