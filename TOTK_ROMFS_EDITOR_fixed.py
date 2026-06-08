#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TOTK ROMFS EDITOR – Pour tous, même les enfants !
"""
import sys, os, io, struct, shutil, tempfile, zipfile, tarfile, re, json
from pathlib import Path
from io import BytesIO

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTabWidget, QTextEdit,
    QLineEdit, QPushButton, QLabel, QFileDialog, QMessageBox,
    QToolBar, QProgressDialog, QMenu, QHeaderView, QComboBox,
    QAbstractItemView, QAction, QDialog, QCheckBox, QScrollArea, QStatusBar
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QTextCharFormat, QSyntaxHighlighter, QTextCursor, QPixmap

# ── Dépendances optionnelles avec messages clairs ──────────────
try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False

try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False
    zstd = None   # Contrôlé au démarrage dans _check_dependencies()

# ═══════════════ CONFIG JEUX ═══════════════
class GameConfig:
    def __init__(self, name, lbl1_slots=101, hash_mult=0x65, align=16, langs=None):
        self.name = name; self.lbl1_slots = lbl1_slots; self.hash_mult = hash_mult
        self.align = align; self.langs = langs or ["USen"]

GAMES = {
    "TotK": GameConfig("Tears of the Kingdom", 101, 0x65, 16, ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh","TWzh"]),
    "BotW": GameConfig("Breath of the Wild", 101, 0x65, 16, ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh"]),
    "LA_NS": GameConfig("Link's Awakening NS", 101, 0x65, 16, ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh","TWzh"]),
}
current_game = GAMES["TotK"]

# ═══════════════ DICTIONNAIRES ═══════════════
# Les dictionnaires Zstd sont chargés automatiquement depuis la romfs
# (fichier ZsDic.pack.zs, généralement dans Pack/).
# Aucun dictionnaire n'est embarqué dans ce code pour des raisons légales.
_dicts = []

def _load_embedded():
    """Pas de dictionnaires embarqués. Retourne False."""
    return False

# Dossiers prioritaires pour les dicts (évite de scanner toute la romfs)
_DICT_SEARCH_DIRS = ['Pack', 'Dict', 'System', '']

def load_all_dicts(romfs_dir):
    """
    Charge les dictionnaires Zstd depuis la romfs.
    Cherche d'abord dans les dossiers prioritaires (Pack/, Dict/, System/, racine),
    puis parcourt l'arborescence complète si rien trouvé.
    """
    global _dicts
    _dicts.clear()
    found_paths = set()

    # Noms et extensions connus pour les fichiers de dictionnaires TotK/BotW
    _DICT_NAME_PATTERNS = [
        lambda f: f.lower().startswith("zsdic"),          # ZsDic.pack.zs
        lambda f: f.lower().endswith(".zsdic"),            # fichiers .zsdic directs
        lambda f: f.lower().endswith(".zstddic"),          # .zstddic
        lambda f: f.lower().endswith(".dict"),             # .dict
        lambda f: "zsdic" in f.lower(),                   # tout nom contenant zsdic
    ]

    def _is_dict_candidate(fname):
        return any(p(fname) for p in _DICT_NAME_PATTERNS)

    # Étape 1 : chercher dans les dossiers prioritaires
    for subdir in _DICT_SEARCH_DIRS:
        candidate_dir = os.path.join(romfs_dir, subdir) if subdir else romfs_dir
        if not os.path.isdir(candidate_dir):
            continue
        for f in os.listdir(candidate_dir):
            if _is_dict_candidate(f):
                found_paths.add(os.path.join(candidate_dir, f))

    # Étape 2 : scan complet si rien trouvé dans les dossiers prioritaires
    if not found_paths:
        for dirpath, _, files in os.walk(romfs_dir):
            for f in files:
                if _is_dict_candidate(f):
                    found_paths.add(os.path.join(dirpath, f))

    # Charger tous les dicts trouvés
    # Chaque fichier peut être : brut, .zs compressé, ou SARC contenant des blobs
    for path in sorted(found_paths):
        try:
            with open(path, 'rb') as fh:
                raw = fh.read()

            # Décompresser si nécessaire
            data = raw
            if raw[:4] == b'\x28\xB5\x2F\xFD':
                try:
                    data = zstd.ZstdDecompressor().decompress(raw)
                except Exception:
                    try:
                        data = zstd.ZstdDecompressor().decompress(raw, max_output_size=50_000_000)
                    except Exception:
                        continue

            # Cas 1 : c'est un SARC → extraire toutes les entrées comme dicts
            if data[:4] == b'SARC':
                try:
                    sarc = _SarcReader(data)
                    for name in sarc.list_files():
                        blob = sarc.get_file(name)
                        # Seuil bas : même les petits dicts sont valides
                        if len(blob) >= 8:
                            try:
                                _dicts.append(zstd.ZstdCompressionDict(blob))
                            except Exception:
                                pass
                except Exception:
                    pass

            # Cas 2 : c'est directement un dictionnaire brut
            elif len(data) >= 8:
                try:
                    _dicts.append(zstd.ZstdCompressionDict(data))
                except Exception:
                    pass

        except Exception:
            pass

def find_dict(data):
    """
    Tente de décompresser data avec les dicts disponibles.
    Retourne les données décompressées, ou None si échec total.
    """
    if not HAS_ZSTD:
        return None
    for d in _dicts:
        try:
            dctx = zstd.ZstdDecompressor(dict_data=d)
            return dctx.decompress(data)
        except Exception:
            continue
    try:
        return zstd.ZstdDecompressor().decompress(data)
    except Exception:
        return None

def find_dict_with_key(data):
    """
    Comme find_dict(), mais retourne (decompressed, dict_used_or_None).
    Permet de recompresser avec le même dictionnaire qu'à l'origine.
    Optimisation : lit l'ID de dict depuis l'en-tête Zstd (frame header)
    pour essayer directement le bon dict quand c'est possible.
    """
    if not HAS_ZSTD:
        return None, None

    # Essayer d'extraire l'ID du dict depuis l'en-tête Zstd
    preferred = _extract_dict_id(data)

    # 1) Essayer le dict préféré en premier (évite de tester tous les dicts)
    if preferred is not None:
        for d in _dicts:
            try:
                if hasattr(d, 'dict_id') and d.dict_id() == preferred:
                    dctx = zstd.ZstdDecompressor(dict_data=d)
                    return dctx.decompress(data), d
            except Exception:
                continue

    # 2) Parcours séquentiel normal
    for d in _dicts:
        try:
            dctx = zstd.ZstdDecompressor(dict_data=d)
            return dctx.decompress(data), d
        except Exception:
            continue

    # 3) Sans dict
    try:
        return zstd.ZstdDecompressor().decompress(data), None
    except Exception:
        return None, None


def _extract_dict_id(data):
    """
    Lit l'ID du dictionnaire depuis l'en-tête d'un flux Zstd.
    Format : magic(4) + frame_header_descriptor(1) + ...
    Retourne l'ID (int) ou None si absent ou illisible.

    Spécification simplifiée du Frame Header Descriptor :
      bit 0   : checksum
      bit 1   : single_segment
      bits 2-3: content_size_flag
      bit 4   : reserved
      bit 5   : dict_id_flag (0=no dict, 1=1B, 2=2B, 3=4B)
    """
    try:
        if len(data) < 6 or data[:4] != b'\x28\xB5\x2F\xFD':
            return None
        fhd = data[4]                    # Frame Header Descriptor
        dict_id_flag = (fhd >> 0) & 0x3  # bits 1-0
        if dict_id_flag == 0:
            return None                  # pas de dict ID dans ce flux
        offset = 5                       # position après FHD
        # Ignorer Window_Descriptor si present (single_segment_flag = bit 5)
        single_segment = (fhd >> 5) & 1
        if not single_segment:
            offset += 1                  # Window_Descriptor = 1 byte
        # Lire l'ID selon sa taille
        if dict_id_flag == 1:
            return data[offset] if offset < len(data) else None
        elif dict_id_flag == 2:
            if offset + 2 > len(data): return None
            return struct.unpack('<H', data[offset:offset+2])[0]
        elif dict_id_flag == 3:
            if offset + 4 > len(data): return None
            return struct.unpack('<I', data[offset:offset+4])[0]
    except Exception:
        pass
    return None

# ═══════════════ SARC ═══════════════
class SarcReader:
    """
    Lecteur SARC unifié — remplace _SarcReader et l'ancien SarcReader.
    Gère :
      - Format SFAT Nintendo (H+H : header_size + file_count en uint16)
      - Format SFAT alternatif (I+I : en uint32, rare)
      - Présence ou absence de SFNT (noms génériques si absent)
      - BOM little-endian et big-endian
    """
    def __init__(self, data, strict_bom=False):
        """
        strict_bom : si True, lève ValueError si le BOM est absent ou invalide.
                     Mettre False pour les dicts (qui peuvent ne pas avoir de BOM valide).
        """
        self.data       = bytes(data)
        self.files      = {}
        self.file_order = []
        self._parse(strict_bom)

    def _parse(self, strict_bom):
        s = BytesIO(self.data)
        if s.read(4) != b'SARC':
            raise ValueError("Magic SARC absent")
        s.read(2)   # header_size
        bom = struct.unpack('<H', s.read(2))[0]
        if strict_bom and bom not in (0xFFFE, 0xFEFF):
            raise ValueError(f"BOM invalide : {hex(bom)}")
        s.read(4)   # file_size
        data_offset = struct.unpack('<I', s.read(4))[0]
        s.read(4)   # version + reserved

        if s.read(4) != b'SFAT':
            raise ValueError("Section SFAT absente")

        # Tenter H+H (Nintendo standard) puis I+I en fallback
        pos = s.tell()
        sfat_hdr   = struct.unpack('<H', s.read(2))[0]
        file_count = struct.unpack('<H', s.read(2))[0]
        if sfat_hdr < 8 or file_count > 65535:
            # Fallback I+I (format alternatif rare)
            s.seek(pos)
            sfat_hdr   = struct.unpack('<I', s.read(4))[0]
            file_count = struct.unpack('<I', s.read(4))[0]
        s.read(4)   # hash_mult

        entries = []   # (name_off_in_sfnt, abs_start, abs_end)
        for i in range(file_count):
            s.read(4)   # name_hash
            name_info  = struct.unpack('<I', s.read(4))[0]
            file_start = struct.unpack('<I', s.read(4))[0]
            file_end   = struct.unpack('<I', s.read(4))[0]
            name_off   = (name_info & 0xFFFF) * 4
            entries.append((name_off, data_offset + file_start, data_offset + file_end))

        magic = s.read(4)
        if magic == b'SFNT':
            # Header SFNT H+H (size + reserved)
            s.read(2); s.read(2)
            sfnt_base = s.tell()
            for i, (name_off, abs_start, abs_end) in enumerate(entries):
                s.seek(sfnt_base + name_off)
                nb = b''
                while True:
                    b = s.read(1)
                    if not b or b == b'\x00':
                        break
                    nb += b
                name = nb.decode('utf-8', errors='replace') if nb else f"file_{i}.bin"
                if 0 <= abs_start <= abs_end <= len(self.data):
                    self.files[name] = self.data[abs_start:abs_end]
                    self.file_order.append(name)
        else:
            # Pas de SFNT : noms génériques (courant pour les dicts)
            for i, (_, abs_start, abs_end) in enumerate(entries):
                if 0 <= abs_start <= abs_end <= len(self.data):
                    name = f"file_{i}.bin"
                    self.files[name] = self.data[abs_start:abs_end]
                    self.file_order.append(name)

    def list_files(self):
        return list(self.file_order)

    def get_file(self, name):
        return self.files.get(name, b'')

# Alias pour compatibilité avec le code existant (chargement des dicts)
_SarcReader = SarcReader

class SarcWriter:
    def __init__(self, hash_mult=None):
        """
        hash_mult : multiplicateur de hash SARC.
        Si None, utilise le hash_mult du current_game (par défaut 0x65 pour TotK/BotW/LA_NS).
        """
        self.entries = []
        self.hash_mult = hash_mult if hash_mult is not None else current_game.hash_mult

    def add_file(self, name, data): self.entries.append((name, data))

    def _hash(self, name, mult=None):
        m = mult if mult is not None else self.hash_mult
        h = 0
        for c in name.encode('utf-8'): h = (h * m + c) & 0xFFFFFFFF
        return h

    def save(self):
        self.entries.sort(key=lambda x: self._hash(x[0]))
        fc = len(self.entries)
        name_offsets = {}; name_block = BytesIO()
        for name, _ in self.entries:
            if name not in name_offsets:
                name_offsets[name] = name_block.tell() // 4
                enc = name.encode('utf-8') + b'\x00'
                name_block.write(enc)
                pad = (4 - (name_block.tell() % 4)) % 4
                if pad: name_block.write(b'\x00' * pad)
        name_data = name_block.getvalue()
        data_positions = []; data_block = BytesIO()
        for _, data in self.entries:
            start = data_block.tell(); data_block.write(data); end = data_block.tell()
            data_positions.append((start, end))
            pad = (4 - (end % 4)) % 4
            if pad: data_block.write(b'\x00' * pad)
        data_data = data_block.getvalue()
        sfat_size = 12 + fc*16; sfnt_size = 8 + len(name_data)
        data_offset = (0x14 + sfat_size + sfnt_size + 0xFF) & ~0xFF
        total_size = data_offset + len(data_data)
        out = BytesIO()
        out.write(b'SARC'); out.write(struct.pack('<H',0x14)); out.write(struct.pack('<H',0xFFFE))
        out.write(struct.pack('<I',total_size)); out.write(struct.pack('<I',data_offset))
        out.write(struct.pack('<H',0x0100)); out.write(struct.pack('<H',0x0000))
        out.write(b'SFAT'); out.write(struct.pack('<H',sfat_size)); out.write(struct.pack('<H',fc))
        out.write(struct.pack('<I', self.hash_mult))  # hash_mult du jeu
        for i, (name, _) in enumerate(self.entries):
            s, e = data_positions[i]
            out.write(struct.pack('<I', self._hash(name)))  # hash avec mult du jeu
            out.write(struct.pack('<I', (name_offsets[name] & 0xFFFF) | 0x01000000))
            out.write(struct.pack('<I', s)); out.write(struct.pack('<I', e))
        out.write(b'SFNT'); out.write(struct.pack('<H',sfnt_size)); out.write(struct.pack('<H',0)); out.write(name_data)
        cur = out.tell()
        if cur < data_offset: out.write(b'\x00' * (data_offset - cur))
        out.write(data_data)
        return out.getvalue()

# ═══════════════ MSBT ═══════════════
class MsbtParser:
    MAGIC = b'MsgStdBn'
    def __init__(self, data, game_cfg=None):
        self.raw = data; self.game = game_cfg or current_game
        self.labels = []; self.texts = {}
        self._parse()
    def _parse(self):
        s = BytesIO(self.raw)
        if s.read(8) != self.MAGIC: raise ValueError("Not MSBT")
        bom = s.read(2); self.enc = 'utf-16-be' if bom == b'\xFE\xFF' else 'utf-16-le'
        s.read(2); section_count = struct.unpack('<H', s.read(2))[0]; s.read(2); s.read(4); s.read(10)
        lbl_map = {}; all_texts = []   # réaffecté par _read_txt2 si section TXT2 présente
        for _ in range(section_count):
            pos = s.tell()
            align = (self.game.align - (pos % self.game.align)) % self.game.align
            if align: s.read(align)
            magic = s.read(4)
            if not magic: break
            sec_size = struct.unpack('<I', s.read(4))[0]; s.read(8); sec_start = s.tell()
            if magic == b'LBL1': lbl_map = self._read_lbl1(s, sec_start, sec_size)
            elif magic == b'TXT2': all_texts = self._read_txt2(s, sec_start, sec_size)
            s.seek(sec_start + sec_size)
        for idx, label in sorted(lbl_map.items()):
            self.labels.append(label)
            self.texts[label] = all_texts[idx] if idx < len(all_texts) else ''
    def _read_lbl1(self, s, sec_start, sec_size):
        num_slots = struct.unpack('<I', s.read(4))[0]
        if num_slots > 10000: return {}
        slots = []
        for _ in range(num_slots):
            count = struct.unpack('<I', s.read(4))[0]; offset = struct.unpack('<I', s.read(4))[0]
            slots.append((count, offset))
        base     = sec_start + 4 + num_slots * 8
        sec_end  = sec_start + sec_size    # borne max de la section LBL1
        result   = {}
        for count, offset in slots:
            if count == 0:
                continue
            abs_offset = base + offset
            # Vérification de plage : ignorer les offsets hors section
            if abs_offset < sec_start or abs_offset >= sec_end:
                continue
            s.seek(abs_offset)
            for _ in range(count):
                # Vérifier qu'il reste de la place pour lire
                if s.tell() >= sec_end:
                    break
                llen = struct.unpack('B', s.read(1))[0]
                if llen == 0 or s.tell() + llen + 4 > sec_end:
                    break
                label = s.read(llen).decode('utf-8', errors='replace')
                idx   = struct.unpack('<I', s.read(4))[0]
                result[idx] = label
        return result
    def _read_txt2(self, s, sec_start, sec_size):
        num = struct.unpack('<I', s.read(4))[0]
        if num > 100000: return []
        offsets = [struct.unpack('<I', s.read(4))[0] for _ in range(num)]
        base = sec_start + 4 + num*4
        texts = []
        for off in offsets:
            s.seek(base + off); chars = []
            while True:
                raw2 = s.read(2)
                if len(raw2) < 2: break
                cp = struct.unpack('<H', raw2)[0]
                if cp == 0: break
                if cp == 0x000E:
                    grp = struct.unpack('<H', s.read(2))[0]; typ = struct.unpack('<H', s.read(2))[0]
                    dsz = struct.unpack('<H', s.read(2))[0]; dat = s.read(dsz)
                    chars.append(f'<tag grp={grp} typ={typ} data={dat.hex()}>')
                elif cp == 0x000F: chars.append('</tag>')
                elif 0xD800 <= cp <= 0xDBFF:
                    # Surrogate haute : lire la surrogate basse
                    raw2b = s.read(2)
                    if len(raw2b) == 2:
                        cp2 = struct.unpack('<H', raw2b)[0]
                        if 0xDC00 <= cp2 <= 0xDFFF:
                            # Paire de surrogates → codepoint réel
                            full_cp = 0x10000 + ((cp - 0xD800) << 10) + (cp2 - 0xDC00)
                            chars.append(chr(full_cp))
                        else:
                            chars.append(f'<U+{cp:04X}><U+{cp2:04X}>')
                    else:
                        chars.append(f'<U+{cp:04X}>')
                else:
                    try: chars.append(chr(cp))
                    except: chars.append(f'<U+{cp:04X}>')
            texts.append(''.join(chars))
        return texts
    def to_txt(self):
        lines = []
        for label in self.labels:
            lines.append(f'[{label}]'); lines.append(self.texts.get(label, '')); lines.append('---')
        return '\n'.join(lines)
    def from_txt(self, txt):
        current = None; buf = []
        for line in txt.splitlines():
            if line.startswith('[') and line.endswith(']') and len(line) > 2:
                if current is not None and current in self.texts: self.texts[current] = '\n'.join(buf)
                current = line[1:-1]; buf = []
            elif line == '---':
                if current is not None and current in self.texts: self.texts[current] = '\n'.join(buf)
                current = None; buf = []
            else:
                if current is not None: buf.append(line)
        if current is not None and current in self.texts: self.texts[current] = '\n'.join(buf)
    def save(self):
        cfg = self.game; out = BytesIO()
        out.write(self.MAGIC); out.write(b'\xFF\xFE'); out.write(b'\x00\x00')
        out.write(struct.pack('<H',2)); out.write(b'\x00\x00')
        size_pos = out.tell(); out.write(struct.pack('<I',0)); out.write(b'\x00'*10)
        def _align():
            pos = out.tell(); pad = (cfg.align - (pos % cfg.align)) % cfg.align
            if pad: out.write(b'\x00'*pad)
        def _section(magic, body):
            _align(); out.write(magic); out.write(struct.pack('<I', len(body)))
            out.write(b'\x00'*8); out.write(body)
        NUM = cfg.lbl1_slots; slots = [[] for _ in range(NUM)]
        for idx, label in enumerate(self.labels):
            h = 0
            for c in label.encode('utf-8'): h = (h*cfg.hash_mult + c) & 0xFFFFFFFF
            slots[h % NUM].append((label, idx))
        lbl_body = BytesIO(); lbl_block = BytesIO()
        lbl_body.write(struct.pack('<I', NUM))
        for slot in slots:
            lbl_body.write(struct.pack('<I', len(slot)))
            lbl_body.write(struct.pack('<I', lbl_block.tell()))
            for label, idx in slot:
                enc = label.encode('utf-8')
                lbl_block.write(struct.pack('B', len(enc))); lbl_block.write(enc)
                lbl_block.write(struct.pack('<I', idx))
        lbl_body.write(lbl_block.getvalue())
        _section(b'LBL1', lbl_body.getvalue())
        strings = [self._encode_text(self.texts.get(lbl,'')) for lbl in self.labels]
        txt_body = BytesIO(); txt_body.write(struct.pack('<I', len(strings)))
        cur_off = 0
        for enc in strings:
            txt_body.write(struct.pack('<I', cur_off)); cur_off += len(enc)
        for enc in strings: txt_body.write(enc)
        _section(b'TXT2', txt_body.getvalue())
        total = out.tell(); out.seek(size_pos); out.write(struct.pack('<I', total))
        return out.getvalue()
    def _encode_text(self, text):
        """
        Encode le texte en UTF-16-LE avec gestion :
        - des tags inline Nintendo <tag grp=X typ=Y data=HH>
        - des balises de fermeture </tag>
        - des caractères Unicode hors BMP (U+10000 → surrogates UTF-16)
        """
        out = BytesIO(); i = 0
        while i < len(text):
            if text[i:i+5] == '<tag ':
                end = text.find('>', i)
                if end != -1:
                    try:
                        parts = {}
                        for tok in text[i+5:end].split():
                            k,v = tok.split('=',1); parts[k] = v
                        grp = int(parts.get('grp','0')); typ = int(parts.get('typ','0'))
                        dat = bytes.fromhex(parts.get('data',''))
                        out.write(struct.pack('<H',0x000E)); out.write(struct.pack('<H',grp))
                        out.write(struct.pack('<H',typ)); out.write(struct.pack('<H',len(dat)))
                        out.write(dat)
                    except: pass
                    i = end + 1; continue
            if text[i:i+6] == '</tag>': out.write(struct.pack('<H',0x000F)); i += 6; continue
            cp = ord(text[i])
            if cp > 0xFFFF:
                # Caractère hors BMP → paire de surrogates UTF-16
                cp  -= 0x10000
                high = 0xD800 + (cp >> 10)
                low  = 0xDC00 + (cp & 0x3FF)
                out.write(struct.pack('<H', high))
                out.write(struct.pack('<H', low))
            else:
                out.write(struct.pack('<H', cp))
            i += 1
        out.write(b'\x00\x00'); return out.getvalue()

# ═══════════════ BYML ═══════════════
class Byml:
    @staticmethod
    def parse(data):
        s = BytesIO(data)
        if s.read(2) != b'BY': raise ValueError("Not BYML")
        version = s.read(1)[0]; s.read(1)
        endian = '<' if version == 2 else '>'
        return Byml._read_node(s, endian)
    @staticmethod
    def _read_node(s, endian):
        node_type = s.read(1)[0]
        if node_type == 0x07: return Byml._read_dict(s, endian)
        elif node_type == 0x06: return Byml._read_array(s, endian)
        elif node_type == 0x00:
            length = struct.unpack(endian + 'I', s.read(4))[0]
            return s.read(length).decode('utf-8', errors='replace')
        elif node_type == 0x02: return struct.unpack(endian + 'I', s.read(4))[0]
        elif node_type == 0x03: return struct.unpack(endian + 'i', s.read(4))[0]
        elif node_type == 0x04: return struct.unpack(endian + 'f', s.read(4))[0]
        elif node_type == 0x05: return struct.unpack(endian + 'd', s.read(8))[0]
        else: return None
    @staticmethod
    def _read_dict(s, endian):
        num = struct.unpack(endian + 'I', s.read(4))[0]; d = {}
        for _ in range(num):
            key = Byml._read_node(s, endian); val = Byml._read_node(s, endian); d[key] = val
        return d
    @staticmethod
    def _read_array(s, endian):
        num = struct.unpack(endian + 'I', s.read(4))[0]; arr = []
        for _ in range(num): arr.append(Byml._read_node(s, endian))
        return arr
    @staticmethod
    def to_json(obj): return json.dumps(obj, indent=2, ensure_ascii=False)
    @staticmethod
    def from_json(json_str): return json.loads(json_str)
    @staticmethod
    def serialize(obj, version=2):
        endian = '<' if version == 2 else '>'; out = BytesIO()
        out.write(b'BY'); out.write(struct.pack('B', version)); out.write(b'\x00')
        Byml._write_node(out, obj, endian); return out.getvalue()
    @staticmethod
    def _write_node(out, obj, endian):
        if isinstance(obj, dict):
            out.write(b'\x07'); Byml._write_dict(out, obj, endian)
        elif isinstance(obj, list):
            out.write(b'\x06'); Byml._write_array(out, obj, endian)
        elif isinstance(obj, str):
            out.write(b'\x00'); enc = obj.encode('utf-8')
            out.write(struct.pack(endian + 'I', len(enc))); out.write(enc)
        elif isinstance(obj, int):
            if obj >= 0: out.write(b'\x02'); out.write(struct.pack(endian + 'I', obj))
            else: out.write(b'\x03'); out.write(struct.pack(endian + 'i', obj))
        elif isinstance(obj, float):
            out.write(b'\x05'); out.write(struct.pack(endian + 'd', obj))
        else: out.write(b'\x00'); out.write(struct.pack(endian + 'I', 0))
    @staticmethod
    def _write_dict(out, d, endian):
        out.write(struct.pack(endian + 'I', len(d)))
        for key, val in d.items():
            Byml._write_node(out, key, endian); Byml._write_node(out, val, endian)
    @staticmethod
    def _write_array(out, arr, endian):
        out.write(struct.pack(endian + 'I', len(arr)))
        for item in arr: Byml._write_node(out, item, endian)

# ═══════════════ ARCHIVES ═══════════════
ARCHIVE_EXT = {'.zip','.7z','.tar','.gz','.bz2','.xz','.sarc','.zs','.pack'}

def read_file(path):
    with open(path, 'rb') as f: return f.read()

def archive_list(path):
    ext = Path(path).suffix.lower()
    try:
        if ext == '.zip':
            with zipfile.ZipFile(path) as z: return [i.filename for i in z.infolist() if not i.is_dir()]
        elif ext == '.7z':
            if not HAS_PY7ZR: raise ImportError('py7zr requis : pip install py7zr')
            with py7zr.SevenZipFile(path, 'r') as sz: return sz.getnames()
        elif ext in ('.tar','.gz','.bz2','.xz'):
            with tarfile.open(path) as t: return [m.name for m in t.getmembers() if m.isfile()]
        elif ext == '.sarc': return SarcReader(read_file(path)).list_files()
        elif ext in ('.zs','.pack'):
            raw = read_file(path); dec = find_dict(raw)
            if dec is None: return [Path(path).stem]
            if dec[:4] == b'SARC': return SarcReader(dec).list_files()
            return [Path(path).stem]
    except: return []
    return []

def archive_extract(arc_path, internal):
    ext = Path(arc_path).suffix.lower()
    if ext == '.zip':
        with zipfile.ZipFile(arc_path) as z: return z.read(internal)
    elif ext == '.7z':
        with py7zr.SevenZipFile(arc_path, 'r') as sz: return sz.read([internal])[internal].read()
    elif ext in ('.tar','.gz','.bz2','.xz'):
        with tarfile.open(arc_path) as t: return t.extractfile(t.getmember(internal)).read()
    elif ext == '.sarc': return SarcReader(read_file(arc_path)).get_file(internal)
    elif ext in ('.zs','.pack'):
        raw = read_file(arc_path); dec = find_dict(raw)
        if dec is None: return b''
        if dec[:4] == b'SARC': return SarcReader(dec).get_file(internal)
        return dec
    return b''

def _best_compressor():
    """Retourne un ZstdCompressor avec le meilleur dict disponible."""
    if not HAS_ZSTD:
        raise RuntimeError("zstandard n'est pas installé. Installez-le avec : pip install zstandard")
    if _dicts:
        return zstd.ZstdCompressor(dict_data=_dicts[0], level=16)
    return zstd.ZstdCompressor(level=16)

# Formats supportés en écriture
WRITABLE_ARCHIVE_EXT = {'.zip', '.sarc', '.zs', '.pack'}

def archive_update(arc_path, internal, new_data):
    """
    Met à jour un fichier interne dans une archive.
    Lève NotImplementedError pour les formats non supportés en écriture (.7z, .tar…).
    """
    ext = Path(arc_path).suffix.lower()
    if ext not in WRITABLE_ARCHIVE_EXT and ext in {'.7z','.tar','.gz','.bz2','.xz'}:
        raise NotImplementedError(
            f"La modification d'archives {ext.upper()} n'est pas supportée.\n"
            f"Extrayez d'abord le fichier, modifiez-le, puis recréez l'archive manuellement."
        )
    if ext == '.zip':
        # Réécriture avec zipfile natif : conserve la structure interne,
        # les métadonnées et évite les problèmes de renommage.
        backup = arc_path + '.bak'
        tmp_out = arc_path + '.tmp'
        try:
            # 1) Sauvegarde atomique avant toute modification
            shutil.copy2(arc_path, backup)

            # 2) Lire le ZIP original et réécrire dans un fichier temporaire
            with zipfile.ZipFile(arc_path, 'r') as z_in, \
                 zipfile.ZipFile(tmp_out, 'w', zipfile.ZIP_DEFLATED) as z_out:
                for item in z_in.infolist():
                    if item.filename == internal:
                        # Écrire la nouvelle version
                        z_out.writestr(item, new_data)
                    else:
                        # Copier les autres entrées telles quelles (métadonnées préservées)
                        z_out.writestr(item, z_in.read(item.filename))

                # Si le fichier interne n'existait pas encore, l'ajouter
                existing = {i.filename for i in z_in.infolist()}
                if internal not in existing:
                    z_out.writestr(internal, new_data)

            # 3) Remplacer atomiquement l'original
            os.replace(tmp_out, arc_path)

            # 4) Supprimer le backup si tout s'est bien passé
            if os.path.exists(backup):
                os.remove(backup)

        except Exception:
            # Restaurer le backup si l'original a été écrasé
            if os.path.exists(backup):
                if not os.path.exists(arc_path):
                    shutil.copy2(backup, arc_path)
                try:
                    os.remove(backup)
                except Exception:
                    pass
            # Nettoyer le fichier temporaire
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except Exception:
                    pass
            raise
    elif ext == '.sarc':
        arc = SarcReader(read_file(arc_path)); w = SarcWriter()
        for n in arc.list_files():
            w.add_file(n, new_data if n == internal else arc.get_file(n))
        with open(arc_path, 'wb') as f: f.write(w.save())
    elif ext in ('.zs','.pack'):
        raw = read_file(arc_path)
        # FIX 7 : mémoriser le dict utilisé pour recompresser avec le même
        dec, used_dict = find_dict_with_key(raw)
        if dec is None:
            return
        def _compressor_for(d):
            if d is not None:
                return zstd.ZstdCompressor(dict_data=d, level=16)
            return _best_compressor()
        if dec[:4] == b'SARC':
            arc = SarcReader(dec); w = SarcWriter()
            for n in arc.list_files():
                w.add_file(n, new_data if n == internal else arc.get_file(n))
            with open(arc_path, 'wb') as f:
                f.write(_compressor_for(used_dict).compress(w.save()))
        else:
            with open(arc_path, 'wb') as f:
                f.write(_compressor_for(used_dict).compress(new_data))

# ═══════════════ DÉCODAGE ═══════════════
def is_text(data):
    if not data: return False
    sample = data[:2048]
    if b'\x00' in sample: return False
    ctrl = sum(1 for b in sample if b < 0x20 and b not in (9,10,13))
    return (ctrl / len(sample)) < 0.05

def build_hex_view(data, max_bytes=65536):
    lines = [f"{'OFFSET':>10}  {'00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F':49}  ASCII"]
    lines.append('─' * len(lines[0]))
    shown = data[:max_bytes]
    for i in range(0, len(shown), 16):
        chunk = shown[i:i+16]; left = ' '.join(f'{b:02X}' for b in chunk[:8])
        right = ' '.join(f'{b:02X}' for b in chunk[8:])
        asc = ''.join(chr(b) if 32 <= b < 127 else '·' for b in chunk)
        lines.append(f'0x{i:08X}  {left:<23}  {right:<23}  {asc}')
    if len(data) > max_bytes: lines.append(f'\n… {len(data):,} octets total')
    return '\n'.join(lines)

def decode_file(raw, hint_ext=''):
    """
    Décode un fichier brut.
    Retourne (mode, display_str, raw_decoded, is_zstd, msbt_or_None, dict_used_or_None).
    dict_used_or_None : le ZstdCompressionDict utilisé pour décompresser,
                        utile pour recompresser avec le même dict.
    """
    is_z      = False
    used_dict = None
    if raw[:4] == b'\x28\xB5\x2F\xFD':
        dec, used_dict = find_dict_with_key(raw)
        if dec is not None:
            raw  = dec
            is_z = True
    ext = hint_ext.lower()
    if ext == '.msbt' or raw[:8] == b'MsgStdBn':
        try:
            _msbt = MsbtParser(raw)
            return 'msbt', _msbt.to_txt(), raw, is_z, _msbt, used_dict
        except: pass
    if ext == '.byml' or (raw[:2] == b'BY' and len(raw) > 3 and raw[2] in (1,2)):
        try: return 'byml', Byml.to_json(Byml.parse(raw)), raw, is_z, None, used_dict
        except: pass
    if raw[:4] == b'SARC':
        try:
            sarc  = SarcReader(raw)
            files = sarc.list_files()
            lines = [f"# Archive SARC — {len(files)} fichier(s)", ""]
            for f in files:
                lines.append(f"  {f:<60}  {len(sarc.get_file(f)):>10,} o")
            return 'sarc', '\n'.join(lines), raw, is_z, None, used_dict
        except: pass
    if is_text(raw):
        try: return 'text', raw.decode('utf-8'), raw, is_z, None, used_dict
        except: pass
        try: return 'text', raw.decode('utf-16'), raw, is_z, None, used_dict
        except: pass
    return 'hex', build_hex_view(raw), raw, is_z, None, used_dict

# ═══════════════ HIGHLIGHTERS ═══════════════
class HexHighlighter(QSyntaxHighlighter):
    def __init__(self, doc): super().__init__(doc)
    def highlightBlock(self, text):
        fmt_off = QTextCharFormat(); fmt_off.setForeground(QColor('#569CD6'))
        fmt_hex = QTextCharFormat(); fmt_hex.setForeground(QColor('#CE9178'))
        fmt_asc = QTextCharFormat(); fmt_asc.setForeground(QColor('#4EC9B0'))
        if re.match(r'^0x[0-9A-Fa-f]{8}', text):
            self.setFormat(0, 10, fmt_off); self.setFormat(12, 49, fmt_hex)
            if len(text) > 63: self.setFormat(63, len(text)-63, fmt_asc)

class MsbtHighlighter(QSyntaxHighlighter):
    def __init__(self, doc): super().__init__(doc)
    def highlightBlock(self, text):
        fmt_lbl = QTextCharFormat(); fmt_lbl.setForeground(QColor('#DCDCAA')); fmt_lbl.setFontWeight(QFont.Bold)
        fmt_sep = QTextCharFormat(); fmt_sep.setForeground(QColor('#555'))
        fmt_tag = QTextCharFormat(); fmt_tag.setForeground(QColor('#C586C0'))
        if text.startswith('[') and text.endswith(']'): self.setFormat(0, len(text), fmt_lbl)
        elif text == '---': self.setFormat(0, len(text), fmt_sep)
        else:
            for m in re.finditer(r'</?tag[^>]*>', text):
                self.setFormat(m.start(), m.end()-m.start(), fmt_tag)

# ═══════════════ RECHERCHE ═══════════════
class FindReplaceDialog(QDialog):
    """
    Dialogue recherche & remplacement robuste.
    - Utilise QTextDocument.find() pour la navigation (compatible ReadOnly)
    - ExtraSelections pour la surbrillance (ne détruit pas la coloration syntaxique)
    - _cur séparé du refresh pour une navigation correcte
    """
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor   = editor
        self._matches = []   # liste de (start, end) en position caractère
        self._cur     = -1
        self.setWindowTitle("Recherche & Remplacement")
        self.setMinimumWidth(520)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._build()
        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog  { background:#f5f5f5; }
            QLabel   { color:#333; }
            QLineEdit{ background:#fff; border:1px solid #bbb; padding:4px 8px; border-radius:3px; }
            QCheckBox{ color:#333; }
            QPushButton{ background:#e0e0e0; border:1px solid #bbb; padding:5px 12px; border-radius:3px; }
            QPushButton:hover{ background:#1976D2; color:#fff; }
        """)

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Rechercher :"))
        self.e_find = QLineEdit()
        self.e_find.setPlaceholderText("Texte, mot-clé ou regex…")
        self.e_find.textChanged.connect(self._on_pattern_changed)
        r1.addWidget(self.e_find)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Remplacer :"))
        self.e_repl = QLineEdit()
        self.e_repl.setPlaceholderText("Texte de remplacement…")
        r2.addWidget(self.e_repl)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        self.chk_case  = QCheckBox("Casse exacte")
        self.chk_word  = QCheckBox("Mot entier")
        self.chk_regex = QCheckBox("Regex")
        self.lbl_count = QLabel("0 résultat(s)")
        self.lbl_count.setStyleSheet("color:#1976D2; font-weight:bold; min-width:100px;")
        for w in (self.chk_case, self.chk_word, self.chk_regex):
            w.stateChanged.connect(self._on_pattern_changed)
            r3.addWidget(w)
        r3.addStretch()
        r3.addWidget(self.lbl_count)
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        for lbl, slot in [
            ("◀ Préc",        self._prev),
            ("▶ Suiv",        self._next),
            ("Remplacer",     self._replace_one),
            ("Tout remplacer",self._replace_all),
            ("Fermer",        self.close),
        ]:
            b = QPushButton(lbl)
            b.clicked.connect(slot)
            r4.addWidget(b)
        lay.addLayout(r4)

    # ── Construction du pattern ────────────────────────────────
    def _build_pattern(self):
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

    # ── Surbrillance via ExtraSelections (non destructif) ──────
    # Limite de caractères pour la surbrillance (évite les lenteurs sur gros fichiers)
    _HIGHLIGHT_LIMIT = 500_000   # ~500 Ko de texte

    def _highlight_all(self):
        """
        Met en surbrillance tous les résultats via setExtraSelections().
        Cette méthode ne touche PAS à la colorisation syntaxique.
        Pour les fichiers > _HIGHLIGHT_LIMIT caractères, la surbrillance
        est désactivée et un message d'information est affiché.
        """
        self._matches.clear()
        rx = self._build_pattern()

        if not rx:
            self.editor.setExtraSelections([])
            self.lbl_count.setText("0 résultat(s)")
            return

        text = self.editor.toPlainText()

        # Limite de performance : pas de surbrillance sur les très gros textes
        if len(text) > self._HIGHLIGHT_LIMIT:
            for m in rx.finditer(text):
                self._matches.append((m.start(), m.end()))
            self.lbl_count.setText(
                f"{len(self._matches)} résultat(s) "
                f"(surbrillance désactivée — fichier > {self._HIGHLIGHT_LIMIT//1000} Ko)"
            )
            # Naviguer quand même avec _highlight_current
            return

        selections = []
        for m in rx.finditer(text):
            self._matches.append((m.start(), m.end()))
            sel = self.editor.ExtraSelection()
            sel.format.setBackground(QColor('#FFF176'))   # jaune pâle
            sel.format.setForeground(QColor('#000000'))
            cur = self.editor.textCursor()
            cur.setPosition(m.start())
            cur.setPosition(m.end(), QTextCursor.KeepAnchor)
            sel.cursor = cur
            selections.append(sel)

        self.editor.setExtraSelections(selections)
        self.lbl_count.setText(f"{len(self._matches)} résultat(s)")

    def _highlight_current(self, idx):
        """Met en vert le résultat courant, en jaune les autres."""
        if not self._matches:
            return
        self._cur  = idx % len(self._matches)
        text       = self.editor.toPlainText()
        selections = []

        for i, (s, e) in enumerate(self._matches):
            sel = self.editor.ExtraSelection()
            if i == self._cur:
                sel.format.setBackground(QColor('#4CAF50'))  # vert = courant
                sel.format.setForeground(QColor('#ffffff'))
            else:
                sel.format.setBackground(QColor('#FFF176'))  # jaune = autres
                sel.format.setForeground(QColor('#000000'))
            cur = self.editor.textCursor()
            cur.setPosition(s)
            cur.setPosition(e, QTextCursor.KeepAnchor)
            sel.cursor = cur
            selections.append(sel)

        self.editor.setExtraSelections(selections)

        # Déplacer le curseur visible sur le résultat courant
        s, e = self._matches[self._cur]
        cur  = self.editor.textCursor()
        cur.setPosition(s)
        cur.setPosition(e, QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cur)
        self.editor.ensureCursorVisible()

    # ── Slots de contrôle ──────────────────────────────────────
    def _on_pattern_changed(self):
        """Appelé quand le texte ou les options changent : recalcule tout."""
        self._highlight_all()
        self._cur = -1

    def _next(self):
        if not self._matches:
            self._highlight_all()
        if not self._matches:
            return
        self._highlight_current(self._cur + 1)

    def _prev(self):
        if not self._matches:
            self._highlight_all()
        if not self._matches:
            return
        self._highlight_current(self._cur - 1)

    def _replace_one(self):
        """Remplace le résultat courant puis avance au suivant."""
        if not self._matches or self._cur < 0:
            self._next()
            return
        if self.editor.isReadOnly():
            QMessageBox.information(self, "Info",
                "Activez d'abord le mode édition (bouton ✏️ Éditer).")
            return
        s, e   = self._matches[self._cur]
        repl   = self.e_repl.text()
        delta  = len(repl) - (e - s)   # différence de longueur après remplacement
        cursor = self.editor.textCursor()
        cursor.setPosition(s)
        cursor.setPosition(e, QTextCursor.KeepAnchor)
        cursor.insertText(repl)
        # Mettre à jour les positions des résultats suivants sans tout recalculer
        new_matches = []
        for i, (ms, me) in enumerate(self._matches):
            if i < self._cur:
                new_matches.append((ms, me))          # avant : inchangé
            elif i == self._cur:
                pass                                   # supprimé (remplacé)
            else:
                new_matches.append((ms + delta, me + delta))  # après : décalé
        self._matches = new_matches
        # Avancer au résultat suivant (ou rester sur le dernier si fin de liste)
        next_idx = min(self._cur, len(self._matches) - 1)
        if self._matches:
            self._highlight_current(next_idx)
        else:
            self.editor.setExtraSelections([])
            self.lbl_count.setText("0 résultat(s)")

    def _replace_all(self):
        """Remplace toutes les occurrences."""
        rx = self._build_pattern()
        if not rx:
            return
        if self.editor.isReadOnly():
            QMessageBox.information(self, "Info",
                "Activez d'abord le mode édition (bouton ✏️ Éditer).")
            return
        txt     = self.editor.toPlainText()
        new_txt = rx.sub(self.e_repl.text(), txt)
        n       = len(rx.findall(txt))
        self.editor.setPlainText(new_txt)
        self._highlight_all()
        QMessageBox.information(self, "Remplacement",
            f"✅ {n} remplacement(s) effectué(s).")

    def closeEvent(self, event):
        """Effacer les surbrillances à la fermeture."""
        self.editor.setExtraSelections([])
        super().closeEvent(event)

