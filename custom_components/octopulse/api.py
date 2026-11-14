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



def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _num(value: Any) -> int | None:
    # bool ist in Python eine Zahl, in den Daten aber nie gemeint
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@dataclass(frozen=True)
class DeviceUser:
    name: str
    active: bool  # False: getrennt (zum Beispiel Remotesitzung ohne Verbindung)


@dataclass(frozen=True)
class DeviceNetwork:
    name: str  # Anzeigename der Verbindung, bei WLAN der WLAN-Name
    kind: str  # wlan, ethernet oder other
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class DeviceDisk:
    drive: str  # zum Beispiel "C:"
    total_gb: int
    free_gb: int

    @property
    def free_percent(self) -> float | None:
        return round(100 * self.free_gb / self.total_gb, 1) if self.total_gb > 0 else None


@dataclass(frozen=True)
class DeviceData:
    """Der letzte Stand, den der Windows-Client eines Geräts gemeldet hat (`device` im Check)."""

    reported_at: datetime | None
    host_name: str
    domain: str
    public_ip: str
    address: str  # Hauptadresse (IPv4 einer echten Verbindung)
    network: str  # Name des Netzwerks dieser Verbindung
    networks: tuple[DeviceNetwork, ...]
    users: tuple[DeviceUser, ...]
    os_name: str
    os_version: str
    os_build: str
    boot: datetime | None
    time_zone: str
    memory_total_mb: int | None
    memory_free_mb: int | None
    disks: tuple[DeviceDisk, ...]
    agent_version: str

    @property
    def memory_used_percent(self) -> float | None:
        if not self.memory_total_mb or self.memory_free_mb is None or self.memory_total_mb <= 0:
            return None
        return round(100 * (self.memory_total_mb - self.memory_free_mb) / self.memory_total_mb, 1)

    @classmethod
    def from_json(cls, data: Any) -> "DeviceData | None":
        """Liest das `device`-Objekt. Bei Unbrauchbarem kommt None, nie eine Ausnahme: Ein Gerät mit seltsamen Daten
        soll nicht den Abruf aller anderen Checks stören."""
        if not isinstance(data, dict):
            return None
        host = data.get("host") if isinstance(data.get("host"), dict) else {}
        os_ = data.get("os") if isinstance(data.get("os"), dict) else {}
        memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
        agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}

        networks: list[DeviceNetwork] = []
        for adapter in data.get("network") or []:
            if not isinstance(adapter, dict):
                continue
            addresses = tuple(a for a in adapter.get("addresses") or [] if isinstance(a, str))
            name = next(
                (_text(adapter.get(k)) for k in ("profile", "ssid", "dns_suffix", "name") if _text(adapter.get(k))), ""
            )
            networks.append(DeviceNetwork(name=name, kind=_text(adapter.get("kind")), addresses=addresses))

        users = tuple(
            DeviceUser(name=_text(u.get("name")), active=u.get("state") != "disconnected")
            for u in data.get("users") or []
            if isinstance(u, dict) and _text(u.get("name"))
        )
        disks: list[DeviceDisk] = []
        for d in data.get("disks") or []:
            if not isinstance(d, dict) or not _text(d.get("drive")):
                continue
            total, free = _num(d.get("total_gb")), _num(d.get("free_gb"))
            if total is not None and free is not None:
                disks.append(DeviceDisk(drive=_text(d["drive"]), total_gb=total, free_gb=free))

        return cls(
            reported_at=_parse_time(data.get("reported_at")),
            host_name=_text(host.get("name")),
            domain=_text(host.get("domain")),
            public_ip=_text(data.get("public_ip")),
            address=_text(data.get("address")),
            network=_text(data.get("network_name")),
            networks=tuple(networks),
            users=users,
            os_name=_text(os_.get("name")),
            os_version=_text(os_.get("version")),
            os_build=_text(os_.get("build")),
            boot=_parse_time(data.get("boot")),
            time_zone=_text(data.get("time_zone")),
            memory_total_mb=_num(memory.get("total_mb")),
            memory_free_mb=_num(memory.get("free_mb")),
            disks=tuple(disks),
            agent_version=_text(agent.get("version")),
        )


@dataclass(frozen=True)
class DnsData:
    """Ergebnis der letzten Auflösung eines DNS-Checks (`dns` im Check, ab OctoPulse 0.4.0)."""

    found: tuple[str, ...]
    ttl_s: int | None

    @classmethod
    def from_json(cls, data: Any) -> "DnsData | None":
        """Liest das `dns`-Objekt. Bei Unbrauchbarem kommt None, nie eine Ausnahme."""
        if not isinstance(data, dict):
            return None
        found = tuple(a.strip() for a in data.get("found") or [] if isinstance(a, str) and a.strip())
        if not found:
            return None
        return cls(found=found, ttl_s=_num(data.get("ttl_s")))


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
    device: DeviceData | None = None  # nur bei Geräten (kind "device"), die sich schon gemeldet haben
    dns: DnsData | None = None  # nur bei DNS-Checks nach der ersten Auflösung
    expected_ips: tuple[str, ...] = ()  # nur bei DNS-Checks: die erwarteten Adressen
    dns_match: str = ""  # contains oder exact
    dns_on_change: str = ""  # alert oder adopt

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Check":
        probe = data.get("probe") if isinstance(data.get("probe"), dict) else {}
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
            device=DeviceData.from_json(data.get("device")),
            dns=DnsData.from_json(data.get("dns")),
            expected_ips=tuple(
                a for a in (probe.get("expected_ips") or []) if isinstance(a, str)
            ),
            dns_match=str(probe.get("dns_match") or ""),
            dns_on_change=str(probe.get("dns_on_change") or ""),
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
