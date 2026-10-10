"""Bounded, read-only IEEE 2030.5 metering and an Itron compatibility profile.

No Home Assistant imports: the same client is exercised against real TLS servers.
"""

from __future__ import annotations

import asyncio
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import aiohttp

MAX_BODY = 256 * 1024
MAX_RESOURCES = 128
NAMESPACES = {"urn:ieee:std:2030.5:ns", "http://ieee.org/2030.5"}
LINKS = {
    "SelfDeviceLink",
    "DeviceInformationLink",
    "UsagePointListLink",
    "UsagePointLink",
    "MeterReadingListLink",
    "MeterReadingLink",
    "ReadingLink",
    "ReadingSetListLink",
    "ReadingSetLink",
    "ReadingListLink",
}
CONTAINERS = {"UsagePoint", "MeterReading", "ReadingSet", "SelfDevice"}
# IEEE 2030.5-2023 UomType. Apparent energy has no HA device class.
UNITS = {
    5: ("A", "current"),
    29: ("V", "voltage"),
    33: ("Hz", "frequency"),
    38: ("W", "power"),
    61: ("VA", "apparent_power"),
    63: ("var", "reactive_power"),
    65: ("%", "power_factor"),
    71: ("VAh", None),
    72: ("Wh", "energy"),
    73: ("varh", "reactive_energy"),
}


class MeterError(Exception):
    """A safe public error; never includes response bodies or credentials."""


class AuthenticationError(MeterError):
    """TLS or meter access rejected."""


class InvalidResponse(MeterError):
    """Malformed or unsafe meter data."""


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def text(element: ET.Element, name: str, default: str = "") -> str:
    return next(
        (child.text.strip() for child in element if local_name(child) == name and child.text),
        default,
    )


def parse_xml(data: bytes) -> ET.Element:
    """Reject DTDs, entities, non-UTF8 and foreign XML before parsing."""
    if len(data) > MAX_BODY:
        raise InvalidResponse("Response too large")
    try:
        decoded = data.decode("utf-8-sig")
        if "<!DOCTYPE" in decoded.upper() or "<!ENTITY" in decoded.upper():
            raise InvalidResponse("XML declarations are not permitted")
        root = ET.fromstring(decoded)
    except (UnicodeError, ET.ParseError) as err:
        raise InvalidResponse("Invalid meter XML") from err
    if not root.tag.startswith("{") or root.tag[1:].split("}")[0] not in NAMESPACES:
        raise InvalidResponse("Unexpected XML namespace")
    return root


def build_ssl_context(
    cert: str, key: str, *, legacy: bool, ca: str | None = None, password: str | None = None
) -> ssl.SSLContext:
    """Only this context permits Itron's TLS 1.2 CCM8 cipher.

    Supplying a CA enables normal certificate AND hostname verification.
    Without a CA the user explicitly selects the Itron compatibility profile.
    """
    if not ca and not legacy:
        raise ValueError("A CA certificate is required for the standard profile")
    context = ssl.create_default_context(cafile=ca)
    if legacy:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("ECDHE-ECDSA-AES128-CCM8:@SECLEVEL=0")
        # Older Itron servers omit RFC 5746 secure-renegotiation support.
        # Permit the initial connection only on this context, while disallowing
        # renegotiation entirely. The standard profile retains normal defaults.
        context.options |= ssl.OP_LEGACY_SERVER_CONNECT | ssl.OP_NO_RENEGOTIATION
    if not ca:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.load_cert_chain(cert, key, password=password or None)
    return context


@dataclass(frozen=True)
class Register:
    """One stable reading resource, with its native measurement semantics."""

    href: str
    name: str
    unit: str
    device_class: str | None
    state_class: str
    multiplier: int = 0
    tier: str | None = None
    scale: Decimal = Decimal(1)

    def value(self, root: ET.Element) -> Decimal:
        raw = text(root, "value")
        try:
            # Int48 in the standard; reject NaN, infinity and fractional values.
            integer = int(raw)
            if not -(2**47) <= integer < 2**47:
                raise ValueError
            return Decimal(integer) * Decimal(10) ** self.multiplier * self.scale
        except (ValueError, InvalidOperation) as err:
            raise InvalidResponse("Invalid reading value") from err