# ═══════════════ ÉDITEUR ═══════════════
class EditorTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.arc_path    = None; self.arc_int  = None; self.file_path = None
        self.raw         = b'';   self.mode    = 'hex'; self.is_zstd   = False
        self.msbt        = None;  self._hl     = None;  self._editing  = False
        self._original_txt = '';  self._prev_mode = None; self._img_widget = None
        # Dict Zstd utilisé lors de la décompression (pour recompresser avec le même)
        self._used_zstd_dict = None

        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        self.lbl_info = QLabel("—"); self.lbl_info.setStyleSheet("background:#e0e0e0; color:#333; padding:3px 10px; font-size:11px;")
        lay.addWidget(self.lbl_info)
        self.editor = QTextEdit(); self.editor.setReadOnly(True); self.editor.setFont(QFont("Consolas", 10))
        self.editor.setLineWrapMode(QTextEdit.NoWrap); lay.addWidget(self.editor)

        bbar = QHBoxLayout(); bbar.setContentsMargins(6,4,6,4); bbar.setSpacing(4)
        self.btn_edit = QPushButton("✏️ Éditer"); self.btn_edit.clicked.connect(self._toggle_edit); bbar.addWidget(self.btn_edit)
        self.btn_hex = QPushButton("🔢 Hex"); self.btn_hex.clicked.connect(self._toggle_hex); bbar.addWidget(self.btn_hex)
        bbar.addSpacing(8)
        self.btn_find = QPushButton("🔍 Rechercher…"); self.btn_find.clicked.connect(lambda: FindReplaceDialog(self.editor, self).show()); bbar.addWidget(self.btn_find)
        bbar.addSpacing(8)
        self.btn_exp = QPushButton("📤 Export TXT"); self.btn_exp.clicked.connect(self._export_txt); self.btn_exp.setEnabled(False); bbar.addWidget(self.btn_exp)
        self.btn_imp = QPushButton("📥 Import TXT"); self.btn_imp.clicked.connect(self._import_txt); self.btn_imp.setEnabled(False); bbar.addWidget(self.btn_imp)
        bbar.addStretch()
        self.btn_save = QPushButton("💾 Sauvegarder"); self.btn_save.clicked.connect(self._save); self.btn_save.setEnabled(False)
        self.btn_save.setStyleSheet("QPushButton{background:#4CAF50; color:white; border:none; padding:6px 12px; border-radius:4px;} QPushButton:hover{background:#45a049;}")
        bbar.addWidget(self.btn_save)
        self.btn_saveas = QPushButton("💾 Sous…"); self.btn_saveas.clicked.connect(self._save_as); bbar.addWidget(self.btn_saveas)
        bw = QWidget(); bw.setLayout(bbar); bw.setStyleSheet("background:#f0f0f0; border-top:1px solid #ccc;"); lay.addWidget(bw)

    def load_direct(self, path):
        self.file_path = path; self.arc_path = None; self.arc_int = None
        try: raw = read_file(path)
        except Exception as e: self.editor.setPlainText(f"❌ Erreur : {e}"); return
        self._display(raw, Path(path).suffix)

    def load_from_archive(self, arc_path, internal):
        self.arc_path = arc_path; self.arc_int = internal; self.file_path = None
        try: raw = archive_extract(arc_path, internal)
        except Exception as e: self.editor.setPlainText(f"❌ Erreur : {e}"); return
        self._display(raw, Path(internal).suffix)

    def _display(self, raw, ext=''):
        if self._img_widget:
            self.layout().removeWidget(self._img_widget); self._img_widget.deleteLater(); self._img_widget = None
            self.editor.show()
        if ext.lower() in ('.png','.jpg','.jpeg'):
            try:
                pix = QPixmap(); pix.loadFromData(raw)
                if not pix.isNull():
                    scroll = QScrollArea(); lbl = QLabel(); lbl.setPixmap(pix); lbl.setAlignment(Qt.AlignCenter)
                    scroll.setWidget(lbl); scroll.setWidgetResizable(True)
                    self.layout().insertWidget(1, scroll); self.editor.hide()
                    self._img_widget = scroll; self.mode = 'image'
                    name = self.arc_int or (os.path.basename(self.file_path) if self.file_path else '?')
                    self.lbl_info.setText(f"  🖼 {name}"); return
            except: pass

        mode, txt, raw_dec, is_z, msbt, used_dict = decode_file(raw, ext)
        self.raw = raw_dec; self.mode = mode; self.is_zstd = is_z
        self.msbt = msbt; self._used_zstd_dict = used_dict
        self._editing = False; self._original_txt = txt; self._prev_mode = None
        self.editor.setReadOnly(True); self.btn_edit.setText("✏️ Éditer"); self.btn_save.setEnabled(False)
        if self._hl: self._hl.setDocument(None)
        if mode == 'hex': self._hl = HexHighlighter(self.editor.document())
        elif mode == 'msbt': self._hl = MsbtHighlighter(self.editor.document())
        else: self._hl = None
        self.editor.setPlainText(txt); self.editor.moveCursor(QTextCursor.Start)
        is_msbt = (mode == 'msbt')
        self.btn_exp.setEnabled(is_msbt); self.btn_imp.setEnabled(is_msbt)
        self.btn_edit.setEnabled(mode in ('msbt','text','byml'))
        name = self.arc_int or (os.path.basename(self.file_path) if self.file_path else '?')
        mode_str = {'msbt':'MSBT','text':'Texte','hex':'Binaire','sarc':'SARC','byml':'BYML'}
        zinfo = ' 🗜' if is_z else ''
        self.lbl_info.setText(f"  {name}  |  {mode_str.get(mode, mode)}  |  {len(raw_dec):,} o{zinfo}")

    def _toggle_edit(self):
        if self.mode not in ('msbt','text','byml'): return
        self._editing = not self._editing
        self.editor.setReadOnly(not self._editing)
        self.btn_edit.setText("🔒 Verrouiller" if self._editing else "✏️ Éditer")
        self.btn_save.setEnabled(self._editing)

    def _toggle_hex(self):
        if self.mode != 'hex':
            # Mémoriser le mode actuel SEULEMENT si ce n'est pas déjà hex
            # (évite d'écraser _prev_mode lors de basculements successifs)
            if self.mode not in ('hex',):
                self._prev_mode = self.mode
            # Détacher proprement l'ancien highlighter avant d'en créer un nouveau
            if self._hl:
                self._hl.setDocument(None)
                self._hl = None
            self._hl = HexHighlighter(self.editor.document())
            self.editor.setPlainText(build_hex_view(self.raw))
            self.editor.moveCursor(QTextCursor.Start)
            self.mode = 'hex'
            self.btn_hex.setText("📝 Normal")
            self.btn_edit.setEnabled(False)   # pas d'édition en mode hex
        else:
            restore_mode = self._prev_mode or 'text'
            ext = Path(self.arc_int or self.file_path or '').suffix
            self._display(self.raw, ext)
            self.btn_hex.setText("🔢 Hex")

    def _build_output(self):
        txt = self.editor.toPlainText()
        if self.mode == 'msbt' and self.msbt:
            self.msbt.from_txt(txt); data = self.msbt.save()
        elif self.mode == 'byml':
            try:
                obj = json.loads(txt); data = Byml.serialize(obj)
            except Exception as e:
                QMessageBox.critical(self, "Erreur JSON", str(e)); return self.raw
        elif self.mode == 'text': data = txt.encode('utf-8')
        else: data = self.raw
        if self.is_zstd:
            # Utiliser le dict d'origine si connu, sinon le meilleur disponible
            if self._used_zstd_dict is not None:
                cctx = zstd.ZstdCompressor(dict_data=self._used_zstd_dict, level=16)
            else:
                cctx = _best_compressor()
            data = cctx.compress(data)
        return data

    def _save(self):
        try:
            data = self._build_output()
            if self.arc_path and self.arc_int:
                archive_update(self.arc_path, self.arc_int, data)
                self._original_txt = self.editor.toPlainText()
                self._status("✅ Sauvegardé dans l'archive")
            elif self.file_path:
                with open(self.file_path, 'wb') as f: f.write(data)
                self._original_txt = self.editor.toPlainText()
                self._status("✅ Fichier sauvegardé")
            else: QMessageBox.warning(self, "Attention", "Aucune destination connue.")
        except Exception as e: QMessageBox.critical(self, "Erreur", str(e))

    def _save_as(self):
        name = os.path.basename(self.arc_int or self.file_path or 'fichier')
        dest, _ = QFileDialog.getSaveFileName(self, "Enregistrer sous…", name)
        if not dest: return
        try:
            with open(dest, 'wb') as f: f.write(self._build_output())
            self._status(f"✅ Enregistré : {dest}")
        except Exception as e: QMessageBox.critical(self, "Erreur", str(e))

    def _export_txt(self):
        if not self.msbt: return
        name = Path(self.arc_int or self.file_path or 'export').stem + '.txt'
        dest, _ = QFileDialog.getSaveFileName(self, "Exporter TXT…", name, "*.txt")
        if dest:
            with open(dest, 'w', encoding='utf-8') as f: f.write(self.editor.toPlainText())
            self._status(f"✅ Exporté : {dest}")

    def _import_txt(self):
        if not self.msbt: return
        src, _ = QFileDialog.getOpenFileName(self, "Importer TXT…", '', "*.txt")
        if not src: return
        try:
            with open(src, 'r', encoding='utf-8') as f: txt = f.read()
            self.msbt.from_txt(txt); self.editor.setPlainText(self.msbt.to_txt())
            self._editing = True; self.editor.setReadOnly(False); self.btn_save.setEnabled(True)
            self.btn_edit.setText("🔒 Verrouiller"); self._status(f"✅ TXT importé : {src}")
        except Exception as e: QMessageBox.critical(self, "Erreur", str(e))

    def _status(self, msg):
        win = self.window()
        if hasattr(win, 'statusBar'): win.statusBar().showMessage(msg, 5000)

    def is_modified(self):
        if self.mode in ('msbt','text','byml'): return self.editor.toPlainText() != self._original_txt
        return False

    def prompt_save(self):
        name = os.path.basename(self.arc_int or self.file_path or "sans nom")
        box = QMessageBox(self); box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Modifications non sauvegardées")
        box.setText(f"Voulez-vous enregistrer les modifications de « {name} » ?")
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        return box.exec_()

