"""Console entry point for stackchan-mcp.

Kept import-time side-effect free: ``load_dotenv`` / logging setup happen
only inside :func:`main`, registered as the ``stackchan-mcp`` console
script and re-exported via ``stackchan_mcp.__main__``.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import errno
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import __version__

if TYPE_CHECKING:
    from .gateway import Gateway
    from .notify_config import NotifyConfig
    from .ownership import LockInfo, LockMode

logger = logging.getLogger(__name__)

_DESCRIPTION = (
    "stdio MCP gateway for the StackChan / xiaozhi-esp32 firmware. "
    "Bridges stdio MCP clients (for example Claude Code) to a StackChan "
    "ESP32 device over WebSocket, and exposes an HTTP capture endpoint "
    "for photo uploads from the device."
)

_EPILOG = """\
Environment variables:
  STACKCHAN_TOKEN          Bearer token shared with the ESP32 firmware.
  VISION_URL               Full public capture URL (e.g. Tailscale Funnel).
  VISION_HOST              LAN IP of this machine, as seen from the ESP32.
  VISION_TOKEN             Optional separate token for VISION_URL uploads.
  STACKCHAN_AUDIO_HOOK_URL / STACKCHAN_AUDIO_HOOK_TOKEN
                           Device-driven listen capture push (token
                           falls back to STACKCHAN_TOKEN).
  HOST / WS_PORT           ESP32 WebSocket server bind (default 0.0.0.0:8765).
  CAPTURE_PORT             HTTP capture server port (default 8766).
  MCP_HTTP_HOST / MCP_HTTP_PORT / MCP_HTTP_ALLOWED_HOSTS
                           Streamable HTTP MCP server (default 127.0.0.1:8767).