def from_metadata(
    href: str, name: str, metadata: ET.Element, reading: ET.Element
) -> Register | None:
    try:
        uom = int(text(metadata, "uom", "0"))
        multiplier = int(text(metadata, "powerOfTenMultiplier", "0"))
        accumulation = int(text(metadata, "accumulationBehaviour", "0"))
        direction = int(text(metadata, "flowDirection", "0"))
    except ValueError as err:
        raise InvalidResponse("Invalid ReadingType") from err
    if uom not in UNITS or not -12 <= multiplier <= 12:
        return None
    unit, device_class = UNITS[uom]
    state_class = "measurement"
    if uom in (71, 72, 73):
        state_class = "total"
        if accumulation in (1, 9) and direction in (1, 19, 20, 21):
            state_class = "total_increasing"
    tier = text(reading, "touTier") or None
    if tier and tier != "0":
        name = f"{name} TOU tier {tier}"
    # CosTheta is a fraction; HA power_factor requires a percentage.
    scale = Decimal(100) if uom == 65 else Decimal(1)
    return Register(href, name, unit, device_class, state_class, multiplier, tier, scale)


def itron_registers() -> list[Register]:
    """Endpoint locations adapted from xcel_itron2mqtt (Apache-2.0).

    Probe capabilities instead of assuming all firmware implements every path.
    Delivered = import from the grid; received = export to the grid.
    """
    registers = [Register("/upt/1/mr/1/r", "Instantaneous demand", "W", "power", "measurement")]
    for index, name, unit, dc in (
        (2, "Energy exported", "Wh", "energy"),
        (3, "Energy imported", "Wh", "energy"),
        (4, "Apparent energy exported", "VAh", None),
        (5, "Apparent energy imported", "VAh", None),
        (6, "Reactive energy exported", "varh", "reactive_energy"),
        (7, "Reactive energy imported", "varh", "reactive_energy"),
    ):
        href = f"/upt/1/mr/{index}/rs/1/r/1" if index in (2, 3) else f"/upt/1/mr/{index}/r"
        registers.append(Register(href, name, unit, dc, "total_increasing"))
    for index, direction in ((8, "exported"), (9, "imported")):
        for tier in range(4):
            registers.append(
                Register(
                    f"/upt/1/mr/{index}/rs/1/r/{tier + 1}",
                    f"Energy {direction} TOU {tier}",
                    "Wh",
                    "energy",
                    "total_increasing",
                    tier=str(tier),
                )
            )
    for index, direction in ((17, "exported"), (18, "imported")):
        registers.append(
            Register(
                f"/upt/1/mr/{index}/r", f"Maximum demand {direction}", "W", "power", "measurement"
            )
        )
    for index, phase in ((19, ""), (20, " phase A"), (21, " phase B"), (22, " phase C")):
        registers.append(
            Register(
                f"/upt/1/mr/{index}/r",
                f"Power factor{phase}",
                "%",
                "power_factor",
                "measurement",
                -3,
                scale=Decimal(100),
            )
        )
    return registers


