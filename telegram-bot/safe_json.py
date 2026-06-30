import copy
import json
import os
import tempfile
from typing import Any, Callable


def _fresh_default(default: Any) -> Any:
    if callable(default):
        return default()
    return copy.deepcopy(default)


def read_json_file(
    path: str,
    default: Any,
    *,
    logger=None,
    encoding: str = "utf-8",
) -> Any:
    try:
        with open(path, encoding=encoding) as f:
            return json.load(f)
    except FileNotFoundError:
        return _fresh_default(default)
    except json.JSONDecodeError as exc:
        if logger:
            logger.warning("JSON file is corrupted: %s (%s)", path, exc)
        return _fresh_default(default)
    except OSError as exc:
        if logger:
            logger.warning("Cannot read JSON file: %s (%s)", path, exc)
        return _fresh_default(default)


def write_json_file(
    path: str,
    data: Any,
    *,
    logger=None,
    encoding: str = "utf-8",
    indent: int = 2,
) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    basename = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{basename}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if logger:
            logger.exception("Cannot write JSON file atomically: %s", path)
        raise
