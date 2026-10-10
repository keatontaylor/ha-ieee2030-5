from pathlib import Path
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ieee2030_5.const import DOMAIN
from custom_components.ieee2030_5.credentials import credential_paths


async def submit(hass, meter_server, certificates, *, source=SOURCE_USER, entry=None, rotate=True):
    server, _, _, _ = meter_server
    uploads = {
        "11111111111111111111111111111111": (certificates / "client.pem").read_bytes(),
        "22222222222222222222222222222222": (certificates / "client.key").read_bytes(),
    }

    async def upload(self, identifier):
        return uploads[identifier]

    data = {"host": "127.0.0.1", "port": server.port, "profile": "itron", "interval": 30}
    if rotate:
        data.update(
            certificate="11111111111111111111111111111111",
            private_key="22222222222222222222222222222222",
        )
    ctx = {"source": source}
    if entry:
        ctx["entry_id"] = entry.entry_id
    with (
        patch("custom_components.ieee2030_5.config_flow.MeterConfigFlow._upload", upload),
        patch("custom_components.ieee2030_5.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context=ctx, data=dict(entry.data) if source == SOURCE_REAUTH else None
        )
        assert result["type"] == FlowResultType.FORM
        return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def test_setup_wizard_real_tls(hass, meter_server, certificates):
    result = await submit(hass, meter_server, certificates)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == "aabbccdd"
    assert "certificate" not in entry.data and "private_key" not in entry.data
    cert, key, _ = credential_paths(hass.config.config_dir, entry.data["credentials"])
    assert Path(key).stat().st_mode & 0o777 == 0o600
    assert Path(cert).parent.stat().st_mode & 0o777 == 0o700
    assert len(list((Path(hass.config.config_dir) / ".storage" / DOMAIN).iterdir())) == 1


async def test_failed_wizard_cleans_credentials(hass, meter_server, certificates):
    server, routes, _, _ = meter_server
    routes["/dcap"] = 403
    result = await submit(hass, meter_server, certificates)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert not list((Path(hass.config.config_dir) / ".storage" / DOMAIN).iterdir())


async def test_duplicate_meter_cleans_new_credentials(hass, meter_server, certificates):
    first = await submit(hass, meter_server, certificates)
    second = await submit(hass, meter_server, certificates)
    assert second["type"] == FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert len(list((Path(hass.config.config_dir) / ".storage" / DOMAIN).iterdir())) == 1
    assert first["result"].unique_id == "aabbccdd"


async def test_rotation_keeps_identity_and_removes_old_keys(hass, meter_server, certificates):
    first = await submit(hass, meter_server, certificates)
    entry = first["result"]
    old = Path(credential_paths(hass.config.config_dir, entry.data["credentials"])[0]).parent
    result = await submit(hass, meter_server, certificates, source=SOURCE_RECONFIGURE, entry=entry)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert not old.exists()
    assert entry.unique_id == "aabbccdd"


async def test_rotation_rejects_another_meter(hass, meter_server, certificates):
    first = await submit(hass, meter_server, certificates)
    entry = first["result"]
    old_id = entry.data["credentials"]
    meter_server[1]["/sdev/sdi"] = (
        '<DeviceInformation xmlns="urn:ieee:std:2030.5:ns"><lFDI>DEADBEEF</lFDI></DeviceInformation>'
    )
    result = await submit(hass, meter_server, certificates, source=SOURCE_RECONFIGURE, entry=entry)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data["credentials"] == old_id
    assert len(list((Path(hass.config.config_dir) / ".storage" / DOMAIN).iterdir())) == 1


async def test_native_home_assistant_file_upload(hass, hass_client, meter_server, certificates):
    """Exercise HA's actual authenticated upload endpoint and consume UUIDs."""
    from aiohttp import FormData
    from homeassistant.components.file_upload import _DATA

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    http_client = await hass_client()
    uploads = {}
    for field, filename in (("certificate", "client.pem"), ("private_key", "client.key")):
        form = FormData()
        form.add_field(
            "file",
            (certificates / filename).read_bytes(),
            filename=filename,
            content_type="application/octet-stream",
        )
        response = await http_client.post("/api/file_upload", data=form)
        assert response.status == 200
        uploads[field] = (await response.json())["file_id"]
    data = {
        "host": "127.0.0.1",
        "port": meter_server[0].port,
        "profile": "itron",
        "interval": 30,
        **uploads,
    }
    original = meter_server[1]["/dcap"]
    meter_server[1]["/dcap"] = 403
    rejected = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert rejected["type"] == FlowResultType.FORM
    assert rejected["errors"] == {"base": "invalid_auth"}
    assert not hass.data[_DATA].files
    meter_server[1]["/dcap"] = original
    # Retry with the same consumed UUIDs after correcting the connection issue.
    with patch("custom_components.ieee2030_5.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert not hass.data[_DATA].files


async def test_reauthenticate_certificate_rotation(hass, meter_server, certificates):
    first = await submit(hass, meter_server, certificates)
    entry = first["result"]
    result = await submit(hass, meter_server, certificates, source=SOURCE_REAUTH, entry=entry)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == "aabbccdd"


async def test_mdns_discovery_prompts_for_certificates(hass, meter_server):
    from ipaddress import ip_address

    from homeassistant.config_entries import SOURCE_ZEROCONF
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    discovery = ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        port=meter_server[0].port,
        hostname="meter.local.",
        type="_smartenergy._tcp.local.",
        name="Itron._smartenergy._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not hass.config_entries.async_entries(DOMAIN)
