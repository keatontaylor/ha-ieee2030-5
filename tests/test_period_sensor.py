"""Local import accounting against synthetic readings and the actual HA lifecycle."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ieee2030_5.const import DOMAIN
from custom_components.ieee2030_5.credentials import store_credentials
from custom_components.ieee2030_5.period_sensor import PeriodStoredData, period_start
from custom_components.ieee2030_5.protocol import Register, imported_energy_register

from .conftest import xml

IMPORT = "/upt/1/mr/3/rs/1/r/1"
HOURLY = "sensor.smart_energy_meter_energy_imported_hourly"
DAILY = "sensor.smart_energy_meter_energy_imported_daily"


@pytest.fixture
async def import_entry(hass, meter_server, certificates, freezer):
    freezer.move_to("2026-10-15T17:30:00Z")  # 11:30 in Denver
    await hass.config.async_set_time_zone("America/Denver")
    meter_server[1][IMPORT] = xml("Reading", "<value>10000</value>")
    credentials = await hass.async_add_executor_job(
        store_credentials,
        hass.config.config_dir,
        (certificates / "client.pem").read_bytes(),
        (certificates / "client.key").read_bytes(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aabbccdd",
        title="Smart Energy Meter",
        data={
            "credentials": credentials,
            "host": "127.0.0.1",
            "port": meter_server[0].port,
            "profile": "itron",
            "interval": 5,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Test accounting/timers independently from automatic HTTP polling.
    entry.runtime_data.update_interval = None
    entry.runtime_data.async_set_updated_data(dict(entry.runtime_data.data))
    return entry


async def update_import(hass, entry, value):
    data = dict(entry.runtime_data.data)
    data[IMPORT] = Decimal(value) if value is not None else None
    entry.runtime_data.async_set_updated_data(data)
    await hass.async_block_till_done()


async def move_clock(hass, freezer, when):
    freezer.move_to(when)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_counters_increment_once_without_http_and_rebase_source_reset(
    hass, import_entry, meter_server
):
    calls = list(meter_server[2])
    assert len(hass.states.async_all("sensor")) == 5
    for entity_id in (HOURLY, DAILY):
        state = hass.states.get(entity_id)
        assert state.state == "0"
        assert state.attributes["unit_of_measurement"] == "kWh"
        assert state.attributes["device_class"] == "energy"
        assert state.attributes["state_class"] == "total_increasing"
    await update_import(hass, import_entry, 11500)
    await update_import(hass, import_entry, 11500)  # repeated cached sample
    assert hass.states.get(HOURLY).state == hass.states.get(DAILY).state == "1.5"
    await update_import(hass, import_entry, None)
    assert hass.states.get(HOURLY).state == STATE_UNAVAILABLE
    await update_import(hass, import_entry, 12000)
    assert hass.states.get(HOURLY).state == "2.0"
    await update_import(hass, import_entry, 100)  # reset the cumulative source
    await update_import(hass, import_entry, 350)
    assert hass.states.get(HOURLY).state == hass.states.get(DAILY).state == "2.25"
    assert meter_server[2] == calls


async def test_local_hour_and_midnight_reset_even_without_new_meter_reading(
    hass, import_entry, meter_server, freezer
):
    calls = list(meter_server[2])
    await update_import(hass, import_entry, 11500)
    await move_clock(hass, freezer, "2026-10-15T18:00:01Z")
    assert hass.states.get(HOURLY).state == "0"
    assert hass.states.get(HOURLY).attributes["last_period"] == "1.5"
    assert hass.states.get(DAILY).state == "1.5"
    await update_import(hass, import_entry, 12000)
    assert hass.states.get(HOURLY).state == "0.5"
    assert hass.states.get(DAILY).state == "2.0"
    await move_clock(hass, freezer, "2026-10-16T06:00:01Z")
    assert hass.states.get(HOURLY).state == hass.states.get(DAILY).state == "0"
    assert hass.states.get(DAILY).attributes["last_period"] == "2.0"
    assert meter_server[2] == calls


async def test_unavailable_source_across_boundary_does_not_add_outage_to_new_hour(
    hass, import_entry, freezer
):
    await update_import(hass, import_entry, 11500)
    await update_import(hass, import_entry, None)
    await move_clock(hass, freezer, "2026-10-15T18:00:01Z")
    await update_import(hass, import_entry, 12000)
    assert hass.states.get(HOURLY).state == "0"
    assert hass.states.get(DAILY).state == "2.0"
    await update_import(hass, import_entry, 12500)
    assert hass.states.get(HOURLY).state == "0.5"


async def test_reload_restores_total_and_baseline_without_double_counting(
    hass, import_entry, meter_server
):
    await update_import(hass, import_entry, 11500)
    meter_server[1][IMPORT] = xml("Reading", "<value>12000</value>")
    assert await hass.config_entries.async_reload(import_entry.entry_id)
    await hass.async_block_till_done()
    import_entry.runtime_data.update_interval = None
    import_entry.runtime_data.async_set_updated_data(dict(import_entry.runtime_data.data))
    await hass.async_block_till_done()
    assert hass.states.get(HOURLY).state == hass.states.get(DAILY).state == "2.0"
    await update_import(hass, import_entry, 12500)
    assert hass.states.get(HOURLY).state == "2.5"


async def test_source_rename_and_display_unit_do_not_change_accounting(hass, import_entry):
    source_id = hass.states.get(HOURLY).attributes["source"]
    registry = er.async_get(hass)
    registry.async_update_entity_options(source_id, "sensor", {"unit_of_measurement": "kWh"})
    registry.async_update_entity(source_id, new_entity_id="sensor.renamed_import")
    await hass.async_block_till_done()
    await update_import(hass, import_entry, 11500)
    assert hass.states.get("sensor.renamed_import").state == "11.5"
    assert hass.states.get(HOURLY).state == "1.5"
    assert hass.states.get(HOURLY).attributes["source"] == "sensor.renamed_import"


async def test_reload_in_new_day_starts_partial_period_without_backfilling(
    hass, import_entry, meter_server, freezer
):
    await update_import(hass, import_entry, 11500)
    assert await hass.config_entries.async_unload(import_entry.entry_id)
    await hass.async_block_till_done()
    await move_clock(hass, freezer, "2026-10-16T17:30:00Z")
    meter_server[1][IMPORT] = xml("Reading", "<value>14000</value>")
    assert await hass.config_entries.async_setup(import_entry.entry_id)
    await hass.async_block_till_done()
    import_entry.runtime_data.update_interval = None
    import_entry.runtime_data.async_set_updated_data(dict(import_entry.runtime_data.data))
    for entity_id in (HOURLY, DAILY):
        assert hass.states.get(entity_id).state == "0"
        assert hass.states.get(entity_id).attributes["last_period"] == "1.5"
    await update_import(hass, import_entry, 14500)
    assert hass.states.get(HOURLY).state == hass.states.get(DAILY).state == "0.5"


async def test_repeated_dst_hour_resets_hourly_but_not_daily(hass, import_entry, freezer):
    await move_clock(hass, freezer, "2026-11-01T07:30:00Z")  # first 01:30, MDT
    await update_import(hass, import_entry, 11000)
    assert hass.states.get(HOURLY).state == "1"
    await move_clock(hass, freezer, "2026-11-01T08:00:01Z")  # second 01:00, MST
    assert hass.states.get(HOURLY).state == "0"
    assert hass.states.get(DAILY).state == "1"
    await update_import(hass, import_entry, 12000)
    assert hass.states.get(HOURLY).state == "1"
    assert hass.states.get(DAILY).state == "2"


async def test_calendar_keys_respect_23_and_25_hour_days(hass):
    await hass.config.async_set_time_zone("America/Denver")
    spring = datetime(2026, 3, 8, 20, tzinfo=UTC)
    fall = datetime(2026, 11, 1, 20, tzinfo=UTC)
    assert period_start(spring + timedelta(days=1), "daily") - period_start(
        spring, "daily"
    ) == timedelta(hours=23)
    assert period_start(fall + timedelta(days=1), "daily") - period_start(
        fall, "daily"
    ) == timedelta(hours=25)


def test_restore_validation_preserves_zero_and_rejects_invalid_data():
    original = PeriodStoredData(
        Decimal("1.25"), "kWh", datetime(2026, 10, 15, 6, tzinfo=UTC), Decimal(0), Decimal(2)
    )
    assert PeriodStoredData.from_dict(original.as_dict()) == original
    for field, value in (
        ("last_total", "NaN"),
        ("last_period", "-1"),
        ("period", "2026-10-15T06:00:00"),
        ("period", "broken"),
        ("native_unit_of_measurement", "Wh"),
    ):
        invalid = original.as_dict() | {field: value}
        assert PeriodStoredData.from_dict(invalid) is None
    assert PeriodStoredData.from_dict({}) is None


def test_import_source_selection_never_sums_export_tou_or_duplicate_aliases():
    primary = Register(IMPORT, "Import", "Wh", "energy", "total_increasing")
    alias = replace(primary, href="/upt/1/mr/3/r")
    assert imported_energy_register({r.href: r for r in (primary, alias)}, "itron") == primary
    assert imported_energy_register({alias.href: alias}, "itron") == alias
    forward = replace(primary, href="/standard/import", flow_direction=1)
    export = replace(forward, href="/standard/export", flow_direction=19)
    tou = replace(forward, href="/standard/tou", tier="1")
    assert (
        imported_energy_register({r.href: r for r in (forward, export, tou)}, "standard") == forward
    )
    assert imported_energy_register({r.href: r for r in (export, tou)}, "standard") is None
    other = replace(forward, href="/another/import")
    assert imported_energy_register({r.href: r for r in (forward, other)}, "standard") is None
