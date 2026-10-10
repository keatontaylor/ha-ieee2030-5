import ssl
from decimal import Decimal

import aiohttp
import pytest

from custom_components.ieee2030_5.protocol import (
    MAX_BODY,
    AuthenticationError,
    InvalidResponse,
    MeterClient,
    MeterError,
    build_ssl_context,
    from_metadata,
    itron_registers,
    parse_xml,
)

from .conftest import xml


def context(certificates, verified=False):
    return build_ssl_context(
        str(certificates / "client.pem"),
        str(certificates / "client.key"),
        legacy=True,
        ca=str(certificates / "ca.pem") if verified else None,
    )


async def test_real_ccm8_mutual_tls_discovery_and_poll(meter_server, certificates):
    server, routes, _, ciphers = meter_server
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates), "itron")
        values = await client.discover()
        assert client.info["lfdi"] == "aabbccdd"
        assert values == {"/reading/1": Decimal(1234), "/upt/1/mr/1/r": Decimal(-42)}
        assert client.registers["/reading/1"].state_class == "total_increasing"
        assert client.registers["/reading/1"].tier == "1"
        assert set(ciphers) == {"ECDHE-ECDSA-AES128-CCM8"}
        routes["/reading/1"] = xml("Reading", "<value>1300</value>")
        assert (await client.poll())["/reading/1"] == 1300
        routes["/reading/1"] = 404
        assert (await client.poll())["/reading/1"] is None
        routes["/reading/1"] = 403
        with pytest.raises(AuthenticationError):
            await client.poll()


async def test_verified_legacy_tls(meter_server, certificates):
    server, _, _, _ = meter_server
    async with aiohttp.ClientSession() as session:
        client = MeterClient(
            session, "127.0.0.1", server.port, context(certificates, True), "standard"
        )
        await client.discover()
        assert len(client.registers) == 1


async def test_standard_tls_does_not_enable_legacy_cipher(
    meter_server, certificates, expected_server_tls_rejection
):
    server, _, _, _ = meter_server
    strict = build_ssl_context(
        str(certificates / "client.pem"),
        str(certificates / "client.key"),
        legacy=False,
        ca=str(certificates / "ca.pem"),
    )
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, strict, "standard")
        with pytest.raises(MeterError):
            await client.discover()
    assert strict.verify_mode == ssl.CERT_REQUIRED
    assert "ECDHE-ECDSA-AES128-CCM8" not in {item["name"] for item in strict.get_ciphers()}


async def test_client_certificate_required(meter_server, expected_server_tls_rejection):
    server, _, calls, _ = meter_server
    missing = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    missing.check_hostname = False
    missing.verify_mode = ssl.CERT_NONE
    missing.maximum_version = ssl.TLSVersion.TLSv1_2
    missing.set_ciphers("ECDHE-ECDSA-AES128-CCM8:@SECLEVEL=0")
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, missing)
        with pytest.raises(MeterError):
            await client.discover()
    assert not calls


@pytest.mark.parametrize(
    "data",
    [
        b'<!DOCTYPE x [<!ENTITY x "oops">]><Reading>&x;</Reading>',
        b'<Reading xmlns="wrong"><value>1</value></Reading>',
        b'<?xml version="1.0" encoding="utf-16"?>\xff',
        b"x" * (MAX_BODY + 1),
        b"<broken",
    ],
)
def test_unsafe_xml(data):
    with pytest.raises(InvalidResponse):
        parse_xml(data)


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example:8081/x",
        "http://127.0.0.1:8081/x",
        "//evil.example/x",
        "https://user@127.0.0.1:8081/x",
    ],
)
async def test_credentials_never_sent_off_meter(href, meter_server, certificates):
    server, routes, calls, _ = meter_server
    routes["/dcap"] = xml("DeviceCapability", f'<UsagePointListLink href="{href}"/>')
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates))
        with pytest.raises(InvalidResponse):
            await client.discover()
    assert calls == ["/dcap"]


async def test_paginated_reading_discovery(meter_server, certificates):
    server, routes, calls, _ = meter_server
    routes["/readings"] = xml(
        "ReadingList",
        '<Reading href="/reading/1"><value>1234</value></Reading>',
        'all="2" results="1"',
    )
    routes["/readings?s=1&l=20"] = xml(
        "ReadingList",
        '<Reading href="/reading/2"><touTier>2</touTier><value>456</value></Reading>',
        'all="2" results="1"',
    )
    routes["/reading/2"] = xml("Reading", "<value>456</value>")
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates), "standard")
        values = await client.discover()
        assert values["/reading/2"] == 456
    assert "/readings?s=1&l=20" in calls