See gateway/README.md and the top-level README.md for full setup,
including pairing the ESP32 firmware and configuring the WiFi gateway URL.
"""

_STDIO_TRANSPORT = "stdio"
_STREAMABLE_HTTP_TRANSPORT = "streamable-http"
_TRANSPORT_CHOICES = (_STDIO_TRANSPORT, _STREAMABLE_HTTP_TRANSPORT)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stackchan-mcp",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--check", action="store_true", help="Print the current gateway ownership lock status and exit."
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run a non-destructive configuration and port preflight, then exit. "
        "Exit 0 if ready to run, non-zero if at least one blocking issue is found.",
    )
    parser.add_argument(
        "--no-mdns", action="store_true", help="Disable mDNS/DNS-SD advertisement for the WebSocket endpoint."
    )
    subparsers = parser.add_subparsers(dest="command", metavar="{serve}")
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the StackChan gateway.",
        description="Start the StackChan gateway using the selected transport.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve_parser.add_argument(
        "--transport",
        choices=_TRANSPORT_CHOICES,
        default=_STDIO_TRANSPORT,
        help="Gateway transport to serve (default: stdio).",
    )
    serve_parser.add_argument(
        "--no-mdns",
        dest="serve_no_mdns",
        action="store_true",
        help="Disable mDNS/DNS-SD advertisement for the WebSocket endpoint.",
    )
    return parser


# --- Preflight diagnostics (--check) -----------------------------------------------------------------
# Side-effect free: loads ``.env``, reads env, non-blocking ``bind()`` probes of the server
# ports, then a concise human-readable report. Never touches an ESP32 or starts a server.

_BIND_ERROR_PREFIX = "bind error: "


def _check_port(host: str, port: int) -> tuple[bool, str | None]:
    """Probe ``(host, port)`` by binding every resolved address family.

    Returns ``(available, info)``: any family bound → ``(True, None)``; any
    ``EADDRINUSE`` → ``(False, "pid <N>, <cmd>")`` (or None when ``lsof``
    cannot identify the holder); all failed → ``(False, "bind error: ...")``.
    ``SO_REUSEADDR`` mirrors the gateway so TIME_WAIT ports are not
    misreported as in use.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return (False, f"{_BIND_ERROR_PREFIX}getaddrinfo failed: {exc}")

    last_error: str | None = None
    bound_at_least_once = False
    for family, socktype, proto, _canonname, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        if hasattr(socket, "SO_REUSEADDR"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except OSError:
                pass
        try:
            sock.bind(sockaddr)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return (False, _try_get_port_holder(port))
            reason = exc.strerror or (os.strerror(exc.errno) if exc.errno is not None else str(exc))
            last_error = f"{_BIND_ERROR_PREFIX}{reason}"
        else:
            bound_at_least_once = True
        finally:
            sock.close()

    if bound_at_least_once:
        return (True, None)
    return (False, last_error)


def _try_get_port_holder(port: int) -> str | None:
    """Best-effort lookup of the process holding ``port`` via ``lsof``."""
    if shutil.which("lsof") is None:
        return None
    try:
        result = subprocess.run(
            ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpcn"], capture_output=True, text=True, timeout=2, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    pid: str | None = None
    cmd: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c"):
            cmd = line[1:]
    if pid and cmd:
        return f"pid {pid}, {cmd}"
    if pid:
        return f"pid {pid}"
    return None


def _format_port_status(available: bool, holder: str | None) -> str:
    if available:
        return "AVAILABLE"
    if holder is None:
        return "IN USE"
    if holder.startswith(_BIND_ERROR_PREFIX):
        # Surface non-EADDRINUSE bind failures (EADDRNOTAVAIL, EACCES, ...)
        # instead of sending the user after a phantom holding process.
        return f"BIND ERROR ({holder.removeprefix(_BIND_ERROR_PREFIX)})"
    return f"IN USE ({holder})"


_TCP_PORT_RANGE = range(0, 65536)
_INVALID_PORT = "out of TCP port range 0-65535"


def _validate_port_value(raw: str, var: str) -> tuple[int | None, str]:
    """Parse ``raw`` as a TCP port → ``(port, var)`` or ``(None, "<var>=<raw> (...)")``
    so the preflight reports a blocking issue instead of crashing bind()."""
    try:
        value = int(raw)
    except ValueError:
        return (None, f"{var}={raw!r} (not an integer)")
    if value not in _TCP_PORT_RANGE:
        return (None, f"{var}={raw!r} ({_INVALID_PORT})")
    return (value, var)


def _resolve_port(
    var: str, default: int, fallback: str | None = None
) -> tuple[int | None, str]:
    """Resolve a port env var (with optional fallback var) like gateway.py does."""
    for name in (var,) if fallback is None else (var, fallback):
        raw = os.getenv(name)
        if raw is None:
            continue
        return _validate_port_value(raw, name)
    return (default, "default")


def _resolve_ws_port() -> tuple[int | None, str]:
    """Gateway.py precedence: ``WS_PORT`` → ``PORT`` → 8765."""
    return _resolve_port("WS_PORT", 8765, fallback="PORT")


def _resolve_capture_port() -> tuple[int | None, str]:
    """Gateway.py precedence: ``CAPTURE_PORT`` → 8766."""
    return _resolve_port("CAPTURE_PORT", 8766)


# Query parameter names (exact or suffix) always redacted in preflight output.
_SECRET_QUERY_KEYS = frozenset(
    {
        "access_token", "api_key", "apikey", "auth", "auth_token", "key", "password", "secret", "sig", "signature", "token",
    }
)
_SECRET_QUERY_KEY_SUFFIXES = ("signature", "token", "secret", "password", "credential", "credentials")


def _is_secret_query_key(key: str) -> bool:
    lower = key.lower()
    return lower in _SECRET_QUERY_KEYS or any(lower.endswith(s) for s in _SECRET_QUERY_KEY_SUFFIXES)


def _redact_url_secrets(url: str) -> str:
    """Mask userinfo and secret-looking query params so preflight output is
    safe to paste into a public issue or log; non-secret structure is kept
    and unparseable input round-trips unchanged."""
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return url

    netloc = parsed.netloc
    if "@" in netloc:
        # The username alone can leak information; replace the whole userinfo.
        _userinfo, _, host_part = netloc.rpartition("@")
        netloc = f"***:***@{host_part}"

    query = parsed.query
    if query:
        try:
            params = parse_qsl(query, keep_blank_values=True)
        except ValueError:
            params = None
        if params is not None:
            redacted = [(k, "***redacted***") if _is_secret_query_key(k) else (k, v) for k, v in params]
            query = urlencode(redacted)

    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, query, parsed.fragment))


def _load_dotenv() -> None:
    """Lazy ``.env`` loader as a single attachable seam: keeps
    ``import stackchan_mcp.cli`` side-effect free and lets tests monkeypatch
    it to escape the developer's real ``gateway/.env``."""
    from dotenv import load_dotenv

    load_dotenv()


def _run_ownership_check() -> int:
    """Print the current ownership lock status and exit cleanly."""
    from .ownership import is_pid_alive, read_lock

    info = read_lock()
    if info is None:
        print("no current owner")
        print("ownership preflight: ready")
        print("Result: ready. Exit 0.")
    elif is_pid_alive(info["pid"]):
        fields = [
            f"owner_id={info['owner_id']}",
            f"pid={info['pid']}",
            f"start_ts={info['start_ts']}",
            f"host={info['host']}",
        ]
        for key in ("mode", "http_endpoint", "started_by"):
            if key in info:
                fields.append(f"{key}={info[key]}")
        print(" ".join(fields))
    else:
        print(f"stale lock found: pid {info['pid']} not alive")
    return 0


