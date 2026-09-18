"""Tests for mDNS/DNS-SD gateway advertisement."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import pytest

from stackchan_mcp import mdns_advertiser as mdns
from stackchan_mcp.mdns_advertiser import MdnsAdvertiser, build_advertisement

# Canonical address lists used across the refresh-loop scenarios.
OLD_ADDR = ["198.51.100.10"]
NEW_ADDR = ["203.0.113.20"]


def test_service_type_and_txt_defaults() -> None:
    advertisement = build_advertisement(
        host="192.0.2.10",
        port=8765,
        path="/",
    )

    assert advertisement is not None
    assert advertisement.service_type == "_stackchan-mcp._tcp.local."
    assert advertisement.service_name == "stackchan-mcp._stackchan-mcp._tcp.local."
    assert advertisement.port == 8765
    assert advertisement.properties == {"path": "/", "version": "1"}
    assert advertisement.parsed_addresses == ["192.0.2.10"]


def test_service_hostname_is_service_specific() -> None:
    assert mdns._build_service_hostname() == "stackchan-mcp.local."


@pytest.mark.parametrize(
    ("ifaddr", "socket_addresses", "expected", "required", "forbidden"),
    [
        # 10.0.0.5 is RFC1918 (tier 1) and sorts ahead of the public 192.0.2.10.
        (
            [("127.0.0.1", 8), ("192.0.2.10", 24), ("0.0.0.0", 24)],
            [("192.0.2.10", None), ("10.0.0.5", None)],
            ["10.0.0.5", "192.0.2.10"],
            [],
            [],
        ),
        # All kept; the three private addresses move ahead of the public one,
        # and within each tier the original enumeration order is preserved.
        (
            [("203.0.113.7", 24), ("192.168.0.10", 24), ("10.1.2.3", 8), ("172.16.5.6", 12)],
            [],
            ["192.168.0.10", "10.1.2.3", "172.16.5.6", "203.0.113.7"],
            [],
            [],
        ),
        # Real-device CGNAT scenario: 100.64.10.20/32 must remain advertised
        # but be tried only after the reachable LAN address.
        (
            [("100.64.10.20", 32), ("192.168.0.10", 24)],
            [],
            ["192.168.0.10", "100.64.10.20"],
            [],
            [],
        ),
        (
            [("192.168.0.0", 24), ("192.168.0.255", 24), ("192.168.0.10", 24)],
            [],
            ["192.168.0.10"],
            [],
            [],
        ),
        # /31 and /32 have no distinct network/broadcast address; a legitimate
        # host IP on such a prefix must not be dropped.
        (
            [("192.168.5.0", 32), ("10.0.0.0", 31)],
            [],
            ["192.168.5.0", "10.0.0.0"],
            [],
            [],
        ),
        # The socket source carries no prefix; a ".0"-looking address from it
        # cannot be classified as a network address and must be kept.
        (
            [],
            [("192.168.0.0", None), ("192.168.0.10", None)],
            ["192.168.0.0", "192.168.0.10"],
            [],
            [],
        ),
        # Regression: the socket source adopts the ifaddr prefix for network
        # address filtering (192.168.1.0 is excluded as a network address).
        (
            [("192.168.1.42", 24), ("192.168.1.0", 24)],
            [("192.168.1.0", None), ("192.168.1.42", None)],
            None,
            ["192.168.1.42"],
            ["192.168.1.0"],
        ),
        # ifaddr is enumerated before socket; the stable sort keeps that
        # combined order within each tier.
        (
            [("198.51.100.4", 24), ("192.168.1.2", 24)],
            [("203.0.113.9", None), ("10.5.5.5", None)],
            ["192.168.1.2", "10.5.5.5", "198.51.100.4", "203.0.113.9"],
            [],
            [],
        ),
    ],
    ids=[
        "wildcard-uses-all-usable-ipv4",
        "rfc1918-before-others",
        "cgnat-kept-after-rfc1918",
        "network-and-broadcast-excluded",
        "host-prefixes-are-not-network-or-broadcast",
        "no-prefix-addresses-not-excluded",
        "socket-inherits-ifaddr-prefix",
        "mixed-tier-preserves-within-tier-order",
    ],
)
def test_wildcard_host_address_selection(
    monkeypatch: pytest.MonkeyPatch,
    ifaddr: list[tuple[str, int]],
    socket_addresses: list[tuple[str, int | None]],
    expected: list[str] | None,
    required: list[str],
    forbidden: list[str],
) -> None:
    monkeypatch.setattr(mdns, "_iter_ifaddr_ipv4_addresses", lambda: ifaddr)
    monkeypatch.setattr(mdns, "_iter_socket_ipv4_addresses", lambda: socket_addresses)

    advertisement = build_advertisement(host="0.0.0.0", port=8765)

    assert advertisement is not None
    parsed = advertisement.parsed_addresses
    for address in required:
        assert address in parsed
    for address in forbidden:
        assert address not in parsed
    if expected is not None:
        assert parsed == expected


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_not_advertised(host: str) -> None:
    assert build_advertisement(host=host, port=8765) is None


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_unpublishable_ports_are_not_advertised(port: int) -> None:
    assert build_advertisement(host="192.0.2.10", port=port) is None


class RecordingServiceInfo:
    def __init__(self, service_type: str, service_name: str, **kwargs) -> None:
        self.type = service_type
        self.name = service_name
        self.kwargs = kwargs


class RecordingAsyncZeroconf:
    instances: list[RecordingAsyncZeroconf] = []
    register_errors: list[Exception | None] = []

    @classmethod
    def reset(cls, register_errors: list[Exception | None] | None = None) -> None:
        cls.instances = []
        cls.register_errors = list(register_errors or [])

    def __init__(self, *, interfaces=None) -> None:
        self.interfaces = interfaces
        self.registered = []
        self.register_attempts = []
        self.unregistered = []
        self.closed = False
        self.close_count = 0
        self.instances.append(self)

    async def async_register_service(
        self, info: RecordingServiceInfo, *, allow_name_change: bool = False
    ) -> None:
        self.register_attempts.append((info, allow_name_change))
        if self.register_errors:
            error = self.register_errors.pop(0)
            if error is not None:
                raise error
        self.registered.append((info, allow_name_change))

    async def async_unregister_service(self, info: RecordingServiceInfo) -> None:
        self.unregistered.append(info)

    async def async_close(self) -> None:
        self.closed = True
        self.close_count += 1


class RenamingAsyncZeroconf(RecordingAsyncZeroconf):
    """Recording fake whose register rewrites the service name."""

    renamed_name = "stackchan-mcp-2._stackchan-mcp._tcp.local."

    async def async_register_service(
        self, info: RecordingServiceInfo, *, allow_name_change: bool = False
    ) -> None:
        info.name = self.renamed_name
        await super().async_register_service(info, allow_name_change=allow_name_change)


def install_recording_zeroconf(
    monkeypatch: pytest.MonkeyPatch,
    *,
    register_errors: list[Exception | None] | None = None,
    zeroconf_cls: type[RecordingAsyncZeroconf] = RecordingAsyncZeroconf,
) -> list[RecordingAsyncZeroconf]:
    zeroconf_cls.reset(register_errors)
    monkeypatch.setattr(
        mdns,
        "_load_zeroconf_classes",
        lambda: (zeroconf_cls, RecordingServiceInfo),
    )
    return zeroconf_cls.instances


def fast_advertiser(interval: float = 0.01) -> MdnsAdvertiser:
    # Production intervals are validated at 10-300 seconds; tests shorten the
    # private sleep value after construction so debounce behavior is practical.
    advertiser = MdnsAdvertiser(refresh_interval=10.0)
    advertiser._refresh_interval = interval
    return advertiser


def sequence_addresses(values: list[list[str]]):
    remaining = [list(value) for value in values]
    fallback = list(remaining[-1])

    def next_addresses() -> list[str]:
        if remaining:
            return list(remaining.pop(0))
        return list(fallback)

    return next_addresses


async def wait_until(predicate, *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate()


@asynccontextmanager
async def started_advertiser(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sequence: list[list[str]] | None = None,
    addresses: list[str] | None = None,
    host: str = "0.0.0.0",
    port: int = 8765,
    path: str = "/",
    interval: float = 0.01,
    register_errors: list[Exception | None] | None = None,
    zeroconf_cls: type[RecordingAsyncZeroconf] = RecordingAsyncZeroconf,
    setup: Callable[[pytest.MonkeyPatch], None] | None = None,
) -> AsyncIterator[tuple[MdnsAdvertiser, list[RecordingAsyncZeroconf]]]:
    """Start a fast advertiser against the recording fakes; always stop it."""
    instances = install_recording_zeroconf(
        monkeypatch, register_errors=register_errors, zeroconf_cls=zeroconf_cls
    )
    if sequence is not None:
        monkeypatch.setattr(
            mdns, "_enumerate_usable_ipv4_addresses", sequence_addresses(sequence)
        )
    elif addresses is not None:
        monkeypatch.setattr(mdns, "_enumerate_usable_ipv4_addresses", lambda: addresses)
    if setup is not None:
        setup(monkeypatch)
    advertiser = fast_advertiser(interval=interval)
    await advertiser.start(host=host, port=port, path=path)
    try:
        yield advertiser, instances
    finally:
        await advertiser.stop()


@pytest.mark.asyncio
async def test_advertiser_registers_service(monkeypatch: pytest.MonkeyPatch) -> None:
    async with started_advertiser(
        monkeypatch, addresses=["192.0.2.10", "10.0.0.5"]
    ) as (_, instances):
        assert len(instances) == 1
        zeroconf = instances[0]
        assert len(zeroconf.registered) == 1
        info, allow_name_change = zeroconf.registered[0]
        assert allow_name_change is True
        assert info.type == "_stackchan-mcp._tcp.local."
        assert info.name == "stackchan-mcp._stackchan-mcp._tcp.local."
        assert info.kwargs["port"] == 8765
        assert info.kwargs["properties"] == {"path": "/", "version": "1"}
        assert info.kwargs["parsed_addresses"] == ["192.0.2.10", "10.0.0.5"]

    # stop() inside the manager unregisters and closes the zeroconf.
    assert zeroconf.unregistered == [info]
    assert zeroconf.closed is True


@pytest.mark.asyncio
async def test_advertiser_warns_when_service_name_changes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    renamed_service_name = RenamingAsyncZeroconf.renamed_name
    caplog.set_level("WARNING", logger=mdns.__name__)

    async with started_advertiser(
        monkeypatch, host="192.0.2.10", zeroconf_cls=RenamingAsyncZeroconf
    ) as (advertiser, instances):
        assert len(instances) == 1
        zeroconf = instances[0]
        assert len(zeroconf.registered) == 1
        info, allow_name_change = zeroconf.registered[0]
        assert allow_name_change is True
        assert info.name == renamed_service_name
        assert zeroconf.closed is False
        assert advertiser._zeroconf is zeroconf
        assert advertiser._service_info is info
        assert "modified name" in caplog.text
        assert renamed_service_name in caplog.text

    # stop() inside the manager unregisters and closes the zeroconf.
    assert zeroconf.unregistered == [info]
    assert zeroconf.closed is True


@pytest.mark.asyncio
async def test_advertiser_closes_zeroconf_when_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = install_recording_zeroconf(
        monkeypatch, register_errors=[RuntimeError("mock registration failure")]
    )

    advertiser = MdnsAdvertiser()
    with pytest.raises(RuntimeError, match="mock registration failure"):
        await advertiser.start(host="192.0.2.10", port=8765, path="/")

    assert len(instances) == 1
    assert instances[0].closed is True


@pytest.mark.parametrize(
    "sequence",
    [
        # Stable address list: no reconfigure ever triggers.
        [OLD_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR],
        # One transient tick of another address, then back: debounce absorbs it.
        [OLD_ADDR, NEW_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR],
    ],
    ids=["stable-address-list", "transient-single-tick-change"],
)
@pytest.mark.asyncio
async def test_refresh_stable_observation_does_not_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
    sequence: list[list[str]],
) -> None:
    async with started_advertiser(monkeypatch, sequence=sequence) as (
        advertiser,
        instances,
    ):
        await asyncio.sleep(advertiser._refresh_interval * 6)

        assert len(instances) == 1
        assert instances[0].unregistered == []
        assert len(instances[0].registered) == 1
        assert advertiser._last_advertised_addresses == tuple(OLD_ADDR)


@pytest.mark.asyncio
async def test_refresh_changed_address_list_reconfigures_once_after_debounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with started_advertiser(
        monkeypatch, sequence=[OLD_ADDR, OLD_ADDR, NEW_ADDR, NEW_ADDR, NEW_ADDR]
    ) as (advertiser, instances):
        await wait_until(lambda: len(instances) == 2)

        assert instances[0].unregistered == [instances[0].registered[0][0]]
        assert instances[0].closed is True
        assert len(instances[1].registered) == 1
        assert advertiser._last_advertised_addresses == tuple(NEW_ADDR)


@pytest.mark.asyncio
async def test_refresh_survives_transient_empty_build_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with started_advertiser(
        monkeypatch, sequence=[OLD_ADDR, NEW_ADDR, NEW_ADDR, NEW_ADDR, NEW_ADDR, NEW_ADDR]
    ) as (advertiser, instances):
        original_build_advertisement = mdns.build_advertisement
        empty_builds = [None]

        def flaky_build_advertisement(*, host: str, port: int, path: str = "/"):
            if empty_builds:
                return empty_builds.pop(0)
            return original_build_advertisement(host=host, port=port, path=path)

        monkeypatch.setattr(mdns, "build_advertisement", flaky_build_advertisement)
        await wait_until(lambda: len(instances) == 2)

        assert advertiser._refresh_task is not None
        assert not advertiser._refresh_task.done()
        assert instances[0].unregistered == [instances[0].registered[0][0]]
        assert advertiser._last_advertised_addresses == tuple(NEW_ADDR)


@pytest.mark.asyncio
async def test_double_start_closes_previous_registration_before_new_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with started_advertiser(monkeypatch, host="198.51.100.10") as (
        advertiser,
        instances,
    ):
        first_task = advertiser._refresh_task
        await advertiser.start(host="203.0.113.20", port=8765, path="/")

        assert first_task is not None
        assert first_task.done()
        assert len(instances) == 2
        assert instances[0].unregistered == [instances[0].registered[0][0]]
        assert instances[0].closed is True
        assert instances[1].closed is False
        assert advertiser._zeroconf is instances[1]


@pytest.mark.asyncio
async def test_stop_cancels_active_refresh_task_and_closes_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = install_recording_zeroconf(monkeypatch)

    advertiser = fast_advertiser(interval=0.05)
    await advertiser.start(host="198.51.100.10", port=8765, path="/")
    refresh_task = advertiser._refresh_task
    await advertiser.stop()

    assert refresh_task is not None
    assert refresh_task.done()
    assert instances[0].unregistered == [instances[0].registered[0][0]]
    assert instances[0].closed is True
    assert advertiser._refresh_task is None
    assert advertiser._zeroconf is None
    assert advertiser._service_info is None


@pytest.mark.asyncio
async def test_refresh_compares_canonical_address_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = ["198.51.100.10", "203.0.113.20"]
    forced_order = ["203.0.113.20", "198.51.100.10"]

    # The refresh loop compares the canonical list returned by the enumerator.
    # Raw interface ordering is normalized by _select_advertised_addresses; if
    # the canonical order is unchanged, no recycle is needed.
    async with started_advertiser(
        monkeypatch, sequence=[canonical, canonical, canonical, canonical]
    ) as (advertiser, instances):
        await asyncio.sleep(advertiser._refresh_interval * 3)
        assert len(instances) == 1
        assert instances[0].unregistered == []

    async with started_advertiser(
        monkeypatch,
        sequence=[canonical, forced_order, forced_order, forced_order],
    ) as (advertiser, instances):
        await wait_until(lambda: len(instances) == 2)
        assert instances[0].unregistered == [instances[0].registered[0][0]]
        assert advertiser._last_advertised_addresses == tuple(forced_order)


@pytest.mark.asyncio
async def test_refresh_register_failure_cleans_up_and_loop_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with started_advertiser(
        monkeypatch,
        sequence=[OLD_ADDR, NEW_ADDR, NEW_ADDR, NEW_ADDR, NEW_ADDR, NEW_ADDR],
        register_errors=[None, RuntimeError("mock refresh registration failure"), None],
    ) as (advertiser, instances):
        await wait_until(
            lambda: len(instances) == 3 and advertiser._zeroconf is instances[2]
        )

        assert instances[0].unregistered == [instances[0].registered[0][0]]
        assert instances[0].closed is True
        assert instances[1].registered == []
        assert instances[1].closed is True
        assert len(instances[2].registered) == 1
        assert advertiser._refresh_task is not None
        assert not advertiser._refresh_task.done()
        assert advertiser._last_advertised_addresses == tuple(NEW_ADDR)


def test_refresh_interval_validation() -> None:
    with pytest.raises(ValueError):
        MdnsAdvertiser(refresh_interval=5)
    with pytest.raises(ValueError):
        MdnsAdvertiser(refresh_interval=500)
    assert MdnsAdvertiser(refresh_interval=30)._refresh_interval == 30


@pytest.mark.asyncio
async def test_stop_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    async with started_advertiser(monkeypatch, host="198.51.100.10") as (
        advertiser,
        instances,
    ):
        await advertiser.stop()
        close_count = instances[0].close_count
        unregister_count = len(instances[0].unregistered)

        await advertiser.stop()

        assert instances[0].close_count == close_count
        assert len(instances[0].unregistered) == unregister_count


@pytest.mark.asyncio
async def test_refresh_with_concrete_host_ignores_unrelated_interface_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review round 2 Finding 1: when started with a concrete HOST
    (not a wildcard), the refresh loop must compare against that HOST's
    resolution only — not against the full host-interface enumeration.
    Otherwise a multi-NIC / Tailscale host churns the registration on every
    refresh tick even though the actually-advertised set never changes.
    """

    def concrete_host_setup(mp: pytest.MonkeyPatch) -> None:
        # The concrete-host resolver stays constant across all refresh ticks.
        mp.setattr(
            mdns,
            "_resolve_concrete_host_ipv4_addresses",
            lambda host: ["192.0.2.10"] if host == "192.0.2.10" else [],
        )
        # The wildcard enumerator returns a DIFFERENT (extra) set that must NOT
        # influence the refresh decision when host is concrete.
        mp.setattr(
            mdns,
            "_enumerate_usable_ipv4_addresses",
            lambda: ["192.0.2.10", "10.0.0.5"],
        )

    async with started_advertiser(
        monkeypatch,
        host="192.0.2.10",
        setup=concrete_host_setup,
    ) as (advertiser, instances):
        # Let several refresh cycles run; the concrete-host comparison must
        # stay stable so no second zeroconf instance is ever created.
        await asyncio.sleep(advertiser._refresh_interval * 3)

        assert len(instances) == 1
        assert instances[0].unregistered == []
        assert advertiser._last_advertised_addresses == ("192.0.2.10",)


