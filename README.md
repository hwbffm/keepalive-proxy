# Keepalive-Proxy für Meshtastic-WLAN-Nodes

**Version 1.8.1 / Oktober 2026** – Public Domain ([Unlicense](LICENSE))

Kleines Windows-Tray-Programm gegen die **15-Minuten-Trennung**: Ein Meshtastic-Node trennt
einen TCP-Client, von dem er 15 Minuten lang nichts gehört hat. Clients, die im Leerlauf
nichts senden (z. B. [Meshhessen Client](https://github.com/SMLunchen/mh_windowsclient)),
werden deshalb alle 15 Minuten vom Node getrennt. Der Proxy sitzt zwischen Client und Node und
schickt dem Node bei Stille einen Heartbeat – so, wie es die offiziellen Meshtastic-Apps tun.
Getestet mit Meshhessen Client 1.6.4.6 und 1.7.0.

```
Client  ->  127.0.0.1:4405 (Proxy)  ->  Node 192.168.x.x:4403
```

## Warum alle 15 Minuten?

Die Meshtastic-Firmware schließt eine TCP-Verbindung (WLAN/Ethernet, Port 4403), wenn vom Client
**15 Minuten lang kein einziges Byte** kommt (beobachtet mit Firmware 2.7.26 auf einem ESP32-Node).
Das ist eine sinnvolle Schutzfunktion: Der Node nimmt nur **einen** TCP-Client gleichzeitig an, und
eine tote Verbindung – PC im Standby, WLAN weg, Programm abgestürzt – würde diesen Platz sonst
dauerhaft blockieren.

Entscheidend ist nur, was der Client **an den Node** schickt. Empfangene Nachrichten zählen nicht:
Auch wenn auf den Kanälen viel los ist, gilt ein Client, der nur zuhört, für den Node als still.
Die offiziellen Meshtastic-Apps senden deshalb regelmäßig einen kleinen Heartbeat
(`ToRadio.heartbeat`). Ein Client ohne Heartbeat wird nach 15 Minuten Leerlauf getrennt.

Folgen bei jeder Trennung:

- Der Client verbindet sich nach einigen Sekunden neu und lädt Konfiguration und Knotenliste erneut.
- Sein Virtual Node startet neu; daran hängende Programme (Handy-App, Autoresponder) verlieren
  kurz die Verbindung.
- Im Client-Log (`meshhessen-client.log`) stehen „Connection closed by remote host“ /
  „Reader thread stopped unexpectedly“ – im Abstand von **genau 15 Minuten**.

Der Proxy ist ein Behelf für genau diesen Fall. Sendet ein Client künftig selbst Heartbeats, wird
er nicht mehr gebraucht.

## Funktionen

- Reicht alle Daten unverändert in beide Richtungen weiter; Heartbeat nur zwischen zwei Nachrichten
- Heartbeat-Abstand einstellbar (0,5–14 min Stille)
- Tray-Icon **K**: wechselt bei jedem Ping des Nodes die Farbe (grün/blau), rot = Node antwortet nicht
- Verschwindet das Icon (z. B. nach einem Absturz des Explorers), legt der Proxy es nach
  spätestens 20 s neu an
- Klick aufs Icon: die letzten 10 Pings mit Uhrzeit, Antwortzeit und Heartbeat-Status
- Setup im Tray-Menü: IP und Port des Nodes, Ping- und Heartbeat-Abstand
- **Message an Client** (zuschaltbar): simulierte DM vom Absender „Ping Proxy“ bei jedem Ping
  oder Heartbeat – zeigt im Client, dass Proxy und Node laufen; geht nicht ins Mesh
- DMs an „Ping Proxy“ fängt der Proxy ab (nicht ins Mesh), bestätigt sie und antwortet mit
  „Message not transmitted to Mesh - responded by keepalive tool“
- Logdatei mit Größenbegrenzung (höchstens 3 × 500 kB)

## Schnellstart

1. `release/KeepaliveProxy.exe` herunterladen und in einen eigenen Ordner legen
   (z. B. `C:\mesh`). Prüfsumme: `release/KeepaliveProxy.exe.sha256`.
2. Starten, Rechtsklick aufs Tray-Icon **K** → **Setup …** → IP-Adresse des Nodes eintragen.
3. Im Client: Trennen, Host **127.0.0.1**, Port **4405**, Verbinden.
4. Autostart: Verknüpfung auf `KeepaliveProxy.exe` in `shell:startup` legen.

Ausführlich: [Anleitung.txt](Anleitung.txt)

## Aus dem Quellcode

Windows 10/11, Python 3.10 oder neuer im PATH.

- `start_keepalive_proxy.bat` – richtet beim ersten Mal eine `.venv` ein (pystray, pillow)
  und startet den Proxy ohne Konsolenfenster
- `build_exe.bat` – baut `dist\KeepaliveProxy.exe` mit PyInstaller

Optionen: `--node IP:PORT`, `--listen 127.0.0.1:4405`, `--interval SEKUNDEN`, `--ping MINUTEN`,
`--notray`, `--debug`, `--log DATEI`

## Passt dazu

[Autoresponder für Meshtastic](https://github.com/hwbffm/autoresponder) – antwortet über den
Virtual Node des Clients auf Kanal- und Direktnachrichten.

## Danke

Beide Programme bauen auf dem **[Meshhessen Client](https://github.com/SMLunchen/mh_windowsclient)**
von SMLunchen auf ([Handbuch](https://meshhessenclient.de/handbuch/)). Ein großes Dankeschön an
SMLunchen und alle Mitwirkenden für die tolle Arbeit! Der Proxy ist nur eine kleine Ergänzung für den WLAN-Betrieb. Der Client wird mit jedem Release
besser, zuletzt 1.7.0 mit Funk-Echo, Telemetrie-Dashboard und 3D-Gelände auf der Karte. Danke auch
an die **[Mesh-Hessen-Community](https://www.meshhessen.de)** für Netz, MQTT-Server und Dokumentation.

## Lizenz

Gemeinfrei (Public Domain) nach der [Unlicense](LICENSE). Jeder darf das Programm ohne
Einschränkung nutzen, kopieren, verändern und weitergeben.