# ═══════════════ ARBRE DE FICHIERS ═══════════════
class FileTree(QTreeWidget):
    sig_open_file = pyqtSignal(str); sig_open_intern = pyqtSignal(str, str)
    EXT_ICON = {'.sarc':'📦','.zs':'🗜','.msbt':'📝','.zip':'📦','.7z':'📦','.tar':'📦',
                '.txt':'📄','.yaml':'📋','.json':'📋','.xml':'📋','.png':'🖼','.jpg':'🖼','.jpeg':'🖼'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Nom", "Taille"]); self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu); self.customContextMenuRequested.connect(self._ctx)
        self.itemDoubleClicked.connect(self._dclick); self.itemExpanded.connect(self._expand)
        self.root_path = ''
        self.setToolTip("📁 Double-clic sur un dossier pour l'ouvrir.\n📄 Double-clic sur un fichier pour l'éditer.\n📦 Double-clic sur une archive pour voir son contenu.")

    def set_root(self, path):
        self.clear(); self.root_path = path; self._populate(self.invisibleRootItem(), path)
    def load_single(self, path):
        self.clear(); self.root_path = os.path.dirname(path)
        self._add_file(self.invisibleRootItem(), os.path.basename(path), path)
    def _populate(self, parent, path):
        try: entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        except PermissionError: return
        for name in entries:
            full = os.path.join(path, name)
            if os.path.isdir(full):
                item = QTreeWidgetItem(parent, [f"📁 {name}", ""]); item.setData(0, Qt.UserRole, ('dir', full))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator); QTreeWidgetItem(item, ["…", ""])
            else: self._add_file(parent, name, full)
    def _add_file(self, parent, name, full):
        ext = Path(name).suffix.lower(); icon = self.EXT_ICON.get(ext, '📄')
        try: size = self._fmt(os.path.getsize(full))
        except: size = ''
        item = QTreeWidgetItem(parent, [f"{icon} {name}", size]); item.setData(0, Qt.UserRole, ('file', full))
        if ext in ARCHIVE_EXT: item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator); QTreeWidgetItem(item, ["…", ""])
    def _expand(self, item):
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            item.takeChildren()
            d = item.data(0, Qt.UserRole)
            if not d: return
            kind = d[0]
            if kind == 'dir': self._populate(item, d[1])
            elif kind == 'file': self._load_arc_children(item, d[1])
    def _load_arc_children(self, parent, arc_path):
        try:
            files = archive_list(arc_path)
            for f in files:
                ext = Path(f).suffix.lower(); icon = self.EXT_ICON.get(ext, '📄')
                child = QTreeWidgetItem(parent, [f"{icon} {os.path.basename(f)}", ""])
                child.setData(0, Qt.UserRole, ('arc_file', arc_path, f)); child.setToolTip(0, f)
        except Exception as e: QTreeWidgetItem(parent, [f"⚠ {e}", ""])
    def _dclick(self, item, _col):
        data = item.data(0, Qt.UserRole)
        if not data: return
        if data[0] == 'file':
            path = data[1]; ext = Path(path).suffix.lower()
            if ext in ARCHIVE_EXT:
                if item.childCount() == 1 and item.child(0).text(0) == "…": self._expand(item)
                item.setExpanded(True)
            else: self.sig_open_file.emit(path)
        elif data[0] == 'arc_file': self.sig_open_intern.emit(data[1], data[2])
    def _ctx(self, pos):
        items = self.selectedItems()
        if not items: return
        item = items[0]; data = item.data(0, Qt.UserRole)
        if not data: return
        menu = QMenu(self)
        menu.setStyleSheet("QMenu{background:#fff; color:#333; border:1px solid #ccc;} QMenu::item:selected{background:#4CAF50; color:#fff;}")
        if data[0] == 'file':
            path = data[1]; ext = Path(path).suffix.lower()
            if ext not in ARCHIVE_EXT: a = menu.addAction("🔍 Ouvrir"); a.triggered.connect(lambda: self.sig_open_file.emit(path))
            a2 = menu.addAction("📋 Extraire vers…"); a2.triggered.connect(lambda: self._extract_file(path))
            if ext == '.zs': a3 = menu.addAction("📦 Extraire le SARC…"); a3.triggered.connect(lambda: self._extract_sarc(path))
            if ext in ARCHIVE_EXT: a4 = menu.addAction("📁 Extraire tout…"); a4.triggered.connect(lambda: self._extract_all(path))
            if ext == '.msbt': a5 = menu.addAction("📊 Comparer avec…"); a5.triggered.connect(lambda: self._compare_direct(path))
        elif data[0] == 'arc_file':
            arc, internal = data[1], data[2]
            a = menu.addAction("🔍 Ouvrir"); a.triggered.connect(lambda: self.sig_open_intern.emit(arc, internal))
            a2 = menu.addAction("📋 Extraire vers…"); a2.triggered.connect(lambda: self._extract_internal(arc, internal))
            if Path(internal).suffix.lower() == '.msbt':
                a3 = menu.addAction("📤 Exporter TXT…"); a3.triggered.connect(lambda: self._export_msbt(arc, internal))
                a4 = menu.addAction("📊 Comparer avec…"); a4.triggered.connect(lambda: self._compare_arc(arc, internal))
        msbt_sel = [i for i in items if i.data(0, Qt.UserRole) and i.data(0, Qt.UserRole)[0] == 'arc_file' and Path(i.data(0, Qt.UserRole)[2]).suffix.lower() == '.msbt']
        if len(msbt_sel) > 1: a5 = menu.addAction(f"📤 Exporter {len(msbt_sel)} MSBT → TXT…"); a5.triggered.connect(lambda: self._batch_export(msbt_sel))
        menu.exec_(self.viewport().mapToGlobal(pos))
    def _extract_file(self, path):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…", os.path.basename(path))
        if dest: shutil.copy2(path, dest)
    def _extract_internal(self, arc, internal):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…", os.path.basename(internal))
        if dest:
            try:
                with open(dest, 'wb') as f: f.write(archive_extract(arc, internal))
            except Exception as e: QMessageBox.critical(self, "Erreur", str(e))
    def _extract_sarc(self, path):
        dest, _ = QFileDialog.getSaveFileName(self, "Enregistrer le SARC", Path(path).stem + ".sarc", "*.sarc")
        if dest:
            try:
                raw = read_file(path); dec = find_dict(raw)
                if dec is None: raise ValueError("Impossible de décompresser ce fichier.")
                with open(dest, 'wb') as f: f.write(dec)
                QMessageBox.information(self, "Succès", "SARC extrait avec succès.")
            except Exception as e: QMessageBox.critical(self, "Erreur", str(e))
    def _extract_all(self, path):
        dest = QFileDialog.getExistingDirectory(self, "Dossier de destination")
        if not dest: return
        try:
            files = archive_list(path)
            for f in files:
                data = archive_extract(path, f); full = os.path.join(dest, f)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'wb') as out: out.write(data)
            QMessageBox.information(self, "Succès", f"{len(files)} fichiers extraits.")
        except Exception as e: QMessageBox.critical(self, "Erreur", str(e))
    def _export_msbt(self, arc, internal):
        try:
            raw = archive_extract(arc, internal); msbt = MsbtParser(raw)
            dest, _ = QFileDialog.getSaveFileName(self, "Exporter TXT…", Path(internal).stem + '.txt', "*.txt")
            if dest:
                with open(dest, 'w', encoding='utf-8') as f: f.write(msbt.to_txt())
        except Exception as e: QMessageBox.critical(self, "Erreur", str(e))
    def _compare_direct(self, path):
        other, _ = QFileDialog.getOpenFileName(self, "Comparer avec…", filter="*.msbt")
        if other:
            try:
                left = MsbtParser(read_file(path)).to_txt(); right = MsbtParser(read_file(other)).to_txt()
                dlg = QDialog(self); dlg.setWindowTitle("Comparaison"); dlg.resize(800,600)
                lay = QVBoxLayout(dlg); spl = QSplitter(Qt.Horizontal)
                l_edit = QTextEdit(); r_edit = QTextEdit()
                l_edit.setReadOnly(True); r_edit.setReadOnly(True)
                l_edit.setPlainText(left); r_edit.setPlainText(right)
                spl.addWidget(l_edit); spl.addWidget(r_edit); lay.addWidget(spl)
                dlg.exec_()
            except Exception as e: QMessageBox.critical(self, "Erreur", str(e))
    def _compare_arc(self, arc, internal):
        other, _ = QFileDialog.getOpenFileName(self, "Comparer avec…", filter="*.msbt")
        if other:
            try:
                left = MsbtParser(archive_extract(arc, internal)).to_txt(); right = MsbtParser(read_file(other)).to_txt()
                dlg = QDialog(self); dlg.setWindowTitle("Comparaison"); dlg.resize(800,600)
                lay = QVBoxLayout(dlg); spl = QSplitter(Qt.Horizontal)
                l_edit = QTextEdit(); r_edit = QTextEdit()
                l_edit.setReadOnly(True); r_edit.setReadOnly(True)
                l_edit.setPlainText(left); r_edit.setPlainText(right)
                spl.addWidget(l_edit); spl.addWidget(r_edit); lay.addWidget(spl)
                dlg.exec_()
            except Exception as e: QMessageBox.critical(self, "Erreur", str(e))
    def _batch_export(self, items):
        dest_dir = QFileDialog.getExistingDirectory(self, "Dossier destination")
        if not dest_dir: return
        done = 0
        for item in items:
            _, arc, internal = item.data(0, Qt.UserRole)
            try:
                raw = archive_extract(arc, internal); msbt = MsbtParser(raw)
                out = os.path.join(dest_dir, Path(internal).stem + '.txt')
                with open(out, 'w', encoding='utf-8') as f: f.write(msbt.to_txt())
                done += 1
            except: pass
        QMessageBox.information(self, "Export", f"✅ {done} fichier(s) exporté(s).")
    @staticmethod
    def _fmt(sz):
        for u in ('o','Ko','Mo','Go'):
            if sz < 1024: return f"{sz:.0f} {u}"
            sz /= 1024
        return f"{sz:.1f} Go"