def test_reading_metadata_units_and_scaling():
    metadata = parse_xml(
        xml(
            "ReadingType",
            "<uom>72</uom><powerOfTenMultiplier>-3</powerOfTenMultiplier><accumulationBehaviour>1</accumulationBehaviour><flowDirection>4</flowDirection>",
        ).encode()
    )
    reading = parse_xml(xml("Reading", "<value>-1250</value>").encode())
    register = from_metadata("/r", "Net energy", metadata, reading)
    assert register.value(reading) == Decimal("-1.250")
    assert register.state_class == "total"  # A net register can legitimately decrease.
    assert register.unit == "Wh"
    for value in ("nan", "1.1", str(2**47), ""):
        with pytest.raises(InvalidResponse):
            register.value(parse_xml(xml("Reading", f"<value>{value}</value>").encode()))


def test_correct_apparent_reactive_energy_and_power_factor():
    registers = {item.href: item for item in itron_registers()}
    assert registers["/upt/1/mr/4/r"].unit == "VAh"
    assert registers["/upt/1/mr/6/r"].unit == "varh"
    assert (
        registers["/upt/1/mr/19/r"].value(parse_xml(xml("Reading", "<value>950</value>").encode()))
        == 95
    )


async def test_firmware_fallback_without_standard_discovery(meter_server, certificates):
    server, routes, _, _ = meter_server
    routes.pop("/dcap")
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates))
        assert await client.discover() == {"/upt/1/mr/1/r": -42}


@pytest.mark.parametrize("meter_server", [False], indirect=True)
async def test_standard_verified_tls_success(meter_server, certificates):
    server, _, _, ciphers = meter_server
    strict = build_ssl_context(
        str(certificates / "client.pem"),
        str(certificates / "client.key"),
        legacy=False,
        ca=str(certificates / "ca.pem"),
    )
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, strict, "standard")
        assert await client.discover() == {"/reading/1": 1234}
    assert "ECDHE-ECDSA-AES128-CCM8" not in ciphers


async def test_absolute_resource_links_keep_identity_independent_of_ip(meter_server, certificates):
    server, routes, _, _ = meter_server
    routes["/readings"] = xml(
        "ReadingList",
        f'<Reading href="https://127.0.0.1:{server.port}/reading/1"><value>1234</value></Reading>',
        'all="1" results="1"',
    )
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates), "standard")
        assert await client.discover() == {"/reading/1": 1234}


async def test_relative_advertised_links(meter_server, certificates):
    server, routes, _, _ = meter_server
    routes["/sets"] = xml(
        "ReadingSetList",
        '<ReadingSet href="/sets/1"><ReadingListLink href="readings"/></ReadingSet>',
        'all="1" results="1"',
    )
    routes["/sets/readings"] = routes.pop("/readings")
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates), "standard")
        assert await client.discover() == {"/reading/1": 1234}


def test_legacy_server_options_are_scoped_and_renegotiation_disabled(certificates):
    default = ssl.create_default_context()
    before = default.options
    legacy = context(certificates)
    strict = build_ssl_context(
        str(certificates / "client.pem"),
        str(certificates / "client.key"),
        legacy=False,
        ca=str(certificates / "ca.pem"),
    )
    assert legacy.options & ssl.OP_LEGACY_SERVER_CONNECT
    assert legacy.options & ssl.OP_NO_RENEGOTIATION
    assert not strict.options & ssl.OP_LEGACY_SERVER_CONNECT
    assert default.options == before
    assert not default.options & ssl.OP_LEGACY_SERVER_CONNECT


async def test_empty_optional_phase_is_skipped_and_later_empty_reading_unavailable(
    meter_server, certificates
):
    server, routes, _, _ = meter_server
    routes["/upt/1/mr/22/r"] = xml("Reading")  # Unsupported optional phase.
    routes["/readings"] = xml(
        "ReadingList",
        '<Reading href="/reading/1"><value>1234</value></Reading><Reading href="/unused"/>',
        'all="2" results="2"',
    )
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates), "itron")
        values = await client.discover()
        assert values == {"/reading/1": 1234, "/upt/1/mr/1/r": -42}
        assert "/unused" not in client.registers
        assert "/upt/1/mr/22/r" not in client.registers
        routes["/reading/1"] = xml("Reading")
        assert (await client.poll())["/reading/1"] is None


async def test_itron_does_not_crawl_historical_archives_or_repoll_during_setup(
    meter_server, certificates
):
    server, routes, calls, _ = meter_server
    routes["/sets"] = xml(
        "ReadingSetList",
        '<ReadingSet href="/sets/1"><ReadingListLink href="/readings"/></ReadingSet>',
        'all="500" results="1"',
    )
    async with aiohttp.ClientSession() as session:
        client = MeterClient(session, "127.0.0.1", server.port, context(certificates), "itron")
        assert await client.discover() == {"/reading/1": 1234, "/upt/1/mr/1/r": -42}
    assert "/sets" not in calls
    assert "/readings" not in calls
    assert calls.count("/reading/1") == 1
    assert calls.count("/upt/1/mr/1/r") == 1
