"""Where the app keeps its files.

Source lives in basestation/; anything the app *writes* (recent hosts, crash
log, captured clouds) goes in data/ beside it, so the project root stays clean
and one folder is all you ever need to back up or delete.
"""

import tempfile
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
DATA_DIR = ROOT / "data"
ASSETS_DIR = PACKAGE_DIR / "assets"


def data_path(*parts: str) -> Path:
    """A path inside data/, creating data/ on first use.

    If the project folder is read-only (installed under Program Files, a locked
    share), fall back to a folder in the system temp directory rather than
    failing at import time and keeping the whole app from starting.
    """
    global DATA_DIR
    try:
        DATA_DIR.mkdir(exist_ok=True)
    except OSError:
        DATA_DIR = Path(tempfile.gettempdir()) / "SurveyRigBaseStation"
        DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR.joinpath(*parts)
