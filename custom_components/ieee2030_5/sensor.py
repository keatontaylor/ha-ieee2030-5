"""One sensor per supported, discovered meter register."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .protocol import imported_energy_register


async def async_setup_entry(hass, entry, async_add_entities):
    from .period_sensor import ImportPeriodSensor

    coordinator = entry.runtime_data
    sensors = {
        register.href: MeterSensor(coordinator, register)
        for register in coordinator.client.registers.values()
    }
    entities = list(sensors.values())
    if source := imported_energy_register(coordinator.client.registers, coordinator.client.profile):
        entities.extend(
            ImportPeriodSensor(sensors[source.href], cycle) for cycle in ("hourly", "daily")
        )
    async_add_entities(entities)


class MeterSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, register):
        super().__init__(coordinator)
        self.register = register
        info = coordinator.client.info
        self._attr_name = register.name
        self._attr_unique_id = f"{info['lfdi']}:{register.href}"
        self._attr_native_unit_of_measurement = register.unit
        self._attr_device_class = register.device_class
        self._attr_state_class = register.state_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, info["lfdi"])},
            name="Smart Energy Meter",
            manufacturer=info.get("manufacturer") or None,
            model=info.get("model") or None,
            sw_version=info.get("firmware") or None,
        )

    @property
    def available(self):
        return super().available and self.coordinator.data.get(self.register.href) is not None

    @property
    def native_value(self):
        return self.coordinator.data.get(self.register.href)

    @property
    def extra_state_attributes(self):
        attributes = {
            "poll_interval_seconds": self.register.interval(
                self.coordinator.client.instantaneous_interval
            )
        }
        if self.register.tier is not None:
            attributes["tou_tier"] = self.register.tier
        return attributes