# Homebrew lib dirs so opuslib's ctypes.find_library finds libopus.dylib on
# Apple Silicon (/opt/homebrew) and Intel (/usr/local) Macs.
_HOMEBREW_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")


def _ensure_libopus_findable() -> None:
    """Prepend existing Homebrew lib dirs to DYLD_LIBRARY_PATH on macOS so
    opuslib's ``ctypes.find_library("opus")`` can see a vanilla ``brew
    install opus``. Existing entries are kept ahead of the new ones; no-op
    off macOS.
    """
    if platform.system() != "Darwin":
        return

    existing = os.environ.get("DYLD_LIBRARY_PATH", "")
    paths: list[str] = [p for p in existing.split(":") if p]

    prepended = [d for d in _HOMEBREW_LIB_DIRS if d not in paths and os.path.isdir(d)]
    if not prepended:
        return

    os.environ["DYLD_LIBRARY_PATH"] = ":".join(prepended + paths)
    logger.debug(
        "Prepended Homebrew lib dirs to DYLD_LIBRARY_PATH so opuslib can find libopus: %s",
        prepended,
    )


def _setting(label: str, value: str | None, absent: str = "not set") -> None:
    """Print one 2-space-indented ``label value`` preflight line."""
    width = max(20, len(label) + 1)
    print(f"  {label:<{width}}{value if value else absent}")


def _check_port_line(url: str, host: str, port: int, sep: str, issues: int) -> int:
    """Probe one preflight port and print its status line."""
    available, holder = _check_port(host, port)
    print(f"  {url}{sep}{_format_port_status(available, holder)}")
    return issues + (0 if available else 1)


def _report_invalid(label: str, source: str, issues: int) -> int:
    """Print one preflight INVALID line and bump the issue count."""
    print(f"  {label} INVALID ({source})")
    return issues + 1