# ═══════════════ THÈME ═══════════════
STYLE = """
QMainWindow { background:#f5f5f5; }
QMenuBar { background:#ffffff; color:#333; font-size:13px; }
QMenuBar::item:selected { background:#4CAF50; color:white; }
QMenu { background:#ffffff; color:#333; border:1px solid #ccc; }
QMenu::item:selected { background:#4CAF50; color:white; }
QToolBar { background:#ffffff; border-bottom:1px solid #ccc; padding:4px; spacing:6px; }
QStatusBar { background:#4CAF50; color:white; font-size:12px; font-weight:bold; }
QSplitter::handle { background:#ccc; width:3px; }
QTreeWidget { background:#ffffff; border:none; color:#333; font-size:13px; }
QTreeWidget::item { padding:4px 4px; }
QTreeWidget::item:selected { background:#4CAF50; color:white; }
QTreeWidget::item:hover { background:#e8f5e9; }
QHeaderView::section { background:#f0f0f0; color:#555; border:none; border-right:1px solid #ddd; padding:4px 6px; font-size:12px; }
QTextEdit { background:#ffffff; color:#333; border:1px solid #ddd; font-family:'Segoe UI',sans-serif; font-size:12px; selection-background-color:#4CAF50; }
QLineEdit { background:#ffffff; color:#333; border:1px solid #ccc; padding:6px 10px; border-radius:4px; font-size:13px; }
QLineEdit:focus { border-color:#4CAF50; }
QPushButton { background:#e0e0e0; color:#333; border:1px solid #ccc; padding:6px 14px; border-radius:4px; font-size:13px; }
QPushButton:hover { background:#d0d0d0; }
QPushButton:pressed { background:#c0c0c0; }
QPushButton:disabled { color:#999; background:#f0f0f0; }
QTabWidget::pane { border:1px solid #ddd; }
QTabBar::tab { background:#f0f0f0; color:#555; padding:6px 16px; border:1px solid #ddd; border-bottom:none; border-top-left-radius:4px; border-top-right-radius:4px; margin-right:2px; }
QTabBar::tab:selected { background:#ffffff; color:#333; border-bottom:2px solid #4CAF50; }
QTabBar::tab:hover { background:#e0e0e0; }
QComboBox { background:#ffffff; color:#333; border:1px solid #ccc; padding:4px 8px; border-radius:4px; font-size:13px; }
QComboBox QAbstractItemView { background:#ffffff; color:#333; selection-background-color:#4CAF50; }
QScrollBar:vertical { background:#f0f0f0; width:10px; border:none; }
QScrollBar::handle:vertical { background:#ccc; border-radius:5px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QToolTip { background:#4CAF50; color:white; border:none; padding:4px; font-size:12px; }
"""

