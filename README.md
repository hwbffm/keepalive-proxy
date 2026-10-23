# Keepalive-Proxy für Meshtastic-WLAN-Nodes

**Version 1.8.0 / Oktober 2026** – Public Domain ([Unlicense](LICENSE))

Kleines Windows-Tray-Programm gegen die **15-Minuten-Trennung**: Ein Meshtastic-Node trennt
einen TCP-Client, von dem er 15 Minuten lang nichts gehört hat. Clients, die im Leerlauf
nichts senden (z. B. [Mesh Hessen Client](https://www.meshhessen.de) 1.6.4.x), fliegen deshalb
alle 15 Minuten raus. Der Proxy sitzt zwischen Client und Node und schickt dem Node bei
Stille einen Heartbeat – so, wie es die offiziellen Meshtastic-Apps tun.

```
Client  ->  127.0.0.1:4405 (Proxy)  ->  Node 192.168.x.x:4403
```

## Funktionen

- Reicht alle Daten unverändert in beide Richtungen weiter; Heartbeat nur zwischen zwei Nachrichten
- Heartbeat-Abstand einstellbar (0,5–14 min Stille)
- Tray-Icon **K**: wechselt bei jedem Ping des Nodes die Farbe (grün/blau), rot = Node antwortet nicht
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

## Lizenz

Gemeinfrei (Public Domain) nach der [Unlicense](LICENSE). Jeder darf das Programm ohne
Einschränkung nutzen, kopieren, verändern und weitergeben.