def _run_preflight() -> int:
    """Run preflight diagnostics; returns the desired process exit code.

    Output is fixed-width and grep-friendly. Exit 0 = "ready to run";
    blocking issues (port conflicts / unavailability / bind safety) make it
    non-zero. Missing optional config is reported but not blocking.
    """
    _load_dotenv()
    _ensure_libopus_findable()

    issues = 0
    print(f"stackchan-mcp {__version__} preflight")
    print()
    print("Configuration:")
    token = os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN")
    if token:
        _setting("STACKCHAN_TOKEN", "set (***redacted***)")
    else:
        _setting("STACKCHAN_TOKEN", None, absent="not set (gateway will accept any client)")
    _setting("MCP_HTTP_ALLOWED_HOSTS", os.getenv("MCP_HTTP_ALLOWED_HOSTS", ""))

    vision_host = os.getenv("VISION_HOST", "")
    capture_port_raw = os.getenv("CAPTURE_PORT", "8766")
    _setting("VISION_HOST", vision_host)

    vision_url_explicit = os.getenv("VISION_URL", "")
    if vision_url_explicit:
        _setting("VISION_URL", _redact_url_secrets(vision_url_explicit))
    elif vision_host:
        # Derived URL has no userinfo/query params, so no redaction needed.
        _setting("VISION_URL", f"(derived) http://{vision_host}:{capture_port_raw}/capture")
    else:
        _setting("VISION_URL", None, absent="not set (set VISION_HOST or VISION_URL for take_photo)")
    if os.getenv("VISION_TOKEN"):
        _setting("VISION_TOKEN", "set (***redacted***)")
    else:
        _setting("VISION_TOKEN", None, absent="not set (will reuse STACKCHAN_TOKEN)")

    audio_hook_url = os.getenv("STACKCHAN_AUDIO_HOOK_URL", "")
    if audio_hook_url:
        print(f"  STACKCHAN_AUDIO_HOOK_URL  {_redact_url_secrets(audio_hook_url)}")
        if os.getenv("STACKCHAN_AUDIO_HOOK_TOKEN"):
            print("  STACKCHAN_AUDIO_HOOK_TOKEN set (***redacted***)")
        else:
            print("  STACKCHAN_AUDIO_HOOK_TOKEN not set (will reuse STACKCHAN_TOKEN)")
    else:
        print("  STACKCHAN_AUDIO_HOOK_URL  not set (device-driven listen capture disabled)")

    print()
    print("Ports:")
    host = os.getenv("HOST", "0.0.0.0")
    mcp_http_host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
    ws_port, ws_source = _resolve_ws_port()
    cap_port, cap_source = _resolve_capture_port()
    raw_mcp_http_port = os.getenv("MCP_HTTP_PORT")
    if raw_mcp_http_port is None:
        mcp_http_port, mcp_http_source = (8767, "default")
    else:
        mcp_http_port, mcp_http_source = _validate_port_value(raw_mcp_http_port, "MCP_HTTP_PORT")

    if ws_port is None:
        issues = _report_invalid(f"ws://{host}:???    ", ws_source, issues)
    if cap_port is None:
        issues = _report_invalid(f"http://{host}:???  ", cap_source, issues)
    if mcp_http_port is None:
        issues = _report_invalid(f"http://{mcp_http_host}:???/mcp", mcp_http_source, issues)

    if ws_port is not None and cap_port is not None and ws_port == cap_port and ws_port != 0:
        # WS + HTTP capture are separate listeners; the second bind would
        # fail even though each independent probe binds-and-releases fine.
        # Port 0 is excluded: each bind((host, 0)) gets a fresh ephemeral
        # port, so two listeners configured with 0 do not collide.
        print(
            f"  WS_PORT ({ws_source}) and CAPTURE_PORT ({cap_source}) "
            f"both resolve to {ws_port}; the gateway needs distinct ports."
        )
        issues += 1

    if mcp_http_port is not None:
        for label, other_port, other_source in (
            ("WS_PORT", ws_port, ws_source),
            ("CAPTURE_PORT", cap_port, cap_source),
        ):
            if other_port is None or mcp_http_port == 0 or other_port == 0:
                continue
            if mcp_http_port == other_port:
                print(
                    f"  MCP_HTTP_PORT ({mcp_http_source}) and {label} ({other_source}) "
                    f"both resolve to {mcp_http_port}; the daemon needs distinct listener ports."
                )
                issues += 1

    if ws_port is not None:
        issues = _check_port_line(f"ws://{host}:{ws_port}", host, ws_port, "   ", issues)
    if cap_port is not None:
        issues = _check_port_line(f"http://{host}:{cap_port}", host, cap_port, " ", issues)
    if mcp_http_port is not None:
        from .http_server import validate_bind_safety

        issues = _check_port_line(
            f"http://{mcp_http_host}:{mcp_http_port}/mcp", mcp_http_host, mcp_http_port, " ", issues
        )
        try:
            validate_bind_safety(mcp_http_host, token)
        except ValueError as exc:
            print(f"  MCP HTTP bind safety: BLOCKED ({exc})")
            issues += 1

    print()
    if issues == 0:
        print("Result: ready. Exit 0.")
        return 0
    plural = "s" if issues > 1 else ""
    print(f"Result: {issues} issue{plural}. Exit 1.")
    return 1


def _load_gateway_with_notify() -> tuple[NotifyConfig, Gateway]:
    """Load notify config, rotate events when enabled, wire it into the gateway."""
    from .event_log import rotate_old_entries
    from .gateway import get_gateway
    from .notify_config import load_notify_config

    config = load_notify_config()
    if config.jsonl_enabled:
        rotate_old_entries(path=config.jsonl_path)
    gateway = get_gateway()
    esp32 = getattr(gateway, "esp32", None)
    set_notify_config = getattr(esp32, "set_notify_config", None)
    if callable(set_notify_config):
        set_notify_config(config)
    return config, gateway


async def _run(*, advertise_mdns: bool = True) -> None:
    """Start both the ESP32 WebSocket server and the stdio MCP server."""
    import signal

    from .stdio_server import run_stdio_server

    notify_config, gateway = _load_gateway_with_notify()
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _handle_sigterm() -> None:
        if main_task and not main_task.done():
            main_task.cancel()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

    await gateway.start(advertise_mdns=advertise_mdns)
    logger.info("Gateway started, waiting for ESP32 connections...")

    try:
        # Block until the MCP client disconnects.
        await run_stdio_server(notify_config=notify_config)
    except asyncio.CancelledError:
        logger.info("Received termination signal, shutting down...")
    finally:
        await gateway.stop()


