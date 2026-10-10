from pathlib import Path

from homeassistant.const import STATE_UNAVAILABLE
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ieee2030_5.const import DOMAIN
from custom_components.ieee2030_5.credentials import credential_paths, store_credentials


async def test_real_entry_sensor_poll_unload_and_remove(hass, meter_server, certificates):
    credentials = await hass.async_add_executor_job(
        store_credentials,
        hass.config.config_dir,
        (certificates / "client.pem").read_bytes(),
        (certificates / "client.key").read_bytes(),
    )
    data = {
        "credentials": credentials,
        "host": "127.0.0.1",
        "port": meter_server[0].port,
        "profile": "itron",
        "interval": 30,
    }
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="aabbccdd", data=data, title="Smart Energy Meter"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sensors = hass.states.async_all("sensor")
    assert len(sensors) == 2
    energy = next(state for state in sensors if state.attributes.get("device_class") == "energy")
    assert energy.state == "1234"
    assert energy.attributes["unit_of_measurement"] == "Wh"
    assert energy.attributes["state_class"] == "total_increasing"
    assert energy.attributes["tou_tier"] == "1"
    session = entry.runtime_data.client.session
    meter_server[1]["/reading/1"] = 404
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(energy.entity_id).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert session.closed
    assert not Path(credential_paths(hass.config.config_dir, credentials)[0]).parent.exists()
