"""Environment configuration shared by the app, CLI and migrations."""
from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def env_flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} in the environment or .env")
    return value


ENV = os.getenv("ENV", "dev")
CLONE_MODE = env_flag("CLONE_MODE", ENV == "clone")
EXTERNAL_INTEGRATIONS_DISABLED = CLONE_MODE or env_flag("DISABLE_EXTERNAL_INTEGRATIONS")
