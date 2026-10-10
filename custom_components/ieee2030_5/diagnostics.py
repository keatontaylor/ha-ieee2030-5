"""Diagnostics intentionally exclude addresses, identities and all credentials."""


async def async_get_config_entry_diagnostics(hass, entry):
    client = entry.runtime_data.client
    return {
        "profile": entry.data["profile"],
        "interval": entry.data["interval"],
        "firmware": client.info.get("firmware"),
        "register_count": len(client.registers),
        "last_update_success": entry.runtime_data.last_update_success,
        "readings": [
            {
                "unit": item.unit,
                "device_class": item.device_class,
                "state_class": item.state_class,
                "multiplier": item.multiplier,
            }
            for item in client.registers.values()
        ],
    }
