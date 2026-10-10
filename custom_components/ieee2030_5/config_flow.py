"""Home Assistant setup and certificate rotation using native file uploads."""

from __future__ import annotations

import logging
import ssl
from functools import partial

import probatio as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DEFAULT_INTERVAL, DEFAULT_PORT, DOMAIN
from .credentials import credential_paths, remove_credentials, store_credentials
from .protocol import (
    AuthenticationError,
    InvalidResponse,
    MeterClient,
    MeterError,
    build_ssl_context,
)

_LOGGER = logging.getLogger(__name__)

FILE = FileSelector(FileSelectorConfig(accept=".pem,.crt,.cer,.key"))


def schema(defaults: dict, rotate: bool = False) -> vol.Schema:
    fields = {
        vol.Required("host", default=defaults.get("host", "")): str,
        vol.Required("port", default=defaults.get("port", DEFAULT_PORT)): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required("profile", default=defaults.get("profile", "itron")): SelectSelector(
            SelectSelectorConfig(options=["itron", "standard"], translation_key="profile")
        ),
        vol.Required("interval", default=defaults.get("interval", DEFAULT_INTERVAL)): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=3600)
        ),
        vol.Optional("key_password", default=defaults.get("key_password", "")): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
    required = vol.Optional if rotate else vol.Required
    fields[required("certificate")] = FILE
    fields[required("private_key")] = FILE
    fields[vol.Optional("ca_certificate")] = FILE
    return vol.Schema(fields)


class MeterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Discover, verify the connection, then persist one config entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._defaults: dict = {}
        self._reauth = False
        self._uploaded_files: dict[str, bytes] = {}

    async def async_step_zeroconf(self, discovery_info):
        # Discovery has no authenticated meter identity yet. HA's helper handles
        # this case; the actual entry unique ID comes from the TLS-protected LFDI.
        await self._async_handle_discovery_without_unique_id()
        self._defaults = {"host": discovery_info.host, "port": discovery_info.port}
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        return await self._configure(user_input, False)

    async def async_step_reconfigure(self, user_input=None):
        self._defaults = dict(self._get_reconfigure_entry().data)
        return await self._configure(user_input, True)

    async def async_step_reauth(self, entry_data):
        self._reauth = True
        self._defaults = dict(self._get_reauth_entry().data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._configure(user_input, True)

    async def _upload(self, file_id: str) -> bytes:
        # HA consumes each upload UUID once. Keep bytes only in this short-lived
        # flow so correcting a failed connection does not require another upload.
        if file_id in self._uploaded_files:
            return self._uploaded_files[file_id]

        def read():
            with process_uploaded_file(self.hass, file_id) as path:
                if path.stat().st_size > 64 * 1024:
                    raise ValueError("Certificate file is too large")
                return path.read_bytes()

        content = await self.hass.async_add_executor_job(read)
        self._uploaded_files[file_id] = content
        return content

    async def _configure(self, user_input: dict | None, rotate: bool):
        step = "reauth_confirm" if self._reauth else "reconfigure" if rotate else "user"
        errors = {}
        if user_input is not None:
            file_ids = {
                user_input.get(field) for field in ("certificate", "private_key", "ca_certificate")
            }
            self._uploaded_files = {
                key: value for key, value in self._uploaded_files.items() if key in file_ids
            }
            data = {**self._defaults, **user_input}
            new_credentials = None
            session = None
            try:
                if bool(user_input.get("certificate")) != bool(user_input.get("private_key")):
                    raise ValueError("Upload both certificate and private key")
                if user_input.get("certificate"):
                    certificate = await self._upload(user_input["certificate"])
                    key = await self._upload(user_input["private_key"])
                    ca = (
                        await self._upload(user_input["ca_certificate"])
                        if user_input.get("ca_certificate")
                        else None
                    )
                    new_credentials = await self.hass.async_add_executor_job(
                        store_credentials, self.hass.config.config_dir, certificate, key, ca
                    )
                    data["credentials"] = new_credentials
                elif user_input.get("ca_certificate"):
                    raise ValueError("Upload the full certificate set when changing the CA")
                cert, key, ca = await self.hass.async_add_executor_job(
                    credential_paths, self.hass.config.config_dir, data["credentials"]
                )
                context = await self.hass.async_add_executor_job(
                    partial(
                        build_ssl_context,
                        cert,
                        key,
                        legacy=data["profile"] == "itron",
                        ca=ca,
                        password=data.get("key_password"),
                    )
                )
                session = async_create_clientsession(self.hass, auto_cleanup=False)
                client = MeterClient(session, data["host"], data["port"], context, data["profile"])
                await client.discover()
                await self.async_set_unique_id(client.info["lfdi"])
                if rotate:
                    self._abort_if_unique_id_mismatch()
                else:
                    self._abort_if_unique_id_configured()
                for field in ("certificate", "private_key", "ca_certificate"):
                    data.pop(field, None)
                if rotate:
                    entry = (
                        self._get_reauth_entry() if self._reauth else self._get_reconfigure_entry()
                    )
                    old_credentials = entry.data["credentials"]
                    result = self.async_update_reload_and_abort(entry, data_updates=data)
                    new_credentials = None
                    self._uploaded_files.clear()
                    if old_credentials != data["credentials"]:
                        await self.hass.async_add_executor_job(
                            remove_credentials, self.hass.config.config_dir, old_credentials
                        )
                    return result
                result = self.async_create_entry(title="Smart Energy Meter", data=data)
                new_credentials = None
                self._uploaded_files.clear()
                return result
            except AuthenticationError as err:
                _LOGGER.warning("Meter setup rejected: %s", err)
                errors["base"] = "invalid_auth"
            except InvalidResponse as err:
                _LOGGER.warning("Meter discovery failed: %s", err)
                errors["base"] = "invalid_configuration"
            except ValueError, KeyError, OSError, ssl.SSLError:
                _LOGGER.warning("Meter setup has invalid credentials or connection settings")
                errors["base"] = "invalid_configuration"
            except MeterError, TimeoutError:
                _LOGGER.warning("Meter setup could not connect or exceeded the discovery timeout")
                errors["base"] = "cannot_connect"
            finally:
                if session is not None:
                    session.detach()
                if new_credentials:
                    await self.hass.async_add_executor_job(
                        remove_credentials, self.hass.config.config_dir, new_credentials
                    )
        return self.async_show_form(
            step_id=step, data_schema=schema(self._defaults, rotate), errors=errors
        )
