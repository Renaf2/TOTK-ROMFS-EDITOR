#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive Explorer v5.2 – Interface adaptée aux jeunes moddeurs.
Extraction, édition et sauvegarde en un clic, sans connaissance technique.
"""

import sys, os, re, struct, shutil, tempfile, zipfile, tarfile, difflib
from pathlib import Path
from io import BytesIO

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTabWidget, QTextEdit,
    QLineEdit, QPushButton, QLabel, QFileDialog, QMessageBox,
    QToolBar, QProgressDialog, QMenu, QHeaderView, QComboBox,
    QAbstractItemView, QAction, QDialog, QCheckBox, QScrollArea,
    QStatusBar, QToolTip
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import (
    QFont, QColor, QTextCharFormat, QSyntaxHighlighter,
    QTextCursor, QPixmap, QImage
)

import py7zr
import zstandard as zstd

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ═══════════════════════════════════════════════════════════════
#  CONFIGS JEUX
# ═══════════════════════════════════════════════════════════════

class GameConfig:
    def __init__(self, name, lbl1_slots=101, hash_mult=0x65, align=16, langs=None):
        self.name       = name
        self.lbl1_slots = lbl1_slots
        self.hash_mult  = hash_mult
        self.align      = align
        self.langs      = langs or ["USen"]

GAMES = {
    "TotK": GameConfig(
        "Tears of the Kingdom", 101, 0x65, 16,
        ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh","TWzh"]
    ),
    "BotW": GameConfig(
        "Breath of the Wild", 101, 0x65, 16,
        ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh"]
    ),
    "LA_NS": GameConfig(
        "Link's Awakening NS", 101, 0x65, 16,
        ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh","TWzh"]
    ),
}
current_game = GAMES["TotK"]

# ═══════════════════════════════════════════════════════════════
#  ZSTD
# ═══════════════════════════════════════════════════════════════

_zstd_dict = None

def set_zstd_dict(path):
    global _zstd_dict
    with open(path, 'rb') as f:
        _zstd_dict = zstd.ZstdCompressionDict(f.read())

def decompress_zs(data):
    try:
        dctx = zstd.ZstdDecompressor(dict_data=_zstd_dict) if _zstd_dict \
               else zstd.ZstdDecompressor()
        try:
            return dctx.decompress(data)
        except Exception:
            pass
        try:
            return dctx.decompress(data, max_output_size=200_000_000)
        except Exception:
            pass
        return dctx.stream_reader(BytesIO(data)).read()
    except Exception:
        return data

def compress_zs(data):
    cctx = zstd.ZstdCompressor(dict_data=_zstd_dict, level=16) if _zstd_dict \
           else zstd.ZstdCompressor(level=16)
    return cctx.compress(data)

# ═══════════════════════════════════════════════════════════════
#  SARC
# ═══════════════════════════════════════════════════════════════

class SarcReader:
    def __init__(self, data):
        self.data       = bytes(data)
        self.files      = {}
        self.file_order = []
        self._parse()

    def _parse(self):
        s = BytesIO(self.data)
        if s.read(4) != b'SARC':
            raise ValueError("Magic SARC invalide")
        s.read(2)                                          # header_size
        bom = struct.unpack('<H', s.read(2))[0]
        if bom not in (0xFFFE, 0xFEFF):
            raise ValueError(f"BOM invalide : {hex(bom)}")
        s.read(4)                                          # file_size
        data_offset = struct.unpack('<I', s.read(4))[0]
        s.read(4)                                          # version + reserved

        if s.read(4) != b'SFAT':
            raise ValueError("SFAT manquant")
        s.read(4)                                          # sfat_size (I)
        file_count = struct.unpack('<I', s.read(4))[0]
        if file_count > 65535:
            raise ValueError(f"Nombre de fichiers suspect : {file_count}")
        s.read(4)                                          # hash_mult

        entries = []
        for _ in range(file_count):
            s.read(4)                                      # name_hash
            name_info  = struct.unpack('<I', s.read(4))[0]
            file_start = struct.unpack('<I', s.read(4))[0]
            file_end   = struct.unpack('<I', s.read(4))[0]
            name_off   = (name_info & 0xFFFF) * 4
            entries.append((name_off, data_offset + file_start, data_offset + file_end))

        if s.read(4) != b'SFNT':
            raise ValueError("SFNT manquant")
        s.read(4)                                          # sfnt_size
        sfnt_base = s.tell()

        for name_off, start, end in entries:
            s.seek(sfnt_base + name_off)
            nb = b''
            while True:
                b = s.read(1)
                if not b or b == b'\x00':
                    break
                nb += b
            name = nb.decode('utf-8', errors='replace')
            if 0 <= start <= end <= len(self.data):
                self.files[name] = self.data[start:end]
                self.file_order.append(name)

    def list_files(self):
        return list(self.file_order)

    def get_file(self, name):
        return self.files.get(name, b'')


class SarcWriter:
    def __init__(self):
        self.files = []

    def add_file(self, name, data):
        self.files.append((name, bytes(data)))

    @staticmethod
    def _hash(name, mult=0x65):
        h = 0
        for c in name.encode('utf-8'):
            h = (h * mult + c) & 0xFFFFFFFF
        return h

    def save(self):
        self.files.sort(key=lambda x: self._hash(x[0]))
        fc = len(self.files)

        name_offsets = {}
        name_block   = BytesIO()
        for name, _ in self.files:
            if name not in name_offsets:
                name_offsets[name] = name_block.tell() // 4
                enc = name.encode('utf-8') + b'\x00'
                name_block.write(enc)
                pad = (4 - (name_block.tell() % 4)) % 4
                if pad:
                    name_block.write(b'\x00' * pad)
        name_data = name_block.getvalue()

        data_positions = []
        data_block     = BytesIO()
        for _, data in self.files:
            start = data_block.tell()
            data_block.write(data)
            end = data_block.tell()
            data_positions.append((start, end))
            pad = (4 - (end % 4)) % 4
            if pad:
                data_block.write(b'\x00' * pad)
        data_data = data_block.getvalue()

        sfat_size  = 12 + fc * 16
        sfnt_size  = 8  + len(name_data)
        raw_offset = 0x14 + sfat_size + sfnt_size
        data_offset = (raw_offset + 0xFF) & ~0xFF
        total_size  = data_offset + len(data_data)

        out = BytesIO()
        out.write(b'SARC')
        out.write(struct.pack('<H', 0x14))
        out.write(struct.pack('<H', 0xFFFE))
        out.write(struct.pack('<I', total_size))
        out.write(struct.pack('<I', data_offset))
        out.write(struct.pack('<H', 0x0100))
        out.write(struct.pack('<H', 0x0000))
        out.write(b'SFAT')
        out.write(struct.pack('<I', sfat_size))
        out.write(struct.pack('<I', fc))
        out.write(struct.pack('<I', 0x65))
        for i, (name, _) in enumerate(self.files):
            s, e = data_positions[i]
            out.write(struct.pack('<I', self._hash(name)))
            out.write(struct.pack('<I', (name_offsets[name] & 0xFFFF) | 0x01000000))
            out.write(struct.pack('<I', s))
            out.write(struct.pack('<I', e))
        out.write(b'SFNT')
        out.write(struct.pack('<I', sfnt_size))
        out.write(name_data)
        cur = out.tell()
        if cur < data_offset:
            out.write(b'\x00' * (data_offset - cur))
        out.write(data_data)
        return out.getvalue()

# ═══════════════════════════════════════════════════════════════
#  MSBT
# ═══════════════════════════════════════════════════════════════

class MsbtParser:
    MAGIC = b'MsgStdBn'

    def __init__(self, data, game_cfg=None):
        self.raw    = bytes(data)
        self.game   = game_cfg or current_game
        self.labels = []
        self.texts  = {}
        self._parse()

    def _parse(self):
        s = BytesIO(self.raw)
        if s.read(8) != self.MAGIC:
            raise ValueError("Pas un fichier MSBT")
        bom = s.read(2)
        self.enc = 'utf-16-be' if bom == b'\xFE\xFF' else 'utf-16-le'
        s.read(2)
        section_count = struct.unpack('<H', s.read(2))[0]
        s.read(2)
        s.read(4)
        s.read(10)

        lbl_map   = {}
        all_texts = []

        for _ in range(section_count):
            pos   = s.tell()
            align = (self.game.align - (pos % self.game.align)) % self.game.align
            if align:
                s.read(align)
            magic = s.read(4)
            if not magic or len(magic) < 4:
                break
            sec_size  = struct.unpack('<I', s.read(4))[0]
            s.read(8)
            sec_start = s.tell()
            if magic == b'LBL1':
                lbl_map = self._read_lbl1(s, sec_start, sec_size)
            elif magic == b'TXT2':
                all_texts = self._read_txt2(s, sec_start, sec_size)
            s.seek(sec_start + sec_size)

        for idx, label in sorted(lbl_map.items()):
            self.labels.append(label)
            self.texts[label] = all_texts[idx] if idx < len(all_texts) else ''

    def _read_lbl1(self, s, sec_start, sec_size):
        num_slots = struct.unpack('<I', s.read(4))[0]
        if num_slots > 10000:
            return {}
        slots = []
        for _ in range(num_slots):
            count  = struct.unpack('<I', s.read(4))[0]
            offset = struct.unpack('<I', s.read(4))[0]
            slots.append((count, offset))
        base   = sec_start + 4 + num_slots * 8
        result = {}
        for count, offset in slots:
            if count == 0:
                continue
            s.seek(base + offset)
            for _ in range(count):
                llen  = struct.unpack('B', s.read(1))[0]
                label = s.read(llen).decode('utf-8', errors='replace')
                idx   = struct.unpack('<I', s.read(4))[0]
                result[idx] = label
        return result

    def _read_txt2(self, s, sec_start, sec_size):
        num     = struct.unpack('<I', s.read(4))[0]
        if num > 100000:
            return []
        offsets = [struct.unpack('<I', s.read(4))[0] for _ in range(num)]
        base    = sec_start + 4 + num * 4
        texts   = []
        for off in offsets:
            s.seek(base + off)
            chars = []
            while True:
                raw2 = s.read(2)
                if len(raw2) < 2:
                    break
                cp = struct.unpack('<H', raw2)[0]
                if cp == 0:
                    break
                if cp == 0x000E:
                    grp = struct.unpack('<H', s.read(2))[0]
                    typ = struct.unpack('<H', s.read(2))[0]
                    dsz = struct.unpack('<H', s.read(2))[0]
                    dat = s.read(dsz)
                    chars.append(f'<tag grp={grp} typ={typ} data={dat.hex()}>')
                elif cp == 0x000F:
                    chars.append('</tag>')
                else:
                    try:
                        chars.append(chr(cp))
                    except Exception:
                        chars.append(f'<U+{cp:04X}>')
            texts.append(''.join(chars))
        return texts

    def to_txt(self):
        lines = []
        for label in self.labels:
            lines.append(f'[{label}]')
            lines.append(self.texts.get(label, ''))
            lines.append('---')
        return '\n'.join(lines)

    def from_txt(self, txt):
        current = None
        buf     = []
        for line in txt.splitlines():
            if line.startswith('[') and line.endswith(']') and len(line) > 2:
                if current is not None and current in self.texts:
                    self.texts[current] = '\n'.join(buf)
                current = line[1:-1]
                buf     = []
            elif line == '---':
                if current is not None and current in self.texts:
                    self.texts[current] = '\n'.join(buf)
                current = None
                buf     = []
            else:
                if current is not None:
                    buf.append(line)
        if current is not None and current in self.texts:
            self.texts[current] = '\n'.join(buf)

    def save(self):
        cfg = self.game
        out = BytesIO()
        out.write(self.MAGIC)
        out.write(b'\xFF\xFE')
        out.write(b'\x00\x00')
        out.write(struct.pack('<H', 2))
        out.write(b'\x00\x00')
        size_pos = out.tell()
        out.write(struct.pack('<I', 0))
        out.write(b'\x00' * 10)

        def _align():
            pos = out.tell()
            pad = (cfg.align - (pos % cfg.align)) % cfg.align
            if pad:
                out.write(b'\x00' * pad)

        def _section(magic4, body):
            _align()
            out.write(magic4)
            out.write(struct.pack('<I', len(body)))
            out.write(b'\x00' * 8)
            out.write(body)

        # LBL1
        NUM    = cfg.lbl1_slots
        slots  = [[] for _ in range(NUM)]
        for idx, label in enumerate(self.labels):
            h = 0
            for c in label.encode('utf-8'):
                h = (h * cfg.hash_mult + c) & 0xFFFFFFFF
            slots[h % NUM].append((label, idx))
        lbl_body  = BytesIO()
        lbl_block = BytesIO()
        lbl_body.write(struct.pack('<I', NUM))
        for slot in slots:
            lbl_body.write(struct.pack('<I', len(slot)))
            lbl_body.write(struct.pack('<I', lbl_block.tell()))
            for label, idx in slot:
                enc = label.encode('utf-8')
                lbl_block.write(struct.pack('B', len(enc)))
                lbl_block.write(enc)
                lbl_block.write(struct.pack('<I', idx))
        lbl_body.write(lbl_block.getvalue())
        _section(b'LBL1', lbl_body.getvalue())

        # TXT2
        strings = [self._encode_text(self.texts.get(lbl, '')) for lbl in self.labels]
        txt_body = BytesIO()
        txt_body.write(struct.pack('<I', len(strings)))
        cur_off = 0
        for enc in strings:
            txt_body.write(struct.pack('<I', cur_off))
            cur_off += len(enc)
        for enc in strings:
            txt_body.write(enc)
        _section(b'TXT2', txt_body.getvalue())

        total = out.tell()
        out.seek(size_pos)
        out.write(struct.pack('<I', total))
        return out.getvalue()

    def _encode_text(self, text):
        out = BytesIO()
        i   = 0
        while i < len(text):
            if text[i:i+5] == '<tag ':
                end = text.find('>', i)
                if end != -1:
                    try:
                        parts = {}
                        for tok in text[i+5:end].split():
                            k, v = tok.split('=', 1)
                            parts[k] = v
                        grp = int(parts.get('grp', '0'))
                        typ = int(parts.get('typ', '0'))
                        dat = bytes.fromhex(parts.get('data', ''))
                        out.write(struct.pack('<H', 0x000E))
                        out.write(struct.pack('<H', grp))
                        out.write(struct.pack('<H', typ))
                        out.write(struct.pack('<H', len(dat)))
                        out.write(dat)
                    except Exception:
                        pass
                    i = end + 1
                    continue
            if text[i:i+6] == '</tag>':
                out.write(struct.pack('<H', 0x000F))
                i += 6
                continue
            out.write(struct.pack('<H', ord(text[i])))
            i += 1
        out.write(b'\x00\x00')
        return out.getvalue()

# ═══════════════════════════════════════════════════════════════
#  ARCHIVES (list / extract / update / extract_sarc)
# ═══════════════════════════════════════════════════════════════

ARCHIVE_EXT = {'.zip', '.7z', '.tar', '.gz', '.bz2', '.xz', '.sarc', '.zs'}

def read_file(path):
    with open(path, 'rb') as f:
        return f.read()

def archive_list(path):
    ext = Path(path).suffix.lower()
    try:
        if ext == '.zip':
            with zipfile.ZipFile(path) as z:
                return [i.filename for i in z.infolist() if not i.is_dir()]
        elif ext == '.7z':
            with py7zr.SevenZipFile(path, 'r') as sz:
                return sz.getnames()
        elif ext in ('.tar', '.gz', '.bz2', '.xz'):
            with tarfile.open(path) as t:
                return [m.name for m in t.getmembers() if m.isfile()]
        elif ext == '.sarc':
            return SarcReader(read_file(path)).list_files()
        elif ext == '.zs':
            dec = decompress_zs(read_file(path))
            if dec[:4] == b'SARC':
                return SarcReader(dec).list_files()
            return [Path(path).stem]
    except Exception:
        pass
    return []

def archive_extract(arc_path, internal):
    ext = Path(arc_path).suffix.lower()
    if ext == '.zip':
        with zipfile.ZipFile(arc_path) as z:
            return z.read(internal)
    elif ext == '.7z':
        with py7zr.SevenZipFile(arc_path, 'r') as sz:
            return sz.read([internal])[internal].read()
    elif ext in ('.tar', '.gz', '.bz2', '.xz'):
        with tarfile.open(arc_path) as t:
            return t.extractfile(t.getmember(internal)).read()
    elif ext == '.sarc':
        return SarcReader(read_file(arc_path)).get_file(internal)
    elif ext == '.zs':
        dec = decompress_zs(read_file(arc_path))
        if dec[:4] == b'SARC':
            return SarcReader(dec).get_file(internal)
        return dec
    return b''

def archive_update(arc_path, internal, new_data):
    ext = Path(arc_path).suffix.lower()
    if ext == '.zip':
        tmp = tempfile.mkdtemp()
        try:
            with zipfile.ZipFile(arc_path) as z:
                z.extractall(tmp)
            dst = os.path.join(tmp, internal)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as f:
                f.write(new_data)
            base = arc_path[:-4]
            if os.path.exists(arc_path):
                os.remove(arc_path)
            shutil.make_archive(base, 'zip', tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    elif ext == '.sarc':
        arc = SarcReader(read_file(arc_path))
        w   = SarcWriter()
        for n in arc.list_files():
            w.add_file(n, new_data if n == internal else arc.get_file(n))
        with open(arc_path, 'wb') as f:
            f.write(w.save())
    elif ext == '.zs':
        dec = decompress_zs(read_file(arc_path))
        if dec[:4] == b'SARC':
            arc = SarcReader(dec)
            w   = SarcWriter()
            for n in arc.list_files():
                w.add_file(n, new_data if n == internal else arc.get_file(n))
            with open(arc_path, 'wb') as f:
                f.write(compress_zs(w.save()))
        else:
            with open(arc_path, 'wb') as f:
                f.write(compress_zs(new_data))

def extract_sarc_from_zs(zs_path, output_path):
    """Décompresse un .zs et sauvegarde le .sarc sous-jacent."""
    with open(zs_path, 'rb') as f:
        data = f.read()
    dec = decompress_zs(data)
    if dec[:4] != b'SARC':
        raise ValueError("Le fichier .zs ne contient pas de SARC")
    with open(output_path, 'wb') as out:
        out.write(dec)

# ═══════════════════════════════════════════════════════════════
#  DÉCODAGE + VUE HEX
# ═══════════════════════════════════════════════════════════════

def _is_text(data):
    if not data:
        return False
    sample = data[:2048]
    if b'\x00' in sample:
        return False
    ctrl = sum(1 for b in sample if b < 0x20 and b not in (9, 10, 13))
    return (ctrl / len(sample)) < 0.05

def build_hex_view(data, max_bytes=65536):
    hdr   = f"{'OFFSET':>10}  {'00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F':49}  ASCII"
    lines = [hdr, '─' * len(hdr)]
    shown = data[:max_bytes]
    for i in range(0, len(shown), 16):
        chunk = shown[i:i+16]
        left  = ' '.join(f'{b:02X}' for b in chunk[:8])
        right = ' '.join(f'{b:02X}' for b in chunk[8:])
        asc   = ''.join(chr(b) if 32 <= b < 127 else '·' for b in chunk)
        lines.append(f'0x{i:08X}  {left:<23}  {right:<23}  {asc}')
    if len(data) > max_bytes:
        lines.append(f'\n… {len(data):,} octets total (limité à {max_bytes:,})')
    return '\n'.join(lines)

def decode_file(raw, hint_ext=''):
    """
    Retourne (mode, display_str, raw_decoded, is_zstd, msbt_or_None)
    mode : 'msbt' | 'sarc' | 'text' | 'hex' | 'image'
    """
    is_z = False
    if raw[:4] == b'\x28\xB5\x2F\xFD':
        dec = decompress_zs(raw)
        if dec != raw:
            raw  = dec
            is_z = True

    ext = hint_ext.lower()

    # MSBT
    if ext == '.msbt' or raw[:8] == b'MsgStdBn':
        try:
            msbt = MsbtParser(raw)
            return 'msbt', msbt.to_txt(), raw, is_z, msbt
        except Exception as e:
            return 'hex', f"# Erreur MSBT : {e}\n\n" + build_hex_view(raw), raw, is_z, None

    # SARC → liste lisible
    if raw[:4] == b'SARC':
        try:
            sarc  = SarcReader(raw)
            files = sarc.list_files()
            lines = [
                f"# Archive SARC — {len(files)} fichier(s)",
                "# Double-cliquez sur un fichier dans l'arbre de gauche pour l'ouvrir.",
                "",
            ]
            for f in files:
                sz = len(sarc.get_file(f))
                lines.append(f"  {f:<60}  {sz:>10,} o")
            return 'sarc', '\n'.join(lines), raw, is_z, None
        except Exception:
            pass

    # Texte
    if _is_text(raw):
        try:
            return 'text', raw.decode('utf-8'), raw, is_z, None
        except Exception:
            pass
        try:
            return 'text', raw.decode('utf-16'), raw, is_z, None
        except Exception:
            pass

    return 'hex', build_hex_view(raw), raw, is_z, None

# ═══════════════════════════════════════════════════════════════
#  HIGHLIGHTERS
# ═══════════════════════════════════════════════════════════════

class HexHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self._off = QTextCharFormat()
        self._off.setForeground(QColor('#569CD6'))
        self._off.setFontWeight(QFont.Bold)
        self._hex = QTextCharFormat()
        self._hex.setForeground(QColor('#CE9178'))
        self._asc = QTextCharFormat()
        self._asc.setForeground(QColor('#4EC9B0'))
        self._sep = QTextCharFormat()
        self._sep.setForeground(QColor('#555555'))

    def highlightBlock(self, text):
        if re.match(r'^0x[0-9A-Fa-f]{8}', text):
            self.setFormat(0, 10, self._off)
            self.setFormat(12, 49, self._hex)
            if len(text) > 63:
                self.setFormat(63, len(text) - 63, self._asc)
        elif text.startswith('─') or text.startswith('OFFSET') or text.startswith('#'):
            self.setFormat(0, len(text), self._sep)


class MsbtHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self._lbl = QTextCharFormat()
        self._lbl.setForeground(QColor('#DCDCAA'))
        self._lbl.setFontWeight(QFont.Bold)
        self._sep = QTextCharFormat()
        self._sep.setForeground(QColor('#555555'))
        self._tag = QTextCharFormat()
        self._tag.setForeground(QColor('#C586C0'))

    def highlightBlock(self, text):
        if text.startswith('[') and text.endswith(']'):
            self.setFormat(0, len(text), self._lbl)
        elif text == '---':
            self.setFormat(0, len(text), self._sep)
        else:
            for m in re.finditer(r'</?tag[^>]*>', text):
                self.setFormat(m.start(), m.end() - m.start(), self._tag)

# ═══════════════════════════════════════════════════════════════
#  DIALOG RECHERCHE / REMPLACEMENT (correction sélection doc)
# ═══════════════════════════════════════════════════════════════

class FindReplaceDialog(QDialog):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor   = editor
        self._matches = []
        self._cur     = -1
        self.setWindowTitle("Recherche & Remplacement")
        self.setMinimumWidth(540)
        self._build()
        self.setStyleSheet("""
            QDialog,QWidget{background:#252526;color:#d4d4d4;}
            QLabel{color:#d4d4d4;}
            QLineEdit{background:#3c3c3c;color:#d4d4d4;border:1px solid #555;padding:4px 8px;border-radius:3px;}
            QCheckBox{color:#d4d4d4;}
            QPushButton{background:#3a3a3a;color:#d4d4d4;border:1px solid #555;padding:5px 14px;border-radius:3px;}
            QPushButton:hover{background:#094771;}
        """)

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Rechercher :"))
        self.e_find = QLineEdit()
        self.e_find.setPlaceholderText("Tapez ici…")
        self.e_find.textChanged.connect(self._refresh)
        r1.addWidget(self.e_find)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Remplacer :"))
        self.e_repl = QLineEdit()
        self.e_repl.setPlaceholderText("Remplacement…")
        r2.addWidget(self.e_repl)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        self.chk_case  = QCheckBox("Casse exacte")
        self.chk_word  = QCheckBox("Mot entier")
        self.chk_regex = QCheckBox("Regex")
        self.lbl_count = QLabel("0 résultat(s)")
        self.lbl_count.setStyleSheet("color:#569CD6; font-weight:bold;")
        for w in (self.chk_case, self.chk_word, self.chk_regex):
            w.stateChanged.connect(self._refresh)
            r3.addWidget(w)
        r3.addStretch()
        r3.addWidget(self.lbl_count)
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        for label, slot in [
            ("◀ Précédent", self._prev),
            ("▶ Suivant",   self._next),
            ("Remplacer",   self._replace_one),
            ("Tout remplacer", self._replace_all),
            ("Fermer",      self.close),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            r4.addWidget(b)
        lay.addLayout(r4)

    def _pattern(self):
        pat = self.e_find.text()
        if not pat:
            return None
        if not self.chk_regex.isChecked():
            pat = re.escape(pat)
        if self.chk_word.isChecked():
            pat = r'\b' + pat + r'\b'
        flags = 0 if self.chk_case.isChecked() else re.IGNORECASE
        try:
            return re.compile(pat, flags)
        except re.error:
            return None

    def _refresh(self):
        # CORRECTION : effacement de toutes les surbrillances en sélectionnant tout le document
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.Start)
        cur.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cur.setCharFormat(QTextCharFormat())
        self.editor.setTextCursor(cur)

        self._matches = []
        rx = self._pattern()
        if not rx:
            self.lbl_count.setText("0 résultat(s)")
            return
        text = self.editor.toPlainText()
        fmt  = QTextCharFormat()
        fmt.setBackground(QColor('#613214'))
        fmt.setForeground(QColor('#ffffff'))
        for m in rx.finditer(text):
            self._matches.append((m.start(), m.end()))
            c = self.editor.textCursor()
            c.setPosition(m.start())
            c.setPosition(m.end(), QTextCursor.KeepAnchor)
            c.setCharFormat(fmt)
        self.lbl_count.setText(f"{len(self._matches)} résultat(s)")
        self._cur = -1

    def _go(self, idx):
        if not self._matches:
            return
        self._cur = idx % len(self._matches)
        s, e      = self._matches[self._cur]
        fmt = QTextCharFormat()
        fmt.setBackground(QColor('#D4A017'))
        fmt.setForeground(QColor('#000000'))
        c = self.editor.textCursor()
        c.setPosition(s)
        c.setPosition(e, QTextCursor.KeepAnchor)
        c.setCharFormat(fmt)
        self.editor.setTextCursor(c)
        self.editor.ensureCursorVisible()

    def _next(self):
        self._refresh()
        self._go(self._cur + 1)

    def _prev(self):
        self._refresh()
        self._go(self._cur - 1)

    def _replace_one(self):
        c = self.editor.textCursor()
        if c.hasSelection():
            c.insertText(self.e_repl.text())
        self._next()

    def _replace_all(self):
        rx = self._pattern()
        if not rx:
            return
        txt = rx.sub(self.e_repl.text(), self.editor.toPlainText())
        self.editor.setPlainText(txt)
        self._refresh()

# ═══════════════════════════════════════════════════════════════
#  DIALOG COMPARAISON MSBT
# ═══════════════════════════════════════════════════════════════

class CompareDialog(QDialog):
    def __init__(self, left_data, right_data, left_name="Original", right_name="Modifié", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Comparaison : {left_name}  ↔  {right_name}")
        self.resize(1200, 700)
        self.setStyleSheet("QDialog,QWidget{background:#1e1e1e;color:#d4d4d4;}"
                           "QTextEdit{background:#1e1e1e;color:#d4d4d4;font-family:Consolas;font-size:11px;}")
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel(f"  {left_name}"))
        top.addStretch()
        top.addWidget(QLabel(f"{right_name}  "))
        lay.addLayout(top)

        self.left_edit  = QTextEdit(); self.left_edit.setReadOnly(True)
        self.right_edit = QTextEdit(); self.right_edit.setReadOnly(True)

        spl = QSplitter(Qt.Horizontal)
        spl.addWidget(self.left_edit)
        spl.addWidget(self.right_edit)
        lay.addWidget(spl)

        try:
            lm = MsbtParser(left_data)
            rm = MsbtParser(right_data)
            self._show_diff(lm.to_txt(), rm.to_txt())
        except Exception as e:
            self.left_edit.setPlainText(f"Erreur : {e}")

    def _show_diff(self, left_txt, right_txt):
        differ  = difflib.Differ()
        diffs   = list(differ.compare(left_txt.splitlines(), right_txt.splitlines()))
        l_lines, r_lines = [], []
        for line in diffs:
            if line.startswith('  '):
                l_lines.append(line[2:])
                r_lines.append(line[2:])
            elif line.startswith('- '):
                l_lines.append(f'<span style="background:#5a1a1a">{line[2:]}</span>')
                r_lines.append('<span style="color:#444">—</span>')
            elif line.startswith('+ '):
                l_lines.append('<span style="color:#444">—</span>')
                r_lines.append(f'<span style="background:#1a5a1a">{line[2:]}</span>')
        self.left_edit.setHtml('<br>'.join(l_lines))
        self.right_edit.setHtml('<br>'.join(r_lines))

# ═══════════════════════════════════════════════════════════════
#  ONGLET ÉDITEUR
# ═══════════════════════════════════════════════════════════════

class EditorTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.arc_path      = None
        self.arc_int       = None
        self.file_path     = None
        self.raw           = b''
        self.mode          = 'hex'
        self.is_zstd       = False
        self.msbt          = None
        self._hl           = None
        self._editing      = False
        self._original_txt = ''
        self._prev_mode    = None
        self._img_widget   = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Bannière d'info (en haut)
        self.lbl_info = QLabel("—")
        self.lbl_info.setStyleSheet(
            "background:#252526; color:#888; padding:3px 10px; font-size:11px;"
        )
        lay.addWidget(self.lbl_info)

        # Éditeur
        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QTextEdit.NoWrap)
        self.editor.setFont(QFont("Consolas", 10))
        lay.addWidget(self.editor)

        # Barre de recherche rapide (Ctrl+F)
        self._bar = QWidget()
        self._bar.setStyleSheet("background:#2d2d2d; border-top:1px solid #444;")
        bl = QHBoxLayout(self._bar)
        bl.setContentsMargins(6, 3, 6, 3)
        bl.addWidget(QLabel("Recherche rapide :"))
        self._e_srch = QLineEdit()
        self._e_srch.setPlaceholderText("Tapez puis Entrée…")
        self._e_srch.returnPressed.connect(self._srch_next)
        bl.addWidget(self._e_srch)
        for lbl, fn in [("↓", self._srch_next), ("↑", self._srch_prev)]:
            b = QPushButton(lbl)
            b.setFixedWidth(28)
            b.clicked.connect(fn)
            bl.addWidget(b)
        btn_close_bar = QPushButton("✕")
        btn_close_bar.setFixedWidth(28)
        btn_close_bar.clicked.connect(lambda: self._bar.setVisible(False))
        bl.addWidget(btn_close_bar)
        self._bar.setVisible(False)
        lay.addWidget(self._bar)

        # Barre de boutons (en bas)
        bbar = QHBoxLayout()
        bbar.setContentsMargins(6, 4, 6, 4)
        bbar.setSpacing(4)

        self.btn_edit = QPushButton("✏️ Éditer")
        self.btn_edit.setToolTip("Activer/désactiver l'édition du contenu")
        self.btn_edit.clicked.connect(self._toggle_edit)
        bbar.addWidget(self.btn_edit)

        self.btn_hex = QPushButton("🔢 Hex")
        self.btn_hex.setToolTip("Basculer entre la vue texte et la vue hexadécimale")
        self.btn_hex.clicked.connect(self._toggle_hex)
        bbar.addWidget(self.btn_hex)

        bbar.addSpacing(8)

        self.btn_find = QPushButton("🔍 Rechercher…")
        self.btn_find.setToolTip("Ouvrir la recherche et le remplacement avancés")
        self.btn_find.clicked.connect(lambda: FindReplaceDialog(self.editor, self).show())
        bbar.addWidget(self.btn_find)

        bbar.addSpacing(8)

        self.btn_exp_txt = QPushButton("📤 Export TXT")
        self.btn_exp_txt.setToolTip("Exporter ce fichier MSBT en texte .txt lisible")
        self.btn_exp_txt.clicked.connect(self._export_txt)
        self.btn_exp_txt.setEnabled(False)
        bbar.addWidget(self.btn_exp_txt)

        self.btn_imp_txt = QPushButton("📥 Import TXT")
        self.btn_imp_txt.setToolTip("Importer un fichier .txt et l'appliquer à ce MSBT")
        self.btn_imp_txt.clicked.connect(self._import_txt)
        self.btn_imp_txt.setEnabled(False)
        bbar.addWidget(self.btn_imp_txt)

        bbar.addStretch()

        self.btn_save = QPushButton("💾 Sauvegarder")
        self.btn_save.setToolTip("Sauvegarder directement dans l'archive/le fichier source")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        self.btn_save.setStyleSheet(
            "QPushButton{background:#0e7a0e;border-color:#1a9a1a;color:#fff;}"
            "QPushButton:hover{background:#0a6a0a;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;border-color:#333;}"
        )
        bbar.addWidget(self.btn_save)

        self.btn_saveas = QPushButton("💾 Sous…")
        self.btn_saveas.setToolTip("Enregistrer sous un nouveau nom/emplacement")
        self.btn_saveas.clicked.connect(self._save_as)
        bbar.addWidget(self.btn_saveas)

        bw = QWidget()
        bw.setLayout(bbar)
        bw.setStyleSheet("background:#252526; border-top:1px solid #333;")
        lay.addWidget(bw)

    # ── chargement ─────────────────────────────────────────────

    def load_direct(self, path):
        self.file_path = path
        self.arc_path  = None
        self.arc_int   = None
        try:
            raw = read_file(path)
        except Exception as e:
            self.editor.setPlainText(f"❌ Erreur de lecture :\n{e}")
            return
        self._display(raw, Path(path).suffix)

    def load_from_archive(self, arc_path, internal):
        self.arc_path  = arc_path
        self.arc_int   = internal
        self.file_path = None
        try:
            raw = archive_extract(arc_path, internal)
        except Exception as e:
            self.editor.setPlainText(f"❌ Erreur d'extraction :\n{e}")
            return
        self._display(raw, Path(internal).suffix)

    def _display(self, raw, ext=''):
        # Nettoyage image précédente
        if self._img_widget:
            self.layout().removeWidget(self._img_widget)
            self._img_widget.deleteLater()
            self._img_widget = None
            self.editor.show()

        # Images
        if ext.lower() in ('.png', '.jpg', '.jpeg'):
            try:
                pix = QPixmap()
                pix.loadFromData(raw)
                if not pix.isNull():
                    scroll = QScrollArea()
                    lbl    = QLabel()
                    lbl.setPixmap(pix)
                    lbl.setAlignment(Qt.AlignCenter)
                    scroll.setWidget(lbl)
                    scroll.setWidgetResizable(True)
                    lay = self.layout()
                    lay.insertWidget(1, scroll)
                    self.editor.hide()
                    self._img_widget = scroll
                    self.mode = 'image'
                    name = self.arc_int or (os.path.basename(self.file_path) if self.file_path else '?')
                    self.lbl_info.setText(f"  🖼 {name}  │  Image")
                    return
            except Exception:
                pass

        mode, txt, raw_dec, is_z, msbt = decode_file(raw, ext)
        self.raw           = raw_dec
        self.mode          = mode
        self.is_zstd       = is_z
        self.msbt          = msbt
        self._editing      = False
        self._original_txt = txt
        self._prev_mode    = None

        self.editor.setReadOnly(True)
        self.btn_edit.setText("✏️ Éditer")
        self.btn_save.setEnabled(False)

        if self._hl:
            self._hl.setDocument(None)
        if mode == 'hex':
            self._hl = HexHighlighter(self.editor.document())
        elif mode == 'msbt':
            self._hl = MsbtHighlighter(self.editor.document())
        else:
            self._hl = None

        self.editor.setPlainText(txt)
        self.editor.moveCursor(QTextCursor.Start)

        is_msbt = (mode == 'msbt')
        self.btn_exp_txt.setEnabled(is_msbt)
        self.btn_imp_txt.setEnabled(is_msbt)
        self.btn_edit.setEnabled(mode in ('msbt', 'text'))

        name     = self.arc_int or (os.path.basename(self.file_path) if self.file_path else '?')
        mode_str = {'msbt':'MSBT','text':'Texte','hex':'Binaire/Hex','sarc':'SARC (contenu)'}
        zinfo    = '  🗜 zstd' if is_z else ''
        self.lbl_info.setText(
            f"  {name}  │  {mode_str.get(mode, mode)}  │  {len(raw_dec):,} o{zinfo}"
        )
        self.btn_hex.setText("🔢 Hex")

    # ── actions ────────────────────────────────────────────────

    def _toggle_edit(self):
        if self.mode not in ('msbt', 'text'):
            return
        self._editing = not self._editing
        self.editor.setReadOnly(not self._editing)
        self.btn_edit.setText("🔒 Lecture seule" if self._editing else "✏️ Éditer")
        self.btn_save.setEnabled(self._editing)

    def _toggle_hex(self):
        if self.mode != 'hex':
            self._prev_mode = self.mode
            if self._hl:
                self._hl.setDocument(None)
            self._hl = HexHighlighter(self.editor.document())
            self.editor.setPlainText(build_hex_view(self.raw))
            self.editor.moveCursor(QTextCursor.Start)
            self.mode = 'hex'
            self.btn_hex.setText("📝 Normal")
        else:
            self.mode = self._prev_mode or 'text'
            ext = Path(self.arc_int or self.file_path or '').suffix
            self._display(self.raw, ext)
            self.btn_hex.setText("🔢 Hex")

    def _build_output(self):
        txt = self.editor.toPlainText()
        if self.mode == 'msbt' and self.msbt:
            self.msbt.from_txt(txt)
            data = self.msbt.save()
        elif self.mode == 'text':
            data = txt.encode('utf-8')
        else:
            data = self.raw
        if self.is_zstd:
            data = compress_zs(data)
        return data

    def _save(self):
        try:
            data = self._build_output()
            if self.arc_path and self.arc_int:
                archive_update(self.arc_path, self.arc_int, data)
                self._original_txt = self.editor.toPlainText()
                self._status("✅ Sauvegardé dans l'archive")
            elif self.file_path:
                with open(self.file_path, 'wb') as f:
                    f.write(data)
                self._original_txt = self.editor.toPlainText()
                self._status("✅ Fichier sauvegardé")
            else:
                QMessageBox.warning(self, "Attention", "Aucune destination connue.")
        except Exception as e:
            QMessageBox.critical(self, "Erreur sauvegarde", str(e))

    def _save_as(self):
        name = os.path.basename(self.arc_int or self.file_path or 'fichier')
        dest, _ = QFileDialog.getSaveFileName(self, "Enregistrer sous…", name)
        if not dest:
            return
        try:
            with open(dest, 'wb') as f:
                f.write(self._build_output())
            self._status(f"✅ Enregistré : {dest}")
        except Exception as e:
            QMessageBox.critical(self, "Erreur", str(e))

    def _export_txt(self):
        if not self.msbt:
            return
        name = Path(self.arc_int or self.file_path or 'export').stem + '.txt'
        dest, _ = QFileDialog.getSaveFileName(self, "Exporter TXT…", name, "*.txt")
        if dest:
            try:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(self.editor.toPlainText())
                self._status(f"✅ Exporté : {dest}")
            except Exception as e:
                QMessageBox.critical(self, "Erreur export", str(e))

    def _import_txt(self):
        if not self.msbt:
            return
        src, _ = QFileDialog.getOpenFileName(self, "Importer TXT…", '', "*.txt")
        if not src:
            return
        try:
            with open(src, 'r', encoding='utf-8') as f:
                txt = f.read()
            self.msbt.from_txt(txt)
            self.editor.setPlainText(self.msbt.to_txt())
            self._editing = True
            self.editor.setReadOnly(False)
            self.btn_save.setEnabled(True)
            self.btn_edit.setText("🔒 Lecture seule")
            self._status(f"✅ TXT importé : {src}")
        except Exception as e:
            QMessageBox.critical(self, "Erreur import", str(e))

    def _srch_next(self):
        txt = self._e_srch.text()
        if not txt:
            return
        cur = self.editor.textCursor()
        if cur.hasSelection():
            cur.setPosition(cur.selectionEnd())
        found = self.editor.document().find(txt, cur)
        if found.isNull():
            cur.setPosition(0)
            found = self.editor.document().find(txt, cur)
        if not found.isNull():
            self.editor.setTextCursor(found)

    def _srch_prev(self):
        txt = self._e_srch.text()
        if not txt:
            return
        cur = self.editor.textCursor()
        if cur.hasSelection():
            cur.setPosition(cur.selectionStart())
        found = self.editor.document().find(txt, cur, self.editor.document().FindBackward)
        if not found.isNull():
            self.editor.setTextCursor(found)

    def _status(self, msg):
        win = self.window()
        if hasattr(win, 'statusBar'):
            win.statusBar().showMessage(msg, 5000)

    def is_modified(self):
        if self.mode in ('msbt', 'text'):
            return self.editor.toPlainText() != self._original_txt
        return False

    def prompt_save(self):
        name = os.path.basename(self.arc_int or self.file_path or "sans nom")
        box  = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Modifications non sauvegardées")
        box.setText(f"Voulez-vous enregistrer les modifications de « {name} » ?")
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        return box.exec_()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F and event.modifiers() == Qt.ControlModifier:
            self._bar.setVisible(True)
            self._e_srch.setFocus()
        else:
            super().keyPressEvent(event)

# ═══════════════════════════════════════════════════════════════
#  ARBRE DE FICHIERS (corrigé)
# ═══════════════════════════════════════════════════════════════

class FileTree(QTreeWidget):
    sig_open_file   = pyqtSignal(str)
    sig_open_intern = pyqtSignal(str, str)

    EXT_ICON = {
        '.sarc':'📦', '.zs':'🗜', '.msbt':'📝',
        '.zip':'📦', '.7z':'📦', '.tar':'📦',
        '.txt':'📄', '.yaml':'📋', '.json':'📋', '.xml':'📋',
        '.png':'🖼', '.jpg':'🖼', '.jpeg':'🖼',
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Nom", "Taille"])
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx)
        self.itemDoubleClicked.connect(self._dclick)
        self.itemExpanded.connect(self._expand)
        self.root_path = ''
        self.setToolTip("📁 Double‑cliquez sur un dossier pour l'ouvrir.\n📄 Double‑cliquez sur un fichier pour l'éditer.")

    def set_root(self, path):
        self.clear()
        self.root_path = path
        self._populate(self.invisibleRootItem(), path)

    def load_single(self, path):
        self.clear()
        self.root_path = os.path.dirname(path)
        self._add_file(self.invisibleRootItem(), os.path.basename(path), path)

    def _populate(self, parent, path):
        try:
            entries = sorted(
                os.listdir(path),
                key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower())
            )
        except PermissionError:
            return
        for name in entries:
            full = os.path.join(path, name)
            if os.path.isdir(full):
                item = QTreeWidgetItem(parent, [f"📁 {name}", ""])
                item.setData(0, Qt.UserRole, ('dir', full))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                QTreeWidgetItem(item, ["…", ""])
            else:
                self._add_file(parent, name, full)

    def _add_file(self, parent, name, full):
        ext  = Path(name).suffix.lower()
        icon = self.EXT_ICON.get(ext, '📄')
        try:
            size = self._fmt(os.path.getsize(full))
        except Exception:
            size = ''
        item = QTreeWidgetItem(parent, [f"{icon} {name}", size])
        item.setData(0, Qt.UserRole, ('file', full))
        if ext in ARCHIVE_EXT:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            QTreeWidgetItem(item, ["…", ""])

    def _expand(self, item):
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            item.takeChildren()
            data = item.data(0, Qt.UserRole)
            if not data:
                return
            kind = data[0]
            if kind == 'dir':
                self._populate(item, data[1])
            elif kind == 'file':
                self._load_arc_children(item, data[1])

    def _load_arc_children(self, parent, arc_path):
        """Affiche la liste des fichiers internes de l'archive."""
        try:
            files = archive_list(arc_path)
            for f in files:
                ext  = Path(f).suffix.lower()
                icon = self.EXT_ICON.get(ext, '📄')
                child = QTreeWidgetItem(parent, [f"{icon} {os.path.basename(f)}", ""])
                child.setData(0, Qt.UserRole, ('arc_file', arc_path, f))
                child.setToolTip(0, f)
        except Exception as e:
            QTreeWidgetItem(parent, [f"⚠ {e}", ""])

    def _dclick(self, item, _col):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == 'file':
            if Path(data[1]).suffix.lower() not in ARCHIVE_EXT:
                self.sig_open_file.emit(data[1])
        elif data[0] == 'arc_file':
            self.sig_open_intern.emit(data[1], data[2])

    def _ctx(self, pos):
        items = self.selectedItems()
        if not items:
            return
        item = items[0]
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#2d2d2d;color:#ccc;border:1px solid #555;}"
            "QMenu::item:selected{background:#094771;}"
        )

        if data[0] == 'file':
            path = data[1]
            ext  = Path(path).suffix.lower()
            if ext not in ARCHIVE_EXT:
                a = menu.addAction("🔍 Ouvrir")
                a.triggered.connect(lambda: self.sig_open_file.emit(path))
            a2 = menu.addAction("📋 Extraire vers…")
            a2.triggered.connect(lambda: self._extract_file(path))
            if ext == '.zs':
                a3 = menu.addAction("📦 Extraire le SARC…")
                a3.triggered.connect(lambda: self._extract_sarc(path))
            if ext == '.msbt':
                a3 = menu.addAction("📊 Comparer avec…")
                a3.triggered.connect(lambda: self._compare_direct(path))

        elif data[0] == 'arc_file':
            arc, internal = data[1], data[2]
            a = menu.addAction("🔍 Ouvrir")
            a.triggered.connect(lambda: self.sig_open_intern.emit(arc, internal))
            a2 = menu.addAction("📋 Extraire vers…")
            a2.triggered.connect(lambda: self._extract_internal(arc, internal))
            if Path(internal).suffix.lower() == '.msbt':
                a3 = menu.addAction("📤 Exporter TXT…")
                a3.triggered.connect(lambda: self._export_msbt(arc, internal))
                a4 = menu.addAction("📊 Comparer avec…")
                a4.triggered.connect(lambda: self._compare_arc(arc, internal))

        # Export lot multi-sélection
        msbt_sel = [
            i for i in items
            if i.data(0, Qt.UserRole)
            and i.data(0, Qt.UserRole)[0] == 'arc_file'
            and Path(i.data(0, Qt.UserRole)[2]).suffix.lower() == '.msbt'
        ]
        if len(msbt_sel) > 1:
            a5 = menu.addAction(f"📤 Exporter {len(msbt_sel)} MSBT → TXT…")
            a5.triggered.connect(lambda: self._batch_export(msbt_sel))

        menu.exec_(self.viewport().mapToGlobal(pos))

    def _extract_file(self, path):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…", os.path.basename(path))
        if dest:
            shutil.copy2(path, dest)

    def _extract_internal(self, arc, internal):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…", os.path.basename(internal))
        if dest:
            try:
                with open(dest, 'wb') as f:
                    f.write(archive_extract(arc, internal))
            except Exception as e:
                QMessageBox.critical(self, "Erreur", str(e))

    def _extract_sarc(self, path):
        """Extraire le SARC d'un .zs"""
        dest, _ = QFileDialog.getSaveFileName(
            self, "Enregistrer le SARC", Path(path).stem + ".sarc", "*.sarc"
        )
        if dest:
            try:
                extract_sarc_from_zs(path, dest)
                QMessageBox.information(self, "Succès", "SARC extrait avec succès.")
            except Exception as e:
                QMessageBox.critical(self, "Erreur", str(e))

    def _export_msbt(self, arc, internal):
        try:
            raw  = archive_extract(arc, internal)
            msbt = MsbtParser(raw)
            dest, _ = QFileDialog.getSaveFileName(
                self, "Exporter TXT…", Path(internal).stem + '.txt', "*.txt"
            )
            if dest:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(msbt.to_txt())
        except Exception as e:
            QMessageBox.critical(self, "Erreur", str(e))

    def _compare_direct(self, path):
        other, _ = QFileDialog.getOpenFileName(self, "Comparer avec…", filter="*.msbt")
        if other:
            try:
                dlg = CompareDialog(read_file(path), read_file(other),
                                    os.path.basename(path), os.path.basename(other), self)
                dlg.exec_()
            except Exception as e:
                QMessageBox.critical(self, "Erreur", str(e))

    def _compare_arc(self, arc, internal):
        other, _ = QFileDialog.getOpenFileName(self, "Comparer avec…", filter="*.msbt")
        if other:
            try:
                dlg = CompareDialog(archive_extract(arc, internal), read_file(other),
                                    os.path.basename(internal), os.path.basename(other), self)
                dlg.exec_()
            except Exception as e:
                QMessageBox.critical(self, "Erreur", str(e))

    def _batch_export(self, items):
        dest_dir = QFileDialog.getExistingDirectory(self, "Dossier destination")
        if not dest_dir:
            return
        done = 0
        for item in items:
            arc, internal = item.data(0, Qt.UserRole)[1], item.data(0, Qt.UserRole)[2]
            try:
                raw  = archive_extract(arc, internal)
                msbt = MsbtParser(raw)
                out  = os.path.join(dest_dir, Path(internal).stem + '.txt')
                with open(out, 'w', encoding='utf-8') as f:
                    f.write(msbt.to_txt())
                done += 1
            except Exception:
                pass
        QMessageBox.information(self, "Export", f"✅ {done} fichier(s) exporté(s).")

    @staticmethod
    def _fmt(sz):
        for u in ('o', 'Ko', 'Mo', 'Go'):
            if sz < 1024:
                return f"{sz:.0f} {u}"
            sz /= 1024
        return f"{sz:.1f} To"

# ═══════════════════════════════════════════════════════════════
#  STYLE
# ═══════════════════════════════════════════════════════════════

STYLE = """
QMainWindow,QWidget     { background:#1e1e1e; color:#d4d4d4; font-size:13px; }
QMenuBar                { background:#2d2d2d; color:#ccc; }
QMenuBar::item:selected { background:#094771; }
QMenu                   { background:#2d2d2d; color:#ccc; border:1px solid #555; }
QMenu::item:selected    { background:#094771; }
QToolBar                { background:#2d2d2d; border:none; padding:3px; spacing:4px; }
QStatusBar              { background:#007ACC; color:#fff; font-size:11px; font-weight:bold; }
QSplitter::handle       { background:#333; width:3px; }

QTreeWidget             { background:#252526; border:none; color:#d4d4d4; }
QTreeWidget::item       { padding:3px 4px; }
QTreeWidget::item:selected  { background:#094771; color:#fff; }
QTreeWidget::item:hover     { background:#2a2d2e; }
QHeaderView::section    { background:#2d2d2d; color:#888; border:none;
                           border-right:1px solid #3a3a3a; padding:4px 6px; font-size:11px; }

QTextEdit               { background:#1e1e1e; color:#d4d4d4; border:none;
                           font-family:'Cascadia Code','Consolas',monospace;
                           font-size:11px; selection-background-color:#264F78; }
QLineEdit               { background:#3c3c3c; color:#d4d4d4; border:1px solid #555;
                           padding:4px 8px; border-radius:3px; }
QLineEdit:focus         { border-color:#007ACC; }
QPushButton             { background:#3a3a3a; color:#d4d4d4; border:1px solid #555;
                           padding:4px 12px; border-radius:3px; }
QPushButton:hover       { background:#094771; border-color:#007ACC; }
QPushButton:pressed     { background:#005a9e; }
QPushButton:disabled    { color:#555; background:#2a2a2a; border-color:#333; }

QTabWidget::pane        { border:1px solid #333; }
QTabBar::tab            { background:#2d2d2d; color:#888; padding:6px 16px; border:none; }
QTabBar::tab:selected   { background:#1e1e1e; color:#d4d4d4; border-bottom:2px solid #007ACC; }
QTabBar::tab:hover      { color:#ccc; }

QComboBox               { background:#3c3c3c; color:#d4d4d4; border:1px solid #555;
                           padding:3px 8px; border-radius:3px; }
QComboBox QAbstractItemView { background:#2d2d2d; color:#d4d4d4; selection-background-color:#094771; }
QScrollBar:vertical     { background:#252526; width:10px; border:none; }
QScrollBar::handle:vertical { background:#424242; border-radius:5px; min-height:20px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QToolTip                { background:#2d2d2d; color:#d4d4d4; border:1px solid #555; padding:4px; }
"""

# ═══════════════════════════════════════════════════════════════
#  FENÊTRE PRINCIPALE
# ═══════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Archive Explorer v5.2 – Modding simplifié")
        self.setGeometry(80, 80, 1400, 860)
        self.setStyleSheet(STYLE)
        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self.statusBar().showMessage(
            "Bienvenue !  📁 Ouvrez un dossier ou  📄 un fichier pour commencer."
        )

    # ── menu ───────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        mf = mb.addMenu("Fichier")
        for label, shortcut, fn in [
            ("📁 Ouvrir dossier…",  "Ctrl+O", self._open_folder),
            ("📄 Ouvrir fichier…",  "Ctrl+F", self._open_file),
            ("—",                   None,      None),
            ("📤 Export MSBT→TXT (lot)…", None, self._batch_export),
            ("📥 Import TXT→MSBT (lot)…", None, self._batch_import),
            ("—",                   None,      None),
            ("Quitter",             "Ctrl+Q",  self.close),
        ]:
            if label == "—":
                mf.addSeparator()
                continue
            a = QAction(label, self)
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(fn)
            mf.addAction(a)

        me = mb.addMenu("Édition")
        a_fr = QAction("🔍 Rechercher/Remplacer…", self)
        a_fr.setShortcut("Ctrl+H")
        a_fr.triggered.connect(self._open_findreplace)
        me.addAction(a_fr)

    # ── toolbar ────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = self.addToolBar("Principal")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        # Jeu
        tb.addWidget(QLabel("  🎮 Jeu : "))
        self.combo_game = QComboBox()
        self.combo_game.addItems(list(GAMES.keys()))
        self.combo_game.setToolTip(
            "Choisissez le jeu cible.\n"
            "TotK = Tears of the Kingdom\n"
            "BotW = Breath of the Wild\n"
            "LA_NS = Link's Awakening NS"
        )
        self.combo_game.currentTextChanged.connect(self._change_game)
        tb.addWidget(self.combo_game)
        tb.addSeparator()

        # Dossier
        tb.addWidget(QLabel("  📁 "))
        self.e_root = QLineEdit()
        self.e_root.setMinimumWidth(250)
        self.e_root.setReadOnly(True)
        self.e_root.setPlaceholderText("Chemin du dossier romfs…")
        self.e_root.setToolTip("Dossier de travail actuellement ouvert")
        tb.addWidget(self.e_root)

        btn_folder = QPushButton("📁 Dossier")
        btn_folder.setToolTip("Ouvrir un dossier romfs (Ctrl+O)")
        btn_folder.clicked.connect(self._open_folder)
        tb.addWidget(btn_folder)

        btn_file = QPushButton("📄 Fichier")
        btn_file.setToolTip("Ouvrir un fichier seul (.sarc, .zs, .msbt…) (Ctrl+F)")
        btn_file.clicked.connect(self._open_file)
        tb.addWidget(btn_file)
        tb.addSeparator()

        # Dictionnaire Zstd
        tb.addWidget(QLabel("  🗜 Dict : "))
        self.e_dict = QLineEdit()
        self.e_dict.setMaximumWidth(160)
        self.e_dict.setReadOnly(True)
        self.e_dict.setPlaceholderText("(optionnel)")
        self.e_dict.setToolTip(
            "Dictionnaire Zstd pour TotK.\n"
            "Améliore la décompression des fichiers .zs.\n"
            "Fichier : ZsDic.pack.zs ou similaire."
        )
        tb.addWidget(self.e_dict)
        btn_dict = QPushButton("Charger .dict")
        btn_dict.setToolTip("Charger un dictionnaire Zstd (.dict / .zsdic)")
        btn_dict.clicked.connect(self._load_dict)
        tb.addWidget(btn_dict)
        tb.addSeparator()

        # Export / Import lot
        btn_exp = QPushButton("📤 Export lot")
        btn_exp.setToolTip("Exporter tous les MSBT du dossier en fichiers .txt")
        btn_exp.clicked.connect(self._batch_export)
        tb.addWidget(btn_exp)

        btn_imp = QPushButton("📥 Import lot")
        btn_imp.setToolTip("Réimporter des fichiers .txt modifiés dans les MSBT correspondants")
        btn_imp.clicked.connect(self._batch_import)
        tb.addWidget(btn_imp)

    # ── zone centrale ──────────────────────────────────────────

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # Panneau gauche : arbre + aide contextuelle
        left = QWidget()
        ll   = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        # Petite aide en haut de l'arbre
        hint = QLabel(
            "  📁 Dossier ou 📄 Fichier → double-clic pour ouvrir"
        )
        hint.setStyleSheet(
            "background:#2d2d2d; color:#888; padding:4px 8px; font-size:11px;"
        )
        ll.addWidget(hint)

        self.tree = FileTree()
        self.tree.sig_open_file.connect(self._open_tab_direct)
        self.tree.sig_open_intern.connect(self._open_tab_intern)
        self.tree.setMinimumWidth(200)
        ll.addWidget(self.tree)
        left.setMaximumWidth(420)
        splitter.addWidget(left)

        # Panneau droit : onglets
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.setDocumentMode(True)

        # Page d'accueil dans les onglets
        welcome = QWidget()
        wl      = QVBoxLayout(welcome)
        wl.setAlignment(Qt.AlignCenter)
        wl_lbl  = QLabel(
            "🗡️  Archive Explorer v5.2 – Modding simplifié\n\n"
            "① Sélectionnez votre jeu (TotK / BotW / LA_NS) dans la barre du haut\n"
            "② Ouvrez un dossier romfs  ou  un fichier direct\n"
            "③ Double-cliquez sur un fichier dans l'arbre pour l'éditer\n\n"
            "Formats supportés : .sarc  .zs  .msbt  .txt  .yaml  .json  .png\n\n"
            "Ctrl+F  — recherche rapide dans l'éditeur\n"
            "Ctrl+H  — recherche & remplacement avancé"
        )
        wl_lbl.setAlignment(Qt.AlignCenter)
        wl_lbl.setStyleSheet("color:#888; font-size:13px; line-height:1.8;")
        wl.addWidget(wl_lbl)
        self.tabs.addTab(welcome, "Accueil")

        splitter.addWidget(self.tabs)
        splitter.setSizes([320, 1080])

    # ── ouverture ──────────────────────────────────────────────

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Ouvrir le dossier romfs")
        if path:
            self.e_root.setText(path)
            self.tree.set_root(path)
            self.statusBar().showMessage(f"Dossier ouvert : {path}")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Ouvrir un fichier",
            filter="Tous les fichiers (*);;Archives (*.sarc *.zs);;Messages (*.msbt);;Texte (*.txt)"
        )
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext in ARCHIVE_EXT:
            self.e_root.setText(os.path.dirname(path))
            self.tree.load_single(path)
            self.statusBar().showMessage(f"Archive ouverte : {path}")
        else:
            self._open_tab_direct(path)

    def _open_tab_direct(self, path):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.file_path == path:
                self.tabs.setCurrentIndex(i)
                return
        tab = EditorTab()
        tab.load_direct(path)
        name = os.path.basename(path)
        idx  = self.tabs.addTab(tab, name)
        self.tabs.setTabToolTip(idx, path)
        self.tabs.setCurrentWidget(tab)

    def _open_tab_intern(self, arc_path, internal):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.arc_path == arc_path \
               and tab.arc_int == internal:
                self.tabs.setCurrentIndex(i)
                return
        tab  = EditorTab()
        tab.load_from_archive(arc_path, internal)
        name = os.path.basename(internal)
        idx  = self.tabs.addTab(tab, name)
        self.tabs.setTabToolTip(idx, f"{arc_path}  →  {internal}")
        self.tabs.setCurrentWidget(tab)

    def _close_tab(self, idx):
        tab = self.tabs.widget(idx)
        if isinstance(tab, EditorTab) and tab.is_modified():
            ret = tab.prompt_save()
            if ret == QMessageBox.Save:
                tab._save()
            elif ret == QMessageBox.Cancel:
                return
        self.tabs.removeTab(idx)
        if tab:
            tab.deleteLater()

    def _open_findreplace(self):
        tab = self.tabs.currentWidget()
        if isinstance(tab, EditorTab):
            FindReplaceDialog(tab.editor, self).show()

    # ── jeu / dict ─────────────────────────────────────────────

    def _change_game(self, name):
        global current_game
        if name in GAMES:
            current_game = GAMES[name]
            self.statusBar().showMessage(
                f"Jeu : {GAMES[name].name}  |  {len(GAMES[name].langs)} langues"
            )

    def _load_dict(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Charger un dictionnaire Zstd", '',
            "Dictionnaire Zstd (*.dict *.zsdic *.zstddic);;Tous (*)"
        )
        if path:
            try:
                set_zstd_dict(path)
                self.e_dict.setText(os.path.basename(path))
                self.statusBar().showMessage(f"✅ Dictionnaire Zstd chargé : {path}")
            except Exception as e:
                QMessageBox.critical(self, "Erreur dict", str(e))

    # ── export / import par lot ────────────────────────────────

    def _batch_export(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Erreur",
                "Ouvrez d'abord un dossier avec le bouton 📁 Dossier.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Dossier de destination pour les .txt")
        if not dest:
            return

        prog = QProgressDialog("Export en cours…", "Annuler", 0, 0, self)
        prog.setWindowTitle("Export MSBT → TXT")
        prog.setWindowModality(Qt.WindowModal)
        prog.show()
        done, errs = 0, []

        for dirpath, _, files in os.walk(root):
            if prog.wasCanceled():
                break
            for fname in files:
                if prog.wasCanceled():
                    break
                fpath = os.path.join(dirpath, fname)
                ext   = Path(fname).suffix.lower()
                prog.setLabelText(f"Traitement : {fname}")
                QApplication.processEvents()

                if ext == '.msbt':
                    try:
                        raw = read_file(fpath)
                        if raw[:4] == b'\x28\xB5\x2F\xFD':
                            raw = decompress_zs(raw)
                        msbt = MsbtParser(raw)
                        rel  = os.path.relpath(fpath, root)
                        out  = os.path.join(dest, rel + '.txt')
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with open(out, 'w', encoding='utf-8') as f:
                            f.write(msbt.to_txt())
                        done += 1
                    except Exception as e:
                        errs.append(f"{fname}: {e}")

                elif ext in ('.sarc', '.zs'):
                    try:
                        raw = read_file(fpath)
                        if ext == '.zs':
                            raw = decompress_zs(raw)
                        if raw[:4] != b'SARC':
                            continue
                        sarc = SarcReader(raw)
                        for iname in sarc.list_files():
                            if Path(iname).suffix.lower() != '.msbt':
                                continue
                            try:
                                msbt = MsbtParser(sarc.get_file(iname))
                                rel  = os.path.relpath(fpath, root)
                                out  = os.path.join(dest, rel, iname + '.txt')
                                os.makedirs(os.path.dirname(out), exist_ok=True)
                                with open(out, 'w', encoding='utf-8') as f:
                                    f.write(msbt.to_txt())
                                done += 1
                            except Exception as e:
                                errs.append(f"{iname}: {e}")
                    except Exception as e:
                        errs.append(f"{fname}: {e}")

        prog.close()
        msg = f"✅ Export terminé : {done} fichier(s) exporté(s)."
        if errs:
            msg += f"\n\n⚠ Erreurs ({len(errs)}) :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Export par lot terminé", msg)

    def _batch_import(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Erreur",
                "Ouvrez d'abord un dossier avec le bouton 📁 Dossier.")
            return
        src = QFileDialog.getExistingDirectory(
            self, "Dossier source des fichiers .txt modifiés"
        )
        if not src:
            return

        prog = QProgressDialog("Import en cours…", "Annuler", 0, 0, self)
        prog.setWindowTitle("Import TXT → MSBT")
        prog.setWindowModality(Qt.WindowModal)
        prog.show()
        done, errs = 0, []

        for dirpath, _, files in os.walk(src):
            if prog.wasCanceled():
                break
            for fname in files:
                if prog.wasCanceled():
                    break
                if not fname.endswith('.txt'):
                    continue
                txt_path = os.path.join(dirpath, fname)
                rel_txt  = os.path.relpath(txt_path, src)
                rel_orig = rel_txt[:-4] if rel_txt.endswith('.txt') else rel_txt
                orig     = os.path.join(root, rel_orig)
                prog.setLabelText(f"Traitement : {fname}")
                QApplication.processEvents()

                if os.path.isfile(orig) and orig.lower().endswith('.msbt'):
                    try:
                        raw  = read_file(orig)
                        is_z = raw[:4] == b'\x28\xB5\x2F\xFD'
                        if is_z:
                            raw = decompress_zs(raw)
                        msbt = MsbtParser(raw)
                        with open(txt_path, 'r', encoding='utf-8') as f:
                            msbt.from_txt(f.read())
                        out = msbt.save()
                        if is_z:
                            out = compress_zs(out)
                        with open(orig, 'wb') as f:
                            f.write(out)
                        done += 1
                    except Exception as e:
                        errs.append(f"{rel_orig}: {e}")
                else:
                    parts = Path(rel_orig).parts
                    for i in range(len(parts) - 1, 0, -1):
                        arc_rel  = os.path.join(*parts[:i])
                        int_name = '/'.join(parts[i:])
                        arc_path = os.path.join(root, arc_rel)
                        if os.path.isfile(arc_path) and \
                           Path(arc_path).suffix.lower() in ('.sarc', '.zs'):
                            try:
                                iraw = archive_extract(arc_path, int_name)
                                msbt = MsbtParser(iraw)
                                with open(txt_path, 'r', encoding='utf-8') as f:
                                    msbt.from_txt(f.read())
                                archive_update(arc_path, int_name, msbt.save())
                                done += 1
                            except Exception as e:
                                errs.append(f"{rel_orig}: {e}")
                            break

        prog.close()
        msg = f"✅ Import terminé : {done} fichier(s) mis à jour."
        if errs:
            msg += f"\n\n⚠ Erreurs ({len(errs)}) :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Import par lot terminé", msg)

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())