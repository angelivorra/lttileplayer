#!/usr/bin/env python3
"""Servidor TCP de eventos para los clientes del robot (solenoides,
pantallas, vocoder...).

Reemplaza al bridge `server-midi.py` del repo lgptclient: en vez de sacar
MIDI por ALSA para que otro proceso lo selle y lo reparta, el player —que es
quien secuencia— lo emite ya sellado. Gana precisión (ver `EventServer.emit`)
y quita un proceso y el `aconnect` intermedios.

El protocolo es EL MISMO, byte a byte, para no tocar los clientes ya
desplegados: líneas ASCII terminadas en \\n, puerto 8888, TCP_NODELAY.

    CONFIG,<delay_ms>,<debug>,<ruido>,<pantalla>   al conectar
    SYNC,<ts_ms>                                   al conectar + latido
    NOTA,<ts_ms>,<nota>,<canal>,<velocidad>
    CC,<ts_ms>,<valor>,<canal>,<control>
    START,<ts_ms> / STOP,<ts_ms> / END,<ts_ms>
    BPM,<ts_ms>,<bpm>

Los clientes no ejecutan al recibir: programan la acción en
`ts + delay_ms` (1 s por defecto), y sincronizan su reloj con los SYNC del
propio socket, sin NTP. Ese margen de un segundo es también lo que hace
seguro usar TCP aquí: una retransmisión en LAN cabe de sobra en el hueco,
así que la fiabilidad sale gratis y no hay que pelearse con multicast WiFi.
"""

from __future__ import annotations

import queue
import socket
import threading
import time

TCP_PORT = 8888
HEARTBEAT_SECONDS = 5.0
# Cola acotada: si un cliente se atasca preferimos tirar eventos suyos antes
# que dejar que crezca sin límite. El hilo de audio nunca espera aquí.
MAX_QUEUED = 512


def now_ms() -> int:
    return int(time.time() * 1000)


class EventServer:
    """Acepta clientes y les difunde eventos. Pensado para que `emit()` se
    pueda llamar desde el hilo de audio: solo encola y vuelve."""

    def __init__(self, port: int = TCP_PORT, config: dict | None = None,
                 on_event=None):
        cfg = config or {}
        self._config_line = (
            f"CONFIG,{cfg.get('delay', 1000)},{int(bool(cfg.get('debug', 0)))},"
            f"{int(bool(cfg.get('ruido', 0)))},"
            f"{int(bool(cfg.get('pantalla', 1)))}\n"
        )
        self._on_event = on_event          # callback(msg) para avisos en la UI
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._queued = 0
        self.dropped = 0
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()      # protege la lista de clientes
        self._running = True

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", port))
        self._sock.listen(8)
        self.port = port

        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               daemon=True)
        self._send_thread = threading.Thread(target=self._send_loop,
                                             daemon=True)
        self._beat_thread = threading.Thread(target=self._heartbeat_loop,
                                             daemon=True)
        for t in (self._accept_thread, self._send_thread, self._beat_thread):
            t.start()

    # -- API pública (se puede llamar desde el hilo de audio) ----------------

    def emit(self, kind: str, ts_ms: int, *fields):
        """Encola un evento ya sellado. `ts_ms` es el instante en que el
        cliente debe considerar que ocurre (ver nota de precisión abajo)."""
        if not self._running:
            return
        parts = ",".join(str(f) for f in fields)
        line = f"{kind},{ts_ms}" + (f",{parts}" if parts else "") + "\n"
        if self._queued >= MAX_QUEUED:
            self.dropped += 1
            return
        self._queue.put(line.encode("ascii", "replace"))
        self._queued += 1

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def close(self):
        self._running = False
        self._queue.put(None)
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()

    # -- hilos internos ------------------------------------------------------

    def _notify(self, msg: str):
        if self._on_event is not None:
            self._on_event(msg)

    def _accept_loop(self):
        while self._running:
            try:
                self._sock.settimeout(0.5)
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # El cliente necesita la config y una referencia de reloj
                # antes que cualquier evento.
                client.sendall(self._config_line.encode())
                client.sendall(f"SYNC,{now_ms()}\n".encode())
            except OSError:
                try:
                    client.close()
                except OSError:
                    pass
                continue
            with self._lock:
                self._clients.append(client)
            self._notify(f"cliente conectado: {addr[0]}")

    def _broadcast(self, data: bytes):
        with self._lock:
            clients = list(self._clients)
        dead = []
        for c in clients:
            try:
                c.sendall(data)
            except OSError:
                dead.append(c)
        if dead:
            with self._lock:
                for d in dead:
                    if d in self._clients:
                        self._clients.remove(d)
            for d in dead:
                try:
                    d.close()
                except OSError:
                    pass
            self._notify(f"cliente desconectado ({len(dead)})")

    def _send_loop(self):
        while True:
            data = self._queue.get()
            self._queued = max(0, self._queued - 1)
            if data is None:
                return
            self._broadcast(data)

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(HEARTBEAT_SECONDS)
            if not self._running:
                return
            with self._lock:
                if not self._clients:
                    continue
            self._broadcast(f"SYNC,{now_ms()}\n".encode())


class EventMidiOut:
    """Sink MidiOut del engine que difunde por `EventServer`.

    La cuenta de tiempos, que es lo delicado: el engine sabe en qué instante
    de reloj se OIRÁ cada evento (`event_time_ms`, que ya suma el retardo de
    audio), y el cliente no ejecuta al recibir sino en `ts + delay_ms`. Para
    que el solenoide dispare justo cuando suena la nota hay que mandar

        ts = instante_audible - delay_del_cliente

    Así cuadra con cualquier combinación de retardo de audio y `delay`, no
    solo cuando ambos valen 1 s.
    """

    def __init__(self, server: EventServer, engine_ref: dict,
                 client_delay_ms: int = 1000):
        self.server = server
        self._engine_ref = engine_ref      # {"engine": Engine} del player
        self.client_delay_ms = client_delay_ms

    def _ts(self) -> int:
        engine = self._engine_ref.get("engine")
        audible = engine.event_time_ms() if engine is not None else now_ms()
        return audible - self.client_delay_ms

    def note_on(self, channel, note, velocity):
        self.server.emit("NOTA", self._ts(), note, channel, velocity)

    def note_off(self, channel, note):
        # El protocolo no lleva note off: los clientes disparan solenoides con
        # la nota y cierran solos. Se ignora a propósito.
        pass

    def cc(self, channel, control, value):
        self.server.emit("CC", self._ts(), value, channel, control)

    def program_change(self, channel, program):
        pass                                # sin equivalente en el protocolo

    def transport_start(self):
        self.server.emit("START", self._ts())

    def transport_stop(self, finished: bool):
        self.server.emit("END" if finished else "STOP", self._ts())
