# OctoPulse für Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home-Assistant-Integration für [OctoPulse](https://git.manns-welt.eu/Ceddy/Pulse), den selbst gehosteten Healthcheck-Monitor. Sie arbeitet in beide Richtungen:

- **OctoPulse → Home Assistant:** Alle Checks erscheinen als Geräte mit Status, letztem Lebenszeichen und einem Schalter zum Pausieren; Windows-PCs zusätzlich mit Rechnername, IP, Netzwerk, Benutzer und Speicher. Damit baust du Automationen („Spielserver ausgefallen → Nachricht aufs Handy“) und Dashboards.
- **Home Assistant → OctoPulse:** Die Integration schickt regelmäßig ein Lebenszeichen. Stürzt Home Assistant ab oder fällt das Netz aus, schlägt OctoPulse Alarm. Das ersetzt `rest_command` und die Automation aus der OctoPulse-Anleitung.

## Voraussetzungen

- Home Assistant 2024.12 oder neuer
- Ein laufender OctoPulse-Server, den Home Assistant erreicht (HTTPS empfohlen)
- Ein API-Key aus OctoPulse unter **API**

## Installation über HACS

1. HACS → Integrationen → Drei-Punkte-Menü → **Benutzerdefiniertes Repository**
2. Adresse dieses Repositorys eintragen, Kategorie **Integration**
3. **OctoPulse** installieren und Home Assistant neu starten
4. Einstellungen → Geräte & Dienste → **Integration hinzufügen** → **OctoPulse**

Von Hand geht es auch: den Ordner `custom_components/octopulse` nach `<config>/custom_components/` kopieren und neu starten.

## Einrichtung

1. **Server:** Adresse (zum Beispiel `https://octopulse.example.com`) und API-Key eintragen. „Zertifikat prüfen“ schaltest du nur ab, wenn der Server ein selbst ausgestelltes Zertifikat benutzt.
2. **Heartbeat:** Du hast drei Möglichkeiten.
   - **Neuen Heartbeat-Check anlegen (empfohlen):** Die Integration legt den Check in OctoPulse selbst an und merkt sich die Ping-Adresse. Du kopierst nichts. Voreingestellt sind „alle 5 Minuten“ und 5 Minuten Karenzzeit.
   - **Vorhandene Ping-Adresse benutzen:** Wenn du den Check schon hast. Die Adresse sieht aus wie `https://…/ping/<token>`. Zur Prüfung geht gleich ein Lebenszeichen raus.
   - **Ohne Heartbeat:** Dann nutzt du nur die Anzeige der Checks.

Der API-Key hat die Rechte seines Benutzers. Ein Admin-Key zeigt alle Checks aller Benutzer, der Key eines normalen Benutzers nur dessen eigene. Für Home Assistant genügt der Key eines normalen Benutzers.

## Was angelegt wird

Der **Server** erscheint als Gerät *OctoPulse (host)*, jeder **Check** als eigenes Gerät darunter. Neue Checks bekommen automatisch Entitäten, gelöschte verlieren ihr Gerät, umbenannte werden umbenannt.

**Je Check:**

| Entität | Bedeutung |
|---|---|
| `binary_sensor.<check>` (Konnektivität) | **An** = online. **Aus** = ausgefallen oder überfällig, bei Geräten auch offline. **Unbekannt** = pausiert, wartend oder in Wartung |
| `sensor.<check>_status` | `up`, `late`, `down`, `new`, `paused`, `maintenance`, `offline` (nur Geräte) (angezeigt als Online, Überfällig, Ausgefallen, Wartet, Pausiert, Wartung, Offline) |
| `sensor.<check>_letztes_lebenszeichen` | Zeitpunkt des letzten Pings oder der letzten erfolgreichen Prüfung |
| `switch.<check>_uberwachung` | **Aus** pausiert den Check in OctoPulse (keine Alarme), **An** setzt ihn fort |