@pytest.mark.asyncio
async def test_reconfigure_register_failure_then_ip_revert_still_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review round 2 Finding 2: if a reconfigure closes the old
    registration but the new registration then fails, the cached
    ``_last_advertised_addresses`` must not point at the now-defunct old
    value. Otherwise, if the host IP reverts to the old value (Wi-Fi
    re-association, DHCP renewal returning the previous lease, etc.), the
    refresh loop sees ``current == _last_advertised`` and stays quiet —
    leaving the advertisement permanently dead until manual restart.
    """
    # Sequence: initial register (OLD) → refresh observes NEW → debounce
    # confirm new → reconfigure: close old, register new fails → IP reverts
    # to old → refresh observes old (≠ None after the failed reconfigure)
    # → debounce confirm old → reconfigure: register succeeds.
    async with started_advertiser(
        monkeypatch,
        sequence=[OLD_ADDR, NEW_ADDR, NEW_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR],
        register_errors=[None, RuntimeError("mock reconfigure register fail"), None],
    ) as (advertiser, instances):
        # Wait until the third zeroconf instance is created — this is the
        # recovery that only happens if the previous failure cleared
        # _last_advertised.
        await wait_until(
            lambda: len(instances) == 3 and advertiser._zeroconf is instances[2]
        )

        # instance 0: original (old IP) was closed during the failed reconfigure.
        assert instances[0].closed is True
        # instance 1: register call raised, no successful registration recorded,
        # internal cleanup closes the partially-constructed zeroconf.
        assert instances[1].registered == []
        assert instances[1].closed is True
        # instance 2: recovery succeeded against the reverted (old) IP.
        assert len(instances[2].registered) == 1
        assert advertiser._last_advertised_addresses == tuple(OLD_ADDR)
        assert advertiser._refresh_task is not None
        assert not advertiser._refresh_task.done()


@pytest.mark.asyncio
async def test_reconfigure_close_failure_then_revert_to_old_ip_still_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review round 2 Finding 3: if the OLD-instance close itself
    raises during reconfigure (e.g. the old interface vanished and
    ``async_unregister`` / ``async_close`` fail), the cached
    ``_last_advertised_addresses`` must already have been cleared so that
    a subsequent IP revert is still picked up by the refresh loop. If we
    cleared only after a successful close, this code path would leave the
    cache pointing at the old (now-dead) registration and silently stop
    advertising forever.
    """
    # Make the close path fail exactly once — on the FIRST close that
    # follows the initial register. Subsequent closes (on the recovery
    # path and stop()) succeed normally. Two healthy registrations bracket
    # the failing reconfigure attempt: instance 0 is the initial register,
    # instance 1 the recovery register after the IP reverts.
    close_calls = {"n": 0}
    original_close = RecordingAsyncZeroconf.async_close

    async def flaky_async_close(self: RecordingAsyncZeroconf) -> None:
        close_calls["n"] += 1
        if close_calls["n"] == 1:
            self.closed = True
            self.close_count += 1
            raise RuntimeError("mock async_close failure on old-interface teardown")
        await original_close(self)

    async with started_advertiser(
        monkeypatch,
        sequence=[OLD_ADDR, NEW_ADDR, NEW_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR, OLD_ADDR],
        setup=lambda mp: mp.setattr(
            RecordingAsyncZeroconf, "async_close", flaky_async_close
        ),
    ) as (advertiser, instances):
        # Wait for the recovery: a second zeroconf instance is only created
        # if the refresh loop saw _last_advertised_addresses == None after
        # the failed close (rather than the stale old value) and re-tried.
        await wait_until(
            lambda: len(instances) == 2 and advertiser._zeroconf is instances[1]
        )

        # instance 0: marked closed (the mock still set the flag) and the
        # failure was raised so reconfigure aborted partway through.
        assert instances[0].closed is True
        # instance 1: recovery succeeded against the reverted (old) IP.
        assert len(instances[1].registered) == 1
        assert advertiser._last_advertised_addresses == tuple(OLD_ADDR)
        assert advertiser._refresh_task is not None
        assert not advertiser._refresh_task.done()


