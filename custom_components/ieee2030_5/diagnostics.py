"""Diagnostics intentionally exclude addresses, identities and all credentials."""


async def async_get_config_entry_diagnostics(hass, entry):
    client = entry.runtime_data.client
    return {
        "profile": entry.data["profile"],
        "interval": entry.data["interval"],
        "firmware": client.info.get("firmware"),
        "register_count": len(client.registers),
        "last_update_success": entry.runtime_data.last_update_success,
        "registers_in_backoff": client.registers_in_backoff,
        "readings": [
            {
                "unit": item.unit,
                "device_class": item.device_class,
                "state_class": item.state_class,
                "multiplier": item.multiplier,
                "poll_category": item.category,
                "poll_interval_seconds": item.interval(client.instantaneous_interval),
            }
            for item in client.registers.values()
        ],
    }
