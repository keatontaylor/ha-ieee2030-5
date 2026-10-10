"""Run against real Home Assistant and a local mutual-TLS meter."""

import asyncio
import ipaddress
import ssl
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def enable_custom_components_fixture(enable_custom_integrations):
    """Allow HA to load this repository as a custom integration."""


@pytest.fixture
def certificates(tmp_path):
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mock meter CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for role, usage in (
        ("server", ExtendedKeyUsageOID.SERVER_AUTH),
        ("client", ExtendedKeyUsageOID.CLIENT_AUTH),
    ):
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        (tmp_path / f"{role}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path / f"{role}.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return tmp_path


@pytest.fixture
def hass_config_dir(tmp_path):
    return str(tmp_path / "ha_config")


def xml(tag, body="", attributes=""):
    return f'<{tag} xmlns="urn:ieee:std:2030.5:ns" {attributes}>{body}</{tag}>'


@pytest.fixture
async def meter_server(certificates, aiohttp_server, socket_enabled, request):
    """HTTPS fixture enforces both the actual CCM8 cipher and client auth."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if getattr(request, "param", True):
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("ECDHE-ECDSA-AES128-CCM8:@SECLEVEL=0")
    context.load_cert_chain(certificates / "server.pem", certificates / "server.key")
    context.load_verify_locations(certificates / "ca.pem")
    context.verify_mode = ssl.CERT_REQUIRED
    routes = {
        "/dcap": xml(
            "DeviceCapability", '<SelfDeviceLink href="/sdev"/><UsagePointListLink href="/upt"/>'
        ),
        "/sdev": xml("SelfDevice", '<DeviceInformationLink href="/sdev/sdi"/>'),
        "/sdev/sdi": xml(
            "DeviceInformation", "<lFDI>AABBCCDD</lFDI><swVer>3.2.50</swVer><mfID>Itron</mfID>"
        ),
        "/upt": xml(
            "UsagePointList",
            '<UsagePoint href="/upt/1"><MeterReadingListLink href="/mr"/></UsagePoint>',
            'all="1" results="1"',
        ),
        "/mr": xml(
            "MeterReadingList",
            '<MeterReading href="/mr/1"><description>Grid import</description><ReadingTypeLink href="/rt"/><ReadingLink href="/reading/1"/><ReadingSetListLink href="/sets"/></MeterReading>',
            'all="1" results="1"',
        ),
        "/rt": xml(
            "ReadingType",
            "<uom>72</uom><powerOfTenMultiplier>0</powerOfTenMultiplier><accumulationBehaviour>1</accumulationBehaviour><flowDirection>1</flowDirection>",
        ),
        "/sets": xml(
            "ReadingSetList",
            '<ReadingSet href="/sets/1"><ReadingListLink href="/readings"/></ReadingSet>',
            'all="1" results="1"',
        ),
        "/readings": xml(
            "ReadingList",
            '<Reading href="/reading/1"><touTier>1</touTier><value>1234</value></Reading>',
            'all="1" results="1"',
        ),
        "/reading/1": xml("Reading", "<touTier>1</touTier><value>1234</value>"),
        "/upt/1/mr/1/r": xml("Reading", "<value>-42</value>"),
    }
    calls = []
    ciphers = []

    async def respond(request):
        calls.append(str(request.rel_url))
        ciphers.append(request.transport.get_extra_info("ssl_object").cipher()[0])
        body = routes.get(str(request.rel_url))
        if body is None:
            return web.Response(status=404)
        if isinstance(body, int):
            return web.Response(status=body)
        return web.Response(text=body, content_type="application/sep+xml")

    app = web.Application()
    app.router.add_get("/{tail:.*}", respond)
    server = await aiohttp_server(app, ssl=context)
    return server, routes, calls, ciphers


@pytest.fixture
async def expected_server_tls_rejection(hass):
    """A deliberately rejected handshake is expected on the mock server."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    rejected = []

    def handle(loop, context):
        error = context.get("exception")
        if isinstance(error, ssl.SSLError) and error.reason in (
            "NO_SHARED_CIPHER",
            "PEER_DID_NOT_RETURN_A_CERTIFICATE",
        ):
            rejected.append(error.reason)
            return
        previous(loop, context)

    loop.set_exception_handler(handle)
    yield rejected
    loop.set_exception_handler(previous)
    assert rejected
