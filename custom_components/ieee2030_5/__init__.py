"""Local smart energy meter integration."""

import logging
import ssl
from datetime import timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .credentials import credential_paths, remove_credentials
from .protocol import AuthenticationError, MeterClient, MeterError, build_ssl_context

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]


class MeterCoordinator(DataUpdateCoordinator):
    """Poll the meter sequentially; a failure marks values unavailable."""

    def __init__(self, hass, entry, client):
        super().__init__(
            hass,
            _LOGGER,
            name="Smart Energy Meter",
            config_entry=entry,
            update_interval=timedelta(seconds=entry.data["interval"]),
        )
        self.client = client

    async def _async_update_data(self):
        try:
            return await self.client.poll()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed("Meter certificate or TLS connection rejected") from err
        except MeterError as err:
            raise UpdateFailed(str(err)) from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        cert, key, ca = await hass.async_add_executor_job(
            credential_paths, hass.config.config_dir, entry.data["credentials"]
        )
        context = await hass.async_add_executor_job(
            partial(
                build_ssl_context,
                cert,
                key,
                legacy=entry.data["profile"] == "itron",
                ca=ca,
                password=entry.data.get("key_password"),
            )
        )
        session = async_create_clientsession(hass, auto_cleanup=False)
        entry.async_on_unload(session.detach)
        client = MeterClient(
            session, entry.data["host"], entry.data["port"], context, entry.data["profile"]
        )
        await client.discover()
        if client.info["lfdi"] != entry.unique_id:
            raise ConfigEntryNotReady(
                "This address now belongs to a different meter; reconfigure it"
            )
        coordinator = MeterCoordinator(hass, entry, client)
        await coordinator.async_config_entry_first_refresh()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed("Meter access rejected; reconfigure certificates") from err
    except (MeterError, ValueError, OSError, ssl.SSLError, TimeoutError) as err:
        raise ConfigEntryNotReady("Unable to initialize the meter connection") from err
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.async_add_executor_job(
        remove_credentials, hass.config.config_dir, entry.data["credentials"]
    )