Die Entitäts-IDs entstehen beim Anlegen aus den Namen in der Sprache deiner Oberfläche (aus „Überwachung“ wird `…_uberwachung`, bei englischer Oberfläche `…_monitoring`). Die Beispiele hier gehen von deutscher Oberfläche aus. Prüfe die IDs unter Einstellungen → Geräte & Dienste → Entitäten.

Warum „unbekannt“ bei pausiert und in Wartung? In beiden Fällen sagt OctoPulse bewusst nichts über den Dienst. Eine Automation soll daraus keinen Alarm machen.

Der Binärsensor trägt außerdem Attribute: `status`, `kind`, `interval_s`, `grace_s`, bei Bedarf `schedule`, `target`, `detail` (letzte Fehlermeldung einer aktiven Prüfung) und `status_since`.

**Zusätzlich bei Geräte-Checks** (Windows-PC mit dem [OctoPulse-Client](https://git.manns-welt.eu/Ceddy/Pulse), Check-Art *Gerät*). Sie zeigen den letzten Stand, den der Client gemeldet hat. Ab OctoPulse 0.3.3 (die API liefert dort `device`); bei einem älteren Server bleiben diese Sensoren leer.

| Entität | Bedeutung |
|---|---|
| `sensor.<pc>_rechnername` | Name des Rechners, Attribut `domain` (Domäne oder Arbeitsgruppe) |
| `sensor.<pc>_ip_adresse` | Hauptadresse (IPv4 einer echten Verbindung, nicht Hyper-V/WSL/VPN), Attribut `addresses` mit allen |
| `sensor.<pc>_offentliche_ip` | So sieht OctoPulse das Gerät im Internet |
| `sensor.<pc>_netzwerk` | Name des Netzwerks (bei WLAN der WLAN-Name), Attribut `connections` mit allen Verbindungen |
| `sensor.<pc>_angemeldet` | Angemeldete Benutzer, „Niemand“ ohne Anmeldung; Attribute `users` und `disconnected` |
| `sensor.<pc>_betriebssystem` | zum Beispiel „Windows 11 Pro 24H2“, Attribut `build` |
| `sensor.<pc>_gestartet` | Zeitpunkt des letzten Starts von Windows (Diagnose) |
| `sensor.<pc>_arbeitsspeicher_belegt` | Prozent; Attribute `total_mb`, `free_mb` |
| `sensor.<pc>_laufwerk_c_frei` (je festem Laufwerk) | Freier Platz in GB; Attribute `total_gb`, `free_percent`. Neue Laufwerke kommen von selbst dazu |
| `sensor.<pc>_client_version` | Version des Clients (Diagnose), Attribut `time_zone` |

Geräte lösen in OctoPulse **keine Alarme** aus. Ist der PC aus, steht der Status auf `offline`, der Binärsensor ist **aus**, aber „Probleme“ und „Checks mit Problem“ bleiben unberührt. Nach dem Abmelden bleibt der letzte Stand in den Sensoren stehen (der Zeitpunkt steht im Sensor *Letztes Lebenszeichen*). Hat sich ein Gerät noch nie gemeldet, sind die Werte *unbekannt*.

Beispiel: Nachricht, wenn ein PC angeht und Max sich anmeldet:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.pc_max_angemeldet
    not_to: [unknown, unavailable, Niemand]
actions:
  - action: notify.mobile_app_handy
    data:
      message: "{{ trigger.to_state.state }} ist an {{ states('sensor.pc_max_rechnername') }} angemeldet"
```

**Zusätzlich bei DNS-Checks** (OctoPulse prüft, ob ein Name noch auf die erwartete IP-Adresse zeigt, zum Beispiel bei Dyn-DNS). Ab OctoPulse 0.4.0:

| Entität | Bedeutung |
|---|---|
| `sensor.<check>_aufgeloste_ip` | Die IP-Adresse(n), auf die der Name bei der letzten Auflösung zeigte (bei mehreren mit Komma getrennt). Attribute: `addresses` (Liste), `ttl` (Sekunden), `expected` (erwartete Adressen), `match` (`contains` oder `exact`), `on_change` (`alert` oder `adopt`). Vor der ersten Auflösung *unbekannt* |

Der Name steht im Attribut `target` des Binärsensors. Der Binärsensor ist **aus**, wenn die gefundene Adresse nicht der erwarteten entspricht (Alarmzustand) und „Probleme“ an. Im Modus *Neue Adresse übernehmen* gilt eine geänderte Adresse nicht als Ausfall: Der Sensor „Aufgelöste IP“ zeigt dann einfach die neue Adresse, und eine Automation kann auf die Änderung reagieren:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.heimnetz_dns_aufgeloste_ip
    not_from: [unknown, unavailable]
    not_to: [unknown, unavailable]
actions:
  - action: notify.mobile_app_handy
    data:
      message: "Die IP von {{ state_attr('binary_sensor.heimnetz_dns', 'target') }} ist jetzt {{ trigger.to_state.state }}"
```

**Für den Server:**

| Entität | Bedeutung |
|---|---|
| `binary_sensor.octopulse_…_probleme` | An, sobald mindestens ein Check ausgefallen oder überfällig ist. Das Attribut `checks` nennt sie |
| `sensor.…_checks_online`, `…_checks_mit_problem`, `…_checks` | Zähler (Pausierte und Checks in Wartung zählen nicht als Problem) |
| `sensor.…_letzter_heartbeat` | Wann Home Assistant zuletzt erfolgreich ein Lebenszeichen gesendet hat. Bei Fehlern steht der Grund im Attribut `last_error` |

## Heartbeat

Der erste Ping geht raus, sobald Home Assistant gestartet ist, danach einer pro Intervall. Bei einem Verbindungsfehler gibt es genau eine Wiederholung nach 5 Sekunden, der nächste Takt folgt ohnehin. Ein Ausfall landet nur beim Übergang im Log (nicht bei jedem Takt), die Ping-Adresse steht nie im Log. Sie enthält ein Geheimnis und wird wie ein Passwort behandelt.

Der Heartbeat hängt **nicht** am API-Key. Ist der Key abgelaufen, läuft der Heartbeat weiter. Die Checks werden nicht mehr angezeigt, und Home Assistant bittet dich um einen neuen Key. Sonst würde OctoPulse Home Assistant fälschlich für ausgefallen halten.

> **Wichtig:** Das Intervall des Checks in OctoPulse muss größer sein als der Takt hier. Mit „alle 5 Minuten“ plus 5 Minuten Karenzzeit überlebt ein einzelner verpasster Ping.

### Dienst `octopulse.send_heartbeat`

Schickt sofort ein Signal an die Ping-Adresse. Damit meldest du Abläufe, zum Beispiel ein Backup:

```yaml
actions:
  - action: octopulse.send_heartbeat
    data:
      signal: start
  - action: hassio.backup_full
  - action: octopulse.send_heartbeat
    data:
      signal: success
      message: "Backup fertig"
```

| Feld | Bedeutung |
|---|---|
| `signal` | `success` (Standard, normales Lebenszeichen), `start` (Lauf beginnt) oder `fail` (Lauf gescheitert, löst sofort Alarm aus) |
| `message` | Optionaler Text (höchstens 4 KB), den OctoPulse im Ereignisprotokoll des Checks ablegt |
| `config_entry` | Nur nötig, wenn mehrere OctoPulse-Server eingerichtet sind. Ohne Angabe bekommen alle Server mit Heartbeat das Signal |

## Beispiele

**Nachricht, wenn ein Check ausfällt:**

```yaml
alias: OctoPulse Ausfall melden
triggers:
  - trigger: state
    entity_id: binary_sensor.spielserver
    to: "off"
    for: "00:01:00"
actions:
  - action: notify.mobile_app_handy
    data:
      title: "Ausfall"
      message: "{{ trigger.to_state.name }}: {{ state_attr(trigger.entity_id, 'detail') or 'keine Details' }}"
```

**Check bei Wartungsarbeiten pausieren:**

```yaml
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.spielserver_uberwachung
  # … Server neu starten …
  - action: switch.turn_on
    target:
      entity_id: switch.spielserver_uberwachung
```

**Sammelwarnung auf dem Dashboard:** Der Binärsensor `…_probleme` ist an, sobald irgendein Check ein Problem hat. Das Attribut `checks` listet die Namen.

## Einstellungen

Einstellungen → Geräte & Dienste → OctoPulse → **Konfigurieren**:

- **Abfrageintervall und Takt des Heartbeats:** Die Checks werden standardmäßig alle 60 Sekunden abgefragt (30 bis 3600 Sekunden). OctoPulse begrenzt Anfragen pro Adresse. Zu kurze Intervalle bringen wenig, die Checks selbst laufen dort ohnehin in ihrem eigenen Takt.
- **Neuen Heartbeat-Check anlegen** oder **andere Ping-Adresse benutzen**
- **Heartbeat abschalten:** Home Assistant sendet nichts mehr. Der Check in OctoPulse bleibt bestehen und wird dort bald überfällig. Lösche oder pausiere ihn dort.

Über das Drei-Punkte-Menü des Eintrags gibt es außerdem **Neu konfigurieren** (Adresse oder Key ändern). Wird der Key abgelehnt, fragt Home Assistant selbst nach einem neuen.

## Fehlersuche

| Problem | Ursache und Lösung |
|---|---|
| „Keine Verbindung zum Server“ | Erreicht Home Assistant die Adresse? Vom Home-Assistant-Rechner aus mit `curl -I https://…` testen. Bei einem selbst ausgestellten Zertifikat „Zertifikat prüfen“ abschalten |
| „Der Server antwortet, aber nicht wie OctoPulse“ | Falsche Adresse, oder ein Proxy liefert eine andere Seite. Die Adresse ist die Basis ohne Pfad |
| „Der API-Key wurde abgelehnt“ | Key in OctoPulse unter *API* prüfen. Der Benutzer darf nicht gesperrt sein |
| Es erscheinen weniger Checks als erwartet | Der Key gehört einem normalen Benutzer und sieht nur dessen eigene Checks. Admin-Key benutzen oder die Checks dem Benutzer übertragen |
| OctoPulse meldet Home Assistant als überfällig | Sensor „Letzter Heartbeat“ ansehen: Steht dort ein Fehler (`last_error`)? Ist das Intervall des Checks in OctoPulse kürzer als der Takt hier? |
| „OctoPulse kennt diese Ping-Adresse nicht“ | Der Token wurde in OctoPulse erneuert oder der Check gelöscht. Einen neuen Heartbeat anlegen (Konfigurieren) |

Für Fehlerberichte: Einstellungen → Geräte & Dienste → OctoPulse → Drei-Punkte-Menü → **Diagnose herunterladen**. Adresse, API-Key und Ping-Adresse sind darin geschwärzt.

## Entwicklung und Tests

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements_test.txt   # unter Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest
```

Home Assistant ist für Linux gebaut. Unter Windows fehlen den Test-Paketen die Module `fcntl` und `resource`. Lege in der virtuellen Umgebung (`.venv/Lib/site-packages/`) zwei leere Platzhalter-Dateien mit diesen Namen an (`fcntl.py` mit `LOCK_EX = 2`, `LOCK_NB = 4` und einer Funktion `flock(fd, op)`, `resource.py` mit `RLIMIT_NOFILE = 7`, `getrlimit` und `setrlimit`). Die Tests umgehen selbst die Socket-Sperre, die unter Windows die Ereignisschleife stört (`tests/conftest.py`). Mit WSL oder Docker brauchst du das alles nicht.

Die Tests laufen gegen einen simulierten OctoPulse-Server (kein echtes Netz) und prüfen Geräte-Sensoren, Einrichtung, Neuanmeldung, Optionen, Entitäten, Schalter, Heartbeat (Takt, Wiederholung, Fehler, Geheimhaltung des Tokens) und den Dienst.
