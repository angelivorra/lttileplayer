#!/usr/bin/env python3
"""
Parser experimental para proyectos de LittleGPTracker (lgptsav.dat).

Estructura del archivo:
  - Primeros 4 bytes: tamaño del XML descomprimido (little-endian int)
  - Resto: datos comprimidos con el codificador LZ77 de Marcus Geelnard
    (sources/Externals/Compression/lz.c)

El XML contiene nodos PROJECT, SONG, TABLES, GROOVES, INSTRUMENTBANK.
Los buffers binarios se almacenan como texto hexadecimal en chunks DATA.
"""

import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


def lz_read_var_size(data: bytes, pos: int) -> tuple[int, int]:
    """Lee un entero de tamaño variable usado por el compresor LZ."""
    y = 0
    num_bytes = 0
    while True:
        b = data[pos]
        y = (y << 7) | (b & 0x7F)
        pos += 1
        num_bytes += 1
        if not (b & 0x80):
            break
    return y, num_bytes


def lz_uncompress(data: bytes) -> bytes:
    """Descomprime un buffer codificado con LZ77 de Marcus Geelnard."""
    if len(data) < 1:
        return b""

    marker = data[0]
    in_pos = 1
    out = bytearray()

    while in_pos < len(data):
        symbol = data[in_pos]
        in_pos += 1

        if symbol == marker:
            if in_pos >= len(data):
                break
            if data[in_pos] == 0:
                out.append(marker)
                in_pos += 1
            else:
                length, n = lz_read_var_size(data, in_pos)
                in_pos += n
                offset, n = lz_read_var_size(data, in_pos)
                in_pos += n
                for _ in range(length):
                    out.append(out[-offset])
        else:
            out.append(symbol)

    return bytes(out)


def decompress_lgptsav(path: Path) -> bytes:
    """Abre un lgptsav.dat y devuelve el XML descomprimido.

    LGPT guarda el XML directamente si cabe, o lo comprime con LZ77.
    PersistencyService::Load primero intenta parsear el buffer tal cual;
    si falla, interpreta los primeros 4 bytes como tamaño y el resto como LZ.
    """
    raw = path.read_bytes()

    # Primero intentar como XML plano
    try:
        ET.fromstring(raw)
        return raw
    except ET.ParseError:
        pass

    # Si no es XML válido, descomprimir LZ77
    full_length = struct.unpack("<I", raw[:4])[0]
    comp = raw[4:]
    xml = lz_uncompress(comp)
    if len(xml) != full_length:
        print(f"Advertencia: tamaño descomprimido {len(xml)} != esperado {full_length}")
    return xml


def decode_hex_buffer(element: ET.Element) -> bytes:
    """Decodifica un nodo de buffer hexadecimal (DATA chunks)."""
    out = bytearray()
    for child in element.findall("DATA"):
        value = child.get("VALUE")
        if value is not None:
            length = int(child.get("LENGTH", "0"))
            out.extend(bytes([int(value)]) * length)
        else:
            text = (child.text or "").strip()
            out.extend(bytes.fromhex(text))
    return bytes(out)


