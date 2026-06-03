#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive Explorer v4
- Arbre de fichiers lazy (dossier OU fichier seul)
- SARC / ZS explorables directement, contenu lisible (pas de hex brut)
- MSBT lisible + éditable + export/import TXT & YAML par lot
- Vue Hex avec offsets + colonne ASCII lisible
- Recherche / remplacement avancé (regex, casse, mot entier)
- Configs : TotK, BotW, LA_NS (Link's Awakening NS)
- Dictionnaire Zstd chargeable
- Onglets multi-fichiers
"""

import sys, os, re, struct, shutil, tempfile, zipfile, tarfile
from pathlib import Path
from io import BytesIO

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTabWidget,
    QTextEdit, QLineEdit, QPushButton, QLabel, QFileDialog, QMessageBox,
    QToolBar, QProgressDialog, QMenu, QHeaderView, QComboBox,
    QAbstractItemView, QAction, QDialog, QCheckBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import (
    QFont, QColor, QTextCharFormat, QSyntaxHighlighter, QTextCursor
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
    def __init__(self, name, lbl1_num_slots=101, hash_mult=0x65,
                 align_bytes=16, language_order=None):
        self.name           = name
        self.lbl1_num_slots = lbl1_num_slots
        self.hash_mult      = hash_mult
        self.align_bytes    = align_bytes
        self.language_order = language_order or []

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
        # essai 1 : décompression directe
        try:
            return dctx.decompress(data)
        except Exception:
            pass
        # essai 2 : avec taille max
        try:
            return dctx.decompress(data, max_output_size=200_000_000)
        except Exception:
            pass
        # essai 3 : stream
        return dctx.stream_reader(BytesIO(data)).read()
    except Exception:
        return data   # retourne l'original si tout échoue

def compress_zs(data):
    cctx = zstd.ZstdCompressor(dict_data=_zstd_dict, level=16) if _zstd_dict \
           else zstd.ZstdCompressor(level=16)
    return cctx.compress(data)

# ═══════════════════════════════════════════════════════════════
#  SARC  (Nintendo Simple ARChive)
# ═══════════════════════════════════════════════════════════════

class SarcReader:
    """
    Lit un SARC Nintendo.
    SFAT header = 4B magic + 4B sfat_size(I) + 4B file_count(I) + 4B hash_mult
    (pas H+H — le Writer et le Reader utilisent tous les deux I+I)
    """
    def __init__(self, data):
        self.data       = data
        self.files      = {}
        self.file_order = []
        self._parse()

    def _parse(self):
        s = BytesIO(self.data)

        # ── SARC header ──
        if s.read(4) != b'SARC':
            raise ValueError("Magic SARC manquant")
        s.read(2)   # header_size (0x14)
        bom = struct.unpack('<H', s.read(2))[0]
        if bom not in (0xFFFE, 0xFEFF):
            raise ValueError(f"BOM SARC invalide : {hex(bom)}")
        s.read(4)   # file_size
        data_offset = struct.unpack('<I', s.read(4))[0]
        s.read(4)   # version + reserved

        # ── SFAT ──
        if s.read(4) != b'SFAT':
            raise ValueError("SFAT manquant")
        s.read(4)   # sfat_size  (I, pas H)
        file_count = struct.unpack('<I', s.read(4))[0]
        if file_count > 65535:
            raise ValueError(f"Nb fichiers suspect : {file_count}")
        s.read(4)   # hash_mult

        entries = []
        for _ in range(file_count):
            s.read(4)   # name_hash
            name_info  = struct.unpack('<I', s.read(4))[0]
            file_start = struct.unpack('<I', s.read(4))[0]
            file_end   = struct.unpack('<I', s.read(4))[0]
            name_off   = (name_info & 0xFFFF) * 4
            entries.append((name_off, file_start, file_end))

        # ── SFNT ──
        if s.read(4) != b'SFNT':
            raise ValueError("SFNT manquant")
        s.read(4)   # sfnt_size (I)
        sfnt_base = s.tell()

        for name_off, file_start, file_end in entries:
            s.seek(sfnt_base + name_off)
            nb = b''
            while True:
                b = s.read(1)
                if not b or b == b'\x00':
                    break
                nb += b
            name = nb.decode('utf-8', errors='replace')
            abs_s = data_offset + file_start
            abs_e = data_offset + file_end
            if 0 <= abs_s <= abs_e <= len(self.data):
                self.files[name] = self.data[abs_s:abs_e]
                self.file_order.append(name)

    def list_files(self):
        return list(self.file_order)

    def get_file(self, name):
        return self.files.get(name, b'')


class SarcWriter:
    """
    Reconstruit un SARC.
    SFAT header écrit en I+I (cohérent avec SarcReader).
    """
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

        # Bloc noms (SFNT)
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

        # Bloc données
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

        fc          = len(self.files)
        sfat_size   = 12 + fc * 16        # I(size) + I(count) + I(hash) + fc*16
        sfnt_size   = 8  + len(name_data) # magic(4) + size(4) + data
        raw_offset  = 0x14 + sfat_size + 4 + sfnt_size  # 4 = SFNT magic
        data_offset = (raw_offset + 0xFF) & ~0xFF        # aligné 0x100
        total_size  = data_offset + len(data_data)

        out = BytesIO()
        # SARC
        out.write(b'SARC')
        out.write(struct.pack('<H', 0x14))    # header_size
        out.write(struct.pack('<H', 0xFFFE))  # BOM LE
        out.write(struct.pack('<I', total_size))
        out.write(struct.pack('<I', data_offset))
        out.write(struct.pack('<H', 0x0100))  # version
        out.write(struct.pack('<H', 0x0000))  # reserved
        # SFAT
        out.write(b'SFAT')
        out.write(struct.pack('<I', sfat_size))  # I, pas H
        out.write(struct.pack('<I', fc))          # I, pas H
        out.write(struct.pack('<I', 0x65))        # hash_mult
        for i, (name, _) in enumerate(self.files):
            s, e = data_positions[i]
            out.write(struct.pack('<I', self._hash(name)))
            out.write(struct.pack('<I', (name_offsets[name] & 0xFFFF) | 0x01000000))
            out.write(struct.pack('<I', s))
            out.write(struct.pack('<I', e))
        # SFNT
        out.write(b'SFNT')
        out.write(struct.pack('<I', sfnt_size))
        out.write(name_data)
        # Padding
        cur = out.tell()
        if cur < data_offset:
            out.write(b'\x00' * (data_offset - cur))
        out.write(data_data)
        return out.getvalue()

# ═══════════════════════════════════════════════════════════════
#  MSBT  (Nintendo MsgStdBn)
# ═══════════════════════════════════════════════════════════════

class MsbtParser:
    MAGIC = b'MsgStdBn'

    def __init__(self, data, game_cfg=None):
        self.raw      = bytes(data)
        self.game     = game_cfg or current_game
        self.labels   = []       # ordre des labels
        self.texts    = {}       # label -> str
        self._parse()

    # ── lecture ────────────────────────────────────────────────

    def _parse(self):
        s = BytesIO(self.raw)
        if s.read(8) != self.MAGIC:
            raise ValueError("Pas un MSBT (magic invalide)")
        bom = s.read(2)
        self.enc = 'utf-16-be' if bom == b'\xFE\xFF' else 'utf-16-le'
        s.read(2)   # unknown
        section_count = struct.unpack('<H', s.read(2))[0]
        s.read(2)   # unknown
        s.read(4)   # file_size
        s.read(10)  # padding → position 0x20

        lbl_map   = {}   # txt2_index -> label
        all_texts = []   # textes dans l'ordre TXT2

        for _ in range(section_count):
            pos   = s.tell()
            align = (self.game.align_bytes - (pos % self.game.align_bytes)) % self.game.align_bytes
            if align:
                s.read(align)
            magic = s.read(4)
            if not magic or len(magic) < 4:
                break
            sec_size  = struct.unpack('<I', s.read(4))[0]
            s.read(8)   # padding
            sec_start = s.tell()

            if magic == b'LBL1':
                lbl_map = self._parse_lbl1(s, sec_start, sec_size)
            elif magic == b'TXT2':
                all_texts = self._parse_txt2(s, sec_start, sec_size)
            # ATR1, NLI1 etc. : ignorées à la lecture
            s.seek(sec_start + sec_size)

        for idx, label in sorted(lbl_map.items()):
            self.labels.append(label)
            self.texts[label] = all_texts[idx] if idx < len(all_texts) else ''

    def _parse_lbl1(self, s, sec_start, sec_size):
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

    def _parse_txt2(self, s, sec_start, sec_size):
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
                if cp == 0x000E:   # tag inline Nintendo
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

    # ── export ─────────────────────────────────────────────────

    def to_txt(self):
        """Format : [label]\\ntexte\\n---"""
        lines = []
        for label in self.labels:
            lines.append(f'[{label}]')
            lines.append(self.texts.get(label, ''))
            lines.append('---')
        return '\n'.join(lines)

    def from_txt(self, txt):
        """Met à jour self.texts depuis le format to_txt()."""
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

    def to_yaml(self):
        if not HAS_YAML:
            return '# pyyaml non installé\n'
        return yaml.dump(
            {label: self.texts.get(label, '') for label in self.labels},
            allow_unicode=True, sort_keys=False
        )

    def from_yaml(self, yaml_str):
        if not HAS_YAML:
            return
        data = yaml.safe_load(yaml_str)
        if not isinstance(data, dict):
            return
        for label, text in data.items():
            if label in self.texts:
                self.texts[label] = str(text) if text is not None else ''

    # ── sauvegarde binaire ─────────────────────────────────────

    def save(self):
        cfg = self.game
        out = BytesIO()

        # Header 0x20
        out.write(self.MAGIC)
        out.write(b'\xFF\xFE')      # BOM LE
        out.write(b'\x00\x00')
        out.write(struct.pack('<H', 2))   # 2 sections : LBL1 + TXT2
        out.write(b'\x00\x00')
        size_pos = out.tell()
        out.write(struct.pack('<I', 0))   # file_size (rempli après)
        out.write(b'\x00' * 10)           # padding → 0x20

        def _align():
            pos = out.tell()
            pad = (cfg.align_bytes - (pos % cfg.align_bytes)) % cfg.align_bytes
            if pad:
                out.write(b'\x00' * pad)

        def _section(magic4, body):
            _align()
            out.write(magic4)
            out.write(struct.pack('<I', len(body)))
            out.write(b'\x00' * 8)
            out.write(body)

        # ── LBL1 ──
        NUM = cfg.lbl1_num_slots
        slots = [[] for _ in range(NUM)]
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

        # ── TXT2 ──
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

        # Remplir file_size
        total = out.tell()
        out.seek(size_pos)
        out.write(struct.pack('<I', total))
        return out.getvalue()

    def _encode_text(self, text):
        out = BytesIO()
        i   = 0
        while i < len(text):
            # tag inline <tag grp=X typ=Y data=ZZZZ>
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
#  ARCHIVE  (list / extract / save-in-place)
# ═══════════════════════════════════════════════════════════════

ARCHIVE_EXT = {'.zip', '.7z', '.tar', '.gz', '.bz2', '.xz', '.sarc', '.zs'}

def _read(path):
    with open(path, 'rb') as f:
        return f.read()

def archive_list(path):
    """Retourne la liste des fichiers internes d'une archive."""
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
            return SarcReader(_read(path)).list_files()
        elif ext == '.zs':
            dec = decompress_zs(_read(path))
            if dec[:4] == b'SARC':
                return SarcReader(dec).list_files()
            return [Path(path).stem]
    except Exception:
        pass
    return []

def archive_extract(arc_path, internal):
    """Extrait et retourne les bytes d'un fichier interne."""
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
        return SarcReader(_read(arc_path)).get_file(internal)
    elif ext == '.zs':
        dec = decompress_zs(_read(arc_path))
        if dec[:4] == b'SARC':
            return SarcReader(dec).get_file(internal)
        return dec
    return b''

def archive_save(arc_path, internal, new_data):
    """Réécrit un fichier interne dans son archive d'origine."""
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
        arc = SarcReader(_read(arc_path))
        w   = SarcWriter()
        for n in arc.list_files():
            w.add_file(n, new_data if n == internal else arc.get_file(n))
        with open(arc_path, 'wb') as f:
            f.write(w.save())
    elif ext == '.zs':
        dec = decompress_zs(_read(arc_path))
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

# ═══════════════════════════════════════════════════════════════
#  DÉCODAGE INTELLIGENT
# ═══════════════════════════════════════════════════════════════

def _is_text(data):
    """Heuristique : binaire ou texte ?"""
    if not data:
        return False
    sample = data[:2048]
    if b'\x00' in sample:
        return False
    ctrl = sum(1 for b in sample if b < 0x20 and b not in (9, 10, 13))
    return (ctrl / len(sample)) < 0.05

def build_hex_view(data, max_bytes=65536):
    """
    Hex view lisible :
    OFFSET      00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F   ASCII
    """
    header = (
        f"{'OFFSET':>10}  "
        f"{'00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F':51}  "
        f"ASCII"
    )
    lines = [header, '─' * len(header)]
    shown = data[:max_bytes]
    for i in range(0, len(shown), 16):
        chunk = shown[i:i+16]
        left  = ' '.join(f'{b:02X}' for b in chunk[:8])
        right = ' '.join(f'{b:02X}' for b in chunk[8:])
        # Colonne ASCII LISIBLE : on garde tous les printables latin/unicode
        asc = ''
        for b in chunk:
            if 32 <= b < 127:
                asc += chr(b)
            else:
                asc += '·'
        lines.append(f'0x{i:08X}  {left:<23}  {right:<23}  {asc}')
    if len(data) > max_bytes:
        lines.append(f'\n… {len(data):,} octets total (affichage limité à {max_bytes:,})')
    return '\n'.join(lines)

def decode_file(raw, hint_ext=''):
    """
    Détecte et décode un fichier.
    Retourne (mode, display_str, raw_decoded, is_zstd, msbt_or_None)
    mode : 'msbt' | 'sarc' | 'text' | 'hex'
    """
    is_z = False

    # ── décompression zstd ──
    if raw[:4] == b'\x28\xB5\x2F\xFD':
        dec = decompress_zs(raw)
        if dec is not raw and dec != raw:
            raw  = dec
            is_z = True

    ext = hint_ext.lower()

    # ── MSBT ──
    if ext == '.msbt' or raw[:8] == b'MsgStdBn':
        try:
            msbt = MsbtParser(raw)
            return 'msbt', msbt.to_txt(), raw, is_z, msbt
        except Exception as e:
            # MSBT corrompu → hex
            return 'hex', f"# Erreur parsing MSBT : {e}\n\n" + build_hex_view(raw), raw, is_z, None

    # ── SARC (afficher la liste des fichiers internes) ──
    if raw[:4] == b'SARC':
        try:
            sarc  = SarcReader(raw)
            files = sarc.list_files()
            lines = [
                f"# Archive SARC — {len(files)} fichier(s)",
                "# Double-cliquez sur un fichier dans l'arbre pour l'ouvrir.",
                "",
            ]
            for f in files:
                size = len(sarc.get_file(f))
                lines.append(f"  {f:<60} {size:>10,} o")
            return 'sarc', '\n'.join(lines), raw, is_z, None
        except Exception:
            pass

    # ── Texte UTF-8 ──
    if _is_text(raw):
        try:
            return 'text', raw.decode('utf-8'), raw, is_z, None
        except Exception:
            pass
        try:
            return 'text', raw.decode('utf-16'), raw, is_z, None
        except Exception:
            pass

    # ── Binaire → Hex ──
    return 'hex', build_hex_view(raw), raw, is_z, None

# ═══════════════════════════════════════════════════════════════
#  HIGHLIGHTERS
# ═══════════════════════════════════════════════════════════════

class HexHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.fmt_off = QTextCharFormat()
        self.fmt_off.setForeground(QColor('#569CD6'))
        self.fmt_off.setFontWeight(QFont.Bold)
        self.fmt_hex = QTextCharFormat()
        self.fmt_hex.setForeground(QColor('#CE9178'))
        self.fmt_asc = QTextCharFormat()
        self.fmt_asc.setForeground(QColor('#4EC9B0'))
        self.fmt_sep = QTextCharFormat()
        self.fmt_sep.setForeground(QColor('#555555'))

    def highlightBlock(self, text):
        if re.match(r'^0x[0-9A-Fa-f]{8}', text):
            self.setFormat(0, 10, self.fmt_off)
            self.setFormat(12, 51, self.fmt_hex)
            if len(text) > 65:
                self.setFormat(65, len(text) - 65, self.fmt_asc)
        elif text.startswith('─') or text.startswith('OFFSET') or text.startswith('#'):
            self.setFormat(0, len(text), self.fmt_sep)


class MsbtHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.fmt_lbl = QTextCharFormat()
        self.fmt_lbl.setForeground(QColor('#DCDCAA'))
        self.fmt_lbl.setFontWeight(QFont.Bold)
        self.fmt_sep = QTextCharFormat()
        self.fmt_sep.setForeground(QColor('#555555'))
        self.fmt_tag = QTextCharFormat()
        self.fmt_tag.setForeground(QColor('#C586C0'))

    def highlightBlock(self, text):
        if text.startswith('[') and text.endswith(']'):
            self.setFormat(0, len(text), self.fmt_lbl)
        elif text == '---':
            self.setFormat(0, len(text), self.fmt_sep)
        else:
            for m in re.finditer(r'</?tag[^>]*>', text):
                self.setFormat(m.start(), m.end() - m.start(), self.fmt_tag)

# ═══════════════════════════════════════════════════════════════
#  DIALOG RECHERCHE / REMPLACEMENT
# ═══════════════════════════════════════════════════════════════

class FindReplaceDialog(QDialog):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor   = editor
        self._matches = []
        self._cur     = -1
        self.setWindowTitle("Recherche & Remplacement")
        self.setMinimumWidth(520)
        self._build()
        self.setStyleSheet("""
            QDialog,QWidget{background:#252526;color:#d4d4d4;}
            QLineEdit{background:#3c3c3c;color:#d4d4d4;border:1px solid #555;padding:3px 6px;}
            QCheckBox{color:#d4d4d4;}
            QPushButton{background:#3a3a3a;color:#d4d4d4;border:1px solid #555;padding:4px 12px;}
            QPushButton:hover{background:#094771;}
        """)

    def _build(self):
        lay = QVBoxLayout(self)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Rechercher :"))
        self.e_find = QLineEdit()
        self.e_find.setPlaceholderText("Texte, mot-clé ou regex…")
        self.e_find.textChanged.connect(self._refresh)
        r1.addWidget(self.e_find)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Remplacer :"))
        self.e_repl = QLineEdit()
        r2.addWidget(self.e_repl)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        self.chk_case  = QCheckBox("Casse exacte")
        self.chk_word  = QCheckBox("Mot entier")
        self.chk_regex = QCheckBox("Regex")
        self.lbl_cnt   = QLabel("0 résultat(s)")
        self.lbl_cnt.setStyleSheet("color:#569CD6;")
        for w in (self.chk_case, self.chk_word, self.chk_regex):
            w.stateChanged.connect(self._refresh)
            r3.addWidget(w)
        r3.addStretch()
        r3.addWidget(self.lbl_cnt)
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        for lbl, fn in [("◀ Préc", self._prev), ("▶ Suiv", self._next),
                         ("Remplacer", self._repl_one),
                         ("Tout remplacer", self._repl_all),
                         ("Fermer", self.close)]:
            b = QPushButton(lbl)
            b.clicked.connect(fn)
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
        cur = self.editor.textCursor()
        cur.select(QTextCursor.Document)
        cur.setCharFormat(QTextCharFormat())
        self.editor.setTextCursor(cur)

        self._matches = []
        rx = self._pattern()
        if not rx:
            self.lbl_cnt.setText("0 résultat(s)")
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
        self.lbl_cnt.setText(f"{len(self._matches)} résultat(s)")
        self._cur = -1

    def _go(self, idx):
        if not self._matches:
            return
        self._cur = idx % len(self._matches)
        s, e = self._matches[self._cur]
        fmt  = QTextCharFormat()
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

    def _repl_one(self):
        c = self.editor.textCursor()
        if c.hasSelection():
            c.insertText(self.e_repl.text())
        self._next()

    def _repl_all(self):
        rx = self._pattern()
        if not rx:
            return
        new = rx.sub(self.e_repl.text(), self.editor.toPlainText())
        self.editor.setPlainText(new)
        self._refresh()

# ═══════════════════════════════════════════════════════════════
#  ONGLET ÉDITEUR
# ═══════════════════════════════════════════════════════════════

class EditorTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.arc_path  = None
        self.arc_int   = None
        self.file_path = None
        self.raw       = b''
        self.mode      = 'hex'
        self.is_zstd   = False
        self.msbt      = None
        self._hl       = None
        self._editing  = False
        self._prev_mode = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Barre info
        self.lbl_info = QLabel("—")
        self.lbl_info.setStyleSheet(
            "background:#252526;color:#888;padding:2px 10px;font-size:11px;"
        )
        lay.addWidget(self.lbl_info)

        # Éditeur principal
        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QTextEdit.NoWrap)
        self.editor.setFont(QFont("Consolas", 10))
        lay.addWidget(self.editor)

        # Barre de recherche rapide Ctrl+F
        self._bar = QWidget()
        blay = QHBoxLayout(self._bar)
        blay.setContentsMargins(4, 2, 4, 2)
        self._e_srch = QLineEdit()
        self._e_srch.setPlaceholderText("Recherche rapide… (Entrée=suivant)")
        self._e_srch.returnPressed.connect(self._srch_next)
        self._btn_sn = QPushButton("↓")
        self._btn_sp = QPushButton("↑")
        self._btn_sc = QPushButton("✕")
        self._btn_sn.clicked.connect(self._srch_next)
        self._btn_sp.clicked.connect(self._srch_prev)
        self._btn_sc.clicked.connect(lambda: self._bar.setVisible(False))
        for w in (self._e_srch, self._btn_sn, self._btn_sp, self._btn_sc):
            blay.addWidget(w)
        self._bar.setVisible(False)
        lay.addWidget(self._bar)

        # Barre de boutons
        bbar = QHBoxLayout()
        bbar.setContentsMargins(4, 3, 4, 3)
        bbar.setSpacing(4)

        self.btn_edit    = QPushButton("✏️ Éditer")
        self.btn_findadv = QPushButton("🔍 Rech/Remp…")
        self.btn_hex     = QPushButton("🔢 Hex")
        self.btn_exp     = QPushButton("📤 Export TXT")
        self.btn_imp     = QPushButton("📥 Import TXT")
        self.btn_save    = QPushButton("💾 Sauver")
        self.btn_saveas  = QPushButton("💾 Sous…")

        self.btn_edit.clicked.connect(self._toggle_edit)
        self.btn_findadv.clicked.connect(self._open_fr)
        self.btn_hex.clicked.connect(self._toggle_hex)
        self.btn_exp.clicked.connect(self._export_txt)
        self.btn_imp.clicked.connect(self._import_txt)
        self.btn_save.clicked.connect(self._save)
        self.btn_saveas.clicked.connect(self._save_as)

        self.btn_save.setEnabled(False)
        self.btn_exp.setEnabled(False)
        self.btn_imp.setEnabled(False)

        for w in (self.btn_edit, self.btn_findadv, self.btn_hex,
                  self.btn_exp, self.btn_imp,
                  self.btn_save, self.btn_saveas):
            bbar.addWidget(w)

        bw = QWidget()
        bw.setLayout(bbar)
        bw.setStyleSheet("background:#252526;border-top:1px solid #333;")
        lay.addWidget(bw)

    # ── chargement ─────────────────────────────────────────────

    def load_direct(self, path):
        self.file_path = path
        self.arc_path  = None
        self.arc_int   = None
        try:
            raw = _read(path)
        except Exception as e:
            self.editor.setPlainText(f"Erreur lecture : {e}")
            return
        self._display(raw, Path(path).suffix)

    def load_from_archive(self, arc_path, internal):
        self.arc_path  = arc_path
        self.arc_int   = internal
        self.file_path = None
        try:
            raw = archive_extract(arc_path, internal)
        except Exception as e:
            self.editor.setPlainText(f"Erreur extraction : {e}")
            return
        self._display(raw, Path(internal).suffix)

    def _display(self, raw, ext=''):
        mode, txt, raw_dec, is_z, msbt = decode_file(raw, ext)
        self.raw       = raw_dec
        self.mode      = mode
        self.is_zstd   = is_z
        self.msbt      = msbt
        self._editing  = False
        self._prev_mode = None

        self.editor.setReadOnly(True)
        self.btn_edit.setText("✏️ Éditer")
        self.btn_save.setEnabled(False)

        # Highlighter
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
        self.btn_exp.setEnabled(is_msbt)
        self.btn_imp.setEnabled(is_msbt)
        # On peut éditer si ce n'est pas un SARC summary
        self.btn_edit.setEnabled(mode in ('msbt', 'text'))

        # Info bar
        name = (self.arc_int or
                (os.path.basename(self.file_path) if self.file_path else '?'))
        mode_str = {'msbt':'MSBT','text':'Texte','hex':'Binaire/Hex','sarc':'SARC (liste)'}.get(mode, mode)
        zinfo    = '  🗜 zstd' if is_z else ''
        self.lbl_info.setText(
            f"  {name}  │  {mode_str}  │  {len(raw_dec):,} o{zinfo}"
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

    def _open_fr(self):
        dlg = FindReplaceDialog(self.editor, self)
        dlg.show()

    def _toggle_hex(self):
        if self.mode == 'hex' and self._prev_mode:
            # retour au mode précédent
            self._display(self.raw,
                Path(self.arc_int or self.file_path or '').suffix)
            self.btn_hex.setText("🔢 Hex")
        else:
            # basculer en hex
            self._prev_mode = self.mode
            if self._hl:
                self._hl.setDocument(None)
            self._hl = HexHighlighter(self.editor.document())
            self.editor.setPlainText(build_hex_view(self.raw))
            self.editor.moveCursor(QTextCursor.Start)
            self.mode = 'hex'
            self.btn_hex.setText("📝 Normal")

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
                archive_save(self.arc_path, self.arc_int, data)
                self._status("✅ Sauvegardé dans l'archive")
            elif self.file_path:
                with open(self.file_path, 'wb') as f:
                    f.write(data)
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
        flags = self.editor.document().FindBackward
        found = self.editor.document().find(txt, cur, flags)
        if not found.isNull():
            self.editor.setTextCursor(found)

    def _status(self, msg):
        win = self.window()
        if hasattr(win, 'statusBar'):
            win.statusBar().showMessage(msg)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F and event.modifiers() == Qt.ControlModifier:
            self._bar.setVisible(True)
            self._e_srch.setFocus()
        else:
            super().keyPressEvent(event)

# ═══════════════════════════════════════════════════════════════
#  ARBRE DE FICHIERS  (lazy loading)
# ═══════════════════════════════════════════════════════════════

class FileTree(QTreeWidget):
    sig_open_file   = pyqtSignal(str)
    sig_open_intern = pyqtSignal(str, str)

    EXT_ICON = {
        '.sarc':'📦', '.zs':'🗜', '.msbt':'📝',
        '.zip':'📦', '.7z':'📦', '.tar':'📦',
        '.txt':'📄', '.yaml':'📋', '.json':'📋', '.xml':'📋',
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

    # ── population ─────────────────────────────────────────────

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
                QTreeWidgetItem(item, ["…", ""])  # placeholder
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
            QTreeWidgetItem(item, ["…", ""])  # placeholder

    def _expand(self, item):
        # Vérifier si c'est un placeholder
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            item.takeChildren()
            kind, path = item.data(0, Qt.UserRole)
            if kind == 'dir':
                self._populate(item, path)
            elif kind == 'file':
                self._load_archive_children(item, path)

    def _load_archive_children(self, parent, arc_path):
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

    # ── interactions ───────────────────────────────────────────

    def _dclick(self, item, _col):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == 'file':
            ext = Path(data[1]).suffix.lower()
            if ext not in ARCHIVE_EXT:
                self.sig_open_file.emit(data[1])
        elif data[0] == 'arc_file':
            _, arc, internal = data
            self.sig_open_intern.emit(arc, internal)

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
            "QMenu{background:#2d2d2d;color:#ccc;}"
            "QMenu::item:selected{background:#094771;}"
        )

        if data[0] == 'file':
            path = data[1]
            ext  = Path(path).suffix.lower()
            if ext not in ARCHIVE_EXT:
                menu.addAction("Ouvrir").triggered.connect(
                    lambda: self.sig_open_file.emit(path))
            menu.addAction("Extraire vers…").triggered.connect(
                lambda: self._extract_file(path))

        elif data[0] == 'arc_file':
            _, arc, internal = data
            menu.addAction("Ouvrir").triggered.connect(
                lambda: self.sig_open_intern.emit(arc, internal))
            menu.addAction("Extraire vers…").triggered.connect(
                lambda: self._extract_internal(arc, internal))
            if Path(internal).suffix.lower() == '.msbt':
                menu.addAction("📤 Export TXT…").triggered.connect(
                    lambda: self._export_msbt(arc, internal))

        # Export lot sur sélection multiple
        msbt_items = [
            i for i in items
            if i.data(0, Qt.UserRole)
            and len(i.data(0, Qt.UserRole)) == 3
            and Path(i.data(0, Qt.UserRole)[2]).suffix.lower() == '.msbt'
        ]
        if len(msbt_items) > 1:
            menu.addAction(f"📤 Exporter {len(msbt_items)} MSBT → TXT…").triggered.connect(
                lambda: self._batch_export(msbt_items))

        menu.exec_(self.viewport().mapToGlobal(pos))

    # ── helpers ────────────────────────────────────────────────

    def _extract_file(self, path):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…",
                                               os.path.basename(path))
        if dest:
            shutil.copy2(path, dest)

    def _extract_internal(self, arc, internal):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…",
                                               os.path.basename(internal))
        if dest:
            try:
                with open(dest, 'wb') as f:
                    f.write(archive_extract(arc, internal))
            except Exception as e:
                QMessageBox.critical(self, "Erreur", str(e))

    def _export_msbt(self, arc, internal):
        try:
            raw  = archive_extract(arc, internal)
            msbt = MsbtParser(raw)
            dest, _ = QFileDialog.getSaveFileName(
                self, "Exporter TXT…",
                Path(internal).stem + '.txt', "*.txt")
            if dest:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(msbt.to_txt())
        except Exception as e:
            QMessageBox.critical(self, "Erreur export", str(e))

    def _batch_export(self, items):
        dest_dir = QFileDialog.getExistingDirectory(self, "Dossier destination")
        if not dest_dir:
            return
        done = 0
        for item in items:
            _, arc, internal = item.data(0, Qt.UserRole)
            try:
                raw  = archive_extract(arc, internal)
                msbt = MsbtParser(raw)
                out  = os.path.join(dest_dir, Path(internal).stem + '.txt')
                with open(out, 'w', encoding='utf-8') as f:
                    f.write(msbt.to_txt())
                done += 1
            except Exception:
                pass
        QMessageBox.information(self, "Export", f"{done} fichier(s) exporté(s).")

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
QMainWindow,QWidget       { background:#1e1e1e; color:#d4d4d4; }
QMenuBar                  { background:#2d2d2d; color:#ccc; }
QMenuBar::item:selected   { background:#094771; }
QMenu                     { background:#2d2d2d; color:#ccc; border:1px solid #555; }
QMenu::item:selected      { background:#094771; }
QToolBar                  { background:#2d2d2d; border:none; padding:3px; spacing:4px; }
QStatusBar                { background:#007ACC; color:#fff; font-size:11px; }
QSplitter::handle         { background:#333; width:3px; }
QTreeWidget               { background:#252526; border:none; color:#d4d4d4; }
QTreeWidget::item         { padding:2px 4px; }
QTreeWidget::item:selected{ background:#094771; color:#fff; }
QTreeWidget::item:hover   { background:#2a2d2e; }
QHeaderView::section      { background:#2d2d2d; color:#888; border:none;
                             border-right:1px solid #3a3a3a; padding:3px 6px; font-size:11px; }
QTextEdit                 { background:#1e1e1e; color:#d4d4d4; border:none;
                             font-family:'Cascadia Code','Consolas',monospace; font-size:11px;
                             selection-background-color:#264F78; }
QLineEdit                 { background:#3c3c3c; color:#d4d4d4; border:1px solid #555;
                             padding:3px 6px; border-radius:3px; }
QLineEdit:focus           { border-color:#007ACC; }
QPushButton               { background:#3a3a3a; color:#d4d4d4; border:1px solid #555;
                             padding:3px 10px; border-radius:3px; }
QPushButton:hover         { background:#094771; border-color:#007ACC; }
QPushButton:pressed       { background:#005a9e; }
QPushButton:disabled      { color:#555; background:#2a2a2a; }
QTabWidget::pane          { border:1px solid #333; }
QTabBar::tab              { background:#2d2d2d; color:#888; padding:5px 14px; border:none; }
QTabBar::tab:selected     { background:#1e1e1e; color:#d4d4d4; border-bottom:2px solid #007ACC; }
QTabBar::tab:hover        { color:#ccc; }
QComboBox                 { background:#3c3c3c; color:#d4d4d4; border:1px solid #555;
                             padding:2px 6px; border-radius:3px; }
QComboBox QAbstractItemView { background:#2d2d2d; color:#d4d4d4;
                               selection-background-color:#094771; }
QScrollBar:vertical       { background:#252526; width:10px; border:none; }
QScrollBar::handle:vertical { background:#424242; border-radius:5px; min-height:20px; }
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical { height:0; }
"""

# ═══════════════════════════════════════════════════════════════
#  FENÊTRE PRINCIPALE
# ═══════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Archive Explorer v4")
        self.setGeometry(80, 80, 1400, 860)
        self.setStyleSheet(STYLE)
        self._build_toolbar()
        self._build_central()
        self.statusBar().showMessage(
            "Prêt — Ouvrir un dossier (Ctrl+O) ou un fichier (Ctrl+F)"
        )

    # ── toolbar ────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = self.addToolBar("Principal")
        tb.setMovable(False)

        tb.addWidget(QLabel(" Jeu : "))
        self.combo_game = QComboBox()
        self.combo_game.addItems(list(GAMES.keys()))
        self.combo_game.currentTextChanged.connect(self._change_game)
        tb.addWidget(self.combo_game)
        tb.addSeparator()

        tb.addWidget(QLabel(" Dossier : "))
        self.e_root = QLineEdit()
        self.e_root.setMinimumWidth(240)
        self.e_root.setReadOnly(True)
        tb.addWidget(self.e_root)

        btn_folder = QPushButton("📁 Dossier")
        btn_folder.clicked.connect(self._open_folder)
        tb.addWidget(btn_folder)

        btn_file = QPushButton("📄 Fichier")
        btn_file.clicked.connect(self._open_file)
        tb.addWidget(btn_file)
        tb.addSeparator()

        tb.addWidget(QLabel(" Dict Zstd : "))
        self.e_dict = QLineEdit()
        self.e_dict.setMaximumWidth(160)
        self.e_dict.setReadOnly(True)
        self.e_dict.setPlaceholderText("(optionnel)")
        tb.addWidget(self.e_dict)
        btn_dict = QPushButton("Charger .dict")
        btn_dict.clicked.connect(self._load_dict)
        tb.addWidget(btn_dict)
        tb.addSeparator()

        btn_exp = QPushButton("📤 Export MSBT→TXT (lot)")
        btn_exp.clicked.connect(self._batch_export)
        tb.addWidget(btn_exp)

        btn_imp = QPushButton("📥 Import TXT→MSBT (lot)")
        btn_imp.clicked.connect(self._batch_import)
        tb.addWidget(btn_imp)

        # Menu
        mb = self.menuBar()
        mf = mb.addMenu("Fichier")
        for label, shortcut, fn in [
            ("Ouvrir dossier…",  "Ctrl+O", self._open_folder),
            ("Ouvrir fichier…",  "Ctrl+F", self._open_file),
        ]:
            a = QAction(label, self)
            a.setShortcut(shortcut)
            a.triggered.connect(fn)
            mf.addAction(a)
        mf.addSeparator()
        aq = QAction("Quitter", self)
        aq.setShortcut("Ctrl+Q")
        aq.triggered.connect(self.close)
        mf.addAction(aq)

    # ── central ────────────────────────────────────────────────

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        self.tree = FileTree()
        self.tree.setMinimumWidth(200)
        self.tree.sig_open_file.connect(self._open_tab_direct)
        self.tree.sig_open_intern.connect(self._open_tab_intern)
        splitter.addWidget(self.tree)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        splitter.addWidget(self.tabs)

        splitter.setSizes([320, 1080])

    # ── ouverture ──────────────────────────────────────────────

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choisir le dossier ROMFS")
        if path:
            self.e_root.setText(path)
            self.tree.set_root(path)
            self.statusBar().showMessage(f"Dossier : {path}")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Ouvrir un fichier", '',
            "Tous fichiers (*);;SARC (*.sarc);;ZS (*.zs);;MSBT (*.msbt)"
        )
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext in ARCHIVE_EXT:
            self.e_root.setText(os.path.dirname(path))
            self.tree.load_single(path)
            self.statusBar().showMessage(f"Archive : {path}")
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
        self.tabs.addTab(tab, os.path.basename(path))
        self.tabs.setCurrentWidget(tab)

    def _open_tab_intern(self, arc_path, internal):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.arc_path == arc_path \
               and tab.arc_int == internal:
                self.tabs.setCurrentIndex(i)
                return
        tab = EditorTab()
        tab.load_from_archive(arc_path, internal)
        self.tabs.addTab(tab, os.path.basename(internal))
        self.tabs.setCurrentWidget(tab)

    def _close_tab(self, idx):
        w = self.tabs.widget(idx)
        self.tabs.removeTab(idx)
        if w:
            w.deleteLater()

    # ── jeu / dict ─────────────────────────────────────────────

    def _change_game(self, name):
        global current_game
        if name in GAMES:
            current_game = GAMES[name]
            self.statusBar().showMessage(f"Config jeu : {GAMES[name].name}")

    def _load_dict(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Dictionnaire Zstd", '', "*.dict *.zsdic *.zstddic"
        )
        if path:
            try:
                set_zstd_dict(path)
                self.e_dict.setText(os.path.basename(path))
                self.statusBar().showMessage(f"Dictionnaire chargé : {path}")
            except Exception as e:
                QMessageBox.critical(self, "Erreur dict", str(e))

    # ── export / import par lot ────────────────────────────────

    def _batch_export(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Erreur", "Ouvrir d'abord un dossier.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Dossier destination TXT")
        if not dest:
            return

        prog = QProgressDialog("Export en cours…", "Annuler", 0, 0, self)
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
                prog.setLabelText(fname)
                QApplication.processEvents()

                if ext == '.msbt':
                    try:
                        raw = _read(fpath)
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
                        raw = _read(fpath)
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
        msg = f"Export terminé : {done} fichier(s)."
        if errs:
            msg += f"\n\nErreurs :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Export par lot", msg)

    def _batch_import(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Erreur", "Ouvrir d'abord un dossier.")
            return
        src = QFileDialog.getExistingDirectory(self, "Dossier source TXT")
        if not src:
            return

        prog = QProgressDialog("Import en cours…", "Annuler", 0, 0, self)
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
                prog.setLabelText(fname)
                QApplication.processEvents()

                if os.path.isfile(orig) and orig.lower().endswith('.msbt'):
                    try:
                        raw  = _read(orig)
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
                        if os.path.isfile(arc_path):
                            if Path(arc_path).suffix.lower() not in ('.sarc', '.zs'):
                                continue
                            try:
                                iraw = archive_extract(arc_path, int_name)
                                msbt = MsbtParser(iraw)
                                with open(txt_path, 'r', encoding='utf-8') as f:
                                    msbt.from_txt(f.read())
                                archive_save(arc_path, int_name, msbt.save())
                                done += 1
                            except Exception as e:
                                errs.append(f"{rel_orig}: {e}")
                            break

        prog.close()
        msg = f"Import terminé : {done} fichier(s) mis à jour."
        if errs:
            msg += f"\n\nErreurs :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Import par lot", msg)

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
