"""Shared low-level helpers for the yorishiro leaf modules.

Several gateway modules independently re-implemented the same small
helpers: atomic state-file writes (``tempfile.mkstemp`` + ``os.replace``),
environ boolean/number parsing, and JSONL append/read/rotation. This
module consolidates those so the per-module copies can shrink to thin
call wrappers.

All functions are pure file/env helpers — no network, no asyncio, no
gateway import — so importing this module has no side effects and
introduces no cycles.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Truthy env-string values (case-insensitive) for ``env_bool``.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
#: Falsy env-string values (case-insensitive) for ``env_bool``.
_FALSY = frozenset({"0", "false", "no", "off"})


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var: truthy ``1/true/yes/on``, falsy ``0/false/no/off``.

    Unset or an unrecognised value falls back to ``default``.
    """
    val = os.getenv(name, "").strip().lower()
    if not val:
        return default
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    return default


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    component: str | None = None,
) -> float:
    """Parse a numeric env var, warning and falling back on garbage.

    ``minimum`` / ``maximum`` are applied after parsing (a NaN or a value
    outside the range clamps to the bound). ``component`` names the module
    in the WARNING log line (defaults to ``name`` itself).
    """
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _warn_invalid(component or name, name, raw, default)
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    component: str | None = None,
) -> int:
    """Parse an int env var, warning and falling back on garbage.

    ``minimum`` / ``maximum`` are applied after parsing. Returns an int.
    """
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _warn_invalid(component or name, name, raw, default)
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def clamp(value: Any, lo: float, hi: float, default: Any) -> Any:
    """Int/float-clamp a value to ``[lo, hi]``; non-numeric -> ``default``."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
    return min(max(v, lo), hi)


def _warn_invalid(component: str, name: str, raw: str, default: Any) -> None:
    logger.warning("%s: invalid %s=%r; using %s", component, name, raw, default)


# ---------------------------------------------------------------------------
# Atomic file writes (write-temp + os.replace so a crash mid-write can never
# truncate state/log files). The caller owns any error handling / logging.
# ---------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` (write-temp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_json(path: Path, data: Any, *, indent: int | None = None) -> None:
    """Atomically replace ``path`` with ``json.dumps(data)``."""
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    atomic_write_text(path, payload)


def atomic_write_lines(path: Path, lines: list[str]) -> None:
    """Atomically replace ``path`` with ``lines`` (write-temp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        tmp.writelines(lines)
        tmp.flush()
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# JSONL append / read / rotation. All errors are the caller's to log; these
# helpers raise so each module can preserve its own WARNING messages.
# ---------------------------------------------------------------------------


def jsonl_append(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line to ``path`` (creating the parent directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def jsonl_read(
    path: Path | None,
    *,
    ts_key: str = "ts_unix",
) -> Iterable[dict[str, Any]]:
    """Yield parsed dict records with a usable numeric ``ts_key`` from JSONL.

    A missing file yields nothing; malformed lines, non-dicts, and rows
    whose ``ts_key`` is not a usable number (including ``bool``) are
    skipped. Empty lines are ignored.
    """
    if path is None or not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = obj.get(ts_key)
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            yield obj


def jsonl_keep_lines(
    path: Path,
    *,
    cutoff: float,
    ts_key: str = "ts_unix",
) -> list[str]:
    """Return JSONL lines to keep after pruning rows older than ``cutoff``.

    Reads ``path`` and returns each surviving line (round-tripped through
    :func:`jsonl_read` so it is re-serialised consistently), i.e. the
    content :func:`atomic_write_lines` should write back. A missing path
    yields ``[]``. Raises on disk/permission errors — the caller owns the
    WARNING handling and the write+``os.replace``.
    """
    if not path.exists():
        return []
    return [
        json.dumps(obj, ensure_ascii=False) + "\n"
        for obj in jsonl_read(path, ts_key=ts_key)
        if obj.get(ts_key, 0.0) >= cutoff
    ]

