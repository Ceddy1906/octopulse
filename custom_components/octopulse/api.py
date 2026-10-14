"""Kleiner Client für die REST-API von OctoPulse und die Ping-Adressen."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from homeassistant.util import dt as dt_util

from .const import HEARTBEAT_MAX_MESSAGE, HEARTBEAT_TIMEOUT, REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class OctoPulseError(Exception):
    """Basisklasse aller Fehler dieser Integration."""


class OctoPulseConnectionError(OctoPulseError):
    """Der Server ist nicht erreichbar (Netz, DNS, TLS, Zeitüberschreitung)."""


class OctoPulseAuthError(OctoPulseError):
    """Der API-Key fehlt, ist ungültig oder gehört einem gesperrten Benutzer."""


class OctoPulseApiError(OctoPulseError):
    """OctoPulse hat die Anfrage abgelehnt. `message` stammt aus der Antwort des Servers."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_base_url(raw: str) -> str:
    """Macht aus der Eingabe eine saubere Basisadresse ohne Pfad und ohne abschließenden Schrägstrich.

    Wirft ValueError, wenn es keine http(s)-Adresse mit Host ist. Eine fehlende Angabe des Protokolls
    gilt als https, weil OctoPulse hinter einem Reverse Proxy mit Zertifikat läuft.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("leer")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("keine http(s)-Adresse")
    if parts.username or parts.password:
        raise ValueError("Zugangsdaten gehören nicht in die Adresse")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def host_of(url: str) -> str:
    """Nur der Rechnername, für Logs und Titel. Nie die ganze Adresse: Ping-URLs enthalten einen geheimen Token."""
    return urlsplit(url).hostname or "?"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


@dataclass(frozen=True)
class Check:
    """Ein Check, wie ihn `GET /api/v1/checks` liefert."""

    id: int
    name: str
    kind: str
    status: str
    public: bool
    period_s: int
    grace_s: int
    schedule: str
    target: str
    last_ping: datetime | None
    status_since: datetime | None
    last_detail: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Check":
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or f"Check {data['id']}"),
            kind=str(data.get("kind") or ""),
            status=str(data.get("status") or "new"),
            public=bool(data.get("public")),
            period_s=int(data.get("period_s") or 0),
            grace_s=int(data.get("grace_s") or 0),
            schedule=str(data.get("schedule") or ""),
            target=str(data.get("target") or ""),
            last_ping=_parse_time(data.get("last_ping")),
            status_since=_parse_time(data.get("status_since")),
            last_detail=str(data.get("last_detail") or ""),
        )


class OctoPulseClient:
    """Spricht mit einem OctoPulse-Server. Die Sitzung gehört Home Assistant (nicht schließen)."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            async with self._session.request(
                method, url, headers=headers, json=json, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                if resp.status == 401:
                    raise OctoPulseAuthError("API-Key fehlt oder ist ungültig")
                if resp.status == 429:
                    raise OctoPulseApiError("OctoPulse meldet zu viele Anfragen", resp.status)
                if resp.status >= 400:
                    message = body.get("error") if isinstance(body, dict) else None
                    raise OctoPulseApiError(str(message or f"HTTP {resp.status}"), resp.status)
                return body
        except OctoPulseError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OctoPulseConnectionError(f"{type(err).__name__}: {err}") from err

    async def async_get_checks(self) -> list[Check]:
        body = await self._request("GET", "/api/v1/checks")
        if not isinstance(body, dict) or not isinstance(body.get("checks"), list):
            raise OctoPulseApiError("unerwartete Antwort von OctoPulse (kein OctoPulse-Server oder falsche Adresse?)")
        checks: list[Check] = []
        for raw in body["checks"]:
            try:
                checks.append(Check.from_json(raw))
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Check mit unerwarteten Daten übersprungen: %s", raw)
        return checks

    async def async_pause(self, check_id: int) -> None:
        await self._request("POST", f"/api/v1/checks/{int(check_id)}/pause")

    async def async_resume(self, check_id: int) -> None:
        await self._request("POST", f"/api/v1/checks/{int(check_id)}/resume")

    async def async_create_heartbeat(self, name: str, period_s: int, grace_s: int) -> tuple[Check, str]:
        """Legt einen Heartbeat-Check an. Liefert den Check und die Ping-Adresse (der Token wird nur jetzt verraten)."""
        body = await self._request(
            "POST",
            "/api/v1/checks",
            json={"kind": "heartbeat", "name": name, "period_s": period_s, "grace_s": grace_s},
        )
        if not isinstance(body, dict) or not body.get("ping_url"):
            raise OctoPulseApiError("OctoPulse hat keine Ping-Adresse geliefert")
        return Check.from_json(body), str(body["ping_url"])


async def async_send_ping(
    session: aiohttp.ClientSession, ping_url: str, signal: str = "success", message: str | None = None
) -> None:
    """Schickt ein Lebenszeichen an eine Ping-Adresse von OctoPulse.

    signal: success (Standard), start (Lauf beginnt) oder fail (Lauf ist gescheitert).
    Erfolg ist jede 2xx-Antwort. Die Adresse steht nie in einer Fehlermeldung, sie enthält den geheimen Token.
    """
    url = ping_url.rstrip("/")
    if signal == "start":
        url += "/start"
    elif signal == "fail":
        url += "/fail"
    kwargs: dict[str, Any] = {}
    method = "GET"
    if message:
        method = "POST"
        kwargs["data"] = message.encode("utf-8")[:HEARTBEAT_MAX_MESSAGE]
        kwargs["headers"] = {"Content-Type": "text/plain; charset=utf-8"}
    try:
        async with session.request(
            method, url, timeout=aiohttp.ClientTimeout(total=HEARTBEAT_TIMEOUT), **kwargs
        ) as resp:
            if resp.status == 404:
                raise OctoPulseApiError("Ping-Adresse unbekannt (Token erneuert oder Check gelöscht?)", 404)
            if resp.status == 429:
                raise OctoPulseApiError("OctoPulse meldet zu viele Anfragen", 429)
            if resp.status >= 300:
                raise OctoPulseApiError(f"HTTP {resp.status}", resp.status)
    except OctoPulseError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OctoPulseConnectionError(type(err).__name__) from err
