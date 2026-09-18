"""Tests for Stack-chan notification config loading."""

import logging
from pathlib import Path

import pytest

from stackchan_mcp.event_log import DEFAULT_LOG_PATH, PATH_ENV_VAR
from stackchan_mcp.notify_config import (
    CONFIG_ENV_VAR,
    MessageTemplate,
    load_notify_config,
    render_template,
    resolve_notify_config_path,
)


def _isolate_config_env(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv(PATH_ENV_VAR, raising=False)
    return home, xdg


def _write_xdg_config(xdg: Path, body: str) -> Path:
    path = xdg / "stackchan-mcp" / "notify.yml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def _load_yaml_config(monkeypatch, tmp_path, body):
    """Isolate env and load a notify config from an XDG yaml body."""
    _, xdg = _isolate_config_env(monkeypatch, tmp_path)
    _write_xdg_config(xdg, body)
    return load_notify_config()


def test_default_no_env_no_file_returns_all_off(monkeypatch, tmp_path):
    _isolate_config_env(monkeypatch, tmp_path)

    config = load_notify_config()

    assert config.legacy_event_enabled is False
    assert config.channels_enabled is False
    assert config.jsonl_enabled is False
    assert config.jsonl_path == DEFAULT_LOG_PATH
    assert config.messages[("touch", "tap")] == MessageTemplate(
        action="head_pat",
        template="head was tapped",
    )
    assert config.messages[("touch", "stroke")] == MessageTemplate(
        action="head_stroke",
        template="head was stroked for {duration_ms}ms",
    )


@pytest.mark.parametrize(
    "body, legacy, channels, jsonl",
    [
        ("legacy_event:\n  enabled: true\n", True, False, False),
        ("channels:\n  enabled: true\n", False, True, False),
        (
            "legacy_event:\n"
            "  enabled: true\n"
            "channels:\n"
            "  enabled: true\n"
            "jsonl:\n"
            "  enabled: true\n",
            True,
            True,
            True,
        ),
    ],
)
def test_yaml_enable_flags(monkeypatch, tmp_path, body, legacy, channels, jsonl):
    config = _load_yaml_config(monkeypatch, tmp_path, body)
    assert config.legacy_event_enabled is legacy
    assert config.channels_enabled is channels
    assert config.jsonl_enabled is jsonl


def test_yaml_jsonl_only_with_custom_path(monkeypatch, tmp_path):
    custom_path = tmp_path / "events.jsonl"
    config = _load_yaml_config(
        monkeypatch,
        tmp_path,
        f"jsonl:\n  enabled: true\n  path: {custom_path}\n",
    )

    assert config.legacy_event_enabled is False
    assert config.channels_enabled is False
    assert config.jsonl_enabled is True
    assert config.jsonl_path == custom_path


def test_stackchan_events_path_overrides_yaml_jsonl_path(monkeypatch, tmp_path):
    _, xdg = _isolate_config_env(monkeypatch, tmp_path)
    yaml_path = tmp_path / "from-yaml.jsonl"
    env_path = tmp_path / "from-env.jsonl"
    _write_xdg_config(xdg, f"jsonl:\n  enabled: true\n  path: {yaml_path}\n")
    monkeypatch.setenv(PATH_ENV_VAR, str(env_path))

    config = load_notify_config()

    assert config.jsonl_enabled is True
    assert config.jsonl_path == env_path


def test_stackchan_notify_config_env_loads_that_path(monkeypatch, tmp_path):
    _isolate_config_env(monkeypatch, tmp_path)
    config_path = tmp_path / "explicit-notify.yml"
    config_path.write_text("channels:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config_path))

    assert resolve_notify_config_path() == config_path
    assert load_notify_config().channels_enabled is True


def test_stackchan_notify_config_env_missing_warns_and_returns_none(
    monkeypatch, tmp_path, caplog
):
    _isolate_config_env(monkeypatch, tmp_path)
    missing = tmp_path / "missing.yml"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(missing))

    with caplog.at_level(logging.WARNING):
        path = resolve_notify_config_path()

    assert path is None
    assert "non-existent file" in caplog.text


@pytest.mark.parametrize(
    "body, log_substring",
    [
        ("legacy_event: [\n", "Failed to load Stack-chan notify config"),
        (
            "legacy_event:\n  enabled: yes please\n",
            "legacy_event.enabled must be a boolean",
        ),
    ],
)
def test_bad_yaml_falls_back_to_all_off(
    monkeypatch, tmp_path, caplog, body, log_substring
):
    _, xdg = _isolate_config_env(monkeypatch, tmp_path)
    _write_xdg_config(xdg, body)

    with caplog.at_level(logging.WARNING):
        config = load_notify_config()

    assert config.legacy_event_enabled is False
    assert config.channels_enabled is False
    assert config.jsonl_enabled is False
    assert log_substring in caplog.text


def test_custom_messages_override_matching_default(monkeypatch, tmp_path):
    config = _load_yaml_config(
        monkeypatch,
        tmp_path,
        "messages:\n"
        "  touch:\n"
        "    tap:\n"
        "      action: head_knock\n"
        "      template: \"(head knock, {duration_ms}ms)\"\n",
    )

    assert config.messages[("touch", "tap")] == MessageTemplate(
        action="head_knock",
        template="(head knock, {duration_ms}ms)",
    )
    assert config.messages[("touch", "stroke")].action == "head_stroke"


def test_render_template_substitutes_and_preserves_unknown_placeholders():
    rendered = render_template("tap {duration_ms}ms {unknown}", {"duration_ms": 350})

    assert rendered == "tap 350ms {unknown}"


def test_render_template_falls_back_on_malformed_format_strings():
    """Malformed templates must not crash the dispatch path.

    ``str.format_map`` raises ``AttributeError`` for ``{x.attr}`` on a
    non-attribute value and ``TypeError`` for ``{x[idx]}`` on a
    non-subscriptable value; each bad template is returned as-is.
    """
    payload = {"duration_ms": 350}

    # ``{duration_ms.foo}`` triggers AttributeError on int's .foo access.
    attr_template = "tap {duration_ms.foo}ms"
    assert render_template(attr_template, payload) == attr_template

    # ``{duration_ms[bad]}`` triggers TypeError on int subscript.
    item_template = "tap {duration_ms[bad]}ms"
    assert render_template(item_template, payload) == item_template

    # ``{unknown.foo}`` exercises the _SafeFormatDict missing key path
    # combined with a downstream attribute access; the fallback should
    # also return the original template here rather than raise.
    unknown_attr_template = "tap {unknown.foo}ms"
    assert render_template(unknown_attr_template, payload) == unknown_attr_template
