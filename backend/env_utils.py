from pathlib import Path
from io import StringIO

from dotenv import load_dotenv

# Project root (one level above this backend package)
BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"
DATA_DIR    = BASE_DIR / "data"
ENV_FILE    = BASE_DIR / ".env"


def load_project_env(override: bool = True) -> None:
    """Load the project's .env file, overriding any stale system env vars.

    A UTF-8 BOM at the start of the file would corrupt the first variable name
    (e.g. ``\\ufeffSMTP_SERVER`` instead of ``SMTP_SERVER``), so we strip it
    before handing the file to python-dotenv.
    """
    if ENV_FILE.exists():
        env_path: Path = ENV_FILE
        with env_path.open("r", encoding="utf-8-sig") as handle:
            content = handle.read()
        load_dotenv(stream=StringIO(content), override=override)
    else:
        import warnings
        warnings.warn(f".env file not found at {ENV_FILE}. SMTP config may be missing.")
