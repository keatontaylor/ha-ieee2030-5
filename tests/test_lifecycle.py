from datetime import timedelta
from pathlib import Path

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ieee2030_5.const import DOMAIN
from custom_components.ieee2030_5.credentials import credential_paths, store_credentials


async def test_real_entry_sensor_poll_unload_and_remove(
    hass, meter_server, certificates, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("custom_components.ieee2030_5.protocol.monotonic", lambda: clock[0])
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
        "interval": 5,
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
    assert energy.attributes["poll_interval_seconds"] == 300
    power = next(state for state in sensors if state.attributes.get("device_class") == "power")
    assert power.attributes["poll_interval_seconds"] == 5
    # Setup reuses the discovery values rather than fetching every register again.
    assert meter_server[2].count("/reading/1") == 1
    assert meter_server[2].count("/upt/1/mr/1/r") == 1
    meter_server[1]["/reading/1"] = (
        '<Reading xmlns="urn:ieee:std:2030.5:ns"><touTier>1</touTier><value>2345</value></Reading>'
    )
    meter_server[1]["/upt/1/mr/1/r"] = (
        '<Reading xmlns="urn:ieee:std:2030.5:ns"><value>-43</value></Reading>'
    )
    clock[0] = 1005
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(power.entity_id).state == "-43"
    assert hass.states.get(energy.entity_id).state == "1234"
    # When both are overdue, demand goes first and TOU follows on the next tick.
    clock[0] = 1301
    for _ in range(2):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(energy.entity_id).state == "2345"
    changed_state = hass.states.get(energy.entity_id)
    reported_before = changed_state.last_reported
    updated_before = changed_state.last_updated
    calls_before = len(meter_server[2])
    clock[0] = 1306
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done(wait_background_tasks=True)
    unchanged_state = hass.states.get(energy.entity_id)
    assert len(meter_server[2]) > calls_before
    assert unchanged_state.state == "2345"
    assert unchanged_state.last_updated == updated_before
    assert unchanged_state.last_reported > reported_before
    session = entry.runtime_data.client.session
    meter_server[1]["/upt/1/mr/1/r"] = 404
    clock[0] = 1311
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(power.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(energy.entity_id).state == "2345"
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert session.closed
    assert not Path(credential_paths(hass.config.config_dir, credentials)[0]).parent.exists()
