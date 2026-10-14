"""Einrichtung, Neuanmeldung und Optionen der OctoPulse-Integration."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    OctoPulseApiError,
    OctoPulseAuthError,
    OctoPulseClient,
    OctoPulseConnectionError,
    OctoPulseError,
    async_send_ping,
    host_of,
    normalize_base_url,
)
from .const import (
    CONF_API_KEY,
    CONF_HEARTBEAT_CHECK_ID,
    CONF_HEARTBEAT_GRACE_MINUTES,
    CONF_HEARTBEAT_MINUTES,
    CONF_HEARTBEAT_NAME,
    CONF_HEARTBEAT_URL,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_HEARTBEAT_GRACE_MINUTES,
    DEFAULT_HEARTBEAT_MINUTES,
    DEFAULT_HEARTBEAT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_HEARTBEAT_MINUTES,
    MAX_SCAN_INTERVAL,
    MIN_HEARTBEAT_MINUTES,
    MIN_SCAN_INTERVAL,
    SIGNAL_SUCCESS,
)

_LOGGER = logging.getLogger(__name__)

_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL, autocomplete="url"))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off"))
_TEXT = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))


def _minutes(minimum: int = MIN_HEARTBEAT_MINUTES, maximum: int = MAX_HEARTBEAT_MINUTES) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
    )


def _server_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, vol.UNDEFINED)): _URL,
            vol.Required(CONF_API_KEY, default=defaults.get(CONF_API_KEY, vol.UNDEFINED)): _SECRET,
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): BooleanSelector(),
        }
    )


async def _async_check_server(hass, url: str, api_key: str, verify_ssl: bool) -> str | None:
    """Prüft Adresse und Key. Liefert einen Fehlerschlüssel oder None, wenn alles stimmt."""
    client = OctoPulseClient(async_get_clientsession(hass, verify_ssl=verify_ssl), url, api_key)
    try:
        await client.async_get_checks()
    except OctoPulseAuthError:
        return "invalid_auth"
    except OctoPulseConnectionError:
        return "cannot_connect"
    except OctoPulseApiError:
        return "invalid_response"
    except Exception:  # noqa: BLE001 - jeder andere Fehler soll im Dialog landen und nicht als Absturz
        _LOGGER.exception("Unerwarteter Fehler beim Prüfen von OctoPulse")
        return "unknown"
    return None


def _normalize_ping_url(raw: str) -> str:
    """Prüft, dass es eine Ping-Adresse von OctoPulse ist (…/ping/<token>), und schneidet Anhängsel ab."""
    parts = urlsplit(normalize_base_url(raw))
    segments = [s for s in parts.path.split("/") if s]
    if "ping" not in segments:
        raise ValueError("kein /ping/-Pfad")
    index = segments.index("ping")
    if len(segments) <= index + 1:
        raise ValueError("Token fehlt")
    path = "/" + "/".join(segments[: index + 2])  # /…/ping/<token>, ohne /start oder /fail
    return f"{parts.scheme}://{parts.netloc}{path}"


class _HeartbeatSteps:
    """Dialogschritte für den Heartbeat. Gleich bei der Ersteinrichtung und in den Optionen."""

    hass: Any

    # Das Gegenstück liefert die Zugangsdaten und entscheidet, was nach dem Heartbeat passiert.
    def _server_credentials(self) -> tuple[str, str, bool]:
        raise NotImplementedError

    async def _async_heartbeat_chosen(self, ping_url: str, check_id: int | None, minutes: int) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_heartbeat_create(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Legt in OctoPulse einen Heartbeat-Check an und merkt sich dessen Ping-Adresse."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": ""}
        if user_input is not None:
            url, key, verify = self._server_credentials()
            client = OctoPulseClient(async_get_clientsession(self.hass, verify_ssl=verify), url, key)
            minutes = int(user_input[CONF_HEARTBEAT_MINUTES])
            grace = int(user_input[CONF_HEARTBEAT_GRACE_MINUTES])
            name = user_input[CONF_HEARTBEAT_NAME].strip() or DEFAULT_HEARTBEAT_NAME
            try:
                check, ping_url = await client.async_create_heartbeat(name, minutes * 60, grace * 60)
            except OctoPulseAuthError:
                errors["base"] = "invalid_auth"
            except OctoPulseConnectionError:
                errors["base"] = "cannot_connect"
            except OctoPulseApiError as err:
                errors["base"] = "create_failed"
                placeholders["error"] = err.message
            except OctoPulseError:
                errors["base"] = "unknown"
            else:
                return await self._async_heartbeat_chosen(ping_url, check.id, minutes)
        defaults = user_input or {}
        return self.async_show_form(
            step_id="heartbeat_create",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HEARTBEAT_NAME, default=defaults.get(CONF_HEARTBEAT_NAME, DEFAULT_HEARTBEAT_NAME)): _TEXT,
                    vol.Required(
                        CONF_HEARTBEAT_MINUTES, default=defaults.get(CONF_HEARTBEAT_MINUTES, DEFAULT_HEARTBEAT_MINUTES)
                    ): _minutes(),
                    vol.Required(
                        CONF_HEARTBEAT_GRACE_MINUTES,
                        default=defaults.get(CONF_HEARTBEAT_GRACE_MINUTES, DEFAULT_HEARTBEAT_GRACE_MINUTES),
                    ): _minutes(0, MAX_HEARTBEAT_MINUTES),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_heartbeat_url(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Übernimmt eine vorhandene Ping-Adresse und prüft sie mit einem echten Lebenszeichen."""
        errors: dict[str, str] = {}
        if user_input is not None:
            _, _, verify = self._server_credentials()
            minutes = int(user_input[CONF_HEARTBEAT_MINUTES])
            try:
                ping_url = _normalize_ping_url(user_input[CONF_HEARTBEAT_URL])
            except ValueError:
                errors[CONF_HEARTBEAT_URL] = "invalid_ping_url"
            else:
                try:
                    await async_send_ping(async_get_clientsession(self.hass, verify_ssl=verify), ping_url, SIGNAL_SUCCESS)
                except OctoPulseConnectionError:
                    errors["base"] = "cannot_connect"
                except OctoPulseError:
                    errors[CONF_HEARTBEAT_URL] = "ping_rejected"
                else:
                    return await self._async_heartbeat_chosen(ping_url, None, minutes)
        defaults = user_input or {}
        return self.async_show_form(
            step_id="heartbeat_url",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HEARTBEAT_URL, default=defaults.get(CONF_HEARTBEAT_URL, vol.UNDEFINED)): _SECRET,
                    vol.Required(
                        CONF_HEARTBEAT_MINUTES, default=defaults.get(CONF_HEARTBEAT_MINUTES, DEFAULT_HEARTBEAT_MINUTES)
                    ): _minutes(),
                }
            ),
            errors=errors,
        )