class MeterClient:
    """Use a caller-owned session; no shared SSL context or global overrides."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        context: ssl.SSLContext,
        profile: str = "itron",
    ) -> None:
        if not host or any(char in host for char in "/?#@") or not 1 <= port <= 65535:
            raise ValueError("Enter a hostname or IP address, not a URL")
        host = host.strip("[]")
        address = f"[{host}]" if ":" in host else host
        self.base = f"https://{address}:{port}"
        self.session = session
        self.context = context
        self.profile = profile
        self.info: dict[str, str] = {}
        self.registers: dict[str, Register] = {}
        self._visited: set[str] = set()
        self._discovered_values: dict[str, Decimal] = {}

    def url(self, href: str) -> str:
        url = urljoin(self.base + "/", href)
        parsed, base = urlsplit(url), urlsplit(self.base)
        if (parsed.scheme, parsed.hostname, parsed.port) != (base.scheme, base.hostname, base.port):
            raise InvalidResponse("Meter links must stay on the meter")
        if parsed.username or parsed.password or parsed.fragment:
            raise InvalidResponse("Invalid meter link")
        return url

    async def get(self, href: str) -> ET.Element | None:
        url = self.url(href)
        try:
            async with self.session.get(
                url,
                ssl=self.context,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Accept": "application/sep+xml, application/xml"},
            ) as response:
                if response.status == 404:
                    return None
                if response.status in (401, 403):
                    raise AuthenticationError("Meter rejected access")
                if response.status != 200:
                    raise MeterError(f"Meter returned HTTP {response.status}")
                body = bytearray()
                async for part in response.content.iter_chunked(8192):
                    body.extend(part)
                    if len(body) > MAX_BODY:
                        raise InvalidResponse("Response too large")
                return parse_xml(bytes(body))
        except (aiohttp.ClientSSLError, ssl.SSLError) as err:
            reason = getattr(getattr(err, "os_error", err), "reason", "SSL_ERROR")
            raise AuthenticationError(f"TLS connection rejected ({reason})") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MeterError("Cannot connect to meter") from err

    async def discover(self) -> dict[str, Decimal | None]:
        self._visited.clear()
        self.registers.clear()
        self._discovered_values.clear()
        async with asyncio.timeout(90):
            await self._walk("/dcap")
            if not self.info.get("lfdi"):
                root = await self.get("/sdev/sdi")
                if root is not None:
                    self._identity(root)
            if not self.info.get("lfdi"):
                raise InvalidResponse("Meter does not advertise an LFDI")
            if self.profile == "itron":
                for register in itron_registers():
                    if register.href in self.registers:
                        continue
                    root = await self.get(register.href)
                    if root is not None and text(root, "value"):
                        self._discovered_values[register.href] = register.value(root)
                        self.registers[register.href] = register
            if not self.registers:
                raise InvalidResponse("No supported readings discovered")
            return dict(self._discovered_values)

    def _identity(self, root: ET.Element) -> None:
        lfdi = text(root, "lFDI")
        if lfdi:
            self.info = {
                "lfdi": lfdi.lower(),
                "firmware": text(root, "swVer"),
                "manufacturer": text(root, "mfID"),
                "model": text(root, "mfModel"),
            }

    async def _walk(
        self, href: str, metadata: ET.Element | None = None, name: str = "Meter reading"
    ) -> None:
        url = self.url(href)
        if url in self._visited:
            return
        if len(self._visited) >= MAX_RESOURCES:
            raise InvalidResponse("Meter discovery exceeded resource limit")
        self._visited.add(url)
        root = await self.get(href)
        if root is not None:
            await self._element(root, href, metadata, name)
            # IEEE List resources paginate with s (start) and l (limit).
            if local_name(root).endswith("List"):
                try:
                    total = int(root.get("all", "0"))
                    count = int(root.get("results", str(len(root))))
                    query = dict(parse_qsl(urlsplit(url).query))
                    start = int(query.get("s", "0"))
                except ValueError as err:
                    raise InvalidResponse("Invalid list pagination") from err
                if total > start + count and count > 0:
                    query.update(s=str(start + count), l="20")
                    await self._walk(
                        urlsplit(url)._replace(query=urlencode(query)).geturl(), metadata, name
                    )

    async def _element(
        self, root: ET.Element, href: str, metadata: ET.Element | None, name: str
    ) -> None:
        self._identity(root)
        name = text(root, "description", name)
        for child in root:
            if local_name(child) == "ReadingTypeLink" and child.get("href"):
                metadata = await self.get(urljoin(self.url(href), child.attrib["href"]))
            elif local_name(child) == "ReadingType":
                metadata = child
        if local_name(root) == "Reading" and metadata is not None and text(root, "value"):
            reading_url = urlsplit(self.url(urljoin(self.url(href), root.get("href", href))))
            reading_href = reading_url.path + ("?" + reading_url.query if reading_url.query else "")
            self.url(reading_href)
            register = from_metadata(reading_href, name, metadata, root)
            if register:
                self._discovered_values[reading_href] = register.value(root)
                self.registers[reading_href] = register
        for child in root:
            tag = local_name(child)
            if self.profile == "itron" and tag == "ReadingSetListLink":
                # Itron exposes large historical interval archives here. Current
                # registers and TOU counters are probed via the firmware profile.
                continue
            if tag in LINKS and child.get("href"):
                await self._walk(urljoin(self.url(href), child.attrib["href"]), metadata, name)
            elif tag in CONTAINERS or tag == "Reading":
                await self._element(
                    child, urljoin(self.url(href), child.get("href", href)), metadata, name
                )

    async def poll(self) -> dict[str, Decimal | None]:
        try:
            async with asyncio.timeout(60):
                return await self._poll()
        except TimeoutError as err:
            raise MeterError("Meter polling exceeded time limit") from err

    async def _poll(self) -> dict[str, Decimal | None]:
        values: dict[str, Decimal | None] = {}
        for href, register in self.registers.items():
            root = await self.get(href)
            values[href] = None if root is None or not text(root, "value") else register.value(root)
        return values
