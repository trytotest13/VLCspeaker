#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VLCspeaker, a simple Windows video player with:

  * play / pause / stop / seek
  * playback speed up & down (0.25x ... 8x)
  * volume up & down, mute
  * load subtitle files (.srt, .vtt, .ass/.ssa), shown on the video
  * one big "Read Aloud" button: every subtitle/caption line is spoken
    with Windows text-to-speech (SAPI), so you can watch foreign-language
    movies and *listen* to the captions.

Video playback is powered by VLC (libvlc).
Speech is powered by the Windows voices already on your PC (offline).

Run:    python vlcspeaker.py        (or double-click run.bat)
Check:  python vlcspeaker.py --selftest
"""

import asyncio
import bisect
import collections
import contextlib
import ctypes
from ctypes import wintypes
import difflib
import importlib.util
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile

IS_WINDOWS = (sys.platform == "win32")
APP_NAME = "VLCspeaker"
APP_VERSION = "0.0.1"

# Real folder of the app. When frozen to a .exe (onefile), __file__ points
# into a temp extraction dir, so anchor user data to the .exe's own folder.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# Make libvlc.dll findable BEFORE importing the vlc module.
# --------------------------------------------------------------------------

def _find_vlc_dir():
    """Return a folder that contains libvlc.dll (prefers one matching the
    running Python bitness). VLC_PATH environment variable overrides."""
    candidates = []
    override = os.environ.get("VLC_PATH", "").strip()
    if override:
        candidates.append(override)
    bits = struct.calcsize("P") * 8
    if bits == 64:
        candidates += [r"C:\Program Files\VideoLAN\VLC",
                       r"C:\Program Files (x86)\VideoLAN\VLC"]
    else:
        candidates += [r"C:\Program Files (x86)\VideoLAN\VLC",
                       r"C:\Program Files\VideoLAN\VLC"]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "libvlc.dll")):
            return c
    return None

VLC_DIR = _find_vlc_dir() if IS_WINDOWS else None
if VLC_DIR:
    os.environ["PATH"] = VLC_DIR + os.pathsep + os.environ.get("PATH", "")
    with contextlib.suppress(AttributeError, OSError):
        os.add_dll_directory(VLC_DIR)

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_ERROR = None
except Exception as exc:              # optional: drag & drop from Explorer
    DND_FILES = None
    TkinterDnD = None
    DND_ERROR = str(exc)

try:
    import vlc
    VLC_ERROR = None
except Exception as exc:                       # module missing or dll missing
    vlc = None
    VLC_ERROR = str(exc)

# --------------------------------------------------------------------------
# Subtitle parsing  (.srt / .vtt / .ass / .ssa)
# --------------------------------------------------------------------------

_TS_LONG = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")     # H:MM:SS,mmm
_TS_SHORT = re.compile(r"(\d+):(\d+)[,.](\d+)")          #   MM:SS.mmm (WebVTT)
_TAG_RE = re.compile(r"<[^>]+>")                          # <i>, <font ...>
_BRACE_RE = re.compile(r"\{[^}]*\}")                      # ASS {...} overrides
_ENTITIES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
             ("&apos;", "'"), ("&#39;", "'"), ("&nbsp;", " "),
             ("&lrm;", ""), ("&rlm;", ""))

class Cue:
    __slots__ = ("start", "end", "text")

    def __init__(self, start, end, text):
        self.start = start        # milliseconds
        self.end = end
        self.text = text

def _ms(h, m, s, frac):
    """Time parts -> milliseconds. Fraction digits are padded, so ASS
    centiseconds (".50") and SRT milliseconds (",500") both work."""
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])

def _find_ms(text):
    m = _TS_LONG.search(text)
    if m:
        return _ms(*m.groups())
    m = _TS_SHORT.search(text)
    if m:
        return _ms(0, *m.groups())
    return None

def _clean_lines(raw_lines):
    """Strip markup, entities and decorative notes; return cleaned text."""
    out = []
    for ln in raw_lines:
        ln = _BRACE_RE.sub("", ln)
        ln = _TAG_RE.sub("", ln)
        for a, b in _ENTITIES:
            ln = ln.replace(a, b)
        ln = ln.replace("\\N", "\n").replace("\\n", "\n")   # ASS line breaks
        for ch in "♪♫♬★☆":
            ln = ln.replace(ch, "")
        for part in ln.split("\n"):
            part = part.strip()
            if part:
                out.append(part)
    return "\n".join(out)

def _read_text_best(path):
    with open(path, "rb") as fh:
        data = fh.read()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        with contextlib.suppress(UnicodeDecodeError, UnicodeError):
            return data.decode(enc)
    return data.decode("utf-8", "replace")

def _parse_srt_like(text, vtt=False):
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    cues = []
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if vtt and lines[0].strip().upper().startswith(
                ("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        ti = 0
        if "-->" not in lines[ti]:
            if len(lines) > ti + 1 and "-->" in lines[ti + 1]:
                ti += 1
            else:
                continue
        start = _find_ms(lines[ti].split("-->", 1)[0])
        end = _find_ms(lines[ti].split("-->", 1)[1])
        if start is None or end is None:
            continue
        cues.append(Cue(start, end, _clean_lines(lines[ti + 1:])))
    return cues

def _parse_ass(text):
    cues = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].split(",", 9)
        if len(parts) < 10:
            continue
        start = _find_ms(parts[1])
        end = _find_ms(parts[2])
        if start is None or end is None:
            continue
        cues.append(Cue(start, end, _clean_lines([parts[9]])))
    return cues

def parse_subtitles(path):
    """Parse a subtitle file into a list of Cue, sorted by start time."""
    text = _read_text_best(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".ass", ".ssa"):
        cues = _parse_ass(text)
    elif ext == ".vtt":
        cues = _parse_srt_like(text, vtt=True)
    else:                                   # .srt and unknown -> sniff
        cues = _parse_srt_like(text)
        if not cues:
            cues = _parse_srt_like(text, vtt=True)
        if not cues:
            cues = _parse_ass(text)
    cues = [c for c in cues if c.text and c.end > c.start]
    cues.sort(key=lambda c: c.start)
    return cues

# --------------------------------------------------------------------------
# Subtitle tracks embedded INSIDE the movie file (mkv / mp4 / ...)
# Uses ffmpeg + ffprobe (if installed) to list the tracks and to pull a text
# track out into a temporary .srt, so it can be shown on the video and
# spoken by Read Aloud exactly like a normal subtitle file.
# --------------------------------------------------------------------------

TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "text", "webvtt",
                   "microdvd", "subviewer", "subviewer1", "realtext", "stl",
                   "pjs", "vplayer", "jacosub", "mpl2", "sami"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
                    "vobsub", "dvb_subtitle", "dvbsub", "xsub", "dvb_teletext",
                    "arib_caption", "bluray-subs"}

LANG_NAMES = {
    "eng": "English", "en": "English", "hin": "Hindi", "hi": "Hindi",
    "ben": "Bengali", "bn": "Bengali", "pan": "Punjabi", "pa": "Punjabi",
    "tam": "Tamil", "ta": "Tamil", "tel": "Telugu", "te": "Telugu",
    "mar": "Marathi", "mr": "Marathi", "guj": "Gujarati", "gu": "Gujarati",
    "kan": "Kannada", "kn": "Kannada", "mal": "Malayalam", "ml": "Malayalam",
    "urd": "Urdu", "ur": "Urdu", "nep": "Nepali", "sin": "Sinhala",
    "spa": "Spanish", "es": "Spanish", "fre": "French", "fra": "French",
    "fr": "French", "deu": "German", "ger": "German", "de": "German",
    "ita": "Italian", "it": "Italian", "jpn": "Japanese", "ja": "Japanese",
    "kor": "Korean", "ko": "Korean", "chi": "Chinese", "zho": "Chinese",
    "zh": "Chinese", "rus": "Russian", "ru": "Russian", "ara": "Arabic",
    "ar": "Arabic", "por": "Portuguese", "pt": "Portuguese", "tha": "Thai",
    "vie": "Vietnamese", "ind": "Indonesian", "tur": "Turkish",
    "pol": "Polish", "nld": "Dutch", "dut": "Dutch", "swe": "Swedish",
    "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "ces": "Czech",
    "cze": "Czech", "hun": "Hungarian", "ron": "Romanian", "rum": "Romanian",
    "ell": "Greek", "gre": "Greek", "heb": "Hebrew", "fas": "Persian",
    "per": "Persian", "pe": "Persian", "ukr": "Ukrainian", "srp": "Serbian",
    "hrv": "Croatian", "bul": "Bulgarian", "fil": "Filipino", "tgl": "Filipino",
    "msa": "Malay", "mya": "Burmese",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def find_tool(name):
    return shutil.which(name)

def probe_subtitle_tracks(path):
    """Return a list of {index, codec, title, language} for subtitle streams
    inside the movie, the string 'no-tool' if ffprobe is missing, or
    'error' if the file could not be scanned."""
    exe = find_tool("ffprobe")
    if not exe:
        return "no-tool"
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "s", "-print_format",
             "json", "-show_streams", path],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        data = json.loads((out.stdout or b"{}").decode("utf-8", "replace") or "{}")
    except Exception:
        return "error"
    tracks = []
    for st in data.get("streams", []):
        tags = st.get("tags") or {}
        tracks.append({"index": st.get("index", 0),
                       "codec": (st.get("codec_name") or "").lower(),
                       "title": tags.get("title"),
                       "language": tags.get("language")})
    return tracks

def extract_subtitle_track(path, stream_index, dest):
    """Pull one text subtitle stream out of a movie into dest (.srt)."""
    exe = find_tool("ffmpeg")
    if not exe:
        return False, "ffmpeg not found on PATH"
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-y", "-i", path, "-map", "0:%d" % stream_index,
             "-c:s", "srt", dest],
            capture_output=True, timeout=900, creationflags=_NO_WINDOW)
    except Exception as exc:
        return False, str(exc)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return True, ""
    err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return False, err[-1] if err else "no subtitle output"

def track_label(n, info):
    codec = (info.get("codec") or "").lower()
    if codec in TEXT_SUB_CODECS:
        kind = "text"
    elif codec in IMAGE_SUB_CODECS:
        kind = "pictures"
    else:
        kind = codec or "?"
    bits = []
    if info.get("title"):
        bits.append(info["title"])
    lang = (info.get("language") or "").lower()
    if lang:
        bits.append(LANG_NAMES.get(lang, LANG_NAMES.get(lang[:2], lang)))
    return "Track %d: %s [%s]" % (n + 1, " / ".join(bits) if bits else "subtitles", kind)

# --------------------------------------------------------------------------

FEMALE_VOICE_RE = re.compile(
    r"female|zira|hazel|susan|heera|kalpana|swara|aria|jenny|eva|linda|heidi"
    r"|karen|moira|tessa|fiona|catherine|samantha|victoria|allison|ava|serena"
    r"|veena|raveena|shruti|neerja|elsa|maria|irina|paulina|sabina|hortense"
    r"|julie|caroline|katja|helena|huihui|yaoyao|haruka|ayumi|heami|sunhi"
    r"|damayanti|pramus", re.I)

_PIPER_LANGS = {
    "en_US": "English (US)", "en_GB": "English (UK)", "hi_IN": "Hindi",
    "es_ES": "Spanish", "fr_FR": "French", "de_DE": "German",
    "it_IT": "Italian", "pt_BR": "Portuguese (BR)", "pt_PT": "Portuguese",
    "ru_RU": "Russian", "ja_JP": "Japanese", "zh_CN": "Chinese",
    "ko_KR": "Korean", "ar_JO": "Arabic", "fa_IR": "Persian",
    "tr_TR": "Turkish", "nl_NL": "Dutch", "pl_PL": "Polish",
    "sv_SE": "Swedish", "uk_UA": "Ukrainian", "vi_VN": "Vietnamese",
    "bn_IN": "Bengali", "ta_IN": "Tamil", "te_IN": "Telugu",
    "mr_IN": "Marathi", "gu_IN": "Gujarati", "pa_IN": "Punjabi",
    "ur_PK": "Urdu", "ne_NP": "Nepali", "ml_IN": "Malayalam",
    "kn_IN": "Kannada", "si_LK": "Sinhala", "th_TH": "Thai",
}


def _piper_label(name):
    """'en_US-lessac-medium' -> 'English (US) · Lessac (medium)' for the
    Voice dropdown; the raw name stays available via _voice_raw."""
    stem = name[:-5] if name.lower().endswith(".onnx") else name
    parts = stem.split("-")
    if len(parts) >= 3:
        lang = _PIPER_LANGS.get(parts[0], parts[0].replace("_", " "))
        return "%s · %s (%s)" % (lang, parts[1].capitalize(), parts[2])
    return stem

# OCR of burned-in (hardcoded) captions. Uses Tesseract if installed
# (winget install UB-Mannheim.TesseractOCR). While "OCR" is ON, VLCspeaker
# samples the current video frame about once a second, reads the caption
# strip at the bottom of the picture, shows the text and, with Read Aloud
# ON, speaks it. Captured lines build up as a subtitle track.
# --------------------------------------------------------------------------

def find_tesseract():
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")):
        if os.path.isfile(p):
            return p
    return None

def ocr_clean_text(raw):
    """Keep lines that look like real caption text, drop OCR noise."""
    lines = []
    for ln in (raw or "").splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if len(ln) < 2:
            continue
        if sum(ch.isalpha() for ch in ln) < 2:
            continue
        if _OCR_DROP_LINE.search(ln):
            continue              # watermark / encoder tag, not a caption
        lines.append(ln)
    return " ".join(lines).strip()

# Caption-strip junk that is burned into videos but is never dialogue:
# copyright glyphs/lines, URLs, release/encoder tags.
_OCR_DROP_LINE = re.compile(
    r"(©|\(c\)|\bc\.?\s*[)\]]|copyright|www\.|\.(com|net|org)\b|\bhttps?\b|"
    r"\b(480p|720p|1080p|2160p|hdtv|hdrip|bdrip|webrip|blu-?ray|dvdrip|"
    r"xvid|x26[45]|h26[45]|hevc|aac2?|yify|yts|remux|encodes?)\b|"
    r"\b(entertainment|music\s*,?\s*inc)\b)", re.IGNORECASE)

def _ocr_dual_ok(a, b):
    """Two OCR passes over the same frame must roughly agree before the
    text may be spoken. A real caption is read the same way from a
    binarized and a plain grayscale strip; a corrupt decode frame or a
    background texture reads differently every time, and that noise is
    what made the voice speak gibberish."""
    ka, kb = _ocr_key(a), _ocr_key(b)
    if ka and ka == kb:
        return True
    wa = set(re.findall(r"[a-z]+", (a or "").lower()))
    wb = set(re.findall(r"[a-z]+", (b or "").lower()))
    if wa and wb:
        common = len(wa & wb)
        return common >= 2 and common * 2 >= min(len(wa), len(wb))
    return False           # non-Latin scripts: only exact agreement

def _ocr_key(s):
    """Punctuation/case-insensitive line identity, so the same caption
    read twice (with tiny OCR differences) counts as the same line."""
    return re.sub(r"\W+", "", (s or "").lower(), flags=re.UNICODE)

_OCR_WORD = re.compile(r"[a-z0-9]+")

def _ocr_same_caption(a, b):
    """True when 'a' is another reading of the SAME caption line as 'b'.
    OCR wobbles a character or two between seconds on unchanged text
    ("I'll" -> "I'II"); those re-reads must extend the running cue, not
    become a new cue that is spoken all over again."""
    ka, kb = _ocr_key(a), _ocr_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if len(ka) > len(kb) + 2:
        return False           # longer text: a new line, not a wobble read
    if difflib.SequenceMatcher(None, ka, kb).ratio() < 0.75:
        return False
    wa = set(_OCR_WORD.findall(a.lower()))
    wb = set(_OCR_WORD.findall(b.lower()))
    if len(wa) < 2 or len(wb) < 2:
        return False
    common = len(wa & wb)
    return common * 2 >= max(len(wa), len(wb)) + 1

# Languages probed for the Windows built-in OCR engine. A tag appears in
# the OCR dropdown only if that Windows language feature is installed.
OCR_LANG_CANDIDATES = [
    "en-US", "en-GB", "hi-IN", "bn-IN", "bn-BD", "ta-IN", "te-IN", "mr-IN",
    "gu-IN", "kn-IN", "ml-IN", "pa-IN", "ur-PK", "ne-NP", "si-LK", "es-ES",
    "es-MX", "fr-FR", "de-DE", "it-IT", "pt-BR", "ru-RU", "ar-SA", "fa-IR",
    "zh-CN", "zh-TW", "ja-JP", "ko-KR", "th-TH", "vi-VN", "tr-TR", "id-ID",
    "fil-PH", "nl-NL", "pl-PL", "uk-UA", "el-GR", "he-IL", "sv-SE", "da-DK",
    "fi-FI", "nb-NO", "cs-CZ", "hu-HU", "ro-RO",
]

# --------------------------------------------------------------------------
# Read Aloud: Windows SAPI text-to-speech on a dedicated thread.
# New speech purges what is currently being spoken, so the voice always
# follows the newest caption (important when you change speed or seek).
# --------------------------------------------------------------------------

# --- Piper neural voices (optional, better quality) ------------------------
# Piper (github.com/rhasspy/piper) runs small neural TTS models fully
# offline. Voice models (.onnx + .onnx.json) are downloaded from
# HuggingFace into piper/voices next to this script; each downloaded
# voice then appears in the Voice dropdown as "Piper: <name>".

PIPER_VOICES_DIR = os.path.join(APP_DIR, "piper", "voices")
# Second voice folder under the user profile: the install folder
# (C:\Program Files\VLCSpeaker) is read-only, so freshly downloaded
# voices are saved there instead. Both folders are scanned, so voices
# shipped with the installer and voices downloaded later coexist.
PIPER_USER_VOICES_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    APP_NAME, "piper", "voices")
PIPER_HF_TREE = ("https://huggingface.co/api/models/rhasspy/piper-voices/"
                 "tree/main?recursive=true&limit=1000")
PIPER_HF_RESOLVE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"


def find_piper_voices():
    """Scan both voice folders for .onnx models (with their .json sidecar).
    The install folder wins when the same voice exists in both."""
    out, seen = [], set()
    for d in (PIPER_VOICES_DIR, PIPER_USER_VOICES_DIR):
        with contextlib.suppress(OSError):
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".onnx") or fn in seen:
                    continue
                model = os.path.join(d, fn)
                cfg = model + ".json"
                if os.path.isfile(cfg):
                    out.append({"name": fn[:-5], "model": model, "config": cfg})
                    seen.add(fn)
    return out

def piper_voice_config(cfg_path):
    """Sample rate / speaker count from a voice's .onnx.json sidecar."""
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        audio = data.get("audio") or {}
        return {"sample_rate": int(audio.get("sample_rate") or 22050),
                "num_speakers": int(data.get("num_speakers") or 1)}
    except Exception:
        return {"sample_rate": 22050, "num_speakers": 1}

def fetch_piper_catalog():
    """List downloadable voices from HuggingFace: [(name, size_bytes, path)].
    Follows the tree API's Link: rel="next" pagination (~400 voices)."""
    url, found = PIPER_HF_TREE, {}
    for _page in range(12):
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            link = resp.headers.get("Link") or ""
        for item in data:
            path = item.get("path", "")
            if path.endswith(".onnx"):
                size = (item.get("lfs") or {}).get("size") or item.get("size")
                found[path] = int(size or 0)
        if 'rel="next"' not in link:
            break
        url = link.split("<", 1)[1].split(">", 1)[0]
    out = []
    for path, size in sorted(found.items()):
        name = os.path.basename(path)
        if name.endswith(".onnx"):
            out.append((name[:-5], size, path))
    return out

def download_piper_voice(name, repo_path, dest_dir=None, progress=None):
    """Download one voice (the .onnx model + .json sidecar) from HuggingFace.
    progress(fraction_done, message) is called while data arrives.
    By default the voice goes to the install folder when it is writable,
    otherwise to the user profile (Program Files is read-only)."""
    if dest_dir is None:
        try:
            os.makedirs(PIPER_VOICES_DIR, exist_ok=True)
            test = os.path.join(PIPER_VOICES_DIR, ".wtest")
            with open(test, "w") as f:
                f.write("")
            os.remove(test)
            dest_dir = PIPER_VOICES_DIR
        except OSError:
            dest_dir = PIPER_USER_VOICES_DIR
    os.makedirs(dest_dir, exist_ok=True)
    for suffix in ("", ".json"):          # the model, then its .onnx.json
        url = PIPER_HF_RESOLVE + repo_path + suffix
        dest = os.path.join(dest_dir, name + ".onnx" + suffix)
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    f.write(block)
                    done += len(block)
                    if progress and total:
                        progress(done / total,
                                 "Downloading %s.onnx%s, %.0f%% of %.0f MB"
                                 % (name, suffix or "", 100.0 * done / total,
                                    total / 1e6))
        os.replace(tmp, dest)
    if progress:
        progress(1.0, "Downloaded: %s" % name)


# --------------------------------------------------------------------------
# Live translation (fully offline): Tencent Hy-MT2 GGUF models served by a
# local llama.cpp "llama-server" subprocess on 127.0.0.1.
#
# Picking a language in the MT box downloads (first use only) and starts one
# shared llama-server. Subtitle lines are translated one by one on a worker
# thread, strictly in cue order, and spoken through the normal Read Aloud
# queue; the NEXT line is translated ahead of time (prefetch) so its
# translation is already cached when the line comes up, and that is what
# keeps the extra delay invisible.
#
# Downloads (nothing is bundled with the installer, unlike Piper voices):
#   * llama.cpp Windows release zip (github.com/ggml-org/llama.cpp): the
#     CUDA-12.4 build when an NVIDIA GPU is present (plus the separate
#     cudart zip it needs), the plain CPU build otherwise. Stock release
#     builds are enough for the standard Q4_K_M/Q6_K quants; only
#     Tencent's extreme 2-bit ternary quants need an unmerged llama.cpp
#     patch, and those are not used here.
#   * the model GGUF from HuggingFace (tencent/Hy-MT2-*-GGUF, Apache-2.0).
# Unlike the voices, the user profile folder is tried FIRST: the model is
# big (1.4-4.5 GB) and must not land in a synced folder (OneDrive) or in
# read-only Program Files.
#
# Hy-MT2 has no system prompt; the documented translation instruction goes
# out as the user message and the server applies the GGUF chat template
# (--jinja). Sampling follows Tencent's recommendation for the dense models.

MT_LLAMACPP_DIRS = (
    os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                 APP_NAME, "mt", "llamacpp"),
    os.path.join(APP_DIR, "mt", "llamacpp"))
MT_MODEL_DIRS = (
    os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                 APP_NAME, "mt", "models"),
    os.path.join(APP_DIR, "mt", "models"))

# The languages Hy-MT2 is trained for (displayed name == prompt name).
MT_LANGS = ["English", "Hindi", "Bengali", "Tamil", "Telugu", "Marathi",
            "Gujarati", "Kannada", "Malayalam", "Punjabi", "Urdu", "Nepali",
            "Sinhala", "Chinese", "Japanese", "Korean", "Spanish", "French",
            "German", "Italian", "Portuguese", "Russian", "Arabic",
            "Persian", "Turkish", "Vietnamese", "Indonesian", "Thai",
            "Filipino", "Malay", "Polish", "Dutch", "Ukrainian"]