class OctoPulseConfigFlow(_HeartbeatSteps, ConfigFlow, domain=DOMAIN):
    """Ersteinrichtung: Server und Key, dann optional ein Heartbeat."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}

    def _server_credentials(self) -> tuple[str, str, bool]:
        return self._data[CONF_URL], self._data[CONF_API_KEY], self._data[CONF_VERIFY_SSL]

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_base_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                api_key = user_input[CONF_API_KEY].strip()
                verify = user_input[CONF_VERIFY_SSL]
                await self.async_set_unique_id(url.lower())
                self._abort_if_unique_id_configured()
                if error := await _async_check_server(self.hass, url, api_key, verify):
                    errors["base"] = error
                else:
                    self._data = {CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify}
                    return await self.async_step_heartbeat()
        return self.async_show_form(step_id="user", data_schema=_server_schema(user_input or {}), errors=errors)

    async def async_step_heartbeat(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="heartbeat", menu_options=["heartbeat_create", "heartbeat_url", "finish"])

    async def _async_heartbeat_chosen(self, ping_url: str, check_id: int | None, minutes: int) -> ConfigFlowResult:
        self._data[CONF_HEARTBEAT_URL] = ping_url
        if check_id is not None:
            self._data[CONF_HEARTBEAT_CHECK_ID] = check_id
        self._options[CONF_HEARTBEAT_MINUTES] = minutes
        return await self.async_step_finish()

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"OctoPulse ({host_of(self._data[CONF_URL])})", data=self._data, options=self._options
        )

    # --- Neuanmeldung, wenn der Key nicht mehr gilt ---
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if error := await _async_check_server(
                self.hass, entry.data[CONF_URL], api_key, entry.data.get(CONF_VERIFY_SSL, True)
            ):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_API_KEY: api_key})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): _SECRET}),
            description_placeholders={"host": host_of(entry.data[CONF_URL])},
            errors=errors,
        )

    # --- Adresse oder Key ändern ---
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_base_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                api_key = user_input[CONF_API_KEY].strip()
                verify = user_input[CONF_VERIFY_SSL]
                if error := await _async_check_server(self.hass, url, api_key, verify):
                    errors["base"] = error
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify}
                    )
        defaults = user_input or dict(entry.data)
        return self.async_show_form(step_id="reconfigure", data_schema=_server_schema(defaults), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OctoPulseOptionsFlow:
        return OctoPulseOptionsFlow()


class OctoPulseOptionsFlow(_HeartbeatSteps, OptionsFlow):
    """Abfrageintervall, Heartbeat einrichten, ändern oder entfernen."""

    def _server_credentials(self) -> tuple[str, str, bool]:
        data = self.config_entry.data
        return data[CONF_URL], data[CONF_API_KEY], data.get(CONF_VERIFY_SSL, True)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["settings", "heartbeat_create", "heartbeat_url"]
        if self.config_entry.data.get(CONF_HEARTBEAT_URL):
            options.append("heartbeat_remove")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_HEARTBEAT_MINUTES: int(user_input[CONF_HEARTBEAT_MINUTES]),
                }
            )
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(
                        CONF_HEARTBEAT_MINUTES, default=opts.get(CONF_HEARTBEAT_MINUTES, DEFAULT_HEARTBEAT_MINUTES)
                    ): _minutes(),
                }
            ),
        )

    async def _async_heartbeat_chosen(self, ping_url: str, check_id: int | None, minutes: int) -> ConfigFlowResult:
        data = {**self.config_entry.data, CONF_HEARTBEAT_URL: ping_url}
        data.pop(CONF_HEARTBEAT_CHECK_ID, None)
        if check_id is not None:
            data[CONF_HEARTBEAT_CHECK_ID] = check_id
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(data={**self.config_entry.options, CONF_HEARTBEAT_MINUTES: minutes})

    async def async_step_heartbeat_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Hört auf, Lebenszeichen zu senden. Der Check in OctoPulse bleibt bestehen und wird dort bald überfällig."""
        data = {k: v for k, v in self.config_entry.data.items() if k not in (CONF_HEARTBEAT_URL, CONF_HEARTBEAT_CHECK_ID)}
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(data=dict(self.config_entry.options))
