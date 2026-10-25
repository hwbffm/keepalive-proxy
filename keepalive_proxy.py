# Keepalive-Proxy für Meshtastic-WLAN-Nodes (TCP, Standard-Port 4403)
# SPDX-License-Identifier: Unlicense
#
# Gemeinfrei (Public Domain). Jeder darf dieses Programm ohne Einschränkung nutzen,
# kopieren, verändern, verkaufen und weitergeben – mit oder ohne Namensnennung.
#
# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <https://unlicense.org/>
"""Hält die TCP-Verbindung eines Clients zum Node am Leben.

Hintergrund: Ein Meshtastic-Node trennt einen TCP-Client, von dem er 15 Minuten lang nichts
gehört hat. Clients, die im Leerlauf nichts senden (z. B. Mesh Hessen Client 1.6.4.x), werden
deshalb alle 15 Minuten getrennt und verbinden sich neu. Dieser Proxy sitzt dazwischen:

    Client  ->  127.0.0.1:4405 (Proxy)  ->  Node 192.168.10.41:4403

Er reicht alle Bytes unverändert in beide Richtungen weiter und sendet dem Node zusätzlich
einen Heartbeat (ToRadio.heartbeat), sobald der Client eine Zeit lang nichts gesendet hat.
Der Heartbeat wird nur zwischen zwei Rahmen eingefügt, nie mitten in eine Nachricht.

Im Client dann als Host 127.0.0.1 und als Port 4405 eintragen. Zurück zum direkten Betrieb:
wieder Host = IP des Nodes, Port 4403.

Im Tray zeigt ein Symbol den Zustand: Bei jedem Ping des Nodes (Standard jede Minute)
wechselt es die Farbe zwischen Grün und Blau, bei einem verlorenen Ping wird es rot.
Ein Klick auf das Symbol zeigt rechts unten die letzten 10 Pings (schließt nach 10 s).
Rechtsklick -> Setup: IP-Adresse und Port des Nodes, Ping- und Heartbeat-Abstand,
gespeichert in keepalive_proxy.json.

Start:  pythonw keepalive_proxy.py [--listen 127.0.0.1:4405] [--node 192.168.10.41:4403] [--interval 60]
        [--ping 1]   (--interval: Sekunden Stille bis zum Heartbeat, --ping: Minuten zwischen Pings;
                      ohne Angabe gelten die Werte aus dem Setup bzw. keepalive_proxy.json)
"""
import argparse
import collections
import json
import logging
import logging.handlers
import re
import random
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "1.8.1"
TRAY_CHECK_S = 20                                              # so oft prüfen, ob das Tray-Symbol noch da ist
START1, START2 = 0x94, 0xC3
HEARTBEAT = bytes([START1, START2, 0x00, 0x02, 0x3A, 0x00])   # Rahmen mit ToRadio{heartbeat}
MAX_FRAME = 512                                                # größte gültige Nutzlast
PROXY_NUM = 0x50696E67                                         # simulierter Absender "!50696e67" ("Ping")
PROXY_NAME, PROXY_SHORT = "Ping Proxy", "PING"
REPLY_TEXT = "Message not transmitted to Mesh - responded by keepalive tool"   # Antwort auf DMs an "Ping Proxy"
TEXT_APP, NODEINFO_APP, ROUTING_APP = 1, 4, 5                  # Meshtastic-Portnummern

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
log = logging.getLogger("keepalive_proxy")


class FrameTracker:
    """Verfolgt den Strom Client -> Node: steht er gerade zwischen zwei Rahmen?

    Rahmen: 0x94 0xC3 <Länge hoch> <Länge tief> <Nutzlast>. Davor sendet der Client
    "Weck-Bytes" (mehrfach 0x94), die zwischen den Rahmen liegen.
    """

    def __init__(self):
        self.state = 0          # 0 zwischen Rahmen, 1 nach 0x94, 2/3 Länge, 4 Nutzlast
        self.hi = 0
        self.remaining = 0

    @property
    def at_boundary(self):
        return self.state == 0

    def feed(self, data):
        for b in data:
            if self.state == 0:
                if b == START1:
                    self.state = 1
            elif self.state == 1:
                if b == START2:
                    self.state = 2
                elif b != START1:           # weitere 0x94 sind Weck-Bytes
                    self.state = 0
            elif self.state == 2:
                self.hi = b
                self.state = 3
            elif self.state == 3:
                self.remaining = (self.hi << 8) | b
                self.state = 4 if 0 < self.remaining <= MAX_FRAME else 0
            elif self.state == 4:
                self.remaining -= 1
                if self.remaining == 0:
                    self.state = 0


