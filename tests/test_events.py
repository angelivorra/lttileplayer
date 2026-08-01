"""Servidor TCP de eventos para los clientes del robot.

El protocolo tiene que seguir siendo compatible byte a byte con el bridge
`server-midi.py` del repo lgptclient: hay clientes desplegados (maleta,
sombrilla, roboguitarra, vocoder) que no se van a tocar.
"""
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_server import EventMidiOut, EventServer  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def read_lines(sock, count, timeout=3.0):
    """Lee hasta `count` líneas o hasta agotar el tiempo."""
    sock.settimeout(timeout)
    lines, buf = [], b""
    deadline = time.time() + timeout
    while len(lines) < count and time.time() < deadline:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            break
        if not data:
            break
        buf += data
        while b"\n" in buf and len(lines) < count:
            line, buf = buf.split(b"\n", 1)
            lines.append(line.decode())
    return lines


class FakeEngine:
    """Engine mínimo: solo necesita decir cuándo se oirá el evento."""

    def __init__(self, audible_ms):
        self._audible = audible_ms

    def event_time_ms(self):
        return self._audible


class TestEventServer(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.server = EventServer(
            port=self.port,
            config={"delay": 1000, "debug": 0, "ruido": 0, "pantalla": 1})
        time.sleep(0.15)                    # que el hilo de accept arranque

    def tearDown(self):
        self.server.close()

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def test_saludo_config_y_sync(self):
        """Al conectar, el cliente recibe CONFIG y una referencia de reloj."""
        s = self._connect()
        try:
            lines = read_lines(s, 2)
            self.assertEqual(lines[0], "CONFIG,1000,0,0,1")
            self.assertTrue(lines[1].startswith("SYNC,"))
            # el SYNC lleva un reloj en ms plausible
            ts = int(lines[1].split(",")[1])
            self.assertLess(abs(ts - time.time() * 1000), 5000)
        finally:
            s.close()

    def test_formato_de_los_eventos(self):
        s = self._connect()
        try:
            read_lines(s, 2)                # saludo
            self.server.emit("NOTA", 1234, 60, 3, 100)
            self.server.emit("CC", 1234, 64, 2, 7)
            self.server.emit("START", 1234)
            lines = read_lines(s, 3)
            self.assertEqual(lines, ["NOTA,1234,60,3,100",
                                     "CC,1234,64,2,7",
                                     "START,1234"])
        finally:
            s.close()

    def test_varios_clientes_reciben_lo_mismo(self):
        a, b = self._connect(), self._connect()
        try:
            read_lines(a, 2)
            read_lines(b, 2)
            self.server.emit("NOTA", 99, 40, 0, 127)
            self.assertEqual(read_lines(a, 1), ["NOTA,99,40,0,127"])
            self.assertEqual(read_lines(b, 1), ["NOTA,99,40,0,127"])
        finally:
            a.close()
            b.close()

    def test_sin_clientes_no_falla(self):
        """Emitir sin nadie conectado no puede reventar el hilo de audio."""
        for _ in range(50):
            self.server.emit("NOTA", 1, 2, 3, 4)
        self.assertEqual(self.server.client_count, 0)

    def test_cola_acotada(self):
        """Si nadie consume, se descartan eventos en vez de crecer sin fin."""
        for _ in range(2000):
            self.server.emit("NOTA", 1, 2, 3, 4)
        self.assertGreater(self.server.dropped, 0)


class TestEventMidiOut(unittest.TestCase):
    """El sello temporal es lo delicado: el cliente ejecuta en ts + delay,
    así que hay que restarle su delay al instante audible."""

    def setUp(self):
        self.port = free_port()
        self.server = EventServer(port=self.port, config={"delay": 1000})
        time.sleep(0.15)
        self.ref = {}
        self.sink = EventMidiOut(self.server, self.ref, client_delay_ms=1000)

    def tearDown(self):
        self.server.close()

    def test_resta_el_delay_del_cliente(self):
        self.ref["engine"] = FakeEngine(audible_ms=50_000)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            read_lines(s, 2)
            self.sink.note_on(channel=2, note=60, velocity=100)
            line = read_lines(s, 1)[0]
            # audible 50000, delay del cliente 1000 -> se manda 49000, y el
            # cliente ejecutará en 49000+1000 = 50000: justo cuando suena.
            self.assertEqual(line, "NOTA,49000,60,2,100")
        finally:
            s.close()

    def test_delay_distinto_del_de_audio(self):
        """Debe cuadrar con cualquier delay, no solo con 1 s."""
        sink = EventMidiOut(self.server, self.ref, client_delay_ms=250)
        self.ref["engine"] = FakeEngine(audible_ms=10_000)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            read_lines(s, 2)
            sink.cc(channel=1, control=7, value=64)
            self.assertEqual(read_lines(s, 1)[0], "CC,9750,64,1,7")
        finally:
            s.close()

    def test_transporte(self):
        self.ref["engine"] = FakeEngine(audible_ms=5_000)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            read_lines(s, 2)
            self.sink.transport_start()
            self.sink.transport_stop(finished=True)
            self.sink.transport_stop(finished=False)
            self.assertEqual(read_lines(s, 3),
                             ["START,4000", "END,4000", "STOP,4000"])
        finally:
            s.close()

    def test_note_off_no_se_envia(self):
        """El protocolo no lleva note off: los clientes cierran solos."""
        self.ref["engine"] = FakeEngine(audible_ms=5_000)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            read_lines(s, 2)
            self.sink.note_off(channel=1, note=60)
            self.sink.note_on(channel=1, note=61, velocity=90)
            # solo llega el note on
            self.assertEqual(read_lines(s, 1), ["NOTA,4000,61,1,90"])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