# ═══════════════ FENÊTRE PRINCIPALE ═══════════════
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._check_dependencies()
        self.setWindowTitle("TOTK ROMFS EDITOR – Pour tous !")
        self.setGeometry(80, 80, 1400, 860)
        self.setStyleSheet(STYLE)
        self._build_menu(); self._build_toolbar(); self._build_central()
        self.lbl_dict.setText("🟡 Ouvrir un dossier ROMFS pour charger le dict Zstd")
        self.lbl_dict.setStyleSheet("color:#b8860b; font-weight:bold;")
        self.statusBar().showMessage("Prêt ! 📁 Ouvrir un dossier ROMFS pour commencer.")

    def _check_dependencies(self):
        """
        Vérifie les dépendances au démarrage.
        zstandard est OBLIGATOIRE (décompression des .zs).
        py7zr est optionnel (lecture des .7z).
        """
        if not HAS_ZSTD:
            QMessageBox.critical(
                self, "Dépendance manquante — ERREUR CRITIQUE",
                "La bibliothèque zstandard est introuvable.\n\n"
                "Elle est indispensable pour lire les fichiers .zs de TotK.\n\n"
                "Installez-la avec :\n"
                "    pip install zstandard\n\n"
                "L'application va se fermer."
            )
            sys.exit(1)
        if not HAS_PY7ZR:
            QMessageBox.information(
                self, "Bibliothèque optionnelle absente",
                "py7zr n'est pas installé.\n"
                "Les archives .7z ne pourront pas être ouvertes.\n\n"
                "Installez-la avec :\n"
                "    pip install py7zr"
            )

    def _build_menu(self):
        mb = self.menuBar()
        mf = mb.addMenu("Fichier")
        for label, shortcut, fn in [
            ("📁 Ouvrir dossier ROMFS…", "Ctrl+O", self._open_folder),
            ("📄 Ouvrir un fichier…", "Ctrl+F", self._open_file),
            ("—", None, None),
            ("📤 Export MSBT → TXT (lot)…", None, self._batch_export),
            ("📥 Import TXT → MSBT (lot)…", None, self._batch_import),
            ("—", None, None),
            ("Quitter", "Ctrl+Q", self.close)
        ]:
            if label == "—": mf.addSeparator(); continue
            a = QAction(label, self); a.setShortcut(shortcut) if shortcut else None; a.triggered.connect(fn); mf.addAction(a)
        me = mb.addMenu("Édition")
        a_fr = QAction("🔍 Rechercher/Remplacer…", self); a_fr.setShortcut("Ctrl+H"); a_fr.triggered.connect(self._open_findreplace); me.addAction(a_fr)

    def _build_toolbar(self):
        tb = self.addToolBar("Principal"); tb.setMovable(False)
        tb.addWidget(QLabel("🎮 Jeu : ")); self.combo_game = QComboBox(); self.combo_game.addItems(GAMES.keys())
        self.combo_game.currentTextChanged.connect(self._change_game); tb.addWidget(self.combo_game); tb.addSeparator()
        tb.addWidget(QLabel("📁 Dossier ROMFS : ")); self.e_root = QLineEdit(); self.e_root.setMinimumWidth(300); self.e_root.setReadOnly(True); tb.addWidget(self.e_root)
        btn_folder = QPushButton("📁 Parcourir")
        btn_folder.setToolTip(
            "Choisissez le dossier romfs de votre jeu.\n\n"
            "L'application cherchera automatiquement le fichier\n"
            "ZsDic.pack.zs (souvent dans Pack/) pour décompresser\n"
            "les fichiers .zs du jeu.\n\n"
            "💡 Sans ce fichier, les archives .zs ne pourront pas\n"
            "   être décompressées correctement."
        )
        btn_folder.clicked.connect(self._open_folder); tb.addWidget(btn_folder)
        btn_file = QPushButton("📄 Fichier seul"); btn_file.clicked.connect(self._open_file); tb.addWidget(btn_file); tb.addSeparator()
        self.lbl_dict = QLabel("🔴 Aucun dictionnaire chargé"); self.lbl_dict.setStyleSheet("color:#d32f2f; font-weight:bold;"); tb.addWidget(self.lbl_dict)
        tb.addSeparator()
        btn_exp = QPushButton("📤 Export lot"); btn_exp.clicked.connect(self._batch_export); tb.addWidget(btn_exp)
        btn_imp = QPushButton("📥 Import lot"); btn_imp.clicked.connect(self._batch_import); tb.addWidget(btn_imp)

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal); self.setCentralWidget(splitter)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0,0,0,0); ll.setSpacing(0)
        hint = QLabel("  📁 Dossier ou 📄 Fichier → double-clic pour ouvrir")
        hint.setStyleSheet("background:#fff; color:#888; padding:4px 8px; font-size:11px;"); ll.addWidget(hint)
        self.tree = FileTree(); self.tree.sig_open_file.connect(self._open_tab_direct); self.tree.sig_open_intern.connect(self._open_tab_intern)
        self.tree.setMinimumWidth(200); ll.addWidget(self.tree); left.setMaximumWidth(420)
        splitter.addWidget(left)
        self.tabs = QTabWidget(); self.tabs.setTabsClosable(True); self.tabs.tabCloseRequested.connect(self._close_tab); self.tabs.setDocumentMode(True)
        welcome = QWidget(); wl = QVBoxLayout(welcome); wl.setAlignment(Qt.AlignCenter)
        wl_lbl = QLabel(
            "🗡️  TOTK ROMFS EDITOR\n\n"
            "① Choisissez votre jeu dans la barre du haut\n"
            "② Cliquez sur 📁 Parcourir pour ouvrir votre dossier ROMFS\n"
            "③ Double-cliquez sur une archive pour voir son contenu\n"
            "④ Double-cliquez sur un fichier pour le modifier\n\n"
            "Ctrl+F = rechercher | Ctrl+H = rechercher et remplacer\n\n"
            "📌 Placez ZsDic.pack.zs dans Pack/ (chargé auto a l'ouverture)."
            "   Pour d'autres jeux, placez le fichier ZsDic.pack.zs\n"
            "   dans votre dossier ROMFS."
        )
        wl_lbl.setAlignment(Qt.AlignCenter); wl_lbl.setStyleSheet("color:#555; font-size:14px; line-height:1.8;"); wl.addWidget(wl_lbl)
        self.tabs.addTab(welcome, "Accueil"); splitter.addWidget(self.tabs); splitter.setSizes([320, 1080])

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Ouvrir le dossier ROMFS")
        if path:
            self.e_root.setText(path); self.tree.set_root(path)
            load_all_dicts(path)
            if _dicts:
                self.lbl_dict.setText(f"🟢 {len(_dicts)} dictionnaires trouvés")
                self.lbl_dict.setStyleSheet("color:#388e3c; font-weight:bold;")
            else:
                self.lbl_dict.setText("🔴 Aucun dictionnaire trouvé")
                self.lbl_dict.setStyleSheet("color:#d32f2f; font-weight:bold;")
            self.statusBar().showMessage(f"Dossier ROMFS ouvert : {path}")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un fichier", filter="Tous fichiers (*);;Archives (*.sarc *.zs);;Messages (*.msbt);;Texte (*.txt)")
        if not path: return
        ext = Path(path).suffix.lower()
        parent_dir = os.path.dirname(path)
        # Charger les dicts du dossier parent si pas encore fait
        if not _dicts and parent_dir:
            load_all_dicts(parent_dir)
            if _dicts:
                self.lbl_dict.setText(f"🟢 {len(_dicts)} dictionnaire(s) trouvé(s)")
                self.lbl_dict.setStyleSheet("color:#388e3c; font-weight:bold;")
        if ext in ARCHIVE_EXT:
            self.e_root.setText(parent_dir)
            self.tree.load_single(path)
            self.statusBar().showMessage(f"Archive ouverte : {path}")
        else:
            self._open_tab_direct(path)

    def _open_tab_direct(self, path):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.file_path == path: self.tabs.setCurrentIndex(i); return
        tab = EditorTab(); tab.load_direct(path); self.tabs.addTab(tab, os.path.basename(path)); self.tabs.setCurrentWidget(tab)

    def _open_tab_intern(self, arc_path, internal):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.arc_path == arc_path and tab.arc_int == internal: self.tabs.setCurrentIndex(i); return
        tab = EditorTab(); tab.load_from_archive(arc_path, internal); self.tabs.addTab(tab, os.path.basename(internal)); self.tabs.setCurrentWidget(tab)

    def _close_tab(self, idx):
        tab = self.tabs.widget(idx)
        if isinstance(tab, EditorTab) and tab.is_modified():
            ret = tab.prompt_save()
            if ret == QMessageBox.Save:
                try:
                    tab._save()
                except Exception as e:
                    QMessageBox.critical(self, "Erreur sauvegarde",
                        f"La sauvegarde a échoué :\n{e}\n\n"
                        "L'onglet sera fermé sans sauvegarde.")
            elif ret == QMessageBox.Cancel:
                return
        self.tabs.removeTab(idx)
        if tab:
            tab.deleteLater()

    def closeEvent(self, event):
        """Vérifie les onglets non sauvegardés avant de quitter."""
        modified = []
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.is_modified():
                name = os.path.basename(tab.arc_int or tab.file_path or "sans nom")
                modified.append((i, tab, name))

        if modified:
            names = "\n".join(f"  • {name}" for _, _, name in modified)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Modifications non sauvegardées")
            box.setText(
                f"Ces fichiers ont des modifications non sauvegardées :\n\n{names}\n\n"
                "Voulez-vous les enregistrer avant de quitter ?"
            )
            box.setStandardButtons(
                QMessageBox.SaveAll | QMessageBox.Discard | QMessageBox.Cancel
            )
            box.setDefaultButton(QMessageBox.SaveAll)
            ret = box.exec_()

            if ret == QMessageBox.Cancel:
                event.ignore()
                return
            elif ret == QMessageBox.SaveAll:
                errors = []
                for _, tab, name in modified:
                    try:
                        tab._save()
                    except Exception as e:
                        errors.append(f"{name}: {e}")
                if errors:
                    QMessageBox.warning(self, "Erreurs de sauvegarde",
                        "Certains fichiers n'ont pas pu être sauvegardés :\n\n"
                        + "\n".join(errors))
            # Discard → on ferme sans sauvegarder

        event.accept()

    def _open_findreplace(self):
        tab = self.tabs.currentWidget()
        if isinstance(tab, EditorTab): FindReplaceDialog(tab.editor, self).show()

    def _change_game(self, name):
        global current_game
        if name in GAMES: current_game = GAMES[name]; self.statusBar().showMessage(f"Jeu : {GAMES[name].name}")

    def _batch_export(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root): QMessageBox.warning(self, "Erreur", "Ouvrez d'abord un dossier ROMFS."); return
        dest = QFileDialog.getExistingDirectory(self, "Dossier destination TXT")
        if not dest: return
        prog = QProgressDialog("Export en cours…", "Annuler", 0, 0, self); prog.setWindowModality(Qt.WindowModal); prog.show()
        done, errs = 0, []
        for dirpath, _, files in os.walk(root):
            if prog.wasCanceled(): break
            for fname in files:
                if prog.wasCanceled(): break
                fpath = os.path.join(dirpath, fname); ext = Path(fname).suffix.lower()
                prog.setLabelText(fname); QApplication.processEvents()
                if ext == '.msbt':
                    try:
                        raw = read_file(fpath); dec = find_dict(raw)
                        if dec is None: dec = raw
                        msbt = MsbtParser(dec if dec[:8]==b'MsgStdBn' else raw)
                        rel = os.path.relpath(fpath, root); out = os.path.join(dest, rel + '.txt')
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with open(out, 'w', encoding='utf-8') as f: f.write(msbt.to_txt())
                        done += 1
                    except Exception as e: errs.append(f"{fname}: {e}")
                elif ext in ARCHIVE_EXT:
                    try:
                        internal_files = archive_list(fpath)
                        for internal in internal_files:
                            if Path(internal).suffix.lower() != '.msbt': continue
                            raw = archive_extract(fpath, internal); msbt = MsbtParser(raw)
                            rel = os.path.relpath(fpath, root); out = os.path.join(dest, rel, internal + '.txt')
                            os.makedirs(os.path.dirname(out), exist_ok=True)
                            with open(out, 'w', encoding='utf-8') as f: f.write(msbt.to_txt())
                            done += 1
                    except Exception as e: errs.append(f"{fname}: {e}")
        prog.close()
        msg = f"✅ Export terminé : {done} fichier(s)."
        if errs: msg += f"\n\n⚠ Erreurs :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Export", msg)

    def _batch_import(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root): QMessageBox.warning(self, "Erreur", "Ouvrez d'abord un dossier ROMFS."); return
        src = QFileDialog.getExistingDirectory(self, "Dossier source TXT")
        if not src: return
        prog = QProgressDialog("Import en cours…", "Annuler", 0, 0, self); prog.setWindowModality(Qt.WindowModal); prog.show()
        done, errs = 0, []
        for dirpath, _, files in os.walk(src):
            if prog.wasCanceled(): break
            for fname in files:
                if prog.wasCanceled(): break
                if not fname.endswith('.txt'): continue
                txt_path = os.path.join(dirpath, fname); rel_txt = os.path.relpath(txt_path, src)
                rel_orig = rel_txt[:-4] if rel_txt.endswith('.txt') else rel_txt
                orig = os.path.join(root, rel_orig); prog.setLabelText(fname); QApplication.processEvents()
                if os.path.isfile(orig) and orig.lower().endswith('.msbt'):
                    try:
                        raw = read_file(orig); dec = find_dict(raw)
                        if dec is None: dec = raw
                        msbt = MsbtParser(dec if dec[:8]==b'MsgStdBn' else raw)
                        with open(txt_path, 'r', encoding='utf-8') as f: msbt.from_txt(f.read())
                        out = msbt.save()
                        with open(orig, 'wb') as f: f.write(out); done += 1
                    except Exception as e: errs.append(f"{rel_orig}: {e}")
                else:
                    parts = Path(rel_orig).parts
                    for i in range(len(parts)-1, 0, -1):
                        arc_rel = os.path.join(*parts[:i]); int_name = '/'.join(parts[i:])
                        arc_path = os.path.join(root, arc_rel)
                        if os.path.isfile(arc_path) and Path(arc_path).suffix.lower() in ARCHIVE_EXT:
                            try:
                                iraw = archive_extract(arc_path, int_name); msbt = MsbtParser(iraw)
                                with open(txt_path, 'r', encoding='utf-8') as f: msbt.from_txt(f.read())
                                archive_update(arc_path, int_name, msbt.save()); done += 1
                            except Exception as e: errs.append(f"{rel_orig}: {e}")
                            break
        prog.close()
        msg = f"✅ Import terminé : {done} fichier(s) mis à jour."
        if errs: msg += f"\n\n⚠ Erreurs :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Import", msg)

if __name__ == '__main__':
    app = QApplication(sys.argv); app.setStyle('Fusion')
    win = MainWindow(); win.show()
    sys.exit(app.exec_())