def _configure_gateway_startup() -> None:
    """Load runtime configuration and logging for gateway startup paths."""
    _load_dotenv()
    _ensure_libopus_findable()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _acquire_startup_lock(
    *,
    mode: "LockMode" = _STDIO_TRANSPORT,
    http_endpoint: str | None = None,
    started_by: str | None = None,
) -> "LockInfo":
    """Claim the gateway ownership lock and register normal cleanup."""
    from .ownership import (
        OwnershipError,
        acquire_lock,
        generate_owner_id,
        release_lock_if_owner,
    )

    owner_id = generate_owner_id()
    try:
        if mode == _STDIO_TRANSPORT and http_endpoint is None and started_by is None:
            info = acquire_lock(owner_id)
        else:
            info = acquire_lock(owner_id, mode=mode, http_endpoint=http_endpoint, started_by=started_by)
    except OwnershipError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    try:
        print(
            f"stackchan-mcp: acquired ownership lock (owner_id={info['owner_id']}, pid={info['pid']})",
            file=sys.stderr,
        )
        atexit.register(release_lock_if_owner, info)
    except BaseException:
        release_lock_if_owner(info)
        raise

    return info


def _prepare_stdio_startup() -> "LockInfo":
    """Prepare the existing stdio gateway flow without changing its lock shape."""
    _configure_gateway_startup()
    return _acquire_startup_lock()


def _run_stdio_gateway(*, advertise_mdns: bool = True) -> None:
    """Run the existing stdio MCP gateway flow."""
    from .ownership import release_lock_if_owner

    info = _prepare_stdio_startup()
    try:
        try:
            asyncio.run(_run(advertise_mdns=advertise_mdns))
        except KeyboardInterrupt:
            pass
    finally:
        release_lock_if_owner(info)


def _resolve_mcp_http_endpoint() -> tuple[str, int]:
    """Resolve the Streamable HTTP daemon endpoint from environment."""
    host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
    raw_port = os.getenv("MCP_HTTP_PORT", "8767")
    port, source = _validate_port_value(raw_port, "MCP_HTTP_PORT")
    if port is None:
        print(f"stackchan-mcp: invalid MCP_HTTP_PORT: {source}", file=sys.stderr)
        sys.exit(1)
    return host, port


async def _run_streamable_http_daemon(
    *,
    host: str,
    port: int,
    owner_id: str,
    token: str | None,
    advertise_mdns: bool,
) -> None:
    """Run the Streamable HTTP MCP daemon until the ASGI server exits."""
    import uvicorn

    from .http_server import build_app, make_dispatch_fn
    from .queue import CommandQueue

    notify_config, gateway = _load_gateway_with_notify()
    queue = CommandQueue()
    app = build_app(
        queue,
        gateway=gateway,
        owner_id=owner_id,
        host=host,
        port=port,
        token=token,
        dispatch_fn=make_dispatch_fn(gateway),
        notify_config=notify_config,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info", lifespan="on"))

    await gateway.start(advertise_mdns=advertise_mdns)
    logger.info("Streamable HTTP MCP daemon starting on http://%s:%d/mcp", host, port)
    try:
        await server.serve()
    finally:
        await gateway.stop()


def _run_streamable_http_placeholder(*, advertise_mdns: bool = True) -> None:
    """Run the Streamable HTTP MCP daemon."""
    from .http_server import get_configured_token, validate_bind_safety
    from .ownership import release_lock_if_owner

    _configure_gateway_startup()
    host, port = _resolve_mcp_http_endpoint()
    token = get_configured_token()
    try:
        validate_bind_safety(host, token)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    info: LockInfo | None = None
    try:
        info = _acquire_startup_lock(
            mode=_STREAMABLE_HTTP_TRANSPORT,
            http_endpoint=f"{host}:{port}",
            started_by="cli-serve",
        )
        try:
            asyncio.run(
                _run_streamable_http_daemon(
                    host=host, port=port, owner_id=info["owner_id"], token=token, advertise_mdns=advertise_mdns
                )
            )
        except KeyboardInterrupt:
            pass
    finally:
        if info is not None:
            release_lock_if_owner(info)


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point: parse early flags without starting the
    server, then dispatch the stdio or ``serve`` flow. Side effects stay
    below argument parsing so ``import stackchan_mcp`` remains clean."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.check:
        sys.exit(_run_ownership_check())

    if args.preflight:
        # ``_run_preflight`` loads ``.env`` itself; do not double-load below.
        sys.exit(_run_preflight())

    if args.command is None:
        _run_stdio_gateway(advertise_mdns=not args.no_mdns)
        return

    if args.command == "serve":
        advertise_mdns = not (args.no_mdns or getattr(args, "serve_no_mdns", False))
        if args.transport == _STDIO_TRANSPORT:
            _run_stdio_gateway(advertise_mdns=advertise_mdns)
            return
        _run_streamable_http_placeholder(advertise_mdns=advertise_mdns)
        return


if __name__ == "__main__":
    main()