MT_MODELS = {
    "7b": {"label": "Hy-MT2-7B", "repo": "tencent/Hy-MT2-7B-GGUF",
           "file": "Hy-MT2-7B-Q4_K_M.gguf", "approx_mb": 4500,
           "note": "best quality, needs an NVIDIA GPU"},
    "1.8b": {"label": "Hy-MT2-1.8B", "repo": "tencent/Hy-MT2-1.8B-GGUF",
             "file": "Hy-MT2-1.8B-Q4_K_M.gguf", "approx_mb": 1100,
             "note": "fast, runs on the CPU too"},
}

MT_PROMPT = ("Translate the following text into {lang}. Note that you "
             "should only output the translated result without any "
             "additional explanation:\n{text}")

_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.S | re.I)


def mt_build_prompt(text, lang):
    return MT_PROMPT.format(lang=lang, text=text)


def _noop_progress(frac, msg):
    pass


# Local llama-server calls must never go through a configured HTTP proxy.
_MT_LOCAL_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}))


class Translator:
    """One llama-server subprocess plus an ordered translate worker.

    States (self.state): idle -> downloading -> starting -> ready, or
    error; every change bumps self.rev so the UI picks it up in _tick.
    Jobs submitted by the UI are translated in order; purge() (seek /
    pause / stop / Read Aloud off) drops everything queued so a seek
    never makes the voice read old lines. Finished translations stay in
    the cache, so re-watching a scene is instant."""

    HEALTH_TIMEOUT_S = 300.0
    CACHE_MAX = 4000

    def __init__(self, post=None, on_result=None):
        self.post = post              # fn(callable): run it on the UI thread
        self.on_result = on_result    # fn(idx, text, translation_or_None)
        self._lock = threading.Lock()
        self.state = "idle"
        self.state_msg = ""
        self.rev = 0
        self.error = None
        self.model_key = None         # model the RUNNING server serves
        self._proc = None
        self._port = None
        self._log_fh = None
        self._cpu_fallback = False    # CUDA runtime failed, use the CPU build
        self._gpu_info = self.detect_gpu()
        # ordered worker queue: (gen, idx, speak, text, lang)
        self._q = collections.deque()
        self._gen = 0
        self._wake = threading.Event()
        self._cache = {}              # (lang, text) -> translation
        self._cache_order = collections.deque()
        self._pending = set()         # (lang, text) queued or cached
        self._thread = threading.Thread(target=self._work, daemon=True,
                                        name="mt-translate")
        self._thread.start()

    # ---------------- hardware ----------------

    @staticmethod
    def detect_gpu():
        """The strongest NVIDIA GPU nvidia-smi reports (name + free MiB)."""
        exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
        try:
            out = subprocess.run(
                [exe, "--query-gpu=name,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW)
            best = None
            for ln in (out.stdout or b"").decode("utf-8", "replace").splitlines():
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) < 2 or "nvidia" not in parts[0].lower():
                    continue
                try:
                    free = int(float(parts[1]))
                except ValueError:
                    continue
                if best is None or free > best[1]:
                    best = (parts[0], free)
            if best:
                return {"has": True, "name": best[0], "free_mb": best[1]}
        except Exception:
            pass
        return {"has": False, "name": "", "free_mb": 0}

    def default_model_key(self):
        if self._gpu_info.get("has") and self._gpu_info.get("free_mb", 0) >= 5000:
            return "7b"
        return "1.8b"

    # ---------------- what is on disk ----------------

    @staticmethod
    def find_llama_server():
        # release zips used to extract into bin/, current ones are flat
        for d in MT_LLAMACPP_DIRS:
            for sub in ("", "bin"):
                exe = os.path.join(d, sub, "llama-server.exe")
                if os.path.isfile(exe):
                    return exe
        return None

    @staticmethod
    def find_model_gguf(key):
        fn = (MT_MODELS.get(key) or {}).get("file")
        if not fn:
            return None
        for d in MT_MODEL_DIRS:
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                return p
        return None

    def installed(self, key):
        return self.find_model_gguf(key) is not None

    def llamacpp_ready(self):
        return self.find_llama_server() is not None

    @staticmethod
    def _writable_dir(dirs):
        """First dir of `dirs` that accepts a write test, else the last."""
        for d in dirs:
            try:
                os.makedirs(d, exist_ok=True)
                test = os.path.join(d, ".wtest")
                with open(test, "w"):
                    pass
                os.remove(test)
                return d
            except OSError:
                continue
        return dirs[-1]

    @staticmethod
    def _log_path():
        d = os.path.join(os.environ.get("LOCALAPPDATA")
                         or os.path.expanduser("~"), APP_NAME, "mt")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = tempfile.gettempdir()
        return os.path.join(d, "llama-server.log")

    def _log_tail(self, n=10):
        try:
            with open(self._log_path(), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 8000))
                lines = (f.read().decode("utf-8", "replace")
                         .strip().splitlines())
            return (" | ".join(lines[-n:])[-400:] or "empty log")
        except Exception:
            return "no log"

    # ---------------- downloads ----------------

    def _download(self, url, dest, label, progress):
        """Stream one file to dest (as .part, atomically renamed)."""
        tmp = dest + ".part"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    block = resp.read(512 * 1024)
                    if not block:
                        break
                    f.write(block)
                    done += len(block)
                    if progress and total:
                        progress(done / total, "%s, %.0f%% of %.0f MB"
                                 % (label, 100.0 * done / total, total / 1e6))
        os.replace(tmp, dest)

    def _llama_release_assets(self, want_gpu):
        """(main zip URL, cudart zip URL or None) from the newest llama.cpp
        release that carries Windows x64 builds. The GitHub 'latest'
        endpoint can point at a source-only tag, so scan the list."""
        req = urllib.request.Request(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases"
            "?per_page=10", headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
        for rel in releases:
            urls = {a.get("name", ""): a.get("browser_download_url")
                    for a in rel.get("assets", [])}
            if want_gpu:
                main = next((n for n in urls if re.match(
                    r"^llama-.*-bin-win-cuda-12\.\d+-x64\.zip$", n)), None)
                cud = next((n for n in urls if re.match(
                    r"^cudart-llama-bin-win-cuda-12\.\d+-x64\.zip$", n)), None)
                if main and cud:
                    return urls[main], urls[cud]
            cpu = next((n for n in urls
                        if re.match(r"^llama-.*-bin-win-cpu-x64\.zip$", n)),
                       None)
            if cpu:
                return urls[cpu], None
        return None

    # Disk headroom needed to download+extract each engine build (zips are
    # deleted after extraction, but the peak matters on a full drive).
    CUDA_DISK_MB = 3000
    CPU_DISK_MB = 500

    @staticmethod
    def _free_mb(path):
        p = path if os.path.exists(path) else os.path.dirname(path) or "."
        try:
            return shutil.disk_usage(p).free // (1024 * 1024)
        except Exception:
            return 1 << 30            # unknown: assume plenty

    @staticmethod
    def _llamacpp_is_cuda(exe):
        """The CUDA build is recognized by its cudart DLLs next to the exe."""
        d = os.path.dirname(exe)
        return any(os.path.isfile(os.path.join(d, dll)) for dll in
                   ("cudart64_12.dll", "cudart64_13.dll"))

    def ensure_llamacpp(self, progress=_noop_progress):
        """Make sure a llama-server.exe of the RIGHT build is on disk;
        return (path, is_cuda_build). The CUDA build is used when an NVIDIA
        GPU is present and the disk has room for it; otherwise (or when the
        GPU start failed before) the small CPU build. A CPU build on disk
        is upgraded to CUDA automatically once the disk allows, so a
        low-disk install gets faster by itself later."""
        want_gpu = (bool(self._gpu_info.get("has"))
                    and not self._cpu_fallback)
        exe = self.find_llama_server()
        if exe:
            is_cuda = self._llamacpp_is_cuda(exe)
            if is_cuda or not want_gpu:
                return exe, is_cuda
            if self._free_mb(exe) < self.CUDA_DISK_MB:
                return exe, False     # no room for CUDA, stay on the CPU
        elif want_gpu and self._free_mb(
                self._writable_dir(MT_LLAMACPP_DIRS)) < self.CUDA_DISK_MB:
            want_gpu = False          # GPU wanted but the disk cannot fit it
        dest_root = self._writable_dir(MT_LLAMACPP_DIRS)
        assets = self._llama_release_assets(want_gpu)
        if not assets:
            raise RuntimeError("could not find a Windows llama.cpp release "
                               "(check your internet connection)")
        main_url, cudart_url = assets
        self._download(main_url, os.path.join(dest_root, "llama.zip"),
                       "Downloading the llama.cpp translation engine",
                       progress)
        with zipfile.ZipFile(os.path.join(dest_root, "llama.zip")) as z:
            z.extractall(dest_root)
        if cudart_url:
            self._download(cudart_url, os.path.join(dest_root, "cudart.zip"),
                           "Downloading the GPU runtime (cudart)",
                           progress)
            with zipfile.ZipFile(os.path.join(dest_root, "cudart.zip")) as z:
                z.extractall(dest_root)
        with contextlib.suppress(OSError):
            os.remove(os.path.join(dest_root, "llama.zip"))
            os.remove(os.path.join(dest_root, "cudart.zip"))
        exe = self.find_llama_server()
        if not exe:
            raise RuntimeError("llama.cpp downloaded but llama-server.exe "
                               "was not found in the zip")
        return exe, self._llamacpp_is_cuda(exe)

    def ensure_model(self, key, progress=_noop_progress):
        gguf = self.find_model_gguf(key)
        if gguf:
            return gguf
        info = MT_MODELS[key]
        dest_root = self._writable_dir(MT_MODEL_DIRS)
        need = int(info.get("approx_mb", 1000) * 1.2)
        free = self._free_mb(dest_root)
        if free < need:
            raise RuntimeError("not enough disk space for %s (needs about "
                               "%d MB, only %d MB free)"
                               % (info["label"], need, free))
        url = ("https://huggingface.co/%s/resolve/main/%s"
               % (info["repo"], info["file"]))
        dest = os.path.join(dest_root, info["file"])
        self._download(url, dest, "Downloading %s (%s)"
                       % (info["label"], info["file"]), progress)
        return dest

    # ---------------- server lifecycle ----------------

    def start_async(self, key):
        """(Re)start the server with `key`'s model on a background thread."""
        threading.Thread(target=self._start_work, args=(key,),
                         daemon=True, name="mt-start").start()

    @staticmethod
    def _free_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _stop_proc(self):
        proc, fh = self._proc, self._log_fh
        self._proc = None
        self._log_fh = None
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()

    def stop_server(self):
        self._stop_proc()
        self.model_key = None
        self._bump("idle", "translation off")

    def shutdown(self):
        self._stop_proc()

    def _spawn(self, exe, gguf, use_gpu):
        self._port = self._free_port()
        args = [exe, "--model", gguf, "--host", "127.0.0.1",
                "--port", str(self._port), "--jinja",
                "--ctx-size", "2048",
                "--threads", str(max(2, (os.cpu_count() or 4) - 2)),
                "--n-gpu-layers", "999" if use_gpu else "0"]
        self._log_fh = open(self._log_path(), "ab")
        self._proc = subprocess.Popen(
            args, stdout=self._log_fh, stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW, cwd=os.path.dirname(exe))

    def _health_wait(self):
        """True once /health reports ok; False if the process died first or
        the deadline (model loading from a cold disk) passed."""
        url = "http://127.0.0.1:%d/health" % self._port
        deadline = time.time() + self.HEALTH_TIMEOUT_S
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return False
            try:
                with _MT_LOCAL_OPENER.open(url, timeout=2) as r:
                    if json.loads(r.read().decode("utf-8", "replace")
                                  or "{}").get("status") == "ok":
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _start_work(self, key):
        if self.state == "ready" and self.model_key == key:
            return
        self._stop_proc()
        try:
            self._bump("downloading", "Preparing the translation model…")
            exe, use_gpu = self.ensure_llamacpp()
            gguf = self.ensure_model(key)
            info = MT_MODELS[key]
            self._bump("starting", "Starting %s…" % info["label"])
            self._spawn(exe, gguf, use_gpu)
            ok = self._health_wait()
            if not ok and use_gpu:
                # CUDA runtime / driver mismatch: retry with the CPU build
                self._stop_proc()
                self._cpu_fallback = True
                self._bump("downloading",
                           "GPU start failed, getting the CPU engine…")
                exe = self.ensure_llamacpp()
                self._bump("starting",
                           "Starting %s on the CPU…" % info["label"])
                self._spawn(exe, gguf, False)
                ok = self._health_wait()
            if not ok:
                raise RuntimeError("the translation engine did not become "
                                   "ready: %s" % self._log_tail())
            self.model_key = key
            self._bump("ready", "%s ready" % info["label"])
        except Exception as exc:
            self._stop_proc()
            self._bump("error", str(exc))

    def _bump(self, state, msg=""):
        with self._lock:
            self.state = state
            self.state_msg = msg
            if state == "error":
                self.error = msg
            self.rev += 1

    # ---------------- translate ----------------

    def translate(self, text, lang):
        """Blocking translate (worker thread only). Cached lines return
        instantly; anything else goes to the local llama-server."""
        text = (text or "").strip()
        if not text:
            return ""
        key = (lang, text)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        body = {
            "messages": [{"role": "user",
                          "content": mt_build_prompt(text, lang)}],
            "max_tokens": 320, "temperature": 0.7, "top_p": 0.6,
            "top_k": 20, "repeat_penalty": 1.05,
            "stream": False, "cache_prompt": True,
        }
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % self._port,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with _MT_LOCAL_OPENER.open(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        out = ""
        try:
            out = data["choices"][0]["message"].get("content") or ""
        except Exception:
            out = ""
        out = _THINK_RE.sub("", out).strip().strip('"').strip()
        with self._lock:
            if key not in self._cache:
                self._cache[key] = out
                self._cache_order.append(key)
                while len(self._cache_order) > self.CACHE_MAX:
                    old = self._cache_order.popleft()
                    self._cache.pop(old, None)
        return out

    # ---------------- job queue ----------------

    def submit(self, idx, text, lang):
        """Translate `text` and then deliver it for speaking (in order)."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._q.append((self._gen, idx, True, text, lang))
            self._pending.add((lang, text))
        self._wake.set()

    def prefetch(self, idx, text, lang):
        """Translate ahead of time; only the cache result is used later."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            key = (lang, text)
            if key in self._cache or key in self._pending:
                return
            self._q.append((self._gen, idx, False, text, lang))
            self._pending.add(key)
        self._wake.set()

    def purge(self):
        with self._lock:
            self._gen += 1
            self._q.clear()
            self._pending.clear()
        self._wake.set()

    def _wait_ready(self, speak_job):
        """Block until the server can translate. Prefetch jobs give up
        quickly (the line is prefetched again on the next tick), speak jobs
        wait a while (e.g. while the model is still starting) and then let
        the original text through rather than stalling the voice."""
        if self.state == "ready":
            return True
        deadline = time.time() + (180 if speak_job else 2)
        while time.time() < deadline:
            if self.state in ("ready", "error", "idle"):
                return self.state == "ready"
            time.sleep(0.25)
        return self.state == "ready"

    def _work(self):
        while True:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            while True:
                with self._lock:
                    while self._q and self._q[0][0] != self._gen:
                        _sg, _si, _sp, _st, _sl = self._q.popleft()
                        self._pending.discard((_sl, (_st or "").strip()))
                    item = self._q.popleft() if self._q else None
                if item is None:
                    break
                _gen, idx, speak, text, lang = item
                translation = None
                if self._wait_ready(speak):
                    try:
                        translation = self.translate(text, lang)
                    except Exception:
                        translation = None
                with self._lock:
                    self._pending.discard((lang, text))
                if speak and self.on_result is not None and self.post:
                    self.post(lambda i=idx, t=text, r=translation:
                              self.on_result(i, t, r))


class TTSSpeaker:
    """Windows SAPI text-to-speech on a dedicated thread.

    Spoken lines are QUEUED: when a new caption appears, the line that is
    being read finishes FULLY first, then the next line is spoken, nothing
    is cut mid-sentence. At most MAX_PENDING lines are buffered; if the
    voice falls too far behind the movie, the oldest buffered line is
    dropped so it can catch up. purge() stops speech immediately (used on
    seek / pause / Read Aloud off / Stop)."""

    MAX_PENDING = 3

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = collections.deque()   # (gen, text)
        self._gen = 0                          # bumped by purge()
        self._voice_req = None
        self._rate_req = None
        self._wake = threading.Event()
        self.voices = []          # SAPI display names, filled by the worker
        self.ready = False
        self.error = None
        # --- piper state ---
        self.piper_voices = []    # [{"name","model","config"}], worker-filled
        self.piper_ready = False  # first scan finished
        self.piper_rev = 0        # bumped when the list changes (UI refreshes)
        self.piper_error = None   # missing piper-tts / PyAudio
        self._piper_req = None    # requested voice name -> set_piper_voice()
        self._piper_cur = None    # currently selected name (worker-owned)
        self._piper_voice = None  # loaded PiperVoice (worker-owned)
        self._piper_loaded = None # name of the loaded voice
        self._piper_info = None   # sample_rate / num_speakers
        self._engine = "sapi"     # what the worker currently speaks with
        self._rate_cur = 0        # last applied rate (drives piper speed too)
        self._pa = None           # pyaudio.PyAudio, created on first piper use
        self._thread = threading.Thread(target=self._run, daemon=True, name="tts")
        self._thread.start()

    def say(self, text):
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._pending.append((self._gen, text))
            while len(self._pending) > self.MAX_PENDING:
                self._pending.popleft()       # too far behind: skip oldest
        self._wake.set()

    def purge(self):
        with self._lock:
            self._gen += 1
            self._pending.clear()
        self._wake.set()

    def set_voice(self, index):
        with self._lock:
            self._voice_req = index
        self._wake.set()

    def set_rate(self, rate):
        with self._lock:
            self._rate_req = rate
        self._wake.set()

    def set_piper_voice(self, name):
        """Speak with a Piper neural voice ("en_US-lessac-medium")."""
        with self._lock:
            self._piper_req = name
        self._wake.set()

    def rescan_piper(self):
        threading.Thread(target=self._piper_scan, daemon=True,
                         name="tts-piper-scan").start()

    def _piper_scan(self):
        self.piper_voices = find_piper_voices()
        self.piper_error = None
        if self.piper_voices:
            try:
                import piper   # noqa: F401
                import pyaudio  # noqa: F401
            except Exception as exc:
                # without the modules nothing could be played: never offer
                # voice models that can only ever fail
                self.piper_error = ("pip install piper-tts PyAudio (%s)" % exc)
                self.piper_voices = []
        self.piper_ready = True
        self.piper_rev += 1

    def _piper_length_scale(self):
        # SAPI rate -10..10 -> piper length_scale (smaller = faster)
        factor = 1.0 + self._rate_cur * 0.05
        return max(0.4, min(3.0, 1.0 / max(0.1, factor)))

    def _speak_piper(self, text, gen):
        """Synthesize + play one line with Piper. Returns when the line has
        finished playing or was purged (gen changed)."""
        import pyaudio
        from piper import PiperVoice, SynthesisConfig

        if self._piper_voice is None or self._piper_loaded != self._piper_cur:
            voice = next((v for v in self.piper_voices
                          if v["name"] == self._piper_cur), None)
            if voice is None:
                return
            self._piper_voice = PiperVoice.load(voice["model"])
            self._piper_loaded = voice["name"]
            self._piper_info = piper_voice_config(voice["config"])
        if self._pa is None:
            self._pa = pyaudio.PyAudio()

        kwargs = {"length_scale": self._piper_length_scale()}
        if (self._piper_info or {}).get("num_speakers", 1) > 1:
            kwargs["speaker_id"] = 0
        try:
            syn_cfg = SynthesisConfig(**kwargs)
        except Exception:
            syn_cfg = None

        stream = None
        try:
            for chunk in self._piper_voice.synthesize(text, syn_cfg):
                with self._lock:
                    if self._gen != gen:      # purged while speaking
                        return
                data = chunk.audio_int16_bytes
                if not data:
                    continue
                if stream is None:
                    stream = self._pa.open(
                        format=pyaudio.paInt16,
                        channels=max(1, chunk.sample_channels),
                        rate=chunk.sample_rate, output=True,
                        frames_per_buffer=2048)
                stream.write(data)
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                    stream.close()

    def _run(self):
        try:
            import comtypes.client
        except Exception as exc:
            self.error = "comtypes is not installed (%s). Run: pip install comtypes" % exc
            return
        try:
            comtypes.CoInitialize()
            v = comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True)
            tokens = v.GetVoices()
            for i in range(tokens.Count):
                self.voices.append(tokens.Item(i).GetDescription(0))
            self._v = v
            self.ready = True
        except Exception as exc:
            self.error = "Could not start Windows speech (SAPI): %s" % exc
            return
        with contextlib.suppress(Exception):
            self._piper_scan()                 # find downloaded .onnx voices
        while True:
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            while True:                        # speak the queue in order
                with self._lock:
                    item = None
                    for it in tuple(self._pending):
                        if it[0] == self._gen:  # skip stale, purged lines
                            item = it
                            break
                    if item is not None:
                        self._pending.remove(item)
                    voice_req, self._voice_req = self._voice_req, None
                    rate_req, self._rate_req = self._rate_req, None
                    piper_req, self._piper_req = self._piper_req, None
                if item is None and voice_req is None and rate_req is None \
                        and piper_req is None:
                    break
                with contextlib.suppress(Exception):
                    if rate_req is not None:
                        self._rate_cur = max(-10, min(10, int(rate_req)))
                        v.Rate = self._rate_cur
                    if voice_req is not None:   # a Windows (SAPI) voice
                        self._engine = "sapi"
                        toks = v.GetVoices()
                        if 0 <= voice_req < toks.Count:
                            v.Voice = toks.Item(voice_req)
                    if piper_req is not None:   # a Piper neural voice
                        try:
                            self._piper_cur = piper_req
                            self._piper_voice = None   # force (re)load
                            if next((v2 for v2 in self.piper_voices
                                     if v2["name"] == piper_req), None):
                                self._engine = "piper"
                            else:
                                self._piper_cur = None
                                self._engine = "sapi"
                        except Exception as exc:
                            self._piper_cur = None
                            self._engine = "sapi"
                            self.piper_error = str(exc)
                if item is None:
                    continue
                _gen, text = item
                if self._engine == "piper" and self._piper_cur:
                    with contextlib.suppress(Exception):
                        self._speak_piper(text, _gen)   # blocks until done
                    continue
                try:
                    v.Speak(text, 1)           # async, does NOT purge others
                except Exception:
                    continue
                while True:                    # wait until this line ends
                    with self._lock:
                        purged = (self._gen != _gen)
                    if purged:
                        with contextlib.suppress(Exception):
                            v.Speak("", 3)     # stop it right now
                        break
                    try:
                        if v.Status.RunningState == 1:   # 1 = ready/done
                            break
                    except Exception:
                        break
                    time.sleep(0.05)

# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

VIDEO_TYPES = [("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.wmv "
                             "*.ts *.mpg *.mpeg *.flv *.ogv"),
               ("All files", "*.*")]
SUB_TYPES = [("Subtitle files", "*.srt *.vtt *.ass *.ssa"), ("All files", "*.*")]
SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v",
              ".wmv", ".ts", ".mpg", ".mpeg", ".flv", ".ogv")