class LGPTProject:
    def __init__(self, project_dir: Path):
        self.dir = project_dir
        self.xml: Optional[bytes] = None
        self.root: Optional[ET.Element] = None

        # Datos crudos extraídos del XML
        self.project = {}
        self.song = bytearray()          # 8 canales x 256 filas
        self.chains = bytearray()        # 255 cadenas x 16 pasos
        self.transposes = bytearray()    # 255 cadenas x 16 transpuestas
        self.notes = bytearray()         # 255 frases x 16 notas
        self.instruments = bytearray()   # 255 frases x 16 instrumentos
        self.cmd1: list[str] = []
        self.param1: list[int] = []
        self.cmd2: list[str] = []
        self.param2: list[int] = []
        self.tables: dict[int, dict] = {}
        self.grooves = bytearray()
        self.instrument_bank: dict[int, dict] = {}

    def load(self):
        sav_path = self.dir / "lgptsav.dat"
        self.xml = decompress_lgptsav(sav_path)
        self.root = ET.fromstring(self.xml)

        for child in self.root:
            tag = child.tag
            if tag == "PROJECT":
                self._parse_project(child)
            elif tag == "SONG":
                self._parse_song(child)
            elif tag == "TABLES":
                self._parse_tables(child)
            elif tag == "GROOVES":
                self._parse_grooves(child)
            elif tag == "INSTRUMENTBANK":
                self._parse_instruments(child)

    def _parse_project(self, node: ET.Element):
        for param in node.findall("PARAMETER"):
            name = param.get("NAME")
            value = param.get("VALUE")
            if name and value:
                self.project[name] = value

    def _parse_song(self, node: ET.Element):
        for child in node:
            tag = child.tag
            data = decode_hex_buffer(child)
            if tag == "SONG":
                self.song = bytearray(data)
            elif tag == "CHAINS":
                self.chains = bytearray(data)
            elif tag == "TRANSPOSES":
                self.transposes = bytearray(data)
            elif tag == "NOTES":
                self.notes = bytearray(data)
            elif tag == "INSTRUMENTS":
                self.instruments = bytearray(data)
            elif tag == "COMMAND1":
                self.cmd1 = self._decode_fourcc(data)
            elif tag == "PARAM1":
                self.param1 = self._decode_shorts(data)
            elif tag == "COMMAND2":
                self.cmd2 = self._decode_fourcc(data)
            elif tag == "PARAM2":
                self.param2 = self._decode_shorts(data)

    def _decode_fourcc(self, data: bytes) -> list[str]:
        return [data[i:i + 4].decode("ascii", errors="replace") for i in range(0, len(data), 4)]

    def _decode_shorts(self, data: bytes) -> list[int]:
        return [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data), 2)]

    def _parse_tables(self, node: ET.Element):
        for table in node.findall("TABLE"):
            tid = int(table.get("ID"), 16)
            entry = {"cmd1": [], "param1": [], "cmd2": [], "param2": [],
                     "cmd3": [], "param3": []}
            for child in table:
                data = decode_hex_buffer(child)
                if child.tag == "CMD1":
                    entry["cmd1"] = self._decode_fourcc(data)
                elif child.tag == "PARAM1":
                    entry["param1"] = self._decode_shorts(data)
                elif child.tag == "CMD2":
                    entry["cmd2"] = self._decode_fourcc(data)
                elif child.tag == "PARAM2":
                    entry["param2"] = self._decode_shorts(data)
                elif child.tag == "CMD3":
                    entry["cmd3"] = self._decode_fourcc(data)
                elif child.tag == "PARAM3":
                    entry["param3"] = self._decode_shorts(data)
            self.tables[tid] = entry

    def _parse_grooves(self, node: ET.Element):
        for child in node:
            if child.tag == "DATA":
                self.grooves = bytearray(decode_hex_buffer(node))

    def _parse_instruments(self, node: ET.Element):
        for instr in node.findall("INSTRUMENT"):
            iid = int(instr.get("ID"), 16)
            itype = instr.get("TYPE", "Sample")
            params = {}
            for param in instr.findall("PARAM"):
                name = param.get("NAME")
                value = param.get("VALUE")
                if name and value:
                    params[name] = value
            self.instrument_bank[iid] = {"type": itype, "params": params}

    def list_samples(self) -> list[Path]:
        sample_dir = self.dir / "samples"
        if not sample_dir.exists():
            return []
        return sorted(sample_dir.glob("*.wav"))

    def sample_pool_order(self) -> list[str]:
        """Devuelve los nombres de sample ordenados como LGPT lo hace (descendente)."""
        names = [p.name for p in self.list_samples()]
        # El SamplePool::Sort ordena de mayor a menor con strcmp > 0
        # Implementación burbuja in-place del C:
        names = names[:]
        rest = len(names)
        while rest > 0:
            idx = 0
            for i in range(1, rest):
                if names[i] > names[idx]:
                    idx = i
            names[idx], names[rest - 1] = names[rest - 1], names[idx]
            rest -= 1
        return names


def note_to_midi(note_byte: int) -> int:
    """Convierte una nota LGPT (00-7F?) a número MIDI."""
    return note_byte


NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]


def note_byte_to_name(note: int) -> str:
    """Convierte byte de nota LGPT a nombre tipo C-4."""
    if note == 0xFF:
        return "---"
    if note > 127:
        return f"{note:02X}"
    name = NOTE_NAMES[note % 12]
    octave = note // 12 - 2
    return f"{name}{octave}"


def collect_commands(p: LGPTProject):
    """Recopila todos los comandos usados en phrases y tables."""
    cmds = set()
    cmds.update(c for c in p.cmd1 if c != "----")
    cmds.update(c for c in p.cmd2 if c != "----")
    for t in p.tables.values():
        cmds.update(c for c in t["cmd1"] if c != "----")
        cmds.update(c for c in t["cmd2"] if c != "----")
        cmds.update(c for c in t["cmd3"] if c != "----")
    return sorted(cmds)


def print_phrase(p: LGPTProject, phrase_idx: int):
    print(f"\n=== PHRASE {phrase_idx:02X} ===")
    base = phrase_idx * 16
    for step in range(16):
        idx = base + step
        note = note_byte_to_name(p.notes[idx])
        instr = f"{p.instruments[idx]:02X}" if p.instruments[idx] != 0xFF else "--"
        c1 = p.cmd1[idx]
        v1 = f"{p.param1[idx]:04X}"
        c2 = p.cmd2[idx]
        v2 = f"{p.param2[idx]:04X}"
        print(f"  {step:02X}: {note} {instr}  {c1} {v1}  {c2} {v2}")


def print_chain(p: LGPTProject, chain_idx: int):
    print(f"\n=== CHAIN {chain_idx:02X} ===")
    base = chain_idx * 16
    for step in range(16):
        idx = base + step
        phrase = f"{p.chains[idx]:02X}" if p.chains[idx] != 0xFF else "--"
        transp = p.transposes[idx]
        transp_s = f"{transp:+d}" if transp <= 127 else f"{transp - 256:+d}"
        print(f"  {step:02X}: phrase {phrase}  transpose {transp_s}")


def print_project_summary(p: LGPTProject):
    print("=== PROJECT ===")
    for k, v in p.project.items():
        print(f"  {k}: {v}")

    print("\n=== SONG (primeras filas) ===")
    for row in range(min(16, 256)):
        cells = [f"{p.song[row * 8 + ch]:02X}" if p.song[row * 8 + ch] != 0xFF else "--"
                 for ch in range(8)]
        print(f"  {row:02X}: {' | '.join(cells)}")

    print("\n=== CHAINS usadas ===")
    used_chains = sorted(set(c for c in p.chains if c != 0xFF))
    print(f"  {len(used_chains)} cadenas usadas: {[f'{c:02X}' for c in used_chains[:20]]}")

    print("\n=== PHRASES usadas ===")
    used_phrases = sorted(set(c for c in p.notes if c != 0xFF))
    print(f"  {len(used_phrases)} frases con notas")

    print("\n=== COMANDOS USADOS ===")
    for cmd in collect_commands(p):
        print(f"  {cmd}")

    print("\n=== TABLES ===")
    print(f"  {len(p.tables)} tablas definidas: {sorted(p.tables.keys())}")

    print("\n=== INSTRUMENTS ===")
    for iid in sorted(p.instrument_bank.keys()):
        instr = p.instrument_bank[iid]
        sample = instr["params"].get("sample", "")
        print(f"  {iid:02X} ({instr['type']}): sample={sample}")

    print("\n=== SAMPLES en disco (orden LGPT) ===")
    for idx, name in enumerate(p.sample_pool_order()):
        print(f"  {idx:02X}: {name}")

    # Imprimir una cadena y frase de ejemplo
    first_chain = used_chains[0] if used_chains else None
    if first_chain is not None:
        print_chain(p, first_chain)

    first_phrase = used_phrases[0] if used_phrases else None
    if first_phrase is not None:
        print_phrase(p, first_phrase)


if __name__ == "__main__":
    import sys

    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/angel/LGPT/songs/lgpt_abduccion")
    p = LGPTProject(project_dir)
    p.load()
    print_project_summary(p)