@pytest.mark.asyncio
async def test_register_advertisement_cancellation_closes_zeroconf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review round 2 Finding 4: an ``asyncio.CancelledError`` arriving
    mid-``async_register_service`` (e.g. external ``stop()`` races the
    initial register, or a ``double-start()`` cancels the previous in-flight
    register) must still close the partially-constructed ``AsyncZeroconf``.
    Otherwise the multicast sockets and any partial registration leak past
    the cancelled task. ``except Exception`` is not sufficient because
    ``CancelledError`` derives from ``BaseException`` in Python 3.8+.
    """
    install_recording_zeroconf(monkeypatch)
    monkeypatch.setattr(
        mdns,
        "_enumerate_usable_ipv4_addresses",
        lambda: ["192.0.2.10"],
    )

    # Make register raise CancelledError; capture the zeroconf instance
    # whose async_close should still be called.
    captured: dict[str, RecordingAsyncZeroconf | None] = {"zc": None}

    async def cancelling_register(
        self: RecordingAsyncZeroconf,
        info: RecordingServiceInfo,
        *,
        allow_name_change: bool = False,
    ) -> None:
        captured["zc"] = self
        raise asyncio.CancelledError("mock cancellation during register")

    monkeypatch.setattr(
        RecordingAsyncZeroconf, "async_register_service", cancelling_register
    )

    advertiser = fast_advertiser()
    with pytest.raises(asyncio.CancelledError):
        await advertiser.start(host="0.0.0.0", port=8765, path="/")

    # The partially-constructed AsyncZeroconf must have been closed
    # even though the failure was a BaseException-derived CancelledError.
    assert captured["zc"] is not None
    assert captured["zc"].closed is True
