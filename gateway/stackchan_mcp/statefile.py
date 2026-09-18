"""Shared state-file persistence, env-reading and time-formatting helpers.

The gateway's stateful modules each persist small JSON dicts to
``~/.stackchan/*.json`` (presence thresholds, heartbeat / proactive
daily spoke-counts, activity feeds) with the same discipline:

- **atomic persistence** — write to a temp file in the same directory,
  then ``os.replace``, so a crash mid-write never truncates the live
  file (and a reboot never resurrects a half-written state);
- **tolerant reads** — a missing, garbage or non-dict state file yields
  the safe value (``{}`` / no rows) instead of raising, so the gateway
  always starts on a fresh host;
- **env-or-default paths** — every path is overridable via a documented
  ``STACKCHAN_*`` env var, with ``~`` expanded, and the three-value
  "unset → default / ``off`` → disabled / anything else → that path"
  contract honoured consistently.

This module is the single home for those helpers. The owning modules
keep only their thin wrappers on top: clamping / schema validation plus
the exact WARNING prefix each one already emits.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---- atomic persistence ------------------------------------------------


def write_temp_text(directory: Path, text: str) -> Path:
    """Write ``text`` to a fresh temp file in ``directory``; return its path."""
    fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write(text)
    return Path(tmp)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (write-temp + os.replace).

    Creates the parent directory on demand. Raises OSError on failure;
    callers wrap it in their fire-and-forget WARNING-and-swallow contract.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = write_temp_text(path.parent, text)
    try:
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Persist ``payload`` as JSON atomically (write-temp + os.replace).

    The state-file flavour every owner writes (``json.dump(payload, ...,
    ensure_ascii=False)`` then ``os.replace``); raises OSError on failure.
    """
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False))


# ---- tolerant JSON reads -------------------------------------------------


def read_json_dict(path: Path, *, logger: logging.Logger, label: str) -> dict[str, Any]:
    """Read a JSON dict state file; missing / unreadable / non-dict yields {}.

    The WARNING carries the caller's ``label`` prefix so the per-module
    log line each owner already emits stays byte-identical.
    """
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("%s: unreadable state file %s (%s)", label, path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def read_jsonl(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Parse a JSONL file into ``(raw_line, row)`` pairs.

    Blank lines, malformed JSON and non-dict rows are skipped. The raw
    line text rides along so a rotation rewrite never re-serializes /
    reformats lines a writer emitted (e.g. ``activity_log.append``'s
    canonical lines). A missing or unreadable file raises OSError; the
    caller decides the handling.
    """
    out: list[tuple[str, dict[str, Any]]] = []
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
            out.append((stripped, obj))
    return out


def ts_unix_of(row: dict[str, Any]) -> float | None:
    """A row's numeric ``ts_unix``, or None when absent / unusable.

    Booleans are excluded (``isinstance(True, int)`` is True, but a
    ``ts_unix: true`` line is garbage, not epoch 1).
    """
    ts = row.get("ts_unix")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return float(ts)


# ---- env reading --------------------------------------------------------


def env_path_or_default(name: str, default: str | Path) -> Path:
    """A path from ``name`` (~-expanded), or ``default`` when unset/blank."""
    return Path(os.getenv(name, "") or default).expanduser()


def env_path(
    name: str,
    default: str | Path,
    *,
    blank_as_disabled: bool = False,
    disabled_value: str = "off",
) -> Path | None:
    """Resolve a path env var; None when disabled.

    Unset -> ``default`` (~-expanded). ``disabled_value`` (compared
    case-insensitively on the stripped value) -> None. Blank -> None when
    ``blank_as_disabled``, else ``default``. Anything else -> that path
    (~-expanded).
    """
    raw = os.getenv(name)
    if raw is None:
        return Path(default).expanduser()
    stripped = raw.strip()
    if not stripped:
        return None if blank_as_disabled else Path(default).expanduser()
    if stripped.lower() == disabled_value:
        return None
    return Path(stripped).expanduser()


def env_float(name: str, default: float, logger: logging.Logger) -> float:
    """A numeric env var; garbage logs a WARNING and falls back to ``default``."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "%s: invalid %s=%r; using %s",
            _short_name(logger),
            name,
            raw,
            default,
        )
        return default


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """An int env var; garbage or a value below ``minimum`` falls back."""
    raw = os.getenv(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def env_enabled(name: str) -> bool:
    """True when ``name`` holds an explicit truthy value (1/true/yes/on)."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# ---- time formatting ----------------------------------------------------


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ`` (ownership lock stamps)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_epoch(ts: float, tz: timezone, fmt: str) -> str:
    """Format an epoch second in ``tz`` with ``fmt`` (report labels)."""
    return datetime.fromtimestamp(ts, tz).strftime(fmt)


def _short_name(logger: logging.Logger) -> str:
    """The logger's last dotted component (the module it stands for)."""
    return logger.name.rsplit(".", 1)[-1]