def fmt_time(ms):                    # VLC style: 00:01 / 09:25
    ms = max(0, int(ms)) // 1000
    h, rest = divmod(ms, 3600)
    m, s = divmod(rest, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


# ---------------------------------------------------------------------------
# "VLC Redesign (Community)" design tokens. Geometry/colors come from the
# redesign SVGs: 24-unit icon grid with 1.5-unit rounded strokes, idle
# icons #8F8F8F, hover #AB6314 on a white-6% wash, one orange accent
# #D37918, near-black rounded tracks. The spectrum gradient is from
# volume.svg and encodes loudness (quiet green to loud red); the subtitle
# blocks on the seek track are the mock's sub timeline.
# ---------------------------------------------------------------------------

DESIGN = {"icon": "#8F8F8F", "icon_hover": "#AB6314", "accent": "#D37918",
          "accent_hover": "#E0852B", "wash": "#272727",
          "glyph_dark": "#161616", "bar": "#191919", "track": "#090909",
          "track_fill": "#371F06", "thumb": "#D37918", "cue_block": "#2A2A2A",
          "sep": "#2A2A2A",
          "spectrum": ("#95D318", "#B2A918", "#D37918", "#FF3737")}


class Seekbar(tk.Canvas):
    """Seek slider from the redesign control-bar mock: near-black rounded
    trough, amber filled portion, orange rounded thumb, and subtitle spans
    drawn as blocks on the track (the mock's sub timeline). Click or drag
    anywhere to scrub."""

    HEIGHT = 20                 # slightly taller than before, mock-like
    TROUGH_H = 12
    THUMB_W = 8
    TROUGH = DESIGN["track"]
    FILL = DESIGN["track_fill"]
    THUMB = DESIGN["thumb"]
    MAX_CUES = 1500             # cap: tracks with thousands of tiny cues

    def __init__(self, master, on_scrub=None, on_seek=None, **kw):
        super().__init__(master, height=self.HEIGHT, highlightthickness=0,
                         bd=0, bg=master["bg"], **kw)
        self._frac = 0.0
        self._hover = False
        self._dragging = False
        self._cues = []            # [(start_frac, end_frac)] on the track
        self._cue_sig = None
        self._on_scrub = on_scrub      # fn(True while dragging / False)
        self._on_seek = on_seek        # fn(frac 0..1) on release
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Configure>", lambda e: self._draw())

    def set_cues(self, spans):
        """Show subtitle spans as blocks on the track (pass [] to clear)."""
        self._cues = list(spans or [])
        self._cue_sig = None
        self._draw()

    def _enter(self, _e):
        self._hover = True

    def _leave(self, _e):
        self._hover = False

    def set_fraction(self, frac):
        frac = min(1.0, max(0.0, frac))
        if self._dragging or abs(frac - self._frac) < 0.0005:
            return
        self._frac = frac
        self._draw()

    def _draw(self):
        w = self.winfo_width()
        if w < 20:
            return
        cy = self.HEIGHT // 2
        half = self.TROUGH_H // 2
        pad = self.THUMB_W
        # z-order (bottom to top): trough/fill, cue blocks, thumb. The base
        # is cheap and redrawn every tick; the cue band only when its size
        # or cue count changes, so per-tick repaints stay cheap even with
        # 1000+ cues.
        self.delete("base")
        _rr(self, pad, cy - half, w - pad, cy + half, 6,
            fill=self.TROUGH, outline="", tags="base")
        x = pad + (w - 2 * pad) * self._frac
        if self._frac > 0.001:
            _rr(self, pad, cy - half, max(pad + 2, x), cy + half, 6,
                fill=self.FILL, outline="", tags="base")
        sig = (w, len(self._cues))
        if sig != self._cue_sig:
            self._cue_sig = sig
            self.delete("cues")
            for a, b in self._cues[:self.MAX_CUES]:
                _rr(self, pad + (w - 2 * pad) * a, cy - half + 2,
                    pad + (w - 2 * pad) * b, cy + half - 2, 2,
                    fill=DESIGN["cue_block"], outline="", tags="cues")
        self.delete("thumb")
        _rr(self, x - self.THUMB_W / 2.0, cy - half + 1,
            x + self.THUMB_W / 2.0, cy + half - 1, 3,
            fill=self.THUMB, outline="", tags="thumb")
        self.tag_raise("cues")
        self.tag_raise("thumb")

    def _frac_from_event(self, e):
        pad = self.THUMB_W
        w = max(1, self.winfo_width() - 2 * pad)
        return min(1.0, max(0.0, (e.x - pad) / w))

    def _press(self, e):
        self._dragging = True
        self._frac = self._frac_from_event(e)
        self._draw()
        if self._on_scrub:
            self._on_scrub(True)

    def _motion(self, e):
        if not self._dragging:
            return
        self._frac = self._frac_from_event(e)
        self._draw()

    def _release(self, e):
        if not self._dragging:
            return
        self._dragging = False
        self._frac = self._frac_from_event(e)
        self._draw()
        if self._on_scrub:
            self._on_scrub(False)
        if self._on_seek:
            self._on_seek(self._frac)


class _ToolTip:
    """Small dark hover tooltip (shows after a short delay)."""
    DELAY_MS = 450

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._tw = None
        self._after = None
        widget.bind("<Enter>", lambda _e: self._show(), add="+")
        widget.bind("<Leave>", lambda _e: self._hide(), add="+")
        widget.bind("<ButtonPress>", lambda _e: self._hide(), add="+")

    def _show(self):
        self._cancel()
        self._after = self.widget.after(self.DELAY_MS, self._show_now)

    def _show_now(self):
        self._after = None
        try:
            if not self.widget.winfo_viewable():
                return
        except Exception:
            return
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        with contextlib.suppress(Exception):
            tw.wm_attributes("-topmost", True)
        tk.Label(tw, text=self.text, justify="left",
                 bg="#0c0d10", fg="#e9ebf0", bd=1, relief="solid",
                 font=("Segoe UI", 8), padx=7, pady=3).pack()
        tw.update_idletasks()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() - 30
        sw, sh = tw.winfo_screenwidth(), tw.winfo_screenheight()
        x = max(0, min(x, sw - tw.winfo_reqwidth() - 4))
        y = max(0, min(y, sh - tw.winfo_reqheight() - 4))
        tw.wm_geometry("+%d+%d" % (x, y))
        self._tw = tw

    def _cancel(self):
        if self._after is not None:
            with contextlib.suppress(Exception):
                self.widget.after_cancel(self._after)
            self._after = None
        if self._tw is not None:
            with contextlib.suppress(Exception):
                self._tw.destroy()
            self._tw = None

    def _hide(self):
        self._cancel()


# ---------------------------------------------------------------------------
# "VLC Redesign (Community)" widgets: canvas-drawn icons, the primary play
# pill and the spectrum volume slider, shared by the docked and fullscreen
# bars so both screens look identical. Tokens live in DESIGN (above).
# ---------------------------------------------------------------------------


def _m(cx, cy, s, *units):
    """Map 24-grid unit coords to canvas coords around (cx, cy)."""
    out = []
    for i in range(0, len(units), 2):
        out.append(cx + (units[i] - 12) * s)
        out.append(cy + (units[i + 1] - 12) * s)
    return out


def _rr(cv, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle as a smoothed polygon (canvas rects have no rx)."""
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def _stroke(cv, pts, color, w):
    cv.create_line(*pts, fill=color, width=w, capstyle="round",
                   joinstyle="round")


def _ic_play(cv, cx, cy, s, c):
    cv.create_polygon(_m(cx, cy, s, 9, 6.5, 18.5, 12, 9, 17.5),
                      fill=c, outline=c, joinstyle="round")


def _ic_pause(cv, cx, cy, s, c):
    for x in (7.5, 13.5):
        cv.create_rectangle(*_m(cx, cy, s, x, 6.5, x + 3, 17.5),
                            fill=c, outline=c)


def _ic_stop(cv, cx, cy, s, c):
    cv.create_rectangle(*_m(cx, cy, s, 7.5, 7.5, 16.5, 16.5),
                        fill=c, outline=c)


def _ic_back10(cv, cx, cy, s, c):
    w = max(1, round(1.5 * s))
    _stroke(cv, _m(cx, cy, s, 7.5, 7, 7.5, 17), c, w)
    cv.create_polygon(_m(cx, cy, s, 10.5, 12, 16.5, 7.5, 16.5, 16.5),
                      fill=c, outline=c, joinstyle="round")


def _ic_fwd10(cv, cx, cy, s, c):
    w = max(1, round(1.5 * s))
    _stroke(cv, _m(cx, cy, s, 16.5, 7, 16.5, 17), c, w)
    cv.create_polygon(_m(cx, cy, s, 13.5, 12, 7.5, 7.5, 7.5, 16.5),
                      fill=c, outline=c, joinstyle="round")


def _ic_volume(cv, cx, cy, s, c):
    cv.create_polygon(_m(cx, cy, s, 4.5, 9.5, 7.5, 9.5, 11, 6, 11, 18,
                         7.5, 14.5, 4.5, 14.5), fill=c, outline=c,
                      joinstyle="round")
    w = max(1, round(1.5 * s))
    for x0, y0, x1, y1 in ((12.5, 8.5, 16.5, 15.5), (14.5, 6, 20, 18)):
        cv.create_arc(_m(cx, cy, s, x0, y0, x1, y1), start=-55, extent=110,
                      style="arc", outline=c, width=w)


def _ic_mute(cv, cx, cy, s, c):
    _ic_volume(cv, cx, cy, s, c)
    w = max(1, round(1.5 * s))
    _stroke(cv, _m(cx, cy, s, 15, 9.5, 20.5, 14.5), c, w)
    _stroke(cv, _m(cx, cy, s, 20.5, 9.5, 15, 14.5), c, w)


def _ic_cc(cv, cx, cy, s, c):
    w = max(1, round(1.5 * s))
    _rr(cv, *_m(cx, cy, s, 3.5, 6.5, 20.5, 17.5), 2.5 * s,
        outline=c, width=w, fill="")
    _stroke(cv, _m(cx, cy, s, 6.5, 10, 13, 10), c, w)
    _stroke(cv, _m(cx, cy, s, 6.5, 14, 9, 14), c, w)
    _stroke(cv, _m(cx, cy, s, 10.5, 14, 17.5, 14), c, w)


def _ic_fullscreen(cv, cx, cy, s, c):
    w = max(1, round(1.5 * s))
    for pts in ((6, 10, 6, 6, 10, 6), (14, 6, 18, 6, 18, 10),
                (18, 14, 18, 18, 14, 18), (10, 18, 6, 18, 6, 14)):
        _stroke(cv, _m(cx, cy, s, *pts), c, w)


def _ic_chapters(cv, cx, cy, s, c):
    w = max(1, round(1.5 * s))
    for y in (8, 12, 16):
        _stroke(cv, _m(cx, cy, s, 6, y, 7, y), c, w + max(1, round(s)))
        _stroke(cv, _m(cx, cy, s, 9.5, y, 18, y), c, w)


def _spectrum_color(t):
    """Interpolate the volume spectrum stops at t in 0..1."""
    stops = DESIGN["spectrum"]
    pos = (0.0, 0.579, 0.792, 1.0)
    t = max(0.0, min(1.0, t))
    rgbs = [tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in stops]
    for p0, c0, p1, c1 in zip(pos, rgbs, pos[1:], rgbs[1:]):
        if t <= p1:
            f = 0.0 if p1 <= p0 else (t - p0) / (p1 - p0)
            return "#%02x%02x%02x" % tuple(
                int(a + (b - a) * f) for a, b in zip(c0, c1))
    return stops[-1]


class IconBtn(tk.Canvas):
    """Redesign icon button: grey glyph on the bar, white-6% rounded wash on
    hover, orange glyph when toggled ON. Shared by docked and fullscreen
    bars so both look identical."""

    SIZE = 30

    def __init__(self, master, icon, command=None, tip=None, on=False, **kw):
        super().__init__(master, width=self.SIZE, height=self.SIZE,
                         bg=master["bg"], highlightthickness=0, bd=0, **kw)
        self._icon = icon
        self._command = command
        self._on = bool(on)
        self._hover = False
        self.bind("<Button-1>", lambda _e: self._command and self._command())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        if tip:
            _ToolTip(self, tip)
        self._draw()

    def set_icon(self, icon):
        self._icon = icon
        self._draw()

    def set_on(self, on):
        on = bool(on)
        if on != self._on:
            self._on = on
            self._draw()

    def _set_hover(self, h):
        self._hover = h
        self.config(cursor="hand2" if h else "")
        self._draw()

    def _draw(self):
        self.delete("all")
        if self._hover:
            _rr(self, 1, 1, self.SIZE - 1, self.SIZE - 1, 7,
                fill=DESIGN["wash"], outline="")
        color = DESIGN["accent"] if self._on else (
            DESIGN["icon_hover"] if self._hover else DESIGN["icon"])
        self._icon(self, self.SIZE / 2.0, self.SIZE / 2.0,
                   self.SIZE / 24.0 * 0.8, color)


class PlayPill(tk.Canvas):
    """Primary play/pause control from play.svg: solid orange rounded pill
    with a dark glyph, hover brightens one step."""

    W, H = 54, 28

    def __init__(self, master, command=None, tip=None, playing=False, **kw):
        super().__init__(master, width=self.W, height=self.H,
                         bg=master["bg"], highlightthickness=0, bd=0, **kw)
        self._command = command
        self._playing = bool(playing)
        self._hover = False
        self.bind("<Button-1>", lambda _e: self._command and self._command())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        if tip:
            _ToolTip(self, tip)
        self._draw()

    def set_playing(self, playing):
        playing = bool(playing)
        if playing != self._playing:
            self._playing = playing
            self._draw()

    def _set_hover(self, h):
        self._hover = h
        self.config(cursor="hand2" if h else "")
        self._draw()

    def _draw(self):
        self.delete("all")
        bg = DESIGN["accent_hover"] if self._hover else DESIGN["accent"]
        _rr(self, 0.5, 0.5, self.W - 0.5, self.H - 0.5, 6, fill=bg, outline="")
        s = (self.H - 6) / 24.0
        glyph = _ic_pause if self._playing else _ic_play
        glyph(self, self.W / 2.0, self.H / 2.0, s, DESIGN["glyph_dark"])


class VolumeSlider(tk.Canvas):
    """Volume slider per volume.svg: dark rounded trough filled with the
    spectrum gradient (quiet green to loud red). Drag or click sets 0..1."""

    W, H = 92, 28
    TROUGH_H = 12

    def __init__(self, master, on_change=None, frac=0.8, **kw):
        super().__init__(master, width=self.W, height=self.H,
                         bg=master["bg"], highlightthickness=0, bd=0, **kw)
        self._on_change = on_change
        self._frac = max(0.0, min(1.0, frac))
        self._dragging = False
        for seq in ("<ButtonPress-1>", "<B1-Motion>"):
            self.bind(seq, self._drag)
        self.bind("<ButtonRelease-1>", self._drag)
        self.bind("<Enter>", lambda _e: self.config(cursor="hand2"))
        self.bind("<Leave>", lambda _e: self.config(cursor=""))
        self._draw()

    def get_frac(self):
        return self._frac

    def set_frac(self, frac):
        frac = max(0.0, min(1.0, frac))
        if self._dragging or abs(frac - self._frac) < 0.001:
            return
        self._frac = frac
        self._draw()

    def _drag(self, e):
        self._dragging = (str(e.type) == "ButtonPress" or
                          str(e.type) == "B1-Motion")
        pad = 6
        w = max(1, self.W - 2 * pad)
        frac = max(0.0, min(1.0, (e.x - pad) / float(w)))
        if frac != self._frac:
            self._frac = frac
            self._draw()
            if self._on_change:
                self._on_change(frac)

    def _draw(self):
        self.delete("all")
        cy = self.H // 2
        half = self.TROUGH_H // 2
        pad = 6
        # the trough outline keeps the track readable on the dark bar
        _rr(self, pad, cy - half, self.W - pad, cy + half, 6,
            fill=DESIGN["track"], outline=DESIGN["sep"])
        fw = pad + (self.W - 2 * pad) * self._frac
        n = max(2, int(fw - pad))
        for i in range(n):
            x = pad + i
            t = (x - pad) / float(self.W - 2 * pad)
            self.create_rectangle(x, cy - half + 2, x + 1.5, cy + half - 2,
                                  fill=_spectrum_color(t), outline="")
        if self._frac > 0.001:
            _rr(self, fw - 3, cy - half + 2, min(fw + 3, self.W - pad),
                cy + half - 2, 3, fill=_spectrum_color(self._frac),
                outline="")


class PlayerApp:
    POLL_MS = 100

    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.minsize(1024, 540)
        root.configure(bg="#101014")
        self._center_window(1080, 720)

        style = ttk.Style(root)
        with contextlib.suppress(Exception):
            style.theme_use("clam")
        # VLC-like dark widgets
        style.configure("TCombobox", fieldbackground="#23262e",
                        background="#23262e", foreground="#e9ebf0",
                        arrowcolor="#ff7a1a", bordercolor="#2c2f37",
                        lightcolor="#23262e", darkcolor="#23262e")
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#23262e")],
                  foreground=[("readonly", "#e9ebf0")],
                  background=[("active", "#2f333d")])
        style.configure("Horizontal.TScale", troughcolor="#2c2f37",
                        background="#ff7a1a", bordercolor="#17191f")
        style.map("Horizontal.TScale", background=[("active", "#ff8f3d")])

        self.cues = []
        self.cue_starts = []
        self.current_cue_idx = None
        self.spoken_idx = -1
        self._last_spoken_text = None   # last line handed to the voice
        self._seek_from = None          # playhead before the current seek
        self._resume_skip = False       # forward seek onto the same line
        self._scrubbing = False
        self._was_playing = False
        self._ended = False
        self._tts_ui_ready = False
        self._tts_err_shown = False
        self._piper_ui_rev = 0
        self._piper_err_shown = False
        self._vol_label_cache = None
        self._mute_shown = False
        self.read_aloud = tk.BooleanVar(value=False)
        self.sub_show = tk.BooleanVar(value=True)
        self._fullscreen = False
        self._fs_win = None
        self._fs_hide_after = None
        self._speed_toast = None
        self._speed_toast_after = None
        self._fs_cursor_hidden = False
        self._fs_last_move = 0.0
        self.ocr_mode = False
        self.ocr_cues = []
        self.ocr_starts = []
        self._ocr_last_text = None
        self._ocr_stop = None
        self._ocr_available = False
        self._ocr_backend = None
        self._ocr_lang_tag = None
        self._ocr_engines = {}
        self._ocr_engine_lock = threading.Lock()
        self._choice_popup = None      # open popup state, see _show_choice_popup

        self.tts = TTSSpeaker()

        self._video_path = None
        self._sub_job_q = queue.Queue()   # results from background ffmpeg jobs
        self.mt = Translator(post=lambda fn: self._sub_job_q.put(fn),
                             on_result=self._mt_speak_ready)
        self._mt_target = None          # None = OFF, else target language
        self._mt_ui_rev = -1
        self._embed_job_id = 0
        self.embed_tracks = []
        self._vlc_spu_allowed = False

        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        with contextlib.suppress(Exception):
            self.player.audio_set_volume(100)

        # Video clicks: VLC's D3D11 render surface is a native child window
        # whose owning thread never pumps posted messages, so button clicks
        # on the picture are never dispatched anywhere. We therefore watch
        # the whole system with a WH_MOUSE_LL hook (runs on our Tk loop
        # thread) and keep only clicks that land on VLC's video windows.
        self._ll_mouse_hook = None
        self._ll_mouse_cb = None
        self._click_q = queue.Queue()   # (event_time_ms, x, y) of left-clicks
        self._last_click = (0, None, None)   # time_ms, x, y of last left click
        # WNDPROC subclass of the vout child windows: used only to hide the
        # cursor over the video while idle in fullscreen (WM_SETCURSOR)
        self._vlc_hooks = {}            # hwnd -> original wndproc
        self._vlc_proc_cb = None
        self._vlc_set_wndproc = None
        self._fs_last_ptr = None        # last polled pointer position

        self._build_menu()
        self._dark_title_bar()
        self._build_video_area()
        self._build_controls()
        self._build_status()
        self._bind_keys()
        self._enable_drag_drop()

        # Draw the video inside our tkinter frame.
        with contextlib.suppress(Exception):
            self.player.set_hwnd(self.video_area.winfo_id())

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._wipe_temp_cache()
        self._init_ocr()
        self._init_ll_mouse_hook()
        root.after(self.POLL_MS, self._tick)
        root.focus_set()

    # ---------------- UI construction ----------------

    def _build_menu(self):
        # the native Windows menubar ignores Tk colors, so the dark theme
        # draws its own row and posts the menus right below it
        dark = dict(bg="#17191f", fg="#e9ebf0",
                    activebackground="#2f333d", activeforeground="#e9ebf0",
                    bd=0, relief="flat")
        row = tk.Frame(self.root, bg="#101014")
        row.pack(fill="x", side="top")
        self.menu_row = row

        def cascade(label):
            m = tk.Menu(row, tearoff=0, **dark)
            lbl = tk.Label(row, text=label, bg="#101014", fg="#c9cdd4",
                           padx=10, pady=5, font=("Segoe UI", 9))

            def open_m(_e=None, m=m, lbl=lbl):
                try:
                    m.tk_popup(lbl.winfo_rootx(),
                               lbl.winfo_rooty() + lbl.winfo_height())
                finally:
                    m.grab_release()
            lbl.bind("<Button-1>", open_m)
            lbl.bind("<Enter>", lambda _e: lbl.config(bg="#23262e"))
            lbl.bind("<Leave>", lambda _e: lbl.config(bg="#101014"))
            lbl.pack(side="left")
            return m

        # VLC order: Media Playback Audio Video Subtitle Tools View Help.
        # Our Read-Aloud extras live under Audio / Subtitle / Tools.
        m_media = cascade("Media")
        m_media.add_command(label="Open File…", command=self.open_video,
                            accelerator="Ctrl+O")
        m_media.add_command(label="Open Subtitles…", command=self.open_subtitles,
                            accelerator="Ctrl+U")
        m_media.add_command(label="Close Subtitles", command=self.close_subtitles)
        m_media.add_separator()
        m_media.add_command(label="Quit", command=self.on_close,
                            accelerator="Ctrl+Q")

        m_play = cascade("Playback")
        m_play.add_command(label="Play / Pause", command=self.toggle_play)
        m_play.add_command(label="Stop", command=self.stop)
        m_play.add_separator()
        m_speed = tk.Menu(m_play, tearoff=0, **dark)
        for r in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0, 4.0, 6.0, 8.0):
            m_speed.add_command(label="Speed %gx" % r,
                                command=lambda r=r: self.set_rate(r))
        m_play.add_cascade(label="Speed", menu=m_speed)
        m_play.add_command(label="Jump back 10s",
                           command=lambda: self.seek_ms(-10000))
        m_play.add_command(label="Jump forward 10s",
                           command=lambda: self.seek_ms(10000))
        m_play.add_separator()
        m_play.add_command(label="Fullscreen", command=self.toggle_fullscreen)

        m_audio = cascade("Audio")
        m_audio.add_command(label="Volume up",
                            command=lambda: self.change_volume(10))
        m_audio.add_command(label="Volume down",
                            command=lambda: self.change_volume(-10))
        m_audio.add_command(label="Mute / Unmute", command=self.toggle_mute)
        m_audio.add_separator()
        m_audio.add_command(label="Read Aloud ON/OFF",
                            command=self.toggle_read_aloud)

        m_video = cascade("Video")
        m_video.add_command(label="Fullscreen", command=self.toggle_fullscreen)
        m_video.add_command(label="OCR captions ON/OFF", command=self.toggle_ocr)

        m_sub = cascade("Subtitle")
        m_sub.add_command(label="Add Subtitle File…", command=self.open_subtitles)
        m_sub.add_command(label="Show / Hide subtitles",
                          command=self.toggle_sub_show)
        m_sub.add_command(label="Close Subtitles", command=self.close_subtitles)

        m_tools = cascade("Tools")
        m_tools.add_command(label="Install all dependencies…",
                            command=self.show_install_dialog)
        if not (getattr(sys, "frozen", False)
                and importlib.util.find_spec("piper") is None):
            m_tools.add_command(label="Get Piper voices (neural TTS)…",
                                command=self.show_piper_dialog)
        m_tools.add_command(label="Live translation model (Hy-MT2)…",
                            command=self.show_mt_dialog)
        m_tools.add_command(label="Keyboard shortcuts", command=self.show_help)

        m_view = cascade("View")
        m_view.add_command(label="Minimal Interface",
                           command=self.toggle_fullscreen)
        m_view.add_command(label="Fullscreen", command=self.toggle_fullscreen)

        m_help = cascade("Help")
        m_help.add_command(label="Keyboard shortcuts", command=self.show_help)
        m_help.add_command(label="About", command=self.show_about)

    def _dark_title_bar(self):
        # ask DWM for the dark title bar (Windows 10 1809+); attr 19 is the
        # pre-20H1 build of the same setting
        if not IS_WINDOWS:
            return
        with contextlib.suppress(Exception):
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            for attr in (20, 19):
                v = ctypes.c_int(1)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(v),
                        ctypes.sizeof(v)) == 0:
                    break

    def _build_video_area(self):
        self.video_area = tk.Frame(self.root, bg="#000000", height=430)
        self.video_area.pack(fill="both", expand=True)
        self.video_area.pack_propagate(False)
        self.video_area.bind("<Configure>", self._on_video_resize)

        # Empty screen = one obvious thing to do: open a movie. The whole
        # area (except the button) is clickable and does the same.
        self.placeholder = tk.Frame(self.video_area, bg="#000000")
        self.placeholder.place(relx=0, rely=0, relwidth=1, relheight=1)
        body = tk.Frame(self.placeholder, bg="#000000")
        body.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(body, text="🎬", bg="#000000", fg="#3f4654",
                 font=("Segoe UI", 40)).pack()
        tk.Label(body, text="No video yet", bg="#000000", fg="#c8cdd4",
                 font=("Segoe UI", 17, "bold")).pack(pady=(6, 14))
        open_btn = tk.Button(
            body, text="Open Video  (Ctrl+O)", command=self.open_video,
            bg=self.THEME["accent"], fg="#ffffff", bd=0, relief="flat",
            cursor="hand2", takefocus=0, padx=26, pady=10,
            font=("Segoe UI", 11, "bold"),
            activebackground="#ff8f3d", activeforeground="#ffffff")
        open_btn.pack()
        hint1 = tk.Label(body, text="…or drag & drop a movie file anywhere here",
                         bg="#000000", fg="#8a919c", font=("Segoe UI", 10))
        hint1.pack(pady=(14, 2))
        tk.Label(body,
                 text="Add subtitles with Ctrl+U. Read Aloud speaks every line",
                 bg="#000000", fg="#5b626e", font=("Segoe UI", 9)).pack()
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        for w in (self.placeholder, body, hint1):
            w.bind("<Button-1>", lambda e: self.open_video())

        self.sub_label = tk.Label(
            self.video_area, text="", bg="#000000", fg="#ffffff",
            font=("Segoe UI", 17, "bold"), justify="center",
            wraplength=860, padx=10, pady=4)
        self.sub_label.place(relx=0.5, rely=1.0, anchor="s", y=-24)

        # double-click fullscreen on the video comes through the
        # WH_MOUSE_LL hook (the native vout windows eat real clicks);
        # this binding only covers the subtitle strip, a Tk window the
        # low-level gate intentionally ignores
        self.sub_label.bind(
            "<Double-Button-1>", lambda _e: self.toggle_fullscreen())

    def _on_video_resize(self, event):
        self.sub_label.config(wraplength=max(300, event.width - 80))

    # Palette from the "VLC Redesign (Community)" SVGs. DESIGN (below the
    # widget classes) is the single source; dialogs and menus follow it too.
    THEME = {"bg": "#0e0e0e", "panel": DESIGN["bar"], "btn": "#202020",
             "btn_hover": DESIGN["wash"], "btn_on": DESIGN["accent"],
             "btn_on_hover": DESIGN["accent_hover"],
             "btn_on_fg": DESIGN["glyph_dark"],
             "fg": "#e8e8e8", "fg_dim": DESIGN["icon"],
             "accent": DESIGN["accent"], "sep": "#2a2a2a",
             "status_bg": DESIGN["bar"], "well": "#1d1d1d",
             "border": "#222222"}
    CBG = THEME["panel"]     # control bar background (VLC light grey)

    BTN = {"bg": THEME["btn"], "fg": THEME["fg"],
           "activebackground": THEME["btn_hover"],
           "activeforeground": THEME["fg"], "relief": "flat", "bd": 0,
           "takefocus": 0, "padx": 6, "pady": 4, "font": ("Segoe UI", 9)}

    def _mkbtn(self, parent, text, cmd, off_bg=None, side="left", pad=None,
               tip=None, **over):
        def wrapped():
            cmd()
            self.root.focus_set()
        b = tk.Button(parent, text=text, command=wrapped, cursor="hand2",
                      **{**self.BTN, **over})
        holder = {"off": off_bg or self.THEME["btn"]}

        def _enter(_e):
            b.config(bg=self.THEME["btn_on_hover"]
                     if holder["off"] == self.THEME["btn_on"]
                     else self.THEME["btn_hover"])
        b._bg_holder = holder
        b.bind("<Enter>", _enter)
        b.bind("<Leave>", lambda e: b.config(bg=holder["off"]))
        if tip:
            _ToolTip(b, tip)
        if side:
            b.pack(side=side, **({"padx": pad} if pad else {}))
        return b

    def _set_toggle(self, btn, on, on_text, off_text):
        """VLC-like pressed state: orange highlight when ON, dark surface
        when OFF."""
        off_bg = self.THEME["btn"]
        btn.config(text=on_text if on else off_text,
                   bg=self.THEME["btn_on"] if on else off_bg,
                   fg=self.THEME["btn_on_fg"] if on else self.THEME["fg"])
        btn._bg_holder["off"] = self.THEME["btn_on"] if on else off_bg

    def _mksep(self, parent):
        return tk.Frame(parent, bg=self.THEME["sep"], width=1)

    def _mklabel(self, parent, text, side="left", padx=0, font=("Segoe UI", 9)):
        lbl = tk.Label(parent, text=text, bg=self.CBG, fg=self.THEME["fg_dim"],
                       font=font)
        lbl.pack(side=side, padx=padx)
        return lbl

    # ttk's combobox popdown is a child of the main window on Windows, so in
    # this short bar it opens over the toolbar and gets clipped at the window
    # edge (the Voice list lost its last voices). Post the choices in a
    # borderless top-level instead: it can drop onto free desktop space.
    #
    # An overrideredirect Toplevel can't reliably take the OS keyboard focus
    # on Windows, so while the popup is open its keys are also bound on the
    # opening combo (which keeps focus for keyboard users); the listbox keeps
    # its own copies for the mouse path. All of it is torn down on close.
    def _wire_popup_combo(self, combo, on_change):
        """Show a real drop-down (not the in-window ttk popdown) on open."""
        def open_it(_e=None):
            if str(combo.cget("state")) == "disabled":
                return "break"
            pop = self._choice_popup
            if pop is not None and pop["combo"] is combo:
                self._hide_choice_popup()      # second open toggles closed
                return "break"
            self._show_choice_popup(combo, on_change)
            return "break"
        combo._popup_open_it = open_it
        for seq in ("<ButtonPress-1>", "<KeyPress-Down>",
                    "<KeyPress-Up>", "<KeyPress-space>"):
            combo.bind(seq, open_it)

    _POPUP_KEY_SEQS = ("<KeyPress-Down>", "<KeyPress-Up>", "<Return>",
                       "<KP_Enter>", "<Escape>", "<Key>")

    def _hide_choice_popup(self):
        pop = self._choice_popup
        if pop is None:
            return
        self._choice_popup = None
        combo = pop["combo"]
        for seq in self._POPUP_KEY_SEQS:
            with contextlib.suppress(Exception):
                combo.unbind(seq)
        open_it = getattr(combo, "_popup_open_it", None)
        if open_it is not None:
            for seq in ("<KeyPress-Down>", "<KeyPress-Up>",
                        "<KeyPress-space>"):
                combo.bind(seq, open_it)
        with contextlib.suppress(Exception):
            pop["top"].grab_release()
        with contextlib.suppress(Exception):
            pop["top"].destroy()

    def _show_choice_popup(self, combo, on_change):
        self._hide_choice_popup()
        values = list(combo["values"] or ())
        if not values:
            return
        try:
            current = combo.current()
        except Exception:
            current = -1
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        border = tk.Frame(top, bg=self.THEME["sep"], bd=0)
        border.pack(fill="both", expand=True)
        lb = tk.Listbox(border, font=combo.cget("font"), bd=0,
                        highlightthickness=0, activestyle="none",
                        exportselection=False,
                        bg=self.THEME["btn"], fg=self.THEME["fg"],
                        selectbackground=self.THEME["accent"],
                        selectforeground=self.THEME["btn_on_fg"])
        for v in values:
            lb.insert("end", v)
        with contextlib.suppress(Exception):
            w = int(combo.cget("width"))
            if w > 0:
                lb.config(width=w)
        max_h = 12
        lb.config(height=min(len(values), max_h))
        lb.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        if len(values) > max_h:
            sb = ttk.Scrollbar(border, orient="vertical", command=lb.yview)
            lb.config(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
        if 0 <= current < len(values):
            lb.selection_set(current)
            lb.see(current)

        def nav(delta):
            sel = lb.curselection()
            if sel:
                idx = sel[0] + delta
            else:
                idx = 0 if delta > 0 else len(values) - 1
            idx = max(0, min(idx, len(values) - 1))
            lb.selection_clear(0, "end")
            lb.selection_set(idx)
            lb.see(idx)
            return "break"

        def close(e=None):
            if e is not None:
                try:
                    inside = (top.winfo_rootx() <= e.x_root
                              < top.winfo_rootx() + top.winfo_width()
                              and top.winfo_rooty() <= e.y_root
                              < top.winfo_rooty() + top.winfo_height())
                except Exception:
                    inside = False
                if inside:
                    return             # clicks within the list keep it open
            self._hide_choice_popup()

        def pick(_e=None):
            sel = lb.curselection()
            if sel:
                combo.current(sel[0])
                on_change()
            self._hide_choice_popup()
            combo.focus_set()

        def pick_mouse(_e=None):
            # the popup grabs the pointer as it opens, so the RELEASE of the
            # very click that opened it arrives here with no matching press;
            # it must not commit/close or the dropdown flickers away instantly
            if not press_inside[0]:
                return
            pick()

        press_inside = [False]
        lb.bind("<ButtonPress-1>", lambda _e: press_inside.__setitem__(0, True))
        lb.bind("<ButtonRelease-1>", pick_mouse)
        lb.bind("<Return>", pick)
        lb.bind("<KP_Enter>", pick)
        lb.bind("<Escape>", close)
        lb.bind("<KeyPress-Down>", lambda e: nav(+1))
        lb.bind("<KeyPress-Up>", lambda e: nav(-1))
        top.bind("<Button-1>", close)
        top.bind("<Escape>", close)
        # keyboard path: focus stays on the combo, so it carries the keys
        for seq, fn in (
            ("<KeyPress-Down>", lambda e: nav(+1)),
            ("<KeyPress-Up>", lambda e: nav(-1)),
            ("<Return>", pick), ("<KP_Enter>", pick),
            ("<Escape>", close),
            ("<Key>", lambda e: "break"),   # mute the app's hotkeys meanwhile
        ):
            combo.bind(seq, fn)
        self._choice_popup = {"top": top, "lb": lb, "combo": combo,
                              "pick": pick, "pick_mouse": pick_mouse,
                              "close": close, "nav": nav,
                              "press": lambda: press_inside.__setitem__(0, True)}

        # below the combo when there is room on screen, above otherwise
        top.update_idletasks()
        pw, ph = top.winfo_reqwidth(), top.winfo_reqheight()
        x = combo.winfo_rootx()
        y = combo.winfo_rooty() + combo.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        if y + ph > sh and combo.winfo_rooty() - ph >= 0:
            y = combo.winfo_rooty() - ph
        x = max(0, min(x, sw - pw))
        y = max(0, min(y, sh - ph))
        top.geometry("+%d+%d" % (x, y))
        top.deiconify()
        top.lift()                     # stay above the fullscreen bar
        top.update_idletasks()
        lb.focus_set()
        with contextlib.suppress(Exception):
            top.grab_set()                 # close on click-outside
        top.after_idle(lb.focus_set)   # keep focus once mapping settles

    def _mkicon(self, parent, icon, cmd, tip=None):
        """Redesign icon button (grey glyph, hover wash, orange when ON).
        Returns it unpacked; the caller picks the pack side."""
        b = IconBtn(parent, icon, command=self._cmd(cmd), tip=tip)
        return b

    def _build_controls(self):
        # Redesign dark bottom bar: thin seek row (subtitle spans drawn on
        # the track), one icon toolbar row, slim extras strip. The same
        # widgets serve the fullscreen bar, so both screens match.
        controls = tk.Frame(self.root, bg=self.CBG)
        controls.pack(fill="x", side="bottom")
        self.controls = controls

        # --- Row 1: elapsed | seek slider | total (mock layout) ---
        row1 = tk.Frame(controls, bg=self.CBG)
        row1.pack(fill="x", padx=8, pady=(5, 0))
        self.time_cur_lbl = tk.Label(row1, text="00:00", bg=self.CBG,
                                     fg=self.THEME["fg_dim"],
                                     font=("Segoe UI", 8), width=6)
        self.time_cur_lbl.pack(side="left")
        self.time_total_lbl = tk.Label(row1, text="00:00", bg=self.CBG,
                                       fg=self.THEME["fg_dim"],
                                       font=("Segoe UI", 8), width=6)
        self.time_total_lbl.pack(side="right")
        self.seekbar = Seekbar(row1,
                               on_scrub=lambda d: setattr(self, "_scrubbing", d),
                               on_seek=self._on_seek_to)
        self.seekbar.pack(fill="x", expand=True, side="left", padx=8)

        # --- Row 2: redesign icon toolbar ---
        row2 = tk.Frame(controls, bg=self.CBG)
        row2.pack(fill="x", padx=6, pady=(1, 2))

        self.play_btn = PlayPill(row2, command=self._cmd(self.toggle_play),
                                 tip="Play / Pause  (Space)")
        self.play_btn.pack(side="left", padx=(2, 6))
        self._mkicon(row2, _ic_back10,
                     lambda: self.seek_ms(-10000),
                     tip="Back 10 seconds  (←)").pack(side="left")
        self._mkicon(row2, _ic_stop, self.stop,
                     tip="Stop playback").pack(side="left")
        self._mkicon(row2, _ic_fwd10,
                     lambda: self.seek_ms(10000),
                     tip="Forward 10 seconds  (→)").pack(side="left")
        self._mkicon(row2, _ic_fullscreen, self.toggle_fullscreen,
                     tip="Fullscreen  (F)").pack(side="left", padx=(6, 0))

        self._mksep(row2).pack(side="left", fill="y", padx=5, pady=4)

        self.sub_btn = self._mkicon(row2, _ic_cc, self.toggle_sub_show,
                                    tip="Show / hide subtitles  (C)")
        self.sub_btn.pack(side="left")
        self.sub_btn.set_on(True)
        self._mkbtn(row2, "+Sub", self.open_subtitles,
                    tip="Open a subtitle file  (Ctrl+U)", pad=(4, 0))

        self._mksep(row2).pack(side="left", fill="y", padx=5, pady=4)

        self.ocr_btn = self._mkbtn(row2, "OCR", self.toggle_ocr,
                                   tip="Read burned-in subtitles from "
                                       "the picture (OCR)",
                                   pad=(4, 0))
        self._set_toggle(self.ocr_btn, False, "OCR", "OCR")

        self.read_btn = self._mkbtn(row2, "Read Aloud",
                                    self.toggle_read_aloud,
                                    tip="Speak subtitle lines aloud  (S)",
                                    pad=(4, 0))
        self._set_toggle(self.read_btn, False, "Read Aloud", "Read Aloud")

        # inline extras right of the Read Aloud chip (the fullscreen bar
        # carries the same group, so both screens match)
        self._mksep(row2).pack(side="left", fill="y", padx=4, pady=4)
        small = {"font": ("Segoe UI", 8)}
        self._mklabel(row2, "Subs:", padx=0, **small)
        self.embed_combo = ttk.Combobox(row2, state="readonly", width=12,
                                        font=("Segoe UI", 8))
        self.embed_combo.pack(side="left", padx=(2, 4))
        self.embed_combo.bind("<<ComboboxSelected>>", self._on_embed_selected)
        self._wire_popup_combo(self.embed_combo, self._on_embed_selected)
        _ToolTip(self.embed_combo,
                 "Subtitle track inside the movie (scanned on open)")
        self._embed_set_choices([])
        self.embed_combo.set("(open a movie)")
        self._mklabel(row2, "OCR:", padx=0, **small)
        self.ocr_lang = ttk.Combobox(row2, state="readonly", width=4,
                                     values=["eng"], font=("Segoe UI", 8))
        self.ocr_lang.current(0)
        self.ocr_lang.pack(side="left", padx=(2, 4))
        self.ocr_lang.bind("<<ComboboxSelected>>", self._on_ocr_lang)
        self._wire_popup_combo(self.ocr_lang, self._on_ocr_lang)
        _ToolTip(self.ocr_lang, "Language used when reading picture "
                                "subtitles (OCR)")
        self._mklabel(row2, "Voice:", padx=0, **small)
        self.voice_combo = ttk.Combobox(row2, state="readonly", width=12,
                                        font=("Segoe UI", 8))
        self.voice_combo.pack(side="left", padx=(2, 0))
        self.voice_combo.bind("<<ComboboxSelected>>", self._on_voice_selected)
        self._wire_popup_combo(self.voice_combo, self._on_voice_selected)
        _ToolTip(self.voice_combo,
                 "Voice used by Read Aloud, get Piper voices from the "
                 "Tools menu (neural, offline)")
        self._mksep(row2).pack(side="left", fill="y", padx=4, pady=4)
        self._mklabel(row2, "MT:", padx=0, **small)
        self.mt_lang = ttk.Combobox(row2, state="readonly", width=8,
                                    values=["Off"] + MT_LANGS,
                                    font=("Segoe UI", 8))
        self.mt_lang.current(0)
        self.mt_lang.pack(side="left", padx=(2, 0))
        self.mt_lang.bind("<<ComboboxSelected>>", self._on_mt_lang)
        self._wire_popup_combo(self.mt_lang, self._on_mt_lang)
        _ToolTip(self.mt_lang,
                 "Live translation: every subtitle line is translated to "
                 "this language by a local AI model (offline) before it is "
                 "shown and spoken. First use downloads the model once "
                 "(Tools menu).")

        # volume bottom-right like the mock: mute icon, spectrum slider, %
        vol_right = tk.Frame(row2, bg=self.CBG)
        vol_right.pack(side="right")
        self.mute_btn = self._mkicon(vol_right, _ic_volume, self.toggle_mute,
                                     tip="Mute / Unmute  (M)")
        self.mute_btn.pack(side="right", padx=(4, 2))
        self.vol_lbl = tk.Label(vol_right, text="100%", bg=self.CBG,
                                fg=self.THEME["fg_dim"],
                                font=("Segoe UI", 8), width=4)
        self.vol_lbl.pack(side="right")
        self._vol_slider_busy = False
        self.vol_scale = VolumeSlider(
            vol_right, on_change=lambda f: self._on_vol_slider(f * 125.0),
            frac=0.8)
        self.vol_scale.pack(side="right", padx=(2, 4))

    def _build_status(self):
        # no visible bar anymore; status_var keeps set_status() calls working
        self.status_var = tk.StringVar(
            value="Open a video to start. Subtitle files: .srt .vtt .ass")

    # ---------------- helpers ----------------

    def _cmd(self, fn):
        def wrapped():
            fn()
            self.root.focus_set()
        return wrapped

    def _center_window(self, w, h):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def set_status(self, text):
        self.status_var.set(text)

    # ---------------- playback actions ----------------

    def open_video(self):
        path = filedialog.askopenfilename(title="Open video", filetypes=VIDEO_TYPES)
        if path:
            self.load_video(path)

    def load_video(self, path):
        if self.ocr_mode:
            self._ocr_stop_mode()           # fresh session for the new movie
        self.mt.purge()
        media = self.instance.media_new(path)
        self.player.set_media(media)
        self.player.play()
        self.root.title("%s - VLCspeaker" % os.path.basename(path))
        self.placeholder.place_forget()
        self.spoken_idx = -1
        self.current_cue_idx = None
        self.sub_label.config(text="")
        self._video_path = path
        self._embed_job_id += 1          # cancel any pending extraction
        self._vlc_spu_allowed = False
        self.embed_combo.config(state="readonly")
        self._embed_set_choices([])
        self.embed_combo.set("(scanning movie for subtitle tracks…)")
        self._scan_embed_tracks(path)
        self.set_status("Playing: %s" % os.path.basename(path))
        # auto-load subtitles that sit next to the video with the same name
        stem = os.path.splitext(path)[0]
        for ext in SUB_EXTS:
            cand = stem + ext
            if os.path.isfile(cand):
                self.load_subtitles(cand, quiet=True)
                break

    def _load_cues(self, cues):
        """The one place where the active subtitle track is swapped."""
        self.cues = cues
        self.cue_starts = [c.start for c in cues]
        self.current_cue_idx = None
        self.spoken_idx = -1
        self._last_spoken_text = None
        self._resume_skip = False
        self.sub_label.config(text="")
        self.tts.purge()
        self.mt.purge()
        self._vlc_spu_allowed = False
        with contextlib.suppress(Exception):
            self.player.video_set_spu(-1)   # we render subtitles ourselves

    def open_subtitles(self):
        path = filedialog.askopenfilename(title="Open subtitles", filetypes=SUB_TYPES)
        if path:
            self.load_subtitles(path)

    def load_subtitles(self, path, quiet=False):
        try:
            cues = parse_subtitles(path)
        except Exception as exc:
            if not quiet:
                messagebox.showerror(APP_NAME, "Could not read subtitles:\n%s\n\n%s"
                                     % (os.path.basename(path), exc))
            self.set_status("Could not read subtitles: %s" % exc)
            return
        self._load_cues(cues)
        self.set_status("Subtitles: %s  (%d lines)" % (os.path.basename(path), len(cues)))
        if not cues and not quiet:
            messagebox.showwarning(APP_NAME,
                                   "No subtitle lines found in %s." % os.path.basename(path))

    def close_subtitles(self):
        self._load_cues([])
        self.set_status("Subtitles closed.")

    # --- subtitle tracks embedded inside the movie file ---

    def _embed_set_choices(self, tracks):
        self.embed_tracks = list(tracks or [])
        choices = ["(none)"] + [track_label(i, t)
                                for i, t in enumerate(self.embed_tracks)]
        self.embed_combo["values"] = choices
        self.embed_combo.current(0)

    def _scan_embed_tracks(self, path):
        def work():
            result = probe_subtitle_tracks(path)
            self._sub_job_q.put(lambda: self._embed_tracks_ready(result, path))
        threading.Thread(target=work, daemon=True).start()

    def _embed_tracks_ready(self, result, for_path):
        if for_path != self._video_path:
            return                          # another movie was opened meanwhile
        if result == "no-tool":
            self.embed_combo.set("(install ffmpeg to use in-movie subtitles)")
            self.embed_combo.config(state="disabled")
            return
        if result == "error":
            self._embed_set_choices([])
            self.embed_combo.set("(could not scan movie)")
            return
        self._embed_set_choices(result)
        if not result:
            self.set_status("No subtitle tracks inside this movie. "
                            "Use Add Subtitles (Ctrl+U) for a .srt file.")
            return
        text_tracks = [t for t in result if t["codec"] in TEXT_SUB_CODECS]
        if len(result) == 1 and not self.cues:
            self.embed_combo.current(1)     # single text track: just use it
            self._on_embed_selected()
        elif text_tracks:
            self.set_status("Found %d subtitle track(s) inside the movie, "
                            "pick one in the 'Subtitles inside this movie' dropdown."
                            % len(result))
        else:
            self.set_status("In-movie subtitle tracks are pictures (PGS/VobSub), "
                            "VLC can show them, but Read Aloud needs a text .srt file.")

    def _on_embed_selected(self, _event=None):
        idx = self.embed_combo.current()
        if idx <= 0 or idx - 1 >= len(self.embed_tracks):
            return
        track = self.embed_tracks[idx - 1]
        if track["codec"] in IMAGE_SUB_CODECS:
            self._show_picture_track(idx - 1, idx)
            return
        self._embed_job_id += 1
        job = self._embed_job_id
        video = self._video_path
        self.set_status("Extracting in-movie subtitle track %d…" % idx)

        def work():
            dest_dir = os.path.join(tempfile.gettempdir(), "VLCspeaker")
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except OSError:
                dest_dir = tempfile.gettempdir()
            base = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                          os.path.splitext(os.path.basename(video))[0])[:60] or "video"
            dest = os.path.join(dest_dir, "%s.track%d.srt" % (base, track["index"]))
            ok, err = extract_subtitle_track(video, track["index"], dest)
            self._sub_job_q.put(
                lambda: self._embed_extract_done(job, ok, err, dest, idx))

        threading.Thread(target=work, daemon=True).start()

    def _show_picture_track(self, ordinal, track_no):
        """Picture subtitles (PGS/VobSub): no text exists, so let VLC draw
        them on the video; Read Aloud cannot speak them."""
        shown = False
        with contextlib.suppress(Exception):
            n = self.player.video_get_spu_count() or 0
            if 0 <= ordinal < n:
                self.player.video_set_spu(ordinal)
                self._vlc_spu_allowed = True
                shown = True
        if shown:
            self.set_status("Track %d is picture subtitles, shown by VLC. "
                            "Read Aloud cannot speak pictures; load a text .srt "
                            "file for that." % track_no)
        else:
            self.set_status("Track %d is picture subtitles (PGS/VobSub), "
                            "no text to read aloud." % track_no)
        messagebox.showinfo(
            APP_NAME,
            "Subtitle track %d is stored as PICTURES (PGS/VobSub/DVB).\n"
            "There is no text inside it, so Read Aloud cannot speak it.\n\n%s\n\n"
            "For Read Aloud, load a text subtitle file (.srt)."
            % (track_no,
               "VLC will now show this track on the video." if shown else
               "Start the video, then pick the track again to show it."))

    def _embed_extract_done(self, job, ok, err, dest, track_no):
        if job != self._embed_job_id:
            return                          # a newer request replaced this one
        cues = []
        if ok:
            try:
                cues = parse_subtitles(dest)
            except Exception:
                cues = []
        if cues:
            self._load_cues(cues)
            self.set_status("In-movie subtitles track %d: %d lines loaded, "
                            "press Read Aloud to hear them."
                            % (track_no, len(cues)))
            return
        with contextlib.suppress(Exception):
            self.embed_combo.current(0)
        messagebox.showwarning(
            APP_NAME,
            "Could not read in-movie subtitle track %d.\n\n%s\n\n"
            "The track may be pictures (PGS/VobSub) or ffmpeg is not installed.\n"
            "You can always use a text .srt subtitle file (Add Subtitles)."
            % (track_no, err or "no text found"))
        self.set_status("In-movie subtitle track %d could not be extracted."
                        % track_no)

    def _init_ocr(self):
        def tesseract_langs():
            exe = find_tesseract()
            if not exe:
                return None
            try:
                import pytesseract
                pytesseract.pytesseract.tesseract_cmd = exe
                out = subprocess.run([exe, "--list-langs"],
                                     capture_output=True, timeout=20,
                                     creationflags=_NO_WINDOW)
                langs = [ln.strip()
                         for ln in (out.stdout or b"").decode(
                             "utf-8", "replace").splitlines()
                         if ln.strip() and ln.strip() != "osd"
                         and not ln.strip().lower().startswith(
                             ("available", "list of"))]
                return langs or None
            except Exception:
                return None

        def winrt_langs():
            try:
                import winrt.windows.globalization as glob
                import winrt.windows.media.ocr as ocr
            except Exception:
                return None
            found = []
            try:
                eng = ocr.OcrEngine.try_create_from_user_profile_languages()
                default_tag = eng.recognizer_language.language_tag if eng else None
            except Exception:
                default_tag = None
            for tag in OCR_LANG_CANDIDATES:
                with contextlib.suppress(Exception):
                    # raises when the language pack is not installed
                    if ocr.OcrEngine.try_create_from_language(glob.Language(tag)):
                        found.append(tag)
            if default_tag and default_tag not in found:
                found.insert(0, default_tag)
            return found or None

        def work():
            langs = tesseract_langs()
            backend = "tesseract"
            if not langs:
                backend = "winrt"
                langs = winrt_langs()
            if langs and backend == "winrt":
                with contextlib.suppress(Exception):
                    self._get_ocr_engine(langs[0])   # warm up for instant OCR
            self._sub_job_q.put(lambda l=langs, b=backend: self._ocr_ready(l, b))

        threading.Thread(target=work, daemon=True).start()

    def _get_ocr_engine(self, tag):
        """WinRT OCR engine per language tag, created once, cached.
        Warmed at startup so OCR: ON starts reading with no delay."""
        with self._ocr_engine_lock:
            eng = self._ocr_engines.get(tag)
            if eng is None:
                import winrt.windows.globalization as glob
                import winrt.windows.media.ocr as ocr
                if tag == "auto":
                    eng = ocr.OcrEngine.try_create_from_user_profile_languages()
                else:
                    eng = ocr.OcrEngine.try_create_from_language(glob.Language(tag))
                if eng is not None:
                    self._ocr_engines[tag] = eng
            return eng

    def _ocr_ready(self, langs, backend):
        if langs:
            self._ocr_available = True
            self._ocr_backend = backend
            self.ocr_lang["values"] = langs
            self.ocr_lang.current(0)
            self._ocr_lang_tag = langs[0]
            kind = "Tesseract" if backend == "tesseract" else "Windows built-in"
            self.set_status("OCR ready (%s), caption languages: %s"
                            % (kind, ", ".join(langs[:12])))
        else:
            self.ocr_btn.config(state="disabled", text="OCR unavailable")
            self.ocr_lang.config(state="disabled")
            self.set_status(
                "OCR has no language yet, add one in Windows Settings → "
                "Time & Language → Language & region, or install Tesseract: "
                "winget install UB-Mannheim.TesseractOCR")

    def _on_ocr_lang(self, _event=None):
        with contextlib.suppress(Exception):
            self._ocr_lang_tag = self.ocr_lang.get()
            if self._ocr_backend == "winrt":
                threading.Thread(
                    target=lambda: self._get_ocr_engine(self._ocr_lang_tag),
                    daemon=True).start()

    def toggle_ocr(self):
        if self.ocr_mode:
            self._ocr_stop_mode()
            return
        if not self._ocr_available:
            messagebox.showinfo(
                APP_NAME,
                "OCR needs a caption language with OCR support.\n\n"
                "Add one in Windows:\n"
                "  Settings → Time & Language → Language & region → "
                "Add a language\n"
                "(tick 'Language pack' / 'Basic typing' / 'OCR' features).\n\n"
                "Or install the free Tesseract program:\n"
                "  winget install UB-Mannheim.TesseractOCR\n"
                "then restart VLCspeaker.")
            return
        self.ocr_mode = True
        self._set_toggle(self.ocr_btn, True, "OCR", "OCR")
        self.ocr_cues = []
        self.ocr_starts = []
        self._ocr_last_text = None
        self._cleanup_ocr_files()           # drop any stale snapshot
        self.current_cue_idx = None
        self.spoken_idx = -1
        self.sub_label.config(text="")
        self.tts.purge()
        self.mt.purge()
        self._ocr_stop = threading.Event()
        threading.Thread(target=self._ocr_loop, daemon=True).start()
        msg = ("OCR captions ON, burned-in captions are read every second. "
               "Turn on Read Aloud to hear them.")
        if self._ocr_backend != "tesseract" and not find_tool("ffmpeg"):
            msg += " (Install ffmpeg for the smoothest viewing while OCR runs.)"
        self.set_status(msg)

    def _ocr_stop_mode(self):
        self.ocr_mode = False
        if self._ocr_stop:
            self._ocr_stop.set()
        self._ocr_stop = None
        self._close_open_ocr_cue()
        self._cleanup_ocr_files()          # delete the snapshot temp file
        self._set_toggle(self.ocr_btn, False, "OCR", "OCR")
        self.current_cue_idx = None
        self.spoken_idx = -1
        self.tts.purge()
        self.mt.purge()
        self.set_status("OCR captions OFF, captured %d caption lines this "
                        "session." % len(self.ocr_cues))

    def _ocr_snap_path(self):
        """ONE small temp file for OCR snapshots, reused (overwritten) every
        second, never accumulated, and deleted when OCR turns off."""
        snap_dir = os.path.join(tempfile.gettempdir(), "VLCspeaker")
        try:
            os.makedirs(snap_dir, exist_ok=True)
        except OSError:
            snap_dir = tempfile.gettempdir()
        return os.path.join(snap_dir, "ocr_snap.png")

    def _cleanup_ocr_files(self):
        with contextlib.suppress(OSError):
            p = self._ocr_snap_path()
            if os.path.isfile(p):
                os.remove(p)

    def _wipe_temp_cache(self):
        """Start clean: OCR snapshots and extracted subtitle tracks never
        outlive the app, this folder is wiped on every startup and the
        snapshot on every exit.
        ponytail: wipes the whole cache; keep per-file cache only if
        re-extracting big movies on every start gets annoying."""
        cache = os.path.join(tempfile.gettempdir(), "VLCspeaker")
        shutil.rmtree(cache, ignore_errors=True)

    def _grab_frame(self, ffmpeg, pos_ms, snap):
        """Grab ONE frame at pos_ms from the movie FILE into the snapshot
        path, with a short-lived invisible ffmpeg. VLC playback is never
        touched. Do not replace this with a long-running '-re' stream
        reader: it must be re-seeked to catch up after every start/seek,
        and then trails the playhead by 10+ seconds, delaying all speech."""
        video = self._video_path
        if not video:
            return False
        try:
            before = None
            with contextlib.suppress(OSError):
                before = os.path.getmtime(snap)
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                 # no -hwaccel: software decode survives corrupt streams
                 # that make the GPU decoder drop frames ("no frame!")
                 "-ss", "%.3f" % (max(0, pos_ms) / 1000.0), "-i", video,
                 "-an", "-sn", "-dn",
                 "-vf", "scale=1280:-2,format=gray",
                 "-frames:v", "1", "-update", "1", "-y", snap],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=_NO_WINDOW)
            return os.path.getmtime(snap) != before
        except Exception:
            return False

    def _ocr_loop(self):
        stop = self._ocr_stop
        snap = self._ocr_snap_path()
        ffmpeg = find_tool("ffmpeg")
        loop = None
        if self._ocr_backend == "winrt":
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with contextlib.suppress(Exception):
                self._get_ocr_engine(self._ocr_lang_tag or "auto")  # hot by now

        while stop is self._ocr_stop and not stop.is_set():
            if stop.wait(0.3):
                break
            cycle_start = time.monotonic()
            frame_pos = None
            try:
                playing = (self.player is not None
                           and self.player.is_playing())
                if not playing:
                    continue
                now = self.player.get_time()
                length = self.player.get_length() or 0
                if now < 0 or (length > 0 and now >= length - 500):
                    continue               # nothing left to read
                if ffmpeg:
                    if self._grab_frame(ffmpeg, now, snap):
                        frame_pos = now
                elif self.player.video_take_snapshot(0, snap, 1280, 0) == 0:
                    # no ffmpeg, fall back to VLC snapshots
                    # (works, but the video may hitch for a moment)
                    frame_pos = now
                if frame_pos is None:
                    continue
                tag = self._ocr_lang_tag or "auto"
                if self._ocr_backend == "tesseract":
                    text = self._ocr_tesseract(snap, tag)
                else:
                    png = self._ocr_strip_png(snap)
                    text = loop.run_until_complete(
                        self._winrt_recognize(self._get_ocr_engine(tag), png))
                    # second pass on plain grayscale: both must agree,
                    # else the frame is treated as unreadable noise
                    png2 = self._ocr_gray_png(snap)
                    text2 = loop.run_until_complete(
                        self._winrt_recognize(self._get_ocr_engine(tag), png2))
                    if not _ocr_dual_ok(text, text2):
                        if (text or "").strip() or (text2 or "").strip():
                            continue           # disagree: junk frame/text
                        text = ""              # both empty: caption gone
            except Exception:
                continue                   # a bad frame retries on the next poll
            text = ocr_clean_text(text)
            self._sub_job_q.put(
                lambda t=text, p=frame_pos: self._ocr_text_ready(t, p))
            # pace to ~one capture per second: always fresh with the
            # playhead, never a backlog of old frames
            stop.wait(max(0.0, 0.7 - (time.monotonic() - cycle_start)))
        self._cleanup_ocr_files()

    def _ocr_prep(self, path):
        # Caption strip for OCR: bottom 30% of the frame, doubled, binarized.
        # Hardsubs are bright (white/yellow) with a dark outline, so a high
        # threshold isolates the text.
        # ponytail: fixed threshold 200; auto-threshold if odd backgrounds fail
        from PIL import Image
        img = Image.open(path)
        w, h = img.size
        strip = img.crop((0, int(h * 0.70), w, h)).convert("L")
        strip = strip.resize((strip.width * 2, strip.height * 2), Image.LANCZOS)
        return strip.point(lambda p: 255 if p > 200 else 0)

    def _ocr_strip_png(self, path):
        buf = io.BytesIO()
        self._ocr_prep(path).save(buf, "PNG")
        return buf.getvalue()

    def _ocr_gray_png(self, path):
        # Same caption strip without binarization: the second opinion the
        # dual-OCR agreement check reads (grayscale keeps strokes that the
        # fixed 200 threshold can erase).
        from PIL import Image
        img = Image.open(path)
        w, h = img.size
        strip = img.crop((0, int(h * 0.70), w, h)).convert("L")
        strip = strip.resize((strip.width * 3, strip.height * 3), Image.LANCZOS)
        buf = io.BytesIO()
        strip.save(buf, "PNG")
        return buf.getvalue()

    def _ocr_tesseract(self, path, lang):
        import pytesseract
        return pytesseract.image_to_string(self._ocr_prep(path),
                                           lang=lang or "eng", config="--psm 6")

    async def _winrt_recognize(self, engine, png_bytes):
        import winrt.windows.graphics.imaging as imaging
        import winrt.windows.storage.streams as streams
        stream = streams.InMemoryRandomAccessStream()
        writer = streams.DataWriter(stream)
        writer.write_bytes(png_bytes)
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        decoder = await imaging.BitmapDecoder.create_async(stream)
        bmp = await decoder.get_software_bitmap_async()
        res = await engine.recognize_async(bmp)
        return res.text or ""

    def _ocr_text_ready(self, text, pos=None):
        if not self.ocr_mode:
            return
        if pos is None:
            try:
                pos = max(0, self.player.get_time())
            except Exception:
                return
        t = max(0, int(pos))
        if text == self._ocr_last_text:
            return
        # Same caption continuing (e.g. read again right after a seek, or
        # with tiny OCR differences): keep ONE cue, reopen/extend it
        # instead of appending a duplicate that would be spoken again.
        last = self.ocr_cues[-1] if self.ocr_cues else None
        if (text and last is not None
                and _ocr_same_caption(text, last.text)
                and t <= last.end + 3000):
            self.ocr_cues[-1] = Cue(last.start, max(last.end, t) + 3600000,
                                    last.text)
            self._ocr_last_text = text
            return
        self._close_open_ocr_cue(t)
        if text:
            self.ocr_cues.append(Cue(t, t + 3600000, text))   # open-ended
            self.ocr_starts.append(t)
        self._ocr_last_text = text

    def _close_open_ocr_cue(self, t=None):
        if not self.ocr_cues:
            return
        if t is None:
            try:
                t = max(0, self.player.get_time())
            except Exception:
                t = 0
        last = self.ocr_cues[-1]
        if last.end > t + 1000:                     # still open-ended
            self.ocr_cues[-1] = Cue(last.start, max(last.start + 500, t),
                                    last.text)

    def toggle_play(self):
        if self.player is None:
            return
        if self.player.is_playing():
            self.player.set_pause(1)
            self.tts.purge()
            self.mt.purge()
        else:
            self.player.play()
            self.spoken_idx = -1      # speak the cue at the resume point
            self.current_cue_idx = None   # ...even if its index did not change

    def stop(self):
        if self.player is None:
            return
        self.player.stop()
        self.tts.purge()
        self.mt.purge()
        self.spoken_idx = -1
        self.current_cue_idx = None
        self._last_spoken_text = None
        self._resume_skip = False
        self.sub_label.config(text="")
        self.seekbar.set_fraction(0.0)

    def seek_ms(self, delta):
        if self.player is None:
            return
        t = self.player.get_time()
        if t < 0:
            return
        self._seek_from = t
        self.player.set_time(max(0, t + delta))
        self._after_seek()

    def _after_seek(self):
        self.tts.purge()
        self.mt.purge()
        self.spoken_idx = -1
        self.current_cue_idx = None
        # A forward seek that lands on the line that was JUST being spoken
        # (caption still on screen) would repeat it word for word, the
        # tick skips that one repeat. Backward seeks always speak (re-listen).
        self._resume_skip = False
        try:
            new_t = self.player.get_time()
        except Exception:
            new_t = None
        if (self._last_spoken_text and self._seek_from is not None
                and new_t is not None and new_t > self._seek_from + 250):
            self._resume_skip = True
        self._seek_from = None
        if self.ocr_mode:
            # pre-seek cues hold the old timeline: their windows would match
            # (and re-speak) stale lines as playback crosses them again
            self.ocr_cues = []
            self.ocr_starts = []
            self._ocr_last_text = None      # re-read captions after seeking

    def _on_seek_to(self, frac):
        """Scrub finished on the seek bar, jump to the chosen position."""
        self._scrubbing = False
        if self.player is None:
            return
        length = self.player.get_length()
        if length > 0:
            try:
                self._seek_from = self.player.get_time()
            except Exception:
                self._seek_from = None
            self.player.set_position(min(1.0, max(0.0, frac)))
            self._after_seek()

    # speed ---------------------------------------------------------------

    def set_rate(self, rate=None, delta=None):
        if self.player is None:
            return
        cur = self.player.get_rate() or 1.0
        if rate is not None:
            new = float(rate)
        else:
            new = cur + (delta or 0.0)
        new = max(0.25, min(8.0, round(new * 4) / 4.0))
        self.player.set_rate(new)
        self._sync_tts_rate(new)          # the voice follows the video speed
        self._show_toast("%.2fx" % new, ms=1000, big=True)
        self.set_status("Speed: %.2fx (voice speed follows)." % new)

    def reset_rate(self):
        self.set_rate(1.0)

    def _show_toast(self, text, ms=2600, big=False):
        """Popup notice on the video area: speed changes (big, top right)
        and live-translation state (small, bottom center). One shared
        Toplevel, because VLC paints its own native window over Tk
        children."""
        t = self._speed_toast
        if t is None:
            t = tk.Toplevel(self.root)
            t.overrideredirect(True)
            t.configure(bg=self.THEME["border"])
            self.speed_toast_lbl = tk.Label(t, justify="center",
                                            bg=self.THEME["panel"], pady=8)
            self.speed_toast_lbl.pack(padx=1, pady=1)   # 1px border
            self._speed_toast = t
        if big:
            self.speed_toast_lbl.config(text=text,
                                        font=("Segoe UI", 22, "bold"),
                                        fg=self.THEME["accent"], padx=20)
        else:
            self.speed_toast_lbl.config(text=text,
                                        font=("Segoe UI", 10, "bold"),
                                        fg=self.THEME["fg"], padx=16)
        with contextlib.suppress(Exception):
            t.attributes("-topmost", True)
            t.attributes("-alpha", 0.92)
            t.update_idletasks()
            va = self.video_area
            if big:
                x = va.winfo_rootx() + va.winfo_width() - t.winfo_reqwidth() - 16
                y = va.winfo_rooty() + 16
            else:
                x = (va.winfo_rootx()
                     + (va.winfo_width() - t.winfo_reqwidth()) // 2)
                y = (va.winfo_rooty() + va.winfo_height()
                     - t.winfo_reqheight() - 40)
            t.geometry("+%d+%d" % (max(0, x), max(0, y)))
            t.deiconify()
            t.lift()
        if self._speed_toast_after:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._speed_toast_after)
        self._speed_toast_after = self.root.after(ms, self._hide_toast)

    def _hide_toast(self):
        self._speed_toast_after = None
        if self._speed_toast is not None:
            with contextlib.suppress(Exception):
                self._speed_toast.withdraw()

    # volume --------------------------------------------------------------

    def change_volume(self, delta):
        if self.player is None:
            return
        with contextlib.suppress(Exception):
            cur = self.player.audio_get_volume()
            if cur < 0:
                cur = 100
            new = max(0, min(125, int(cur) + delta))
            self.player.audio_set_volume(new)
            if self.player.audio_get_mute():
                self.player.audio_set_mute(False)
            self._sync_vol_slider(new)

    def _on_vol_slider(self, val):
        # ttk.Scale callback: drag the VLC-style volume slider.
        if self._vol_slider_busy or self.player is None:
            return
        with contextlib.suppress(Exception):
            v = max(0, min(125, int(float(val))))
            self.player.audio_set_volume(v)
            if v > 0 and self.player.audio_get_mute():
                self.player.audio_set_mute(False)

    def _sync_vol_slider(self, vol):
        try:
            self._vol_slider_busy = True
            self.vol_scale.set_frac(max(0, min(125, int(vol))) / 125.0)
        except Exception:
            pass
        finally:
            self._vol_slider_busy = False

    def _push_cue_spans(self):
        """Draw the loaded subtitle cues as blocks on both seek bars."""
        dur = 0
        with contextlib.suppress(Exception):
            dur = self.player.get_length() or 0
        spans = []
        if dur > 0:
            for c in self.cues:
                a = max(0.0, min(1.0, c.start / float(dur)))
                b = max(0.0, min(1.0, c.end / float(dur)))
                if b > a + 0.0005:
                    spans.append((a, b))
        for sb in (self.seekbar, getattr(self, "_fs_seek", None)):
            if sb is not None:
                sb.set_cues(spans)

    def toggle_mute(self):
        if self.player is None:
            return
        with contextlib.suppress(Exception):
            self.player.audio_toggle_mute()

    # subtitles / read aloud ----------------------------------------------

    def toggle_sub_show(self):
        self.sub_show.set(not self.sub_show.get())
        if self.sub_btn is not None:
            self.sub_btn.set_on(self.sub_show.get())
        if not self.sub_show.get():
            self.sub_label.config(text="")

    def toggle_read_aloud(self):
        self.read_aloud.set(not self.read_aloud.get())
        on = self.read_aloud.get()
        self._set_toggle(self.read_btn, on,
                         "Read Aloud", "Read Aloud")
        if on:
            self.spoken_idx = -1
            self.current_cue_idx = None  # speak the caption already on screen
            self._resume_skip = False    # turning it ON should speak now
            self.set_status("Read Aloud is ON: every subtitle line is spoken. "
                            "Tip: lower the video volume a bit.")
        else:
            self.tts.purge()
            self.mt.purge()
            self.set_status("Read Aloud is OFF.")

    def _on_voice_selected(self, _event=None):
        idx = self.voice_combo.current()
        raw = getattr(self, "_voice_raw", [])
        if 0 <= idx < len(raw):
            kind, value = raw[idx]
            if kind == "piper":
                self.tts.set_piper_voice(value)
                self.set_status("Read Aloud voice: Piper %s (neural), "
                                "first line may pause while it loads"
                                % value)
            else:
                self.tts.set_voice(idx)
                self.set_status("Read Aloud voice: %s" % value)
            return
        self.tts.set_voice(idx)
        self.set_status("Read Aloud voice: %s" % self.voice_combo.get())

    # live translation -----------------------------------------------------

    def _on_fs_mt(self, _e=None):
        with contextlib.suppress(Exception):
            self.mt_lang.current(self._fs_mt.current())
        self._on_mt_lang()

    def _on_mt_lang(self, _event=None):
        lang = None
        with contextlib.suppress(Exception):
            if self.mt_lang.current() > 0:
                lang = self.mt_lang.get()
        self._set_mt_target(lang)

    def _pick_mt_model(self):
        """A model already on disk wins (no re-download); the 7B one only
        counts on an NVIDIA GPU. Otherwise auto-pick by hardware."""
        for key in ("7b", "1.8b"):
            if key == "7b" and not self.mt._gpu_info.get("has"):
                continue
            if self.mt.installed(key):
                return key
        return self.mt.default_model_key()

    def _set_mt_target(self, lang):
        self.mt.purge()
        if not lang:
            self._mt_target = None
            self.mt.stop_server()
            self.set_status("Live translation OFF.")
            return
        self._mt_target = lang
        key = self._pick_mt_model()
        if self.mt.state == "ready" and self.mt.model_key == key:
            self._show_toast("Live translation ready: %s" % lang)
            return
        if not (self.mt.llamacpp_ready() and self.mt.installed(key)):
            self._show_toast("Live translation needs a one-time model "
                             "download (Tools menu)…", ms=4000)
            self.show_mt_dialog()
            return
        self.mt.start_async(key)
        self._show_toast("Starting the translation model…")

    def _mt_speak_ready(self, idx, text, translation):
        """UI thread: a translated line is in. It is spoken only if it is
        still the newest line (a seek/purge advanced spoken_idx since);
        the shown caption is swapped for the translation as well."""
        if not self.read_aloud.get() or not self._mt_target:
            return
        if idx != self.spoken_idx:
            return
        out = (translation or "").strip() or text
        self.tts.say(out)
        if translation and self.current_cue_idx == idx and self.sub_show.get():
            with contextlib.suppress(Exception):
                self.sub_label.config(text=translation)

    def _sync_tts_rate(self, video_rate):
        """The voice speaks as fast as the video plays: 2x video -> fastest
        speech, 1x -> normal, 0.5x -> slowest (clamped to the TTS limits)."""
        if video_rate and video_rate > 0:
            rate = int(round(10 * math.log2(video_rate)))
            self.tts.set_rate(max(-10, min(10, rate)))

    def _find_cue_in(self, starts, cues, t):
        if not cues:
            return None
        i = bisect.bisect_right(starts, t) - 1
        if i >= 0 and cues[i].start <= t < cues[i].end:
            return i
        return None

    # ---------------- keyboard ----------------

    def _bind_keys(self):
        pairs = [
            ("<space>", self.toggle_play),
            ("<Key-Left>", lambda: self.seek_ms(-10000)),
            ("<Key-Right>", lambda: self.seek_ms(10000)),
            ("<Key-Up>", lambda: self.change_volume(+5)),
            ("<Key-Down>", lambda: self.change_volume(-5)),
            ("<Key-bracketleft>", lambda: self.set_rate(delta=-0.25)),
            ("<Key-bracketright>", lambda: self.set_rate(delta=+0.25)),
            ("<Key-plus>", lambda: self.set_rate(delta=+0.25)),
            ("<Key-equal>", lambda: self.set_rate(delta=+0.25)),
            ("<Key-KP_Add>", lambda: self.set_rate(delta=+0.25)),
            ("<Key-minus>", lambda: self.set_rate(delta=-0.25)),
            ("<Key-underscore>", lambda: self.set_rate(delta=-0.25)),
            ("<Key-KP_Subtract>", lambda: self.set_rate(delta=-0.25)),
            ("<Key-m>", self.toggle_mute),
            ("<Key-s>", self.toggle_read_aloud),
            ("<Key-c>", self.toggle_sub_show),
            ("<Key-f>", self.toggle_fullscreen),
            ("<F11>", self.toggle_fullscreen),
            ("<Escape>", self._exit_fullscreen),
            ("<Control-o>", lambda: self.open_video()),
            ("<Control-u>", lambda: self.open_subtitles()),
        ]

        def run(fn, _e):
            # typing in a dialog field (piper filter, etc.) must not
            # trigger playback hotkeys
            if isinstance(self.root.focus_get(),
                          (tk.Entry, ttk.Combobox, ttk.Spinbox)):
                return "break"
            self.root.focus_set()
            fn()
            return "break"

        for seq, fn in pairs:
            self.root.bind(seq, lambda e, f=fn: run(f, e))

    # ---------------- drag & drop ----------------

    def _enable_drag_drop(self):
        """Allow dropping video / subtitle files from Explorer onto the window."""
        if DND_FILES is None or not hasattr(self.root, "drop_target_register"):
            return                          # tkinterdnd2 not installed
        # registering the root covers the whole window; the video area and
        # its placeholder are registered too so the big black area always hits
        for widget in (self.root, self.video_area, self.placeholder):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop_files)

    def _on_drop_files(self, event):
        # event.data is a Tcl list; paths with spaces come wrapped in {…}
        video = None
        subs = []
        unknown = None
        for raw in self.root.tk.splitlist(event.data):
            path = os.path.normpath(raw)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in VIDEO_EXTS and video is None:
                video = path
            elif ext in SUB_EXTS:
                subs.append(path)
            elif unknown is None:
                unknown = path
        if video is not None:
            self.load_video(video)
        if subs:
            self.load_subtitles(subs[0])
        if video is None and not subs:
            self.set_status("Not a video or subtitle file: %s"
                            % (os.path.basename(unknown) if unknown
                               else os.path.basename(event.data)))

    def _block_vlc_drop_hijack(self):
        """VLC's own embedded video window would steal files dropped on the
        playing picture and play them behind our back, turn that off."""
        if not IS_WINDOWS:
            return
        with contextlib.suppress(Exception):
            shell32 = ctypes.windll.shell32
            user32 = ctypes.windll.user32

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _each_child(hwnd, _lparam):
                with contextlib.suppress(Exception):
                    shell32.DragAcceptFiles(hwnd, False)
                return True

            user32.EnumChildWindows(self.video_area.winfo_id(), _each_child, 0)

    # ---------- clicks on the native VLC video window ----------
    # VLC's D3D11 vout renders into its own Win32 child windows inside
    # video_area. The render-surface window belongs to a VLC thread that
    # never pumps posted messages, so WM_LBUTTONUP over the picture is
    # never dispatched to any window (and Tk sees nothing either). The
    # only reliable interception point is a global WH_MOUSE_LL hook: it
    # runs on our Tk main-loop thread before any window routing, so we
    # can see every click and keep the ones landing on VLC's video
    # windows. The callback must stay tiny and must never raise.

    def _init_ll_mouse_hook(self):
        if not IS_WINDOWS or self._ll_mouse_hook is not None:
            return
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            class MSLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = (("pt", wintypes.POINT),
                            ("mouseData", wintypes.DWORD),
                            ("flags", wintypes.DWORD),
                            ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t))

            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE,
                wintypes.DWORD]
            # no argtypes on CallNextHookEx: the hook gives us the lParam
            # as a POINTER(MSLLHOOKSTRUCT), which ctypes then passes by
            # its address (declaring LPARAM here would reject it)
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            # without this restype the 64-bit module handle is truncated
            # to 32 bits and SetWindowsHookExW fails with a bad handle
            kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            app = self

            @ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                wintypes.WPARAM,
                                ctypes.POINTER(MSLLHOOKSTRUCT))
            def _ll_proc(n_code, w_param, l_param):
                with contextlib.suppress(Exception):
                    if n_code >= 0 and w_param == 0x0202:   # WM_LBUTTONUP
                        m = l_param.contents
                        app._click_q.put((m.time, m.pt.x, m.pt.y))
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            hmod = kernel32.GetModuleHandleW(None)
            self._ll_mouse_cb = _ll_proc
            self._ll_mouse_hook = user32.SetWindowsHookExW(
                14,                        # WH_MOUSE_LL
                ctypes.cast(_ll_proc, ctypes.c_void_p), hmod, 0)
            if not self._ll_mouse_hook:
                # WH_MOUSE_LL also accepts a NULL module handle
                self._ll_mouse_hook = user32.SetWindowsHookExW(
                    14, ctypes.cast(_ll_proc, ctypes.c_void_p), None, 0)
        except Exception:
            self._ll_mouse_hook = None

    def _unhook_ll_mouse(self):
        with contextlib.suppress(Exception):
            if self._ll_mouse_hook:
                ctypes.windll.user32.UnhookWindowsHookEx(self._ll_mouse_hook)
        self._ll_mouse_hook = None
        self._ll_mouse_cb = None

    # The vout child WNDPROC subclass below only hides the idle cursor in
    # fullscreen (WM_SETCURSOR). Clicks are NOT handled here - the vout
    # render thread never dispatches posted messages, so this proc would
    # never see them (the WH_MOUSE_LL hook does that instead).

    def _init_click_hook(self):
        if not IS_WINDOWS or self._vlc_proc_cb is not None:
            return
        with contextlib.suppress(Exception):
            user32 = ctypes.windll.user32
            set_wndproc = getattr(user32, "SetWindowLongPtrW", None) \
                or getattr(user32, "SetWindowLongW", None)
            if set_wndproc is None:
                return
            set_wndproc.restype = ctypes.c_void_p
            set_wndproc.argtypes = [wintypes.HWND, ctypes.c_int,
                                    ctypes.c_void_p]
            call_wndproc = user32.CallWindowProcW
            call_wndproc.restype = ctypes.c_ssize_t
            call_wndproc.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                     wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM]
            app = self
            hooks = self._vlc_hooks

            @ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                wintypes.UINT, wintypes.WPARAM,
                                wintypes.LPARAM)
            def _vlc_proc(hwnd, msg, wparam, lparam):
                old = hooks.get(hwnd)
                with contextlib.suppress(Exception):
                    if (msg == 0x0020 and app._fs_cursor_hidden
                            and wparam == hwnd and (lparam & 0xFFFF) == 1):
                        # WM_SETCURSOR, idle in fullscreen: no cursor on video
                        user32.SetCursor(None)
                        return 1
                if old:
                    return call_wndproc(old, hwnd, msg, wparam, lparam)
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._vlc_proc_cb = _vlc_proc
            self._vlc_set_wndproc = set_wndproc

    def _hook_vlc_clicks(self):
        """Subclass any (new) VLC child windows so we can hide the cursor
        over the idle video in fullscreen. VLC (re)creates these windows
        as playback starts."""
        if not IS_WINDOWS or self._video_path is None:
            return
        self._init_click_hook()
        if self._vlc_proc_cb is None:
            return
        with contextlib.suppress(Exception):
            user32 = ctypes.windll.user32
            for hwnd in list(self._vlc_hooks):
                if not user32.IsWindow(hwnd):
                    del self._vlc_hooks[hwnd]
            proc_addr = ctypes.cast(self._vlc_proc_cb, ctypes.c_void_p)
            hooks = self._vlc_hooks
            set_wndproc = self._vlc_set_wndproc

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _each_child(hwnd, _lparam):
                if hwnd not in hooks:
                    old = set_wndproc(hwnd, -4, proc_addr)  # GWL_WNDPROC
                    if old:
                        hooks[hwnd] = old
                return True

            user32.EnumChildWindows(self.video_area.winfo_id(),
                                    _each_child, 0)

    def _unhook_vlc_clicks(self):
        with contextlib.suppress(Exception):
            if self._vlc_set_wndproc:
                for hwnd, old in list(self._vlc_hooks.items()):
                    with contextlib.suppress(Exception):
                        self._vlc_set_wndproc(hwnd, -4, old)
        # clear (not rebind): the WNDPROC closure holds this exact dict
        self._vlc_hooks.clear()

    def _note_click(self, t_ms, x, y):
        """Record one left click (event time in ms, screen coords). True
        when this click completed a double-click. Uses the system
        double-click time and distance, like VLC does."""
        try:
            dbl_time = ctypes.windll.user32.GetDoubleClickTime() or 500
            dbl_w = max(4, ctypes.windll.user32.GetSystemMetrics(36)) * 2
            dbl_h = max(4, ctypes.windll.user32.GetSystemMetrics(37)) * 2
        except Exception:
            dbl_time, dbl_w, dbl_h = 500, 8, 8
        t0, x0, y0 = self._last_click
        d = (t_ms - t0) & 0xFFFFFFFF       # GetTickCount-style difference
        if (t0 and d <= dbl_time and x0 is not None
                and abs(x - x0) <= dbl_w and abs(y - y0) <= dbl_h):
            self._last_click = (0, None, None)
            return True
        self._last_click = (t_ms, x, y)
        return False

    def _on_video_click(self, t_ms, x, y):
        if self._note_click(t_ms, x, y):
            self.toggle_fullscreen()

    def _drain_native_clicks(self):
        """Act on clicks the low-level hook queued. Only clicks that land
        on VLC's video child windows count, so clicks on our own toolbars,
        the floating bar or other apps are ignored."""
        if not self._video_path or not self._vlc_hooks:
            with contextlib.suppress(queue.Empty):
                while True:
                    self._click_q.get_nowait()
            return
        try:
            user32 = ctypes.windll.user32
            va = self.video_area.winfo_id()
            x0 = self.video_area.winfo_rootx()
            y0 = self.video_area.winfo_rooty()
            x1 = x0 + self.video_area.winfo_width()
            y1 = y0 + self.video_area.winfo_height()
            while True:
                t_ms, x, y = self._click_q.get_nowait()
                if not (x0 <= x < x1 and y0 <= y < y1):
                    continue                      # outside the video area
                pt = wintypes.POINT(x, y)
                hit = user32.WindowFromPoint(pt)
                if hit in self._vlc_hooks:        # truly on the video
                    self._on_video_click(t_ms, x, y)
        except queue.Empty:
            pass
        except Exception:
            pass

    # ---------- VLC-like floating fullscreen controller ----------
    def _build_fs_bar(self):
        if getattr(self, "_fs_win", None) is not None:
            with contextlib.suppress(Exception):
                if self._fs_win.winfo_exists():
                    return self._fs_win
            self._fs_win = None
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=self.THEME["panel"], bd=0,
                      highlightthickness=1,
                      highlightbackground=self.THEME["border"])
        with contextlib.suppress(Exception):
            win.attributes("-alpha", 0.95)
        win.withdraw()
        r1 = tk.Frame(win, bg=self.THEME["panel"])
        r1.pack(fill="x", padx=12, pady=(8, 4))
        self._fs_cur = tk.Label(r1, text="00:00", bg=self.THEME["panel"],
                                fg=self.THEME["fg"], font=("Segoe UI", 9))
        self._fs_cur.pack(side="left")
        self._fs_total = tk.Label(r1, text="00:00", bg=self.THEME["panel"],
                                  fg=self.THEME["fg_dim"],
                                  font=("Segoe UI", 9))
        self._fs_total.pack(side="right")
        self._fs_seek = Seekbar(r1, on_scrub=self._on_fs_scrub,
                                on_seek=self._on_seek_to)
        self._fs_seek.pack(side="left", fill="x", expand=True, padx=10)
        r2 = tk.Frame(win, bg=self.THEME["panel"])
        r2.pack(fill="x", padx=12, pady=(4, 3))
        # same widgets as the docked bar: play pill + icon buttons
        self._fs_play = PlayPill(r2, command=self._cmd(self.toggle_play),
                                 tip="Play / Pause  (Space)")
        self._fs_play.pack(side="left", padx=(2, 6))
        self._mkicon(r2, _ic_back10,
                     lambda: self.seek_ms(-10000),
                     tip="Back 10 seconds  (←)").pack(side="left")
        self._mkicon(r2, _ic_stop, self.stop,
                     tip="Stop playback").pack(side="left")
        self._mkicon(r2, _ic_fwd10,
                     lambda: self.seek_ms(10000),
                     tip="Forward 10 seconds  (→)").pack(side="left")
        self._mkicon(r2, _ic_fullscreen, self.toggle_fullscreen,
                     tip="Exit fullscreen  (Esc  or  F)").pack(
            side="left", padx=(6, 0))
        self._fs_sep(r2)
        self._fs_cc = self._mkicon(r2, _ic_cc, self.toggle_sub_show,
                                   tip="Show / hide subtitles  (C)")
        self._fs_cc.pack(side="left")
        self._fs_cc.set_on(True)
        self._fs_btn(r2, "+Sub", self.open_subtitles,
                     tip="Open a subtitle file  (Ctrl+U)")
        self._fs_sep(r2)
        self._fs_ocr = self._fs_btn(
            r2, "OCR", self.toggle_ocr,
            tip="Read burned-in subtitles from the picture (OCR)")
        self._fs_read = self._fs_btn(r2, "Read Aloud",
                                     self.toggle_read_aloud,
                                     tip="Speak subtitle lines aloud  (S)")
        # volume on the right of the transport row: mute icon, spectrum
        # slider, % (packed right-to-left, so pack the right-most first)
        self._fs_volpct = tk.Label(r2, text="", bg=self.THEME["panel"],
                                   fg=self.THEME["fg_dim"], font=("Segoe UI", 9),
                                   width=5)
        self._fs_volpct.pack(side="right")
        self._fs_vol = VolumeSlider(
            r2, on_change=lambda f: self._on_vol_slider(f * 125.0),
            frac=0.8)
        self._fs_vol.pack(side="right", padx=2)
        _ToolTip(self._fs_vol, "Click or drag to set volume")
        with contextlib.suppress(Exception):
            v0 = self.player.audio_get_volume() if self.player else 100
            self._fs_vol.set_frac(max(0, min(125, int(v0))) / 125.0)
        self._fs_mute = self._mkicon(r2, _ic_volume, self.toggle_mute,
                                     tip="Mute / Unmute  (M)")
        self._fs_mute.pack(side="right", padx=1)

        # third row: the four pickers (Subs / OCR lang / Voice / MT), set
        # off from the transport row by a hairline so the bar reads as
        # "playback above, sources and settings below" instead of one strip
        tk.Frame(win, bg=self.THEME["sep"], height=1).pack(fill="x")
        r3 = tk.Frame(win, bg=self.THEME["panel"])
        r3.pack(fill="x", padx=12, pady=(5, 8))
        small = {"font": ("Segoe UI", 9)}
        self._mklabel(r3, "Subs:", padx=0, **small)
        self._fs_embed_combo = ttk.Combobox(r3, state="readonly", width=14,
                                            font=("Segoe UI", 9))
        self._fs_embed_combo.pack(side="left", padx=(2, 4))
        self._fs_embed_combo.bind("<<ComboboxSelected>>", self._on_fs_embed)
        self._wire_popup_combo(self._fs_embed_combo, self._on_fs_embed)
        _ToolTip(self._fs_embed_combo,
                 "Subtitle track inside the movie (scanned on open)")
        self._mklabel(r3, "OCR:", padx=0, **small)
        self._fs_ocr_lang = ttk.Combobox(r3, state="readonly", width=6,
                                         font=("Segoe UI", 9))
        self._fs_ocr_lang.pack(side="left", padx=(2, 4))
        self._fs_ocr_lang.bind("<<ComboboxSelected>>", self._on_fs_ocr_lang)
        self._wire_popup_combo(self._fs_ocr_lang, self._on_fs_ocr_lang)
        _ToolTip(self._fs_ocr_lang,
                 "Language used when reading picture subtitles (OCR)")
        self._mklabel(r3, "Voice:", padx=0, **small)
        self._fs_voice_combo = ttk.Combobox(r3, state="readonly", width=16,
                                            font=("Segoe UI", 9))
        self._fs_voice_combo.pack(side="left", padx=(2, 0))
        self._fs_voice_combo.bind("<<ComboboxSelected>>", self._on_fs_voice)
        self._wire_popup_combo(self._fs_voice_combo, self._on_fs_voice)
        _ToolTip(self._fs_voice_combo,
                 "Voice used by Read Aloud, get Piper voices from the "
                 "Tools menu (neural, offline)")
        self._fs_sep(r3)
        self._mklabel(r3, "MT:", padx=0, **small)
        self._fs_mt = ttk.Combobox(r3, state="readonly", width=8,
                                   font=("Segoe UI", 9))
        self._fs_mt.pack(side="left", padx=(2, 0))
        self._fs_mt.bind("<<ComboboxSelected>>", self._on_fs_mt)
        self._wire_popup_combo(self._fs_mt, self._on_fs_mt)
        _ToolTip(self._fs_mt,
                 "Live translation language (offline AI model, see the "
                 "Tools menu)")
        self._fs_sync_combos(force=True)

        win.bind("<Enter>", lambda _e: self._fs_wake())
        win.bind("<Motion>", lambda _e: self._fs_wake())
        self._make_fs_bar_draggable(win)
        self._fs_win = win
        return win

    # ---- fullscreen-bar helpers ----
    def _fs_sep(self, parent):
        tk.Frame(parent, bg=self.THEME["sep"], width=1).pack(
            side="left", fill="y", padx=5, pady=3)

    def _fs_btn(self, parent, text, cmd, tip=None):
        """Flat text chip for the fullscreen bar (same style as the docked
        _mkbtn); carries a _bg_holder so _set_toggle-style ON/OFF
        highlighting works on it too."""
        def wrapped():
            cmd()
            self.root.focus_set()
        b = tk.Button(parent, text=text, command=wrapped, cursor="hand2",
                      font=("Segoe UI", 9), bd=0, relief="flat",
                      bg=self.THEME["btn"], fg=self.THEME["fg"],
                      activebackground=self.THEME["btn_hover"],
                      activeforeground=self.THEME["fg"],
                      padx=6, pady=3, takefocus=0)
        holder = {"off": self.THEME["btn"]}

        def _enter(_e):
            b.config(bg=self.THEME["btn_on_hover"]
                     if holder["off"] == self.THEME["btn_on"]
                     else self.THEME["btn_hover"])
        b._bg_holder = holder
        b.bind("<Enter>", _enter)
        b.bind("<Leave>", lambda e: b.config(bg=holder["off"]))
        if tip:
            _ToolTip(b, tip)
        b.pack(side="left", padx=1)
        return b

    def _fs_apply_toggle(self, btn, on):
        btn.config(bg=self.THEME["btn_on"] if on else self.THEME["btn"],
                   fg=self.THEME["btn_on_fg"] if on else self.THEME["fg"])
        btn._bg_holder["off"] = self.THEME["btn_on"] if on \
            else self.THEME["btn"]

    def _on_fs_embed(self, _e=None):
        with contextlib.suppress(Exception):
            self.embed_combo.current(self._fs_embed_combo.current())
        self._on_embed_selected()

    def _on_fs_ocr_lang(self, _e=None):
        with contextlib.suppress(Exception):
            self.ocr_lang.current(self._fs_ocr_lang.current())
        self._on_ocr_lang()

    def _on_fs_voice(self, _e=None):
        with contextlib.suppress(Exception):
            self.voice_combo.current(self._fs_voice_combo.current())
        self._on_voice_selected()

    def _fs_sync_combos(self, force=False):
        """Mirror the docked Subs/OCR/Voice combos into the fullscreen bar."""
        with contextlib.suppress(Exception):
            for src, dst in ((self.embed_combo, self._fs_embed_combo),
                             (self.ocr_lang, self._fs_ocr_lang),
                             (self.voice_combo, self._fs_voice_combo),
                             (self.mt_lang, self._fs_mt)):
                vals = list(src["values"])
                try:
                    cur = src.current()
                    if not isinstance(cur, int) or cur < 0:
                        cur = -1
                except Exception:
                    cur = -1
                key = (tuple(vals), cur)
                if force or getattr(dst, "_mirror_key", None) != key:
                    dst._mirror_key = key
                    dst["values"] = vals
                    if 0 <= cur < len(vals):
                        dst.current(cur)
                    elif not vals:
                        dst.set("")

    def _on_fs_scrub(self, scrubbing):
        self._scrubbing = scrubbing

    def _fs_cancel_hide(self):
        with contextlib.suppress(Exception):
            if self._fs_hide_after:
                self.root.after_cancel(self._fs_hide_after)
        self._fs_hide_after = None

    def _fs_wake(self, _e=None):
        if not getattr(self, "_fullscreen", False):
            return
        with contextlib.suppress(Exception):
            self.root.config(cursor="")
        self._fs_cursor_hidden = False
        self._fs_last_move = time.time()
        self._fs_show_bar()
        self._fs_cancel_hide()
        with contextlib.suppress(Exception):
            self._fs_hide_after = self.root.after(2800, self._fs_auto_hide)

    def _fs_show_bar(self):
        with contextlib.suppress(Exception):
            win = self._build_fs_bar()
            if not win.winfo_viewable():
                self._fs_place_bar()
                win.deiconify()
            # always re-raise: the fullscreen video window is also
            # topmost now, so the bar must sit above it every time
            win.lift()
            with contextlib.suppress(Exception):
                win.attributes("-topmost", True)
            # the pointer poll keeps waking the bar while the user moves
            # toward an open dropdown; the bar and the popup share the
            # topmost band (the list flips upward over the bottom-docked
            # bar), so without this re-raise the bar lands above the list
            # and swallows the clicks meant for it
            pop = self._choice_popup
            if pop is not None:
                with contextlib.suppress(Exception):
                    pop["top"].attributes("-topmost", True)
                with contextlib.suppress(Exception):
                    pop["top"].lift()

    def _fs_hide_bar(self):
        with contextlib.suppress(Exception):
            if getattr(self, "_fs_win", None) is not None:
                self._fs_win.withdraw()

    def _fs_auto_hide(self):
        self._fs_hide_after = None
        if not getattr(self, "_fullscreen", False):
            return
        if self._choice_popup is not None:
            # a choice popup holds the pointer grab, so Motion over the bar
            # can't wake us; hold the bar (and the cursor) until it closes
            self._fs_hide_after = self.root.after(2800, self._fs_auto_hide)
            return
        with contextlib.suppress(Exception):
            w = getattr(self, "_fs_win", None)
            if w is not None and w.winfo_viewable():
                x, y = w.winfo_pointerxy()
                if (w.winfo_rootx() <= x <= w.winfo_rootx() + w.winfo_width()
                        and w.winfo_rooty() <= y <= w.winfo_rooty() + w.winfo_height()):
                    self._fs_hide_after = self.root.after(2800, self._fs_auto_hide)
                    return
        self._fs_hide_bar()
        with contextlib.suppress(Exception):
            self.root.config(cursor="none")
        self._fs_cursor_hidden = True
        # the root cursor only covers Tk widgets; also hide it over the
        # native VLC video window (its proc checks _fs_cursor_hidden)
        if self._vlc_hooks:
            with contextlib.suppress(Exception):
                user32 = ctypes.windll.user32
                for hwnd in list(self._vlc_hooks):
                    # WM_SETCURSOR (HTCLIENT | WM_MOUSEMOVE<<16), SMTO_ABORTIFHUNG
                    user32.SendMessageTimeoutW(hwnd, 0x0020, hwnd,
                                                0x02000001, 0x0002, 100, None)

    def _fs_place_bar(self):
        with contextlib.suppress(Exception):
            win = self._fs_win
            if win is None:
                return
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            bw = min(1040, int(sw * 0.74))
            win.update_idletasks()
            bh = win.winfo_reqheight() or 86
            pos = getattr(self, "_fs_bar_pos", None)
            if pos:                        # keep where the user dragged it
                x = max(0, min(pos[0], sw - bw))
                y = max(0, min(pos[1], sh - bh))
            else:
                x = (sw - bw) // 2
                y = sh - bh - 64      # clears the subtitle strip at the bottom
            win.geometry("%dx%d+%d+%d" % (bw, bh, x, max(0, y)))

    def _make_fs_bar_draggable(self, win):
        # VLC-style: the controller moves by any empty background; buttons,
        # combos and the canvas sliders (Seekbar / volume wedge) keep their
        # own click handling
        _no_drag = ("Button", "TButton", "TCombobox", "TScrollbar",
                    "TScale", "Canvas", "Entry", "TEntry")
        state = {"on": False, "dx": 0, "dy": 0}

        def start(event):
            if event.widget.winfo_class() in _no_drag:
                state["on"] = False
                return
            state["on"] = True
            state["dx"] = event.x_root - win.winfo_x()
            state["dy"] = event.y_root - win.winfo_y()

        def move(event):
            if not state["on"]:
                return
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x = max(0, min(event.x_root - state["dx"], sw - win.winfo_width()))
            y = max(0, min(event.y_root - state["dy"], sh - win.winfo_height()))
            win.geometry("+%d+%d" % (x, y))
            self._fs_bar_pos = (x, y)  # survive auto-hide/show cycles

        def stop(_e):
            state["on"] = False

        win._fs_drag_start = start     # reachable for _popup_smoke.py
        win._fs_drag_move = move
        win._fs_drag_stop = stop
        win.bind("<ButtonPress-1>", start, add="+")
        win.bind("<B1-Motion>", move, add="+")
        win.bind("<ButtonRelease-1>", stop, add="+")

    def _fs_sync(self, cur_txt, tot_txt, frac, vol, muted):
        with contextlib.suppress(Exception):
            if getattr(self, "_fs_win", None) is None:
                return
            if not self._fs_win.winfo_viewable():
                return
            self._fs_cur.config(text=cur_txt)
            self._fs_total.config(text=tot_txt)
            if not getattr(self._fs_seek, "_dragging", False):
                self._fs_seek.set_fraction(frac)
            # toggles / speed: mirror the docked bar's state
            self._fs_cc.set_on(bool(self.sub_show.get()))
            self._fs_apply_toggle(self._fs_ocr, bool(self.ocr_mode))
            on = bool(self.read_aloud.get())
            self._fs_apply_toggle(self._fs_read, on)
            txt = "Read Aloud"
            if str(self._fs_read["text"]) != txt:
                self._fs_read.config(text=txt)
            self._fs_sync_combos()
            self._fs_vol.set_frac((0 if muted else vol) / 125.0)
            self._fs_volpct.config(text=("Muted" if muted else "%d%%" % int(vol)))
            self._fs_mute.set_icon(_ic_mute if muted else _ic_volume)
            self._fs_mute.set_on(muted)
            playing = self.player.is_playing() if self.player else False
            self._fs_play.set_playing(playing)

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)
        with contextlib.suppress(Exception):
            if self._fullscreen:
                self.menu_row.pack_forget()
            else:
                self.menu_row.pack(fill="x", before=self.video_area)
        if self._fullscreen:
            # stay above other windows, like VLC's fullscreen video
            with contextlib.suppress(Exception):
                self.root.attributes("-topmost", True)
                self.root.lift()
                self.root.focus_force()
            # hide the docked control bar so only the video shows
            with contextlib.suppress(Exception):
                self.controls.pack_forget()
            # force the geometry manager to relayout now: on Windows a
            # bare pack_forget() can leave a ghost strip behind
            with contextlib.suppress(Exception):
                self.root.update_idletasks()
            # belt & braces: if the packer glitches, cover any leftover
            # strip by expanding the video area over the full window
            with contextlib.suppress(Exception):
                self.video_area.pack_forget()
                self.video_area.pack(fill="both", expand=True,
                                     side="top")
                self.root.update_idletasks()
            with contextlib.suppress(Exception):
                self.root.unbind("<Motion>")
            self.root.bind("<Motion>", self._fs_wake, add="+")
            self._fs_last_move = time.time()
            self._fs_cursor_hidden = False
            self._fs_show_bar()
            self._fs_cancel_hide()
            with contextlib.suppress(Exception):
                self._fs_hide_after = self.root.after(2800, self._fs_auto_hide)
        else:
            with contextlib.suppress(Exception):
                self.root.attributes("-topmost", False)
            self._fs_cancel_hide()
            self._hide_choice_popup()      # its grab must not outlive fs mode
            self._fs_hide_bar()
            with contextlib.suppress(Exception):
                self.root.config(cursor="")
            with contextlib.suppress(Exception):
                self.root.unbind("<Motion>")
            self._fs_last_ptr = None
            self._fs_bar_pos = None        # a new session starts bottom-center
            with contextlib.suppress(Exception):
                self.controls.pack_forget()
                self.controls.pack(fill="x", side="bottom")

    def _exit_fullscreen(self):
        if getattr(self, "_fullscreen", False):
            self.toggle_fullscreen()

    # ---------------- help / about ----------------

    def show_piper_dialog(self):
        """Download neural Piper voices from HuggingFace (File menu)."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Get Piper voices, neural TTS")
        dlg.configure(bg=self.CBG)
        dlg.transient(self.root)
        dlg.resizable(True, True)

        info = tk.Label(dlg, bg=self.CBG, fg=self.THEME["fg"], justify="left",
                        text=("Piper voices are small neural networks that sound much more\n"
                              "natural than the built-in Windows voices. They run fully\n"
                              "offline. Pick a voice below (e.g. en_US-lessac-medium for\n"
                              "English, ~63 MB) and click Download, it is saved in\n"
                              "piper\\voices and appears in the Voice dropdown.\n"
                              "Voices for every language: filter with e.g. hi_IN for Hindi."))
        info.pack(anchor="w", padx=12, pady=(10, 6))

        filter_row = tk.Frame(dlg, bg=self.CBG)
        filter_row.pack(fill="x", padx=12)
        tk.Label(filter_row, text="Filter:", bg=self.CBG,
                 fg=self.THEME["fg_dim"]).pack(side="left")
        filter_var = tk.StringVar()
        tk.Entry(filter_row, textvariable=filter_var, width=14,
                 bg="#ffffff", fg="#1a1a1a", insertbackground="#1a1a1a",
                 relief="sunken", bd=1).pack(side="left", padx=6)

        cols = ("voice", "size", "disk")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=13)
        for cid, text_, w, anchor in (("voice", "Voice", 240, "w"),
                                      ("size", "Size", 80, "e"),
                                      ("disk", "On disk", 64, "center")):
            tree.heading(cid, text=text_)
            tree.column(cid, width=w, anchor=anchor)
        tree.pack(fill="both", expand=True, padx=12, pady=6)
        style = ttk.Style(dlg)
        style.configure("Piper.Treeview", background="#ffffff",
                        fieldbackground="#ffffff", foreground="#1a1a1a",
                        rowheight=22)
        tree.configure(style="Piper.Treeview")

        status_var = tk.StringVar(
            value="Loading voice list from huggingface.co…")
        status_lbl = tk.Label(dlg, textvariable=status_var, bg=self.CBG,
                              fg=self.THEME["fg_dim"], anchor="w")
        status_lbl.pack(fill="x", padx=12, pady=(0, 4))

        btns = tk.Frame(dlg, bg=self.CBG)
        btns.pack(fill="x", padx=12, pady=(0, 10))
        dl_btn = tk.Button(btns, text="Download selected", width=20,
                           bg="#e8e8e8", fg="#1a1a1a", bd=1,
                           font=("Segoe UI", 9),
                           activebackground="#d7d7d7")
        dl_btn.pack(side="left")
        def _open_voices_folder():
            # The download folder choice must match download_piper_voice():
            # writable install folder if possible, user profile otherwise.
            try:
                os.makedirs(PIPER_VOICES_DIR, exist_ok=True)
                probe = os.path.join(PIPER_VOICES_DIR, ".wtest")
                with open(probe, "w"):
                    pass
                os.remove(probe)
                dest = PIPER_VOICES_DIR
            except OSError:
                dest = PIPER_USER_VOICES_DIR
                os.makedirs(dest, exist_ok=True)
            os.startfile(dest)

        tk.Button(btns, text="Open voices folder", bd=1,
                  bg="#e8e8e8", fg="#1a1a1a",
                  command=_open_voices_folder
                  ).pack(side="left", padx=8)
        tk.Button(btns, text="Close", bd=1, bg="#e8e8e8",
                  fg="#1a1a1a",
                  command=dlg.destroy).pack(side="right")

        catalog = []          # [(name, size, path)]
        busy = {"downloading": False}

        def refresh_tree():
            flt = filter_var.get().strip().lower()
            have = {v["name"] for v in self.tts.piper_voices}
            tree.delete(*tree.get_children())
            for name, size, path in catalog:
                if flt and flt not in name.lower():
                    continue
                tree.insert("", "end", values=(
                    name, "%.0f MB" % (size / 1e6),
                    "✓" if name in have else ""))
            n = len(tree.get_children())
            status_var.set("%d voices%s" % (n, " (filtering: %s)" % flt if flt else ""))
            # preselect a good default English voice
            if flt == "" and catalog:
                for item in tree.get_children():
                    if tree.set(item, "voice") == "en_US-lessac-medium":
                        tree.selection_set(item)
                        tree.see(item)
                        break

        def fetch_catalog():
            try:
                nonlocal_catalog = fetch_piper_catalog()
            except Exception as exc:
                def _err():
                    status_var.set(
                        "Could not fetch the voice list (%s).\n"
                        "Download manually from huggingface.co/rhasspy/piper-voices "
                        "and put the .onnx + .onnx.json files into piper\\voices." % exc)
                    with contextlib.suppress(Exception):
                        status_lbl.config(fg="#f87171")
                dlg.after(0, _err)
                return
            catalog[:] = nonlocal_catalog
            dlg.after(0, refresh_tree)

        def on_filter(*_a):
            if catalog:
                refresh_tree()

        def do_download():
            sel = tree.selection()
            if not sel or busy["downloading"]:
                return
            name = tree.set(sel[0], "voice")
            entry = next((c for c in catalog if c[0] == name), None)
            if entry is None:
                return
            busy["downloading"] = True
            dl_btn.config(state="disabled", bg=self.THEME["well"])
            status_var.set("Downloading %s …" % name)

            def _prog(frac, msg):
                dlg.after(0, lambda: status_var.set(msg))

            def _work():
                try:
                    download_piper_voice(name, entry[2], progress=_prog)
                except Exception as exc:
                    dlg.after(0, lambda: status_var.set(
                        "Download failed: %s" % exc))
                else:
                    self.tts.rescan_piper()
                    dlg.after(1200, lambda: (refresh_tree(),
                                             status_var.set(
                                                 "Downloaded %s, pick it in the "
                                                 "Voice dropdown (Piper: %s)"
                                                 % (name, name))))
                finally:
                    def _done():
                        busy["downloading"] = False
                        dl_btn.config(state="normal", bg=self.THEME["accent"])
                    dlg.after(0, _done)

            threading.Thread(target=_work, daemon=True).start()

        dl_btn.config(command=do_download)
        filter_var.trace_add("write", on_filter)
        threading.Thread(target=fetch_catalog, daemon=True).start()

        dlg.update_idletasks()
        w, h = 480, 560
        dlg.geometry("%dx%d+%d+%d" % (
            w, h,
            self.root.winfo_x() + max(0, (self.root.winfo_width() - w) // 2),
            self.root.winfo_y() + max(0, (self.root.winfo_height() - h) // 2)))
        dlg.grab_set()

    def show_mt_dialog(self):
        """One-time setup of the offline live translation (Tools menu):
        download the llama.cpp engine and a Hy-MT2 model, then start it."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Live translation (offline AI model)")
        dlg.configure(bg=self.CBG)
        dlg.transient(self.root)
        dlg.resizable(False, False)

        gpu = self.mt._gpu_info
        gpu_line = ("NVIDIA GPU: %s (%.1f GB free), the 7B model is "
                    "recommended." % (gpu.get("name"),
                                      gpu.get("free_mb", 0) / 1024.0)
                    if gpu.get("has") else
                    "No NVIDIA GPU found, the 1.8B model is recommended "
                    "(runs on the CPU).")
        info = tk.Label(dlg, bg=self.CBG, fg=self.THEME["fg"], justify="left",
                        text=(
                            "Pick a language in the MT box and every subtitle\n"
                            "line is translated to it by a local AI model\n"
                            "(Tencent Hy-MT2) before it is shown and spoken.\n"
                            "Fully offline, nothing is sent anywhere.\n\n"
                            "This one-time setup downloads the model and\n"
                            "starts it.\n\n" + gpu_line))
        info.pack(anchor="w", padx=12, pady=(10, 6))

        # the model picks itself: one already on disk wins, else by hardware.
        # No model picker: the choice is fully determined by the machine.
        key = self._pick_mt_model()

        status_var = tk.StringVar(
            value="llama.cpp engine: %s"
                  % ("found" if self.mt.llamacpp_ready() else
                     "will be downloaded (~300 MB)"))
        status_lbl = tk.Label(dlg, textvariable=status_var, bg=self.CBG,
                              fg=self.THEME["fg_dim"], anchor="w",
                              justify="left")
        status_lbl.pack(fill="x", padx=12, pady=(8, 4))

        btns = tk.Frame(dlg, bg=self.CBG)
        btns.pack(fill="x", padx=12, pady=(0, 10))
        go_btn = tk.Button(btns, text="Download & start", width=20,
                           bg="#e8e8e8", fg="#1a1a1a", bd=1,
                           font=("Segoe UI", 9), cursor="hand2",
                           activebackground="#d7d7d7")
        go_btn.pack(side="left")

        def _open_folder():
            dest = None
            for d in MT_MODEL_DIRS:
                if os.path.isdir(d) and os.listdir(d):
                    dest = d
                    break
            if dest is None:
                dest = self.mt._writable_dir(MT_MODEL_DIRS)
                with contextlib.suppress(OSError):
                    os.makedirs(dest, exist_ok=True)
            with contextlib.suppress(Exception):
                os.startfile(dest)

        tk.Button(btns, text="Open model folder", bd=1, bg="#e8e8e8",
                  fg="#1a1a1a", command=_open_folder).pack(side="left", padx=8)
        tk.Button(btns, text="Close", bd=1, bg="#e8e8e8", fg="#1a1a1a",
                  command=dlg.destroy).pack(side="right")

        busy = {"on": False}

        def do_go():
            if busy["on"]:
                return
            key = self._pick_mt_model()
            busy["on"] = True
            go_btn.config(state="disabled", bg=self.THEME["well"])
            status_lbl.config(fg=self.THEME["fg_dim"])
            status_var.set("Working…")

            def _set_status(msg):
                dlg.after(0, lambda m=msg: status_var.set(m))

            def work():
                try:
                    self.mt.ensure_llamacpp(lambda f, m: _set_status(m))
                    self.mt.ensure_model(key, lambda f, m: _set_status(m))
                    _set_status("Starting the model (loads once)…")
                    self.mt.start_async(key)
                except Exception as exc:
                    _set_status("Failed: %s" % exc)
                finally:
                    def _done():
                        busy["on"] = False
                        go_btn.config(state="normal", bg="#e8e8e8")
                    dlg.after(0, _done)

            threading.Thread(target=work, daemon=True).start()

        go_btn.config(command=do_go)

        def poll():
            if not dlg.winfo_exists():
                return
            key = self._pick_mt_model()
            st, mk = self.mt.state, self.mt.model_key
            if st == "ready" and mk == key:
                status_lbl.config(fg=self.THEME["accent"])
                status_var.set("✔ %s is running, pick a language in the "
                               "MT box." % MT_MODELS[key]["label"])
                go_btn.config(text="Running", state="disabled",
                              bg=self.THEME["well"])
            elif st in ("downloading", "starting"):
                status_var.set(self.mt.state_msg or st)
            elif st == "error":
                status_lbl.config(fg="#f87171")
                status_var.set("✘ %s" % (self.mt.error or "failed"))
            else:
                status_lbl.config(fg=self.THEME["fg_dim"])
                status_var.set("Model to use: %s, ~%d MB (%s)%s"
                               % (MT_MODELS[key]["label"],
                                  MT_MODELS[key]["approx_mb"],
                                  MT_MODELS[key]["note"],
                                  ", on disk" if self.mt.installed(key)
                                  else ""))
                go_btn.config(text="Download & start", state="normal",
                              bg="#e8e8e8")
            dlg.after(800, poll)

        poll()

        dlg.update_idletasks()
        w, h = 560, 330
        dlg.geometry("%dx%d+%d+%d" % (
            w, h,
            self.root.winfo_x() + max(0, (self.root.winfo_width() - w) // 2),
            self.root.winfo_y() + max(0, (self.root.winfo_height() - h) // 2)))
        dlg.grab_set()

    def show_install_dialog(self):
        """Install every Python package from requirements.txt (Tools menu).
        Runs pip in the background and shows its output."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Install dependencies")
        dlg.configure(bg=self.CBG)
        dlg.transient(self.root)
        dlg.resizable(True, True)

        missing = _missing_packages()
        if missing:
            info_text = ("These Python packages are not installed:\n  • "
                         + "\n  • ".join(name for _m, name in missing)
                         + "\n\nThis installs everything in requirements.txt "
                           "with pip (needs internet).")
        else:
            info_text = ("All Python packages are installed.\n\n"
                         "You can still run the installer to repair or update "
                         "everything in requirements.txt.")
        info = tk.Label(dlg, bg=self.CBG, fg=self.THEME["fg"], justify="left",
                        text=info_text)
        info.pack(anchor="w", padx=12, pady=(10, 6))

        log = tk.Text(dlg, height=14, width=70, bg=self.THEME["well"],
                      fg="#c9d1d9", relief="flat", bd=0,
                      font=("Consolas", 9), state="disabled")
        log.pack(fill="both", expand=True, padx=12, pady=4)

        status_var = tk.StringVar(value="Ready.")
        status_lbl = tk.Label(dlg, textvariable=status_var, bg=self.CBG,
                              fg=self.THEME["fg_dim"], anchor="w",
                              justify="left")
        status_lbl.pack(fill="x", padx=12, pady=(0, 4))

        btns = tk.Frame(dlg, bg=self.CBG)
        btns.pack(fill="x", padx=12, pady=(0, 10))
        install_btn = tk.Button(btns, text="Install all dependencies",
                                width=24, bg="#e8e8e8", fg="#1a1a1a",
                                bd=1, cursor="hand2",
                                font=("Segoe UI", 9),
                                activebackground="#d7d7d7")
        install_btn.pack(side="left")
        restart_btn = tk.Button(btns, text="Restart VLCspeaker now",
                                width=24, bg="#e8e8e8",
                                fg="#1a1a1a", bd=1, cursor="hand2",
                                font=("Segoe UI", 9),
                                activebackground="#d7d7d7")

        def do_restart():
            _relaunch_app()
            self.on_close()

        def do_install():
            install_btn.config(state="disabled", bg=self.THEME["well"])
            restart_btn.pack_forget()
            log.config(state="normal")
            log.delete("1.0", "end")
            status_lbl.config(fg=self.THEME["fg_dim"])
            status_var.set("Installing, this can take a minute (needs "
                           "internet). The log below fills as pip works.")

            def work():
                ok = pip_install_requirements(
                    lambda line: dlg.after(0, lambda l=line: (
                        log.insert("end", l + "\n"), log.see("end"))))

                def done():
                    install_btn.config(state="normal",
                                       bg=self.THEME["accent"])
                    still = _missing_packages()
                    if ok and not still:
                        status_lbl.config(fg=self.THEME["accent"])
                        status_var.set("✔ All packages installed. Restart to "
                                       "apply any changes.")
                        restart_btn.config(command=do_restart)
                        restart_btn.pack(side="left", padx=8)
                    elif ok:
                        status_lbl.config(fg="#fbbf24")
                        status_var.set(
                            "Done, but these still failed to import: %s\n"
                            "A restart may help, otherwise install them "
                            "manually (see README)." %
                            ", ".join(name for _m, name in still))
                    else:
                        status_lbl.config(fg="#f87171")
                        status_var.set("✘ Installation failed, see the log "
                                       "above.")

                dlg.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        install_btn.config(command=do_install)
        tk.Button(btns, text="Close", bd=0, bg=self.THEME["well"],
                  fg=self.THEME["fg"], cursor="hand2",
                  command=dlg.destroy).pack(side="right")

        dlg.update_idletasks()
        w, h = 600, 440
        dlg.geometry("%dx%d+%d+%d" % (
            w, h,
            self.root.winfo_x() + max(0, (self.root.winfo_width() - w) // 2),
            self.root.winfo_y() + max(0, (self.root.winfo_height() - h) // 2)))
        dlg.grab_set()

    def show_help(self):
        messagebox.showinfo(APP_NAME + ", keyboard shortcuts", """\
Space        Play / Pause
← / →        Seek 10 seconds back / forward
↑ / ↓        Volume up / down
[ / ]        Speed down / up  (also  - / +, 0.25 steps, click the
             orange 1.00x to reset to 1x)
M            Mute / unmute
S            Read Aloud on / off
C            Show / hide subtitle text
F / F11      Fullscreen on / off  (Esc exits)
Ctrl+O       Open a video
Ctrl+U       Open a subtitle file (.srt .vtt .ass)

Drag & drop: drop a video (or subtitle) file from Windows
Explorer onto the VLCspeaker window to play / load it.

OCR button: reads captions BURNED INTO the picture (hardsubs).
While 'OCR: ON', the bottom of the video picture is read about once
a second with the Windows built-in OCR, the text is shown and
spoken by Read Aloud. Pick the caption language in the small box
next to it. OCR languages come from Windows language packs
(Settings → Time & Language → Language & region → add the language),
or install Tesseract for more languages.

'Subtitles inside this movie' lists subtitle tracks stored inside
MKV/MP4 files. Pick a track and it is extracted to text, shown on the
video and spoken by Read Aloud. If the movie has exactly one text
track it is loaded automatically. Picture subtitles (PGS/VobSub,
burned into frames) contain no text and cannot be spoken.

Read Aloud speaks every subtitle line with Windows voices.
Lines are queued: the line being spoken finishes fully before the
next one starts, so long dialogues are never cut mid-sentence. If
the voice falls behind the movie, the oldest unheard line is
skipped to catch up. Pick a voice in the bar below the video;
the voice speed follows the video speed automatically.

Piper neural voices (File → Get Piper voices): sound much more
natural than Windows voices and run fully offline. Download a
voice once (~63 MB, e.g. en_US-lessac-medium), then pick it in the
Voice dropdown as 'Piper: …'. Read Aloud uses it like any other
voice. The first line may pause briefly while the model loads.

Live translation (MT box, next to Voice): pick a language and every
subtitle line is translated into it by a local AI model (Tencent
Hy-MT2) before it is shown on the video and spoken. Fully offline,
nothing is sent anywhere. First use downloads the model once
(Tools → Live translation model): ~1.1 GB for the small 1.8B model
that runs on any PC, or ~4.5 GB for the better 7B model (needs an
NVIDIA GPU). While one line is spoken the next one is already being
translated, so the delay stays invisible. Set MT back to Off to stop
the model and free its memory.""")

    def show_about(self):
        messagebox.showinfo("About " + APP_NAME,
                            "%s %s\n\nVideo playback: VLC (libvlc)\n"
                            "Speech: Windows SAPI voices (offline)\n"
                            "        + Piper neural voices (offline, File → "
                            "Get Piper voices)\n"
                            "Live translation: local Hy-MT2 model via "
                            "llama.cpp (offline, Tools menu)\n\n"
                            "Load a subtitle file in a language you understand, "
                            "press Read Aloud, and listen while you watch."
                            % (APP_NAME, APP_VERSION))

    # ---------------- periodic update loop ----------------

    def on_close(self):
        self._unhook_ll_mouse()
        self._unhook_vlc_clicks()
        with contextlib.suppress(Exception):
            if self.player is not None:
                self.player.stop()
        self.tts.purge()
        self.mt.shutdown()
        self._cleanup_ocr_files()
        self.root.destroy()

    def _tick(self):
        try:
            self._tick_body()
        except Exception as exc:
            self.set_status("Error: %s" % exc)
        # The video picture is a native VLC window: Tk gets no <Motion>
        # events over it, so poll the pointer ourselves. Any movement
        # wakes the floating fullscreen bar (and un-hides the cursor).
        if getattr(self, "_fullscreen", False):
            with contextlib.suppress(Exception):
                ptr = self.root.winfo_pointerxy()
                if ptr != self._fs_last_ptr:
                    self._fs_last_ptr = ptr
                    self._fs_wake()
        self._drain_native_clicks()
        self.root.after(self.POLL_MS, self._tick)

    def _tick_body(self):
        # keep drag & drop ours: the VLC video window is (re)created shortly
        # after playback starts, so re-check every tick while a movie is loaded
        if self._video_path:
            self._block_vlc_drop_hijack()
            self._hook_vlc_clicks()

        # results from background ffmpeg jobs (track scan / extraction)
        while True:
            try:
                fn = self._sub_job_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                self.set_status("Error: %s" % exc)

        # one-time: fill the voice dropdown when the speech engine is ready
        if self.tts.ready and not self._tts_ui_ready:
            self._tts_ui_ready = True
            labels = [n + ("  (female)" if FEMALE_VOICE_RE.search(n) else "")
                      for n in self.tts.voices]
            self.voice_combo["values"] = labels
            self._voice_raw = [("sapi", n) for n in self.tts.voices]
            pick = 0
            for i, n in enumerate(self.tts.voices):      # prefer a female voice
                if FEMALE_VOICE_RE.search(n):
                    pick = i
                    break
            if self.tts.voices:
                self.voice_combo.current(pick)
                self.tts.set_voice(pick)
        if self.tts.error and not self._tts_err_shown:
            self._tts_err_shown = True
            self.read_btn.config(state="disabled", bg="#222", text="Read Aloud unavailable")
            self.set_status("Read Aloud disabled: " + self.tts.error)

        # Piper neural voices appear in the dropdown once scanned / downloaded
        if self.tts.piper_ready and self.tts.piper_rev != self._piper_ui_rev:
            self._piper_ui_rev = self.tts.piper_rev
            if self.tts.piper_voices:
                base = list(self.voice_combo["values"])
                self.voice_combo["values"] = base + \
                    ["Piper: %s" % _piper_label(v["name"])
                     for v in self.tts.piper_voices]
                raw = list(getattr(self, "_voice_raw", []))
                raw += [("piper", v["name"]) for v in self.tts.piper_voices]
                self._voice_raw = raw
            if self.tts.piper_error and not self._piper_err_shown:
                self._piper_err_shown = True
                self.set_status("Piper voices need: " + self.tts.piper_error)

        # live translation state changes -> notice on the video area
        if self.mt.rev != self._mt_ui_rev:
            self._mt_ui_rev = self.mt.rev
            if self._mt_target:
                if self.mt.state == "ready":
                    label = (MT_MODELS.get(self.mt.model_key) or {}).get(
                        "label", "Hy-MT2")
                    self._show_toast("Live translation ready: %s (%s)"
                                     % (self._mt_target, label))
                elif self.mt.state == "error":
                    self._show_toast("Live translation: %s"
                                     % self.mt.state_msg, ms=5000)

        if self.player is None:
            return
        state = self.player.get_state()
        playing = (state == vlc.State.Playing)

        if playing != self._was_playing:
            self._was_playing = playing
            if playing:
                self.play_btn.set_playing(True)
                self.spoken_idx = -1
                self._ended = False
                self.placeholder.place_forget()
                if not self._vlc_spu_allowed:
                    with contextlib.suppress(Exception):
                        # we show subtitles ourselves; stop VLC drawing its own
                        self.player.video_set_spu(-1)
            else:
                self.play_btn.set_playing(False)
                if state in (vlc.State.Paused, vlc.State.Stopped, vlc.State.Ended):
                    self.tts.purge()

        if state == vlc.State.Ended and not self._ended:
            self._ended = True
            self.sub_label.config(text="")
            self.current_cue_idx = None
            self.spoken_idx = -1

        t = max(0, self.player.get_time() or 0)
        dur = max(0, self.player.get_length() or 0)
        # cur/tot also feed the floating fullscreen bar (_fs_sync)
        cur_txt, tot_txt = fmt_time(t), fmt_time(dur)
        frac = (t / float(dur)) if dur > 0 else 0.0
        self.time_cur_lbl.config(text=cur_txt)
        self.time_total_lbl.config(text=tot_txt)
        if not self._scrubbing and dur > 0:
            self.seekbar.set_fraction(frac)
        # subtitle spans on the seek tracks (the mock's sub timeline)
        n = len(self.cues)
        if n != getattr(self, "_cue_n", -1):
            self._cue_n = n
            self._push_cue_spans()

        # volume readout + mute state (shown on the Mute button itself)
        try:
            vol = self.player.audio_get_volume()
        except Exception:
            vol = 100
        try:
            _muted_now = bool(self.player.audio_get_mute())
        except Exception:
            _muted_now = False
        with contextlib.suppress(Exception):
            if getattr(self, '_fullscreen', False):
                self._fs_sync(cur_txt, tot_txt,
                              frac if dur > 0 else 0.0,
                              vol, _muted_now)
        label = "%d%%" % max(0, vol)
        if label != self._vol_label_cache:
            self._vol_label_cache = label
            self.vol_lbl.config(text=label)
        with contextlib.suppress(Exception):
            cur = self.vol_scale.get_frac() * 125.0
            if abs(cur - max(0, vol)) > 1 and not self._scrubbing:
                self._sync_vol_slider(max(0, vol))
        muted = bool(self.player.audio_get_mute())
        if muted != self._mute_shown:
            self._mute_shown = muted
            self.mute_btn.set_icon(_ic_mute if muted else _ic_volume)
            self.mute_btn.set_on(muted)

        # subtitles: display + speak (live OCR cues are used when OCR is ON)
        if self.ocr_mode:
            cues, starts = self.ocr_cues, self.ocr_starts
        else:
            cues, starts = self.cues, self.cue_starts
        if cues and state in (vlc.State.Playing, vlc.State.Paused):
            idx = self._find_cue_in(starts, cues, t)
            if idx != self.current_cue_idx:
                self.current_cue_idx = idx
                if self.sub_show.get():
                    self.sub_label.config(
                        text=cues[idx].text if idx is not None else "")
                # translate the NEXT line while the current one is being
                # spoken, so its translation is ready the moment it comes up
                if (self._mt_target and idx is not None
                        and idx + 1 < len(cues)):
                    self.mt.prefetch(idx + 1, cues[idx + 1].text,
                                     self._mt_target)
            if (idx is not None and idx != self.spoken_idx
                    and self.read_aloud.get() and playing):
                line = cues[idx].text
                if self._resume_skip and line == self._last_spoken_text:
                    self._resume_skip = False
                    self._last_spoken_text = line
                    self.spoken_idx = idx    # already heard, don't repeat
                else:
                    self._resume_skip = False
                    self._last_spoken_text = line
                    self.spoken_idx = idx
                    if self._mt_target:
                        self.mt.submit(idx, line, self._mt_target)
                    else:
                        self.tts.say(line)
        self.sub_label.lift()

# --------------------------------------------------------------------------
# Dependency install (one click)
# --------------------------------------------------------------------------

REQUIREMENTS_PATH = os.path.join(APP_DIR, "requirements.txt")

# import name -> what it is for (shown in the install dialog)
DEPENDENCY_IMPORTS = (
    ("vlc", "python-vlc (video playback)"),
    ("comtypes", "comtypes (Windows voices)"),
    ("tkinterdnd2", "tkinterdnd2 (drag & drop)"),
    ("pytesseract", "pytesseract (OCR)"),
    ("PIL", "pillow (OCR)"),
    ("winrt", "winrt (Windows OCR)"),
    ("piper", "piper-tts (neural voices, optional)"),
    ("pyaudio", "PyAudio (neural voices, optional)"),
)

def _missing_packages():
    """[(import name, description)] of the app's Python packages that are
    not installed. Uses find_spec so nothing heavy is actually imported."""
    missing = []
    for mod, name in DEPENDENCY_IMPORTS:
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append((mod, name))
        except Exception:
            missing.append((mod, name))
    if getattr(sys, "frozen", False):
        # In the .exe everything required is built in, and the optional
        # extras (piper) can not be pip-installed into a frozen app.
        missing = [m for m in missing if m[0] not in ("piper", "pyaudio")]
    return missing

def pip_install_requirements(log=None):
    """pip-install everything in requirements.txt (next to the app).
    `log` gets every output line. Returns True on success."""
    if getattr(sys, "frozen", False):
        if log:
            log("VLCspeaker.exe already has its Python packages built in; "
                "there is nothing to pip-install into it.")
        return True
    if not os.path.isfile(REQUIREMENTS_PATH):
        if log:
            log("requirements.txt not found: %s" % REQUIREMENTS_PATH)
        return False
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
           "-r", REQUIREMENTS_PATH]
    if log:
        log("> " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=os.path.dirname(REQUIREMENTS_PATH),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        if log:
            log("Could not start pip: %s" % exc)
        return False
    for line in proc.stdout:
        line = line.rstrip()
        if line and log:
            log(line)
    rc = proc.wait()
    if rc == 0:
        return True
    if log:
        log("pip exit code %d, installation failed." % rc)
        log("Try manually in a terminal:")
        log("    %s -m pip install --user -r requirements.txt"
            % os.path.basename(sys.executable))
    return False

def _relaunch_app():
    """Start a fresh VLCspeaker process (used after installing packages,
    because newly installed packages only load on startup)."""
    args = [a for a in sys.argv[1:] if a not in ("--smoke", "--selftest")]
    if getattr(sys, "frozen", False):
        cmd = [sys.executable] + args          # the .exe relaunches itself
        cwd = os.path.dirname(os.path.abspath(sys.executable))
    else:
        script = os.path.abspath(__file__)
        cmd = [sys.executable, script] + args
        cwd = os.path.dirname(script)
    with contextlib.suppress(Exception):
        subprocess.Popen(cmd, cwd=cwd)

# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def _vlc_missing_dialog():
    root = tk.Tk()
    root.withdraw()
    # 'No module named …' means the python-vlc PACKAGE is missing, that we
    # can fix ourselves with pip. A dll error means the VLC program itself is
    # missing, only installing VLC from videolan.org helps.
    if "No module named" in (VLC_ERROR or ""):
        msg = ("VLCspeaker's Python packages are not installed (yet).\n\n"
               "%s\n\nInstall all packages now?\n(needs internet, "
               "about a minute)" % VLC_ERROR)
        if messagebox.askyesno(APP_NAME, msg):
            print("Installing required Python packages (needs internet)…")
            if pip_install_requirements(log=print):
                if messagebox.askyesno(
                        APP_NAME, "Packages installed!\nStart VLCspeaker now?"):
                    _relaunch_app()
            else:
                messagebox.showerror(
                    APP_NAME,
                    "Could not install the packages.\nTry manually in a "
                    "terminal:\n\n    %s -m pip install --user "
                    "-r requirements.txt" % os.path.basename(sys.executable))
        root.destroy()
        return
    msg = ("Could not load VLC (libvlc.dll).\n\n%s\n\n"
           "Install the VLC media player from videolan.org\n"
           "(use 64-bit VLC with 64-bit Python).\n"
           "Tip: the Microsoft Store version of VLC is not supported, "
           "install the normal one." % VLC_ERROR)
    if messagebox.askyesno(APP_NAME, msg + "\n\nOpen the VLC download page now?"):
        webbrowser.open("https://www.videolan.org/vlc/download-windows.html")
    root.destroy()

def selftest():
    ok = True
    print("== VLCspeaker self test ==")

    # --- subtitle parsers ---
    srt = ("1\n00:00:01,000 --> 00:00:03,500\nHello <i>world</i>\n\n"
           "2\n00:00:04,000 --> 00:00:06,000\nSecond line\nwith two rows\n")
    cues = _parse_srt_like(srt)
    p1 = (len(cues) == 2 and cues[0].start == 1000 and cues[0].end == 3500
          and cues[0].text == "Hello world"
          and cues[1].text == "Second line\nwith two rows")
    print(("PASS" if p1 else "FAIL"), "SRT parser"); ok &= p1

    vtt = "WEBVTT\n\n01:02.000 --> 01:04.500\nShort time test\n"
    cues = _parse_srt_like(vtt, vtt=True)
    p2 = (len(cues) == 1 and cues[0].start == 62000 and cues[0].end == 64500
          and cues[0].text == "Short time test")
    print(("PASS" if p2 else "FAIL"), "VTT parser"); ok &= p2

    ass = ("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
           "MarginV, Effect, Text\n"
           "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
           "{\\i1}Ass test{\\i0}\\Nsecond line\n")
    cues = _parse_ass(ass)
    p3 = (len(cues) == 1 and cues[0].start == 1000 and cues[0].end == 3000
          and cues[0].text == "Ass test\nsecond line")
    print(("PASS" if p3 else "FAIL"), "ASS parser"); ok &= p3

    # --- libvlc ---
    if vlc is not None:
        ver = vlc.libvlc_get_version().decode()
        print("PASS libvlc loaded (%s) from: %s" % (ver, VLC_DIR or "system PATH"))
    else:
        print("FAIL libvlc: %s" % VLC_ERROR)
        ok = False

    # --- drag & drop ---
    if TkinterDnD is None:
        print("WARN drag & drop: tkinterdnd2 not installed (optional):",
              DND_ERROR)
    else:
        print("PASS drag & drop: tkinterdnd2 available")

    # --- speech ---
    tts = TTSSpeaker()
    for _ in range(40):
        if tts.ready or tts.error:
            break
        time.sleep(0.1)
    if tts.ready:
        print("PASS Windows speech ready, %d voice(s) available:" % len(tts.voices))
        for name in tts.voices[:8]:
            print("      •", name)
        tts.set_rate(1)
        tts.say("V L C speaker is working.")
    else:
        print("FAIL speech:", tts.error)
        ok = False

    # --- piper neural voices (optional) ---
    try:
        import piper, pyaudio  # noqa: F401
        pv = find_piper_voices()
        if pv:
            from piper import PiperVoice
            t0 = time.time()
            voice = PiperVoice.load(pv[0]["model"])
            chunks = list(voice.synthesize("V L C speaker piper check."))
            n = sum(len(c.audio_int16_bytes) for c in chunks)
            print("PASS piper, %d voice(s) on disk; '%s' synthesized "
                  "%d KB of audio in %.1fs"
                  % (len(pv), pv[0]["name"], n // 1024, time.time() - t0))
        else:
            print("PASS piper installed, no voices downloaded yet "
                  "(File > Get Piper voices)")
    except ImportError as exc:
        print("WARN piper not available (optional): %s" % exc)
    except Exception as exc:
        print("FAIL piper:", exc)
        ok = False

    # --- live translation (offline, Hy-MT2 via llama.cpp) ---
    p_mt = (len(MT_LANGS) == 33 and len(set(MT_LANGS)) == 33
            and "English" in MT_LANGS and "Hindi" in MT_LANGS
            and set(MT_MODELS) == {"7b", "1.8b"}
            and all("repo" in m and "file" in m for m in MT_MODELS.values()))
    prompt = mt_build_prompt("Hello world", "Hindi")
    p_mt &= prompt.startswith("Translate the following text into Hindi.")
    p_mt &= prompt.rstrip().endswith("Hello world")
    print(("PASS" if p_mt else "FAIL"), "live translation languages + prompt")
    ok &= p_mt

    print("== self test %s ==" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1

def main(argv):
    if IS_WINDOWS:
        with contextlib.suppress(Exception):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)

    if "--selftest" in argv:
        return selftest()

    if vlc is None:
        if "--smoke" in argv:
            print("SMOKE FAIL: no libvlc:", VLC_ERROR)
            return 2
        _vlc_missing_dialog()
        return 2

    root = None
    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()          # a Tk root that accepts drops
        except Exception:
            root = None                     # broken tkdnd, fall back silently
    if root is None:
        root = tk.Tk()
    PlayerApp(root)
    if "--smoke" in argv:
        root.after(2500, root.destroy)
        root.mainloop()
        print("SMOKE OK")
        return 0
    root.mainloop()
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