class FrameReader:
    """Zerlegt den Strom Node -> Client in vollständige Rahmen (nur zum Mitlesen)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(bytes([START1, START2]))
            if i < 0:
                del self.buf[:-1 if self.buf[-1:] == bytes([START1]) else len(self.buf)]
                return out
            del self.buf[:i]
            if len(self.buf) < 4:
                return out
            n = (self.buf[2] << 8) | self.buf[3]
            if not 0 < n <= MAX_FRAME:
                del self.buf[:1]
                continue
            if len(self.buf) < 4 + n:
                return out
            out.append(bytes(self.buf[4:4 + n]))
            del self.buf[:4 + n]


class UpFilter:
    """Strom Client -> Node: hält Pakete an "Ping Proxy" zurück, alles andere geht unverändert weiter."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        """Liefert (weiterzuleitende Bytes, Liste abgefangener Pakete an "Ping Proxy")."""
        self.buf += data
        out, caught = bytearray(), []
        while True:
            i = self.buf.find(bytes([START1, START2]))
            if i < 0:                                   # Weck-Bytes; ein 0x94 am Ende kann Rahmenanfang sein
                keep = 1 if self.buf[-1:] == bytes([START1]) else 0
                out += self.buf[:len(self.buf) - keep]
                del self.buf[:len(self.buf) - keep]
                break
            out += self.buf[:i]
            del self.buf[:i]
            if len(self.buf) < 4:
                break
            n = (self.buf[2] << 8) | self.buf[3]
            if not 0 < n <= MAX_FRAME:
                out += self.buf[:1]
                del self.buf[:1]
                continue
            if len(self.buf) < 4 + n:                   # Rest des Rahmens kommt mit dem nächsten recv
                break
            fr = bytes(self.buf[:4 + n])
            del self.buf[:4 + n]
            pkt = to_proxy_packet(fr[4:])
            if pkt is None:
                out += fr
            else:
                caught.append(pkt)
        return bytes(out), caught


# --- kleine Protobuf-Helfer für FromRadio (ohne meshtastic-Bibliothek) ------------------
def pb_varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_fields(b):
    """Liefert (Feldnummer, Wire-Typ, Wert) eines Protobuf-Puffers."""
    i = 0
    while i < len(b):
        key = shift = 0
        while True:
            key |= (b[i] & 0x7F) << shift
            shift += 7
            i += 1
            if not b[i - 1] & 0x80:
                break
        num, wt = key >> 3, key & 7
        if wt == 0:
            val = shift = 0
            while True:
                val |= (b[i] & 0x7F) << shift
                shift += 7
                i += 1
                if not b[i - 1] & 0x80:
                    break
        elif wt == 2:
            n = shift = 0
            while True:
                n |= (b[i] & 0x7F) << shift
                shift += 7
                i += 1
                if not b[i - 1] & 0x80:
                    break
            val, i = b[i:i + n], i + n
        elif wt == 5:
            val, i = b[i:i + 4], i + 4
        elif wt == 1:
            val, i = b[i:i + 8], i + 8
        else:
            return
        yield num, wt, val


def my_node_num(from_radio):
    """Nummer des eigenen Nodes aus FromRadio.my_info (Feld 3), sonst None."""
    try:
        for num, wt, val in pb_fields(from_radio):
            if num == 3 and wt == 2:
                for n2, w2, v2 in pb_fields(val):
                    if n2 == 1 and w2 == 0:
                        return v2
    except IndexError:
        pass
    return None


def to_proxy_packet(to_radio):
    """ToRadio{packet} an "Ping Proxy" -> dict (id, want_ack, portnum, payload), sonst None."""
    try:
        for num, wt, val in pb_fields(to_radio):
            if num != 1 or wt != 2:
                continue
            pkt = {"to": None, "id": 0, "want_ack": False, "portnum": 0, "payload": b""}
            for n2, w2, v2 in pb_fields(val):
                if n2 == 2 and w2 == 5:
                    pkt["to"] = struct.unpack("<I", v2)[0]
                elif n2 == 6 and w2 == 5:
                    pkt["id"] = struct.unpack("<I", v2)[0]
                elif n2 == 10 and w2 == 0:
                    pkt["want_ack"] = bool(v2)
                elif n2 == 4 and w2 == 2:
                    for n3, w3, v3 in pb_fields(v2):
                        if n3 == 1 and w3 == 0:
                            pkt["portnum"] = v3
                        elif n3 == 2 and w3 == 2:
                            pkt["payload"] = bytes(v3)
            return pkt if pkt["to"] == PROXY_NUM else None
    except (IndexError, struct.error):
        pass
    return None


def queue_status(from_radio):
    """(frei, max) aus FromRadio.queueStatus (Feld 11), sonst None."""
    try:
        for num, wt, val in pb_fields(from_radio):
            if num == 11 and wt == 2:
                q = {n2: v2 for n2, w2, v2 in pb_fields(val) if w2 == 0}
                return q.get(2, 0), q.get(3, 0)
    except IndexError:
        pass
    return None


def frame(body):
    return bytes([START1, START2, len(body) >> 8, len(body) & 0xFF]) + body


def pb_ld(field, data):
    return pb_varint(field << 3 | 2) + pb_varint(len(data)) + data


def pb_fixed32(field, value):
    return pb_varint(field << 3 | 5) + struct.pack("<I", value)


def proxy_packet(to, portnum, payload, request_id=None):
    """Rahmen mit FromRadio{packet: MeshPacket vom simulierten Absender "Ping Proxy"}."""
    data = pb_varint(1 << 3) + pb_varint(portnum) + pb_ld(2, payload)
    if request_id is not None:                  # Quittung zu einem Paket des Clients
        data += pb_fixed32(6, request_id)
    pkt = (pb_fixed32(1, PROXY_NUM) + pb_fixed32(2, to) + pb_ld(4, data)
           + pb_fixed32(6, random.getrandbits(32)) + pb_fixed32(7, int(time.time())))
    return frame(pb_ld(2, pkt))


def queue_status_frame(free, maxlen, packet_id):
    """FromRadio{queueStatus}: so meldet der Node, dass er ein Paket des Clients angenommen hat."""
    q = (pb_varint(2 << 3) + pb_varint(free) + pb_varint(3 << 3) + pb_varint(maxlen)
         + pb_varint(4 << 3) + pb_varint(packet_id))
    return frame(pb_ld(11, q))


def hostport(text, default_port):
    host, _, port = text.rpartition(":")
    return (host, int(port)) if host else (text, default_port)


class Session:
    """Eine Verbindung Client <-> Node."""

    def __init__(self, client, node_addr, interval):
        self.client = client
        self.node_addr = node_addr
        self.interval = interval
        self.node = None
        self.stop = threading.Event()
        self.lock = threading.Lock()          # schützt Schreiben zum Node und den Tracker
        self.tracker = FrameTracker()
        self.last_tx = time.time()
        self.heartbeats = 0
        self.bytes_up = self.bytes_down = 0
        self.reason = "?"
        self.started = time.time()
        self.debug = False
        self.down_lock = threading.Lock()     # schützt Schreiben zum Client und den Tracker
        self.down_tracker = FrameTracker()
        self.reader = FrameReader()
        self.my_num = None                    # eigener Node, aus my_info beim Verbindungsaufbau
        self.announced = False                # NodeInfo von "Ping Proxy" schon gesendet?
        self.up_filter = UpFilter()
        self.queue = (16, 16)                 # letzter queueStatus des Nodes (frei, max)

    def message_to_client(self, text, frames=(), reply=False):
        """Simulierte DM von "Ping Proxy" an den Client (vorweg ggf. weitere Rahmen);
        False, solange der eigene Node unbekannt ist."""
        if self.my_num is None or self.stop.is_set() or (time.time() - self.started < 30 and not reply):
            return False                       # erst nach dem Konfigurationsabgleich des Clients
        frames = list(frames)
        if text is not None:
            if not self.announced:             # damit der Client einen Namen statt "!50696e67" zeigt
                user = (pb_ld(1, f"!{PROXY_NUM:08x}".encode()) + pb_ld(2, PROXY_NAME.encode())
                        + pb_ld(3, PROXY_SHORT.encode()))
                frames.append(proxy_packet(0xFFFFFFFF, NODEINFO_APP, user))
                self.announced = True
            frames.append(proxy_packet(self.my_num, TEXT_APP, text.encode()[:200]))
        deadline = time.time() + 3
        while frames and time.time() < deadline:
            with self.down_lock:
                if self.down_tracker.at_boundary:       # nie mitten in einen Rahmen des Nodes
                    try:
                        self.client.sendall(frames[0])
                    except OSError:
                        return False
                    self.down_tracker.feed(frames.pop(0))
                    continue
            time.sleep(0.05)
        return not frames

    def answer(self, pkt):
        """Paket des Clients an "Ping Proxy": nicht ins Mesh, sondern selbst quittieren und antworten."""
        text = pkt["payload"].decode("utf-8", "replace") if pkt["portnum"] == TEXT_APP else None
        log.info("Paket an Ping Proxy abgefangen (Port %d)%s", pkt["portnum"],
                 f": {text[:80]!r}" if text is not None else "")
        frames = [queue_status_frame(*self.queue, pkt["id"])]        # wie der Node: Paket angenommen
        if pkt["want_ack"] and self.my_num is not None:              # Zustellbestätigung (Routing, kein Fehler)
            frames.append(proxy_packet(self.my_num, ROUTING_APP, pb_varint(3 << 3) + pb_varint(0), pkt["id"]))
        self.message_to_client(REPLY_TEXT if text is not None else None, frames, reply=True)

    def close(self, reason=None):
        if reason and self.reason == "?":
            self.reason = reason
        self.stop.set()
        for s in (self.client, self.node):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

    def run(self):
        try:
            self.node = socket.create_connection(self.node_addr, timeout=10)
            self.node.settimeout(None)
            for s in (self.client, self.node):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            log.warning("Node %s:%s nicht erreichbar: %s", *self.node_addr, e)
            self.close("Node nicht erreichbar")
            return
        log.info("Verbunden: Client -> Node %s:%s (Heartbeat nach %s s Stille)", *self.node_addr, self.interval)
        threads = [threading.Thread(target=self._up, daemon=True),
                   threading.Thread(target=self._down, daemon=True),
                   threading.Thread(target=self._beat, daemon=True)]
        for t in threads:
            t.start()
        self.stop.wait()
        self.close()
        for t in threads:
            t.join(2)
        log.info("Getrennt nach %.0f min (Grund: %s) | Heartbeats %d | Client->Node %d B, Node->Client %d B",
                 (time.time() - self.started) / 60, self.reason, self.heartbeats, self.bytes_up, self.bytes_down)

    def _up(self):                      # Client -> Node
        try:
            while not self.stop.is_set():
                data = self.client.recv(4096)
                if not data:
                    self.reason = "Client hat getrennt"
                    break
                data, caught = self.up_filter.feed(data)
                for pkt in caught:
                    threading.Thread(target=self.answer, args=(pkt,), daemon=True).start()
                if data:
                    with self.lock:
                        self.tracker.feed(data)
                        self.node.sendall(data)
                        self.last_tx = time.time()
                self.bytes_up += len(data)
                if self.debug:
                    log.info("Client->Node %d B: %s", len(data), data[:48].hex())
        except OSError:
            self.reason = "Client-Fehler" if self.reason == "?" else self.reason
        finally:
            self.stop.set()

    def _down(self):                    # Node -> Client
        try:
            while not self.stop.is_set():
                data = self.node.recv(4096)
                if not data:
                    self.reason = "Node hat getrennt"
                    break
                with self.down_lock:
                    self.down_tracker.feed(data)
                    self.client.sendall(data)
                self.bytes_down += len(data)
                for fr in self.reader.feed(data):
                    if self.my_num is None:
                        self.my_num = my_node_num(fr)
                    self.queue = queue_status(fr) or self.queue
                if self.debug:
                    log.info("Node->Client %d B", len(data))
        except OSError as e:
            self.reason = f"Node-Fehler ({e})" if self.reason == "?" else self.reason
        finally:
            self.stop.set()

    def _beat(self):                    # Heartbeat bei Stille
        while not self.stop.wait(1.0):
            if time.time() - self.last_tx < self.interval:
                continue
            with self.lock:
                if self.tracker.at_boundary:        # nie mitten in einen Rahmen
                    try:
                        self.node.sendall(HEARTBEAT)
                    except OSError:
                        self.reason = "Node-Fehler beim Heartbeat"
                        self.stop.set()
                        return
                    self.heartbeats += 1
                    self.last_tx = time.time()
                    if self.debug or self.heartbeats == 1 or self.heartbeats % max(1, int(900 // self.interval)) == 0:   # etwa alle 15 min loggen
                        log.info("Heartbeat Nr. %d gesendet (Verbindung steht seit %.0f min)",
                                 self.heartbeats, (time.time() - self.started) / 60)
                else:
                    continue
            if state["msg"] == "heartbeat":     # außerhalb des Locks, wartet ggf. auf Rahmengrenze
                self.message_to_client(status_text(self, "Heartbeat"))


CONFIG = HERE / "keepalive_proxy.json"
state = {"session": None, "pings": collections.deque(maxlen=10), "flip": False, "last": None,
         "node": ("192.168.10.41", 4403), "ping_min": 1.0, "interval": 60.0, "debug": False,
         "msg": "aus", "wake": threading.Event(),
         "listen": ("127.0.0.1", 4405)}   # msg: "aus", "ping" oder "heartbeat"
MSG_MODES = {"aus": "Aus", "ping": "Bei jedem Ping", "heartbeat": "Bei jedem Heartbeat"}


def status_text(s, anlass):
    """Text der DM von "Ping Proxy"; nutzt das letzte Ping-Ergebnis."""
    host = state["node"][0]
    if state["pings"]:
        _, ok, ms, _ = state["pings"][0]
        node = f"antwortet ({ms if ms is not None else '?'} ms)" if ok else "antwortet NICHT"
    else:
        node = "noch nicht gepingt"
    return f"{anlass} {time.strftime('%H:%M')} – Proxy läuft, Node {host} {node}, Heartbeats {s.heartbeats}"


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config():
    host, port = state["node"]
    CONFIG.write_text(json.dumps({"node": f"{host}:{port}", "ping_min": state["ping_min"],
                                  "heartbeat_min": state["interval"] / 60,
                                  "message_an_client": state["msg"]}, indent=2), encoding="utf-8")


def icmp_ping(host):
    """Ein Ping an den Node; Rückgabe (erreicht, Laufzeit in ms oder None)."""
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", host], capture_output=True, text=True,
                           errors="replace", timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    ok = r.returncode == 0 and "TTL=" in r.stdout.upper()
    m = re.search(r"[=<]\s*(\d+)\s*ms", r.stdout)
    return ok, (int(m.group(1)) if ok and m else None)


def pinger():
    """Pingt den Node alle ping_min Minuten und merkt sich die letzten 10 Ergebnisse."""
    beats_before = 0
    while True:
        host = state["node"][0]
        ok, ms = icmp_ping(host)
        s = state["session"]
        if s is None or s.stop.is_set():
            beat, beats_before = "Client nicht über den Proxy verbunden", 0
        else:
            beat = "Heartbeat gesendet" if s.heartbeats > beats_before else "Client aktiv, kein Heartbeat nötig"
            beats_before = s.heartbeats
        state["pings"].appendleft((time.time(), ok, ms, beat))
        state["flip"] = not state["flip"]
        state["last"] = ok
        if not ok:
            log.warning("Ping an %s ohne Antwort", host)
        if state["msg"] == "ping" and s is not None:
            s.message_to_client(status_text(s, "Ping"))
        state["wake"].wait(state["ping_min"] * 60)      # Setup weckt vorzeitig
        state["wake"].clear()


def serve(srv):
    try:
        while True:
            client, addr = srv.accept()
            if state["session"] is not None:              # wie der Node: ein neuer Client ersetzt den alten
                state["session"].close("durch neuen Client ersetzt")
            s = state["session"] = Session(client, state["node"], state["interval"])
            s.debug = state["debug"]
            threading.Thread(target=s.run, daemon=True).start()
    except Exception:
        log.exception("Proxy beendet")
        raise


def run_tray(log_path):
    """Tray-Symbol: Farbwechsel bei jedem Ping, Klick zeigt die letzten 10 Pings, Setup im Menü."""
    import ctypes
    import ctypes.wintypes
    import os
    import tkinter as tk
    import pystray
    from PIL import Image, ImageDraw, ImageFont

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)    # tk und Arbeitsbereich in denselben Pixeln
    except (AttributeError, OSError):
        pass

    def image(color):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 61, 61), radius=8, fill=color)
        try:
            font = ImageFont.truetype("arialbd.ttf", 48)
        except OSError:
            font = ImageFont.load_default()
        d.text((32, 33), "K", fill="white", font=font, anchor="mm")
        return img

    icons = {"wait": image((150, 150, 150)), "a": image((0, 160, 60)), "b": image((0, 110, 230)),
             "fail": image((210, 30, 30))}
    root = tk.Tk()
    root.withdraw()
    view = {"win": None, "text": None, "timer": None}
    setup = {"win": None}

    def bottom_right(w):
        """Fenster über die rechte untere Ecke des Arbeitsbereichs (ohne Taskleiste) legen."""
        w.update_idletasks()
        r = ctypes.wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
            right, bottom = r.right, r.bottom
        else:
            right, bottom = w.winfo_screenwidth(), w.winfo_screenheight() - 48
        w.geometry(f"+{right}+{bottom}")                  # erst platzieren, dann Rahmen messen
        w.update()
        frame = max(w.winfo_rooty() - w.winfo_y(), 30)    # Titelleiste
        gap = int(w.winfo_fpixels("3c"))                  # ca. 3 cm Abstand zum unteren Rand
        x = right - w.winfo_width() - 16
        y = bottom - w.winfo_height() - frame - gap
        w.geometry(f"+{x}+{y}")

    def lines():
        if not state["pings"]:
            return "Noch kein Ping."
        out = []
        for t, ok, ms, beat in state["pings"]:
            res = (f"OK   {ms:>4} ms" if ms is not None else "OK        ") if ok else "KEINE ANTWORT"
            out.append(f"{time.strftime('%H:%M:%S', time.localtime(t))}   {res:<14} {beat}")
        return "\n".join(out)

    def arm_timer(seconds=10):
        if view["timer"] is not None:
            root.after_cancel(view["timer"])
        view["timer"] = root.after(seconds * 1000, close)

    def show():
        if view["win"] is None:
            w = view["win"] = tk.Toplevel(root)
            w.title("Keepalive-Proxy – letzte 10 Pings")
            w.attributes("-topmost", True)
            txt = view["text"] = tk.Label(w, font=("Consolas", 10), justify="left", anchor="nw",
                                          padx=12, pady=10)
            txt.pack(fill="both", expand=True)
            tk.Button(w, text="Schließen", width=10, command=close).pack(pady=(0, 10))
            w.protocol("WM_DELETE_WINDOW", close)
            w.bind("<Escape>", lambda e: close())
            # Maus über dem Fenster hält es offen; danach wieder 10 s bis zum Schließen
            w.bind("<Enter>", lambda e: view["timer"] and root.after_cancel(view["timer"]))
            w.bind("<Leave>", lambda e: arm_timer())
        refresh_view()
        bottom_right(view["win"])
        view["win"].deiconify()
        view["win"].lift()
        arm_timer()

    def close():
        if view["timer"] is not None:
            root.after_cancel(view["timer"])
        if view["win"] is not None:
            view["win"].destroy()
        view["win"] = view["text"] = view["timer"] = None

    def refresh_view():
        if view["text"] is not None:
            host, port = state["node"]
            view["win"].title(f"Keepalive-Proxy – letzte 10 Pings an {host}")
            view["text"].config(text=lines())

    def show_setup():
        if setup["win"] is not None:
            setup["win"].lift()
            return
        w = setup["win"] = tk.Toplevel(root)
        w.title("Keepalive-Proxy – Setup")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        f = tk.Frame(w, padx=14, pady=12)
        f.pack()
        host, port = state["node"]
        ip = tk.StringVar(value=host)
        node_port = tk.StringVar(value=str(port))
        minutes = tk.StringVar(value=f"{state['ping_min']:g}")
        beat_min = tk.StringVar(value=f"{state['interval'] / 60:g}")
        rows = [("IP-Adresse des Nodes:", ip, 22), ("Port des Nodes:", node_port, 8),
                ("Ping alle (Minuten):", minutes, 8), ("Heartbeat nach (Minuten Stille):", beat_min, 8)]
        entries = []
        for r, (label, var, width) in enumerate(rows):
            pad = (0 if r == 0 else 8, 0)
            tk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=pad)
            entries.append(tk.Entry(f, textvariable=var, width=width))
            entries[-1].grid(row=r, column=1, sticky="w", padx=(8, 0), pady=pad)
        e_ip = entries[0]
        tk.Label(f, text="Der Node trennt nach 15 min Stille – Heartbeat daher höchstens alle 14 min.",
                 fg="gray", justify="left").grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        lh, lp = state["listen"]
        tk.Label(f, text=f"Im Client als Node eintragen:  {lh}  Port {lp}",
                 font=("Segoe UI", 9, "bold")).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        msg = tk.Label(f, text="", fg="red")
        msg.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def close_setup():
            w.destroy()
            setup["win"] = None

        def save():
            h = ip.get().strip()
            try:
                socket.inet_aton(h)
                if h.count(".") != 3:
                    raise ValueError
            except (OSError, ValueError):
                msg.config(text="Bitte eine gültige IP-Adresse eingeben, z. B. 192.168.10.41")
                return
            try:
                p = int(node_port.get())
                if not 0 < p < 65536:
                    raise ValueError
            except ValueError:
                msg.config(text="Port: ganze Zahl von 1 bis 65535 (meist 4403)")
                return
            try:
                m = float(minutes.get().replace(",", "."))
                if not 0.5 <= m <= 60:
                    raise ValueError
            except ValueError:
                msg.config(text="Ping-Abstand: 0,5 bis 60 Minuten")
                return
            try:
                hb = float(beat_min.get().replace(",", "."))
                if not 0.5 <= hb <= 14:
                    raise ValueError
            except ValueError:
                msg.config(text="Heartbeat: 0,5 bis 14 Minuten")
                return
            changed = (h, p) != state["node"]
            state["node"], state["ping_min"], state["interval"] = (h, p), m, hb * 60
            if state["session"] is not None:                   # gilt sofort, auch für die laufende Verbindung
                state["session"].interval = state["interval"]
            save_config()
            log.info("Setup gespeichert: Node %s:%s, Ping alle %g min, Heartbeat nach %g min", h, p, m, hb)
            if changed and state["session"] is not None:      # Client verbindet sich neu, dann zum neuen Node
                state["session"].close("Node-Adresse geändert")
            state["wake"].set()                                # sofort mit neuen Werten pingen
            close_setup()

        b = tk.Frame(f)
        b.grid(row=8, column=0, columnspan=2, pady=(10, 0))
        tk.Button(b, text="Speichern", width=10, command=save).pack(side="left", padx=4)
        tk.Button(b, text="Abbrechen", width=10, command=close_setup).pack(side="left", padx=4)
        w.protocol("WM_DELETE_WINDOW", close_setup)
        w.bind("<Return>", lambda e: save())
        w.bind("<Escape>", lambda e: close_setup())
        bottom_right(w)
        e_ip.focus_set()

    def set_msg(mode):
        def action(icon, _):
            state["msg"] = mode
            save_config()
            log.info("Message an Client: %s", MSG_MODES[mode])
        return action

    def quit_app(icon, _):
        icon.stop()
        root.after(0, root.destroy)

    icon = pystray.Icon("keepalive_proxy", icons["wait"], "Keepalive-Proxy", menu=pystray.Menu(
        pystray.MenuItem("Letzte 10 Pings …", lambda i, _: root.after(0, show), default=True),
        pystray.MenuItem("Message an Client", pystray.Menu(*(
            pystray.MenuItem(text, set_msg(mode), checked=lambda _, m=mode: state["msg"] == m, radio=True)
            for mode, text in MSG_MODES.items()))),
        pystray.MenuItem("Setup …", lambda i, _: root.after(0, show_setup)),
        pystray.MenuItem("Logdatei öffnen", lambda i, _: os.startfile(log_path)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Beenden", quit_app)))
    icon.run_detached()

    def tray_missing():
        """True, wenn Windows das Tray-Symbol nicht (mehr) kennt – z. B. nach einem Absturz des
        Explorers oder wenn es beim Hochfahren vor der Taskleiste angemeldet wurde."""
        hwnd = getattr(icon, "_hwnd", None)
        if not hwnd:
            return False
        from pystray._util import win32 as pw
        data = pw.NOTIFYICONDATAW(cbSize=ctypes.sizeof(pw.NOTIFYICONDATAW), hWnd=hwnd, hID=id(icon), uFlags=0)
        return not pw.Shell_NotifyIcon(pw.NIM_MODIFY, data)

    shown = {"flip": None, "tray_check": time.time()}

    def poll():
        if state["last"] is not None and shown["flip"] != state["flip"]:
            shown["flip"] = state["flip"]
            icon.icon = icons["fail"] if not state["last"] else icons["a" if state["flip"] else "b"]
            refresh_view()
        s = state["session"]
        client = "Client über Proxy verbunden" if s is not None and not s.stop.is_set() else "Client nicht über Proxy"
        last = state["pings"][0] if state["pings"] else None
        node = ("Node ?" if last is None else
                f"Node OK {last[2] if last[2] is not None else '?'} ms" if last[1] else "Node ANTWORTET NICHT")
        title = f"Keepalive-Proxy – {node} – {client}"[:127]
        if icon.title != title:
            icon.title = title
        if time.time() - shown["tray_check"] >= TRAY_CHECK_S:
            shown["tray_check"] = time.time()
            if tray_missing():
                log.warning("Tray-Symbol fehlt – wird neu angelegt")
                icon._show()
        root.after(500, poll)

    root.after(500, poll)
    root.mainloop()
    os._exit(0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keepalive-Proxy für Meshtastic-WLAN-Nodes")
    ap.add_argument("--listen", default="127.0.0.1:4405", help="Adresse für den Client (Standard 127.0.0.1:4405)")
    ap.add_argument("--node", help="Adresse des Nodes (Standard aus dem Setup, sonst 192.168.10.41:4403)")
    ap.add_argument("--interval", type=float, help="Sekunden Stille bis zum Heartbeat (Standard: Setup, sonst 60)")
    ap.add_argument("--ping", type=float, help="Minuten zwischen zwei Pings (Standard aus dem Setup, sonst 1)")
    ap.add_argument("--debug", action="store_true", help="Client->Node-Rahmen mitloggen (Fehlersuche)")
    ap.add_argument("--log", default=str(HERE / "keepalive_proxy.log"), help="Logdatei")
    ap.add_argument("--notray", action="store_true", help="ohne Tray-Symbol laufen")
    args = ap.parse_args(argv)
    handler = logging.handlers.RotatingFileHandler(args.log, maxBytes=500_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    cfg = load_config()
    state["node"] = hostport(args.node or cfg.get("node") or "192.168.10.41:4403", 4403)
    state["ping_min"] = float(args.ping or cfg.get("ping_min") or 1)
    state["interval"] = args.interval or float(cfg.get("heartbeat_min") or 1) * 60
    state["debug"] = args.debug
    msg = cfg.get("message_an_client", "aus")
    if isinstance(msg, bool):                         # bis 1.3.0 nur an/aus
        msg = "ping" if msg else "aus"
    state["msg"] = msg if msg in MSG_MODES else "aus"
    listen_addr = state["listen"] = hostport(args.listen, 4405)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(listen_addr)                         # kein SO_REUSEADDR: ein zweiter Proxy soll scheitern
    except OSError as e:
        log.error("Port %s:%s belegt (%s) – läuft der Proxy schon?", *listen_addr, e)
        sys.exit(1)
    srv.listen(5)
    log.info("Keepalive-Proxy %s lauscht auf %s:%s -> Node %s:%s, Ping alle %g min, Heartbeat nach %g min",
             VERSION, *listen_addr, *state["node"], state["ping_min"], state["interval"] / 60)
    threading.Thread(target=pinger, daemon=True).start()
    if args.notray:
        serve(srv)
        return
    threading.Thread(target=serve, args=(srv,), daemon=True).start()
    run_tray(args.log)


if __name__ == "__main__":
    main()
