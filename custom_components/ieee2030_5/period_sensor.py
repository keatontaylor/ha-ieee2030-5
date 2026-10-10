"""Restored calendar-hour/day import counters using already-polled readings."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from homeassistant.components.sensor import RestoreSensor, SensorExtraStoredData, SensorStateClass
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .sensor import MeterSensor


def period_start(now: datetime, cycle: str) -> datetime:
    """UTC keys distinguish the two occurrences of a repeated DST hour."""
    local = dt_util.as_local(now)
    start = local.replace(minute=0, second=0, microsecond=0)
    if cycle == "daily":
        start = start.replace(hour=0, fold=0)
    return start.astimezone(UTC)


@dataclass
class PeriodStoredData(SensorExtraStoredData):
    period: datetime
    last_total: Decimal | None
    last_period: Decimal

    def as_dict(self):
        return {
            **super().as_dict(),
            "period": self.period.isoformat(),
            "last_total": str(self.last_total) if self.last_total is not None else None,
            "last_period": str(self.last_period),
        }

    @classmethod
    def from_dict(cls, restored):
        base = SensorExtraStoredData.from_dict(restored)
        if base is None or base.native_unit_of_measurement != UnitOfEnergy.KILO_WATT_HOUR:
            return None
        try:
            period = dt_util.parse_datetime(restored["period"])
            value = Decimal(str(base.native_value))
            last_total = (
                Decimal(restored["last_total"]) if restored["last_total"] is not None else None
            )
            last_period = Decimal(restored["last_period"])
            if (
                period is None
                or period.tzinfo is None
                or any(
                    not number.is_finite() or number < 0
                    for number in (value, last_period, last_total)
                    if number is not None
                )
            ):
                return None
        except KeyError, TypeError, ValueError, InvalidOperation:
            return None
        return cls(value, base.native_unit_of_measurement, period, last_total, last_period)


class ImportPeriodSensor(MeterSensor, RestoreSensor):
    """Accumulate positive import deltas in kWh; no additional meter requests."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, source: MeterSensor, cycle: str):
        super().__init__(source.coordinator, source.register)
        self.source = source
        self.cycle = cycle
        self._attr_name = f"Energy imported {cycle}"
        self._attr_unique_id = f"{source.coordinator.client.info['lfdi']}:energy_imported:{cycle}"
        self._attr_icon = "mdi:counter"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._total = Decimal(0)
        self._last_total: Decimal | None = None
        self._period: datetime | None = None
        self._last_period = Decimal(0)
        self._ready = False

    @property
    def native_value(self):
        return self._total

    @property
    def available(self):
        value = self.coordinator.data.get(self.register.href)
        return self._ready and super().available and value >= 0

    @property
    def extra_state_attributes(self):
        return {
            "source": self.source.entity_id,
            "meter_period": self.cycle,
            "last_period": str(self._last_period),
            "last_reset": self._period.isoformat() if self._period else None,
        }

    @property
    def extra_restore_state_data(self):
        if self._period is None:
            return None
        return PeriodStoredData(
            self._total,
            UnitOfEnergy.KILO_WATT_HOUR,
            self._period,
            self._last_total,
            self._last_period,
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        now = dt_util.utcnow()
        current_period = period_start(now, self.cycle)
        extra = await self.async_get_last_extra_data()
        restored = PeriodStoredData.from_dict(extra.as_dict()) if extra else None
        if restored:
            self._last_period = restored.last_period
            if restored.period == current_period:
                self._total = restored.native_value
                self._last_total = restored.last_total
            else:
                # Do not attribute consumption across downtime to a new hour/day.
                self._last_period = restored.native_value
        self._period = current_period
        self._ready = True
        self._accumulate(now)
        self.async_on_remove(
            async_track_time_change(
                self.hass,
                self._reset_period,
                hour=0 if self.cycle == "daily" else None,
                minute=0,
                second=0,
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE,
                self._time_zone_changed,
            )
        )

    def _rollover(self, now):
        current_period = period_start(now, self.cycle)
        if current_period != self._period:
            self._last_period = self._total
            self._total = Decimal(0)
            self._period = current_period
            if (
                not self.coordinator.last_update_success
                or self.coordinator.data.get(self.register.href) is None
            ):
                # An outage spanning a boundary cannot be split reliably.
                self._last_total = None

    def _accumulate(self, now):
        self._rollover(now)
        value = self.coordinator.data.get(self.register.href)
        if value is None or value < 0:
            return
        if self._last_total is not None and value >= self._last_total:
            self._total += (value - self._last_total) / 1000
        # Rebase after a source reset; never add its lifetime total as consumption.
        self._last_total = value

    @callback
    def _handle_coordinator_update(self):
        if self._ready:
            self._accumulate(dt_util.utcnow())
            self.async_write_ha_state()

    @callback
    def _reset_period(self, now):
        self._rollover(now)
        self.async_write_ha_state()

    @callback
    def _time_zone_changed(self, event):
        self._reset_period(dt_util.utcnow())
