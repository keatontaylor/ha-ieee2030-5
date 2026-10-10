"""Cadence, priority, pacing and backoff without contacting a physical meter."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.ieee2030_5 import protocol
from custom_components.ieee2030_5.protocol import (
    AuthenticationError,
    MeterClient,
    MeterError,
    Register,
    from_metadata,
    itron_registers,
    parse_xml,
)

from .conftest import xml


def reading(value):
    return parse_xml(xml("Reading", f"<value>{value}</value>").encode())


@pytest.fixture
def scheduler(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(protocol, "monotonic", lambda: clock[0])
    client = MeterClient(None, "meter.example", 8081, None)
    client.registers = {
        "/energy": Register("/energy", "Energy", "Wh", "energy", "total_increasing"),
        "/power": Register("/power", "Demand", "W", "power", "measurement"),
        "/tou": Register("/tou", "TOU", "Wh", "energy", "total_increasing", tier="0"),
        "/max": Register(
            "/max", "Maximum", "W", "power", "measurement", poll_category="maximum_demand"
        ),
        "/pf": Register("/pf", "Power factor", "%", "power_factor", "measurement"),
    }
    client._values = dict.fromkeys(client.registers, 10)
    client._next_due = {href: clock[0] + r.interval(5) for href, r in client.registers.items()}
    client.get = AsyncMock(return_value=reading(20))
    return client, clock


def test_register_cadences_and_slow_user_override():
    registers = {r.href: r for r in itron_registers()}
    assert registers["/upt/1/mr/1/r"].interval(5) == 5
    assert registers["/upt/1/mr/19/r"].interval(5) == 30
    assert registers["/upt/1/mr/3/rs/1/r/1"].interval(5) == 60
    assert registers["/upt/1/mr/9/rs/1/r/1"].interval(5) == 300
    assert registers["/upt/1/mr/18/r"].interval(5) == 900
    assert registers["/upt/1/mr/19/r"].interval(120) == 120
    metadata = parse_xml(
        xml("ReadingType", "<uom>38</uom><dataQualifier>8</dataQualifier>").encode()
    )
    assert from_metadata("/maximum", "Peak", metadata, reading(10)).interval(5) == 900


async def test_fast_reads_preserve_cached_counters_and_no_manual_burst(scheduler):
    client, clock = scheduler
    assert await client.poll_due() == dict.fromkeys(client.registers, 10)
    client.get.assert_not_awaited()
    clock[0] += 5
    values = await client.poll_due()
    assert values["/power"] == 20
    assert values["/energy"] == values["/tou"] == 10
    client.get.assert_awaited_once_with("/power", timeout=10.0)
    await client.poll_due()
    assert client.get.await_count == 1
    assert client.next_poll_delay == 5


async def test_priority_and_slow_request_deadline(scheduler):
    client, clock = scheduler
    clock[0] += 60
    await client.poll_due()
    assert client.get.await_args.args == ("/power",)
    assert client.next_poll_delay == 1
    await client.poll_due()
    assert client.get.await_args.args == ("/pf",)
    assert client.get.await_args.kwargs["timeout"] == 4.5
    await client.poll_due()
    assert client.get.await_args.args == ("/energy",)
    assert client._values["/tou"] == 10


async def test_register_failure_backoff_and_other_values_survive(scheduler, caplog):
    client, clock = scheduler
    clock[0] += 5
    client.get.side_effect = MeterError("Cannot connect to meter")
    assert (await client.poll_due())["/power"] is None
    assert client._values["/energy"] == 10
    assert client._next_due["/power"] == 1035
    await client.poll_due()
    assert client.get.await_count == 1
    # A slow register can still update while the failed register backs off.
    clock[0] = 1030
    client.get.side_effect = None
    await client.poll_due()
    assert client.get.await_args.args == ("/pf",)
    clock[0] = 1035
    client.get.side_effect = MeterError("Cannot connect to meter")
    await client.poll_due()
    assert client._next_due["/power"] == 1095
    assert caplog.text.count("Meter register read failed") == 1
    clock[0] = 1095
    client.get.side_effect = None
    await client.poll_due()
    assert client._values["/power"] == 20
    assert "/power" not in client._failures
    assert client._next_due["/power"] == 1100


async def test_authentication_failure_still_triggers_reauth(scheduler):
    client, clock = scheduler
    clock[0] += 5
    client.get.side_effect = AuthenticationError("Meter rejected access")
    with pytest.raises(AuthenticationError):
        await client.poll_due()


@pytest.mark.parametrize("response", [None, xml("Reading")])
async def test_missing_readings_are_unavailable_and_back_off(scheduler, response):
    client, clock = scheduler
    clock[0] += 5
    client.get.return_value = parse_xml(response.encode()) if response else None
    assert (await client.poll_due())["/power"] is None
    assert client._next_due["/power"] == 1035
    assert client.registers_in_backoff == 1


async def test_backoff_never_accelerates_a_slow_user_interval(scheduler):
    client, clock = scheduler
    client.instantaneous_interval = 3600
    clock[0] += 5
    client.get.side_effect = MeterError("Cannot connect to meter")
    await client.poll_due()
    assert client._next_due["/power"] == clock[0] + 3600


async def test_request_pacing_serializes_concurrent_callers(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(protocol, "monotonic", lambda: clock[0])
    client = MeterClient(None, "meter.example", 8081, None)
    starts = []
    sleep = asyncio.sleep

    async def advance(delay):
        clock[0] += delay
        await sleep(0)

    async def get(href, *, timeout):
        starts.append(clock[0])
        await sleep(0)
        return reading(10)

    monkeypatch.setattr(protocol.asyncio, "sleep", advance)
    client._get = get
    await asyncio.gather(client.get("/one"), client.get("/two"), client.get("/three"))
    assert starts == [1000, 1000.5, 1001]
