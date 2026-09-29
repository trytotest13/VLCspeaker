#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VLCspeaker, a simple Windows video player with:

  * play / pause / stop / seek
  * playback speed up & down (0.25x ... 4x)
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
import importlib.util
import io
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

IS_WINDOWS = (sys.platform == "win32")
APP_NAME = "VLCspeaker"
APP_VERSION = "1.0"

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
    try:
        os.add_dll_directory(VLC_DIR)
    except (AttributeError, OSError):
        pass

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
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            pass
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
        lines.append(ln)
    return " ".join(lines).strip()

def _ocr_key(s):
    """Punctuation/case-insensitive line identity, so the same caption
    read twice (with tiny OCR differences) counts as the same line."""
    return re.sub(r"\W+", "", (s or "").lower(), flags=re.UNICODE)

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

PIPER_VOICES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "piper", "voices")
PIPER_HF_TREE = ("https://huggingface.co/api/models/rhasspy/piper-voices/"
                 "tree/main?recursive=true&limit=1000")
PIPER_HF_RESOLVE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"


def find_piper_voices():
    """Scan piper/voices for .onnx models (with their .json sidecar)."""
    out = []
    try:
        for fn in sorted(os.listdir(PIPER_VOICES_DIR)):
            if not fn.endswith(".onnx"):
                continue
            model = os.path.join(PIPER_VOICES_DIR, fn)
            cfg = model + ".json"
            if os.path.isfile(cfg):
                out.append({"name": fn[:-5], "model": model, "config": cfg})
    except OSError:
        pass
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

def download_piper_voice(name, repo_path, dest_dir=PIPER_VOICES_DIR, progress=None):
    """Download one voice (the .onnx model + .json sidecar) from HuggingFace.
    progress(fraction_done, message) is called while data arrives."""
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
                self.piper_error = ("pip install piper-tts PyAudio (%s)" % exc)
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
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

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
        try:
            self._piper_scan()                 # find downloaded .onnx voices
        except Exception:
            pass
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
                try:
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
                except Exception:
                    pass
                if item is None:
                    continue
                _gen, text = item
                if self._engine == "piper" and self._piper_cur:
                    try:
                        self._speak_piper(text, _gen)   # blocks until done
                    except Exception:
                        pass
                    continue
                try:
                    v.Speak(text, 1)           # async, does NOT purge others
                except Exception:
                    continue
                while True:                    # wait until this line ends
                    with self._lock:
                        purged = (self._gen != _gen)
                    if purged:
                        try:
                            v.Speak("", 3)     # stop it right now
                        except Exception:
                            pass
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

def fmt_time(ms):
    ms = max(0, int(ms)) // 1000
    h, rest = divmod(ms, 3600)
    m, s = divmod(rest, 60)
    return "%d:%02d:%02d" % (h, m, s)

class Seekbar(tk.Canvas):
    """Slim custom seek bar: dark rounded trough, green fill, round thumb
    that grows on hover. Click or drag anywhere to scrub. Replaces the
    default ttk.Scale, which looked grey and fiddly."""

    HEIGHT = 20                 # generous click target
    TROUGH_H = 6
    THUMB_R = 7                 # resting thumb radius (grows on hover)
    TROUGH = "#2a3140"
    FILL = "#22c55e"

    def __init__(self, master, on_scrub=None, on_seek=None, **kw):
        super().__init__(master, height=self.HEIGHT, highlightthickness=0,
                         bd=0, bg=master["bg"], **kw)
        self._frac = 0.0
        self._hover = False
        self._dragging = False
        self._on_scrub = on_scrub      # fn(True while dragging / False)
        self._on_seek = on_seek        # fn(frac 0..1) on release
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Configure>", lambda e: self._draw())

    def _enter(self, _e):
        self._hover = True
        self._draw()

    def _leave(self, _e):
        self._hover = False
        self._draw()

    def set_fraction(self, frac):
        frac = min(1.0, max(0.0, frac))
        if self._dragging or abs(frac - self._frac) < 0.0005:
            return
        self._frac = frac
        self._draw()

    def fraction(self):
        return self._frac

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        if x2 - x1 < 2 * r:
            self.create_oval(x1, y1, max(x2, x1 + 1), y2, **kw)
            return
        self.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
        self.create_oval(x1, y1, x1 + 2 * r, y2, **kw)
        self.create_oval(x2 - 2 * r, y1, x2, y2, **kw)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w < 20:
            return
        cy = self.HEIGHT // 2
        half = self.TROUGH_H // 2
        pad = self.THUMB_R + 2
        self._round_rect(pad, cy - half, w - pad, cy + half, half,
                         fill=self.TROUGH, outline="")
        r = self.THUMB_R + 1 if (self._hover or self._dragging) else self.THUMB_R
        x = pad + (w - 2 * pad) * self._frac
        if self._frac > 0:
            self._round_rect(pad, cy - half, x, cy + half, half,
                             fill=self.FILL, outline="")
        self.create_oval(x - r, cy - r, x + r, cy + r,
                         fill=self.FILL, outline="#0f1115")

    def _frac_from_event(self, e):
        pad = self.THUMB_R + 2
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

class PlayerApp:
    POLL_MS = 100

    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.minsize(680, 460)
        root.configure(bg="#0f1115")
        self._center_window(1080, 720)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground="#232936",
                        background="#232936", foreground="#e8eaed",
                        arrowcolor="#e8eaed", bordercolor="#2a3040",
                        lightcolor="#2a3040", darkcolor="#2a3040")
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#232936")],
                  foreground=[("readonly", "#e8eaed")])
        style.configure("Horizontal.TScale", troughcolor="#232936",
                        background="#22c55e", bordercolor="#232936")
        style.map("Horizontal.TScale", background=[("active", "#16a34a")])

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

        self.tts = TTSSpeaker()

        self._video_path = None
        self._sub_job_q = queue.Queue()   # results from background ffmpeg jobs
        self._embed_job_id = 0
        self.embed_tracks = []
        self._vlc_spu_allowed = False

        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        try:
            self.player.audio_set_volume(100)
        except Exception:
            pass

        self._build_menu()
        self._build_video_area()
        self._build_controls()
        self._build_status()
        self._bind_keys()
        self._enable_drag_drop()

        # Draw the video inside our tkinter frame.
        try:
            self.player.set_hwnd(self.video_area.winfo_id())
        except Exception:
            pass

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._wipe_temp_cache()
        self._init_ocr()
        root.after(self.POLL_MS, self._tick)
        root.focus_set()

    # ---------------- UI construction ----------------

    def _build_menu(self):
        bar = tk.Menu(self.root)
        m_file = tk.Menu(bar, tearoff=0)
        m_file.add_command(label="Open Video…", command=self.open_video,
                           accelerator="Ctrl+O")
        m_file.add_command(label="Open Subtitles…", command=self.open_subtitles,
                           accelerator="Ctrl+U")
        m_file.add_command(label="Close Subtitles", command=self.close_subtitles)
        m_file.add_separator()
        m_file.add_command(label="Get Piper voices (neural TTS)…",
                           command=self.show_piper_dialog)
        m_file.add_command(label="⬇ Install all dependencies…",
                           command=self.show_install_dialog)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self.on_close)
        bar.add_cascade(label="File", menu=m_file)

        m_play = tk.Menu(bar, tearoff=0)
        m_play.add_command(label="Play / Pause", command=self.toggle_play)
        m_play.add_command(label="Stop", command=self.stop)
        m_play.add_separator()
        m_speed = tk.Menu(m_play, tearoff=0)
        for r in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
            m_speed.add_command(label="Speed %gx" % r,
                                command=lambda r=r: self.set_rate(r))
        m_play.add_cascade(label="Playback speed", menu=m_speed)
        m_play.add_command(label="Mute / Unmute", command=self.toggle_mute)
        m_play.add_command(label="Fullscreen / exit", command=self.toggle_fullscreen)
        bar.add_cascade(label="Playback", menu=m_play)

        m_help = tk.Menu(bar, tearoff=0)
        m_help.add_command(label="Keyboard shortcuts", command=self.show_help)
        m_help.add_command(label="About", command=self.show_about)
        bar.add_cascade(label="Help", menu=m_help)
        self.menubar = bar
        self.root.config(menu=bar)

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
            body, text="📂  Open Video  (Ctrl+O)", command=self.open_video,
            bg=self.THEME["accent"], fg="#04140a", bd=0, relief="flat",
            cursor="hand2", takefocus=0, padx=26, pady=12,
            font=("Segoe UI", 13, "bold"),
            activebackground="#16a34a", activeforeground="#04140a")
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

    def _on_video_resize(self, event):
        self.sub_label.config(wraplength=max(300, event.width - 80))

    THEME = {"bg": "#0f1115", "panel": "#161a22", "btn": "#232936",
             "btn_hover": "#333c4f", "fg": "#e8eaed", "fg_dim": "#9aa0a6",
             "accent": "#22c55e", "sep": "#2a3040", "status_bg": "#0a0c10",
             "well": "#141920"}
    CBG = THEME["panel"]     # control bar background

    BTN = {"bg": THEME["btn"], "fg": THEME["fg"],
           "activebackground": THEME["btn_hover"],
           "activeforeground": THEME["fg"], "relief": "flat", "bd": 0,
           "takefocus": 0, "padx": 12, "pady": 7, "font": ("Segoe UI", 10)}

    def _mkbtn(self, parent, text, cmd, off_bg=None, side="left", pad=None,
               **over):
        def wrapped():
            cmd()
            self.root.focus_set()
        b = tk.Button(parent, text=text, command=wrapped, cursor="hand2",
                      **{**self.BTN, **over})
        holder = {"off": off_bg or self.THEME["btn"]}
        b._bg_holder = holder
        b.bind("<Enter>", lambda e: b.config(bg=self.THEME["btn_hover"]))
        b.bind("<Leave>", lambda e: b.config(bg=holder["off"]))
        if side:
            b.pack(side=side, **({"padx": pad} if pad else {}))
        return b

    def _set_toggle(self, btn, on, on_text, off_text):
        """One look for every ON/OFF button: bright green + dark text when
        ON, soft grey when OFF, state readable at a glance."""
        off_bg = "#333c4f"
        btn.config(text=on_text if on else off_text,
                   bg="#16a34a" if on else off_bg,
                   fg="#04140a" if on else self.THEME["fg"])
        btn._bg_holder["off"] = "#16a34a" if on else off_bg

    def _mksep(self, parent):
        return tk.Frame(parent, bg=self.THEME["sep"], width=1)

    def _mklabel(self, parent, text, side="left", padx=0):
        lbl = tk.Label(parent, text=text, bg=self.CBG, fg=self.THEME["fg_dim"],
                       font=("Segoe UI", 9))
        lbl.pack(side=side, padx=padx)
        return lbl

    def _build_controls(self):
        controls = tk.Frame(self.root, bg=self.CBG)
        controls.pack(fill="x", side="bottom")
        self.controls = controls

        # Row 1: seek bar + time, the bar spans the window, time sits right
        row1 = tk.Frame(controls, bg=self.CBG)
        row1.pack(fill="x", padx=12, pady=(10, 2))
        self.seekbar = Seekbar(row1,
                               on_scrub=lambda d: setattr(self, "_scrubbing", d),
                               on_seek=self._on_seek_to)
        self.seekbar.pack(fill="x", expand=True, side="left")
        self.time_lbl = tk.Label(row1, text="0:00:00 / 0:00:00", bg=self.CBG,
                                 fg="#e8eaed", font=("Consolas", 11, "bold"),
                                 width=17)
        self.time_lbl.pack(side="left", padx=(12, 0))

        # Row 2: transport | speed | volume, three groups with dividers,
        # fullscreen out on the right
        row2 = tk.Frame(controls, bg=self.CBG)
        row2.pack(fill="x", padx=12, pady=(6, 4))
        slim = {"padx": 9}                    # keep the row inside 680px

        self.play_btn = self._mkbtn(row2, "▶ Play", self.toggle_play,
                                    padx=16, font=("Segoe UI", 11, "bold"))
        self._mkbtn(row2, "■  Stop", self.stop, pad=(6, 0), **slim)
        self._mkbtn(row2, "« 10s", lambda: self.seek_ms(-10000), pad=(6, 0), **slim)
        self._mkbtn(row2, "10s »", lambda: self.seek_ms(10000), pad=(3, 0), **slim)

        self._mksep(row2).pack(side="left", fill="y", padx=6, pady=4)

        self._mkbtn(row2, "Speed −", lambda: self.set_rate(delta=-0.25), **slim)
        self.speed_lbl = tk.Button(row2, text="1.00x", command=self._cmd(self.reset_rate),
                                   bg="#141920", fg="#4ade80", relief="flat",
                                   bd=0, takefocus=0, width=7, cursor="hand2",
                                   font=("Consolas", 11, "bold"))
        self.speed_lbl.pack(side="left")
        self._mkbtn(row2, "Speed +", lambda: self.set_rate(delta=+0.25),
                    pad=(3, 0), **slim)

        self._mksep(row2).pack(side="left", fill="y", padx=6, pady=4)

        self._mkbtn(row2, "Vol −", lambda: self.change_volume(-5), **slim)
        self.vol_lbl = tk.Label(row2, text="100%", bg=self.CBG, fg="#e8eaed",
                                font=("Consolas", 10, "bold"), width=4)
        self.vol_lbl.pack(side="left")
        self._mkbtn(row2, "Vol +", lambda: self.change_volume(+5),
                    pad=(3, 0), **slim)
        self.mute_btn = self._mkbtn(row2, "🔊 Mute", self.toggle_mute,
                                    pad=(6, 0), **slim)
        self._mkbtn(row2, "⛶ Fullscreen", self.toggle_fullscreen,
                    side="right", pad=(0, 0), **slim)

        # Row 3: subtitles & speech, the app's core, kept on one line
        row3 = tk.Frame(controls, bg=self.CBG)
        row3.pack(fill="x", padx=12, pady=(4, 4))

        self.sub_btn = self._mkbtn(row3, "Subtitles: ON", self.toggle_sub_show,
                                   off_bg="#333c4f", padx=10)
        self._set_toggle(self.sub_btn, True, "Subtitles: ON", "Subtitles: OFF")
        self._mkbtn(row3, "📄 Add Subtitles", self.open_subtitles,
                    pad=(6, 0), padx=10)

        self._mksep(row3).pack(side="left", fill="y", padx=8, pady=4)

        self._mklabel(row3, "👁 OCR")
        self.ocr_lang = ttk.Combobox(row3, state="readonly", width=7, values=["eng"])
        self.ocr_lang.current(0)
        self.ocr_lang.pack(side="left", padx=(5, 5))
        self.ocr_lang.bind("<<ComboboxSelected>>", self._on_ocr_lang)
        self.ocr_btn = self._mkbtn(row3, "OCR: OFF", self.toggle_ocr,
                                   off_bg="#333c4f", pad=(0, 0), padx=10)
        self._set_toggle(self.ocr_btn, False, "OCR: ON", "OCR: OFF")

        self.read_btn = self._mkbtn(row3, "🔊 Read Aloud: OFF",
                                    self.toggle_read_aloud, off_bg="#333c4f",
                                    side="right")
        self.read_btn.configure(font=("Segoe UI", 11, "bold"), padx=14,
                                fg="#e8eaed")
        self._set_toggle(self.read_btn, False, "🔊 Read Aloud: ON",
                         "🔊 Read Aloud: OFF")

        # Row 4: subtitle tracks inside the movie
        row4 = tk.Frame(controls, bg=self.CBG)
        row4.pack(fill="x", padx=12, pady=(4, 4))

        embed = tk.Frame(row4, bg=self.CBG)     # label stays LEFT of its box
        embed.pack(side="right")
        self._mklabel(embed, "In-movie subs:")
        self.embed_combo = ttk.Combobox(embed, state="readonly", width=28)
        self.embed_combo.pack(side="left", padx=(6, 0))
        self.embed_combo.bind("<<ComboboxSelected>>", self._on_embed_selected)
        self._embed_set_choices([])
        self.embed_combo.set("(open a movie to scan for subtitle tracks)")

        # Row 5: read-aloud voice settings (voice speed follows the video
        # speed automatically, see _sync_tts_rate)
        row5 = tk.Frame(controls, bg=self.CBG)
        row5.pack(fill="x", padx=12, pady=(4, 10))
        self._mklabel(row5, "Voice:")
        self.voice_combo = ttk.Combobox(row5, state="readonly", width=34)
        self.voice_combo.pack(side="left", padx=(6, 0))
        self.voice_combo.bind("<<ComboboxSelected>>", self._on_voice_selected)

    def _build_status(self):
        self.status_var = tk.StringVar(
            value="Open a video to start. Subtitle files: .srt .vtt .ass")
        bar = tk.Frame(self.root, bg=self.THEME["status_bg"])
        bar.pack(fill="x", side="bottom")
        tk.Label(bar, textvariable=self.status_var, anchor="w",
                 bg=self.THEME["status_bg"], fg=self.THEME["fg_dim"],
                 font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True)
        # one-click dependency installer, always visible in the status bar
        self.install_btn = tk.Button(
            bar, text="⬇ Install dependencies", command=self.show_install_dialog,
            bd=0, relief="flat", takefocus=0, cursor="hand2", padx=10, pady=2,
            bg=self.THEME["status_bg"], fg=self.THEME["accent"],
            activebackground=self.THEME["status_bg"],
            activeforeground="#16a34a", font=("Segoe UI", 9, "bold"))
        self.install_btn.pack(side="right")
        missing = _missing_packages()
        if missing:
            self.status_var.set(
                "Missing Python packages: %s, click ⬇ Install dependencies "
                "to install them." % ", ".join(name for _m, name in missing))

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
        media = self.instance.media_new(path)
        self.player.set_media(media)
        self.player.play()
        self.root.title("%s: %s" % (APP_NAME, os.path.basename(path)))
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
        self._vlc_spu_allowed = False
        try:
            self.player.video_set_spu(-1)   # we render subtitles ourselves
        except Exception:
            pass

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
        try:
            n = self.player.video_get_spu_count() or 0
            if 0 <= ordinal < n:
                self.player.video_set_spu(ordinal)
                self._vlc_spu_allowed = True
                shown = True
        except Exception:
            pass
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
        try:
            self.embed_combo.current(0)
        except Exception:
            pass
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
                try:
                    # raises when the language pack is not installed
                    if ocr.OcrEngine.try_create_from_language(glob.Language(tag)):
                        found.append(tag)
                except Exception:
                    pass
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
                try:
                    self._get_ocr_engine(langs[0])   # warm up for instant OCR
                except Exception:
                    pass
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
        try:
            self._ocr_lang_tag = self.ocr_lang.get()
            if self._ocr_backend == "winrt":
                threading.Thread(
                    target=lambda: self._get_ocr_engine(self._ocr_lang_tag),
                    daemon=True).start()
        except Exception:
            pass

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
        self._set_toggle(self.ocr_btn, True, "👁 OCR: ON", "👁 OCR: OFF")
        self.ocr_cues = []
        self.ocr_starts = []
        self._ocr_last_text = None
        self._cleanup_ocr_files()           # drop any stale snapshot
        self.current_cue_idx = None
        self.spoken_idx = -1
        self.sub_label.config(text="")
        self.tts.purge()
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
        self._set_toggle(self.ocr_btn, False, "👁 OCR: ON", "👁 OCR: OFF")
        self.current_cue_idx = None
        self.spoken_idx = -1
        self.tts.purge()
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
        try:
            p = self._ocr_snap_path()
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass

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
            try:
                before = os.path.getmtime(snap)
            except OSError:
                pass
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-hwaccel", "auto",
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
            try:
                self._get_ocr_engine(self._ocr_lang_tag or "auto")  # hot by now
            except Exception:
                pass

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
                and _ocr_key(text) and _ocr_key(text) == _ocr_key(last.text)
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
        else:
            self.player.play()
            self.spoken_idx = -1      # speak the cue at the resume point

    def stop(self):
        if self.player is None:
            return
        self.player.stop()
        self.tts.purge()
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
            self._close_open_ocr_cue()
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
        new = max(0.25, min(4.0, round(new * 4) / 4.0))
        self.player.set_rate(new)
        self._sync_tts_rate(new)          # the voice follows the video speed
        self.speed_lbl.config(text="%.2fx" % new)
        self.set_status("Speed: %.2fx (voice speed follows)." % new)

    def reset_rate(self):
        self.set_rate(1.0)

    # volume --------------------------------------------------------------

    def change_volume(self, delta):
        if self.player is None:
            return
        try:
            cur = self.player.audio_get_volume()
            if cur < 0:
                cur = 100
            self.player.audio_set_volume(max(0, min(125, int(cur) + delta)))
            if self.player.audio_get_mute():
                self.player.audio_set_mute(False)
        except Exception:
            pass

    def toggle_mute(self):
        if self.player is None:
            return
        try:
            self.player.audio_toggle_mute()
        except Exception:
            pass

    # subtitles / read aloud ----------------------------------------------

    def toggle_sub_show(self):
        self.sub_show.set(not self.sub_show.get())
        if self.sub_btn is not None:
            self._set_toggle(self.sub_btn, self.sub_show.get(),
                             "Subtitles: ON", "Subtitles: OFF")
        if not self.sub_show.get():
            self.sub_label.config(text="")

    def toggle_read_aloud(self):
        self.read_aloud.set(not self.read_aloud.get())
        on = self.read_aloud.get()
        self._set_toggle(self.read_btn, on,
                         "🔊 Read Aloud: ON", "🔊 Read Aloud: OFF")
        if on:
            self.spoken_idx = -1
            self._resume_skip = False    # turning it ON should speak now
            self.set_status("Read Aloud is ON: every subtitle line is spoken. "
                            "Tip: lower the video volume a bit.")
        else:
            self.tts.purge()
            self.set_status("Read Aloud is OFF.")

    def _on_voice_selected(self, _event=None):
        idx = self.voice_combo.current()
        if idx < 0:
            label = self.voice_combo.get()
        else:
            label = self.voice_combo.get() or \
                (self.tts.voices[idx] if idx < len(self.tts.voices) else "?")
        if label.startswith("Piper: "):
            name = label[len("Piper: "):]
            self.tts.set_piper_voice(name)
            self.set_status("Read Aloud voice: Piper %s (neural), first "
                            "line may pause while it loads" % name)
        else:
            self.tts.set_voice(idx)
            self.set_status("Read Aloud voice: %s" % label)

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

    def _find_cue(self, t):
        return self._find_cue_in(self.cue_starts, self.cues, t)

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
            ("<Key-m>", self.toggle_mute),
            ("<Key-s>", self.toggle_read_aloud),
            ("<Key-c>", self.toggle_sub_show),
            ("<Key-f>", self.toggle_fullscreen),
            ("<F11>", self.toggle_fullscreen),
            ("<Escape>", self._exit_fullscreen),
            ("<Control-o>", lambda: self.open_video()),
            ("<Control-u>", lambda: self.open_subtitles()),
        ]
        for seq, fn in pairs:
            self.root.bind(seq, lambda e, f=fn: (self.root.focus_set(), f(), "break")[2])

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
        try:
            import ctypes
            from ctypes import wintypes

            shell32 = ctypes.windll.shell32
            user32 = ctypes.windll.user32

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _each_child(hwnd, _lparam):
                try:
                    shell32.DragAcceptFiles(hwnd, False)
                except Exception:
                    pass
                return True

            user32.EnumChildWindows(self.video_area.winfo_id(), _each_child, 0)
        except Exception:
            pass

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)
        # hide the menubar too, or its white strip stays visible on Windows
        self.root.config(menu="" if self._fullscreen else self.menubar)
        if self._fullscreen:
            self.controls.pack_forget()
            self.set_status("Fullscreen, press Esc to exit.")
        else:
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
                 bg="#232936", fg="#e8eaed", insertbackground="#e8eaed",
                 relief="flat").pack(side="left", padx=6)

        cols = ("voice", "size", "disk")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=13)
        for cid, text_, w, anchor in (("voice", "Voice", 240, "w"),
                                      ("size", "Size", 80, "e"),
                                      ("disk", "On disk", 64, "center")):
            tree.heading(cid, text=text_)
            tree.column(cid, width=w, anchor=anchor)
        tree.pack(fill="both", expand=True, padx=12, pady=6)
        style = ttk.Style(dlg)
        style.configure("Piper.Treeview", background="#232936",
                        fieldbackground="#232936", foreground="#e8eaed",
                        rowheight=22)
        tree.configure(style="Piper.Treeview")

        status_var = tk.StringVar(
            value="Loading voice list from huggingface.co…")
        status_lbl = tk.Label(dlg, textvariable=status_var, bg=self.CBG,
                              fg=self.THEME["fg_dim"], anchor="w")
        status_lbl.pack(fill="x", padx=12, pady=(0, 4))

        btns = tk.Frame(dlg, bg=self.CBG)
        btns.pack(fill="x", padx=12, pady=(0, 10))
        dl_btn = tk.Button(btns, text="⬇ Download selected", width=20,
                           bg=self.THEME["accent"], fg="#04140a", bd=0,
                           font=("Segoe UI", 9, "bold"),
                           activebackground="#16a34a")
        dl_btn.pack(side="left")
        tk.Button(btns, text="Open voices folder", bd=0,
                  bg=self.THEME["well"], fg=self.THEME["fg"],
                  command=lambda: (os.makedirs(PIPER_VOICES_DIR, exist_ok=True),
                                   os.startfile(PIPER_VOICES_DIR))
                  ).pack(side="left", padx=8)
        tk.Button(btns, text="Close", bd=0, bg=self.THEME["well"],
                  fg=self.THEME["fg"],
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
                    try:
                        status_lbl.config(fg="#f87171")
                    except Exception:
                        pass
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

    def show_install_dialog(self):
        """Install every Python package from requirements.txt (File menu /
        status-bar button). Runs pip in the background and shows its output."""
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
        install_btn = tk.Button(btns, text="⬇ Install all dependencies",
                                width=24, bg=self.THEME["accent"], fg="#04140a",
                                bd=0, cursor="hand2",
                                font=("Segoe UI", 9, "bold"),
                                activebackground="#16a34a")
        install_btn.pack(side="left")
        restart_btn = tk.Button(btns, text="↻ Restart VLCspeaker now",
                                width=24, bg=self.THEME["btn"],
                                fg=self.THEME["fg"], bd=0, cursor="hand2",
                                font=("Segoe UI", 9, "bold"),
                                activebackground=self.THEME["btn_hover"])

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
[ / ]        Speed down / up  (0.25 steps, click the green x to reset to 1x)
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
voice. The first line may pause briefly while the model loads.""")

    def show_about(self):
        messagebox.showinfo("About " + APP_NAME,
                            "%s %s\n\nVideo playback: VLC (libvlc)\n"
                            "Speech: Windows SAPI voices (offline)\n"
                            "        + Piper neural voices (offline, File → "
                            "Get Piper voices)\n\n"
                            "Load a subtitle file in a language you understand, "
                            "press Read Aloud, and listen while you watch."
                            % (APP_NAME, APP_VERSION))

    # ---------------- periodic update loop ----------------

    def on_close(self):
        try:
            if self.player is not None:
                self.player.stop()
        except Exception:
            pass
        self.tts.purge()
        self._cleanup_ocr_files()
        self.root.destroy()

    def _tick(self):
        try:
            self._tick_body()
        except Exception as exc:
            self.set_status("Error: %s" % exc)
        self.root.after(self.POLL_MS, self._tick)

    def _tick_body(self):
        # keep drag & drop ours: the VLC video window is (re)created shortly
        # after playback starts, so re-check every tick while a movie is loaded
        if self._video_path:
            self._block_vlc_drop_hijack()

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
                    ["Piper: %s" % v["name"] for v in self.tts.piper_voices]
            if self.tts.piper_error and not self._piper_err_shown:
                self._piper_err_shown = True
                self.set_status("Piper voices need: " + self.tts.piper_error)

        if self.player is None:
            return
        state = self.player.get_state()
        playing = (state == vlc.State.Playing)

        if playing != self._was_playing:
            self._was_playing = playing
            if playing:
                self.play_btn.config(text="❚❚ Pause")
                self.spoken_idx = -1
                self._ended = False
                self.placeholder.place_forget()
                if not self._vlc_spu_allowed:
                    try:
                        # we show subtitles ourselves; stop VLC drawing its own
                        self.player.video_set_spu(-1)
                    except Exception:
                        pass
            else:
                self.play_btn.config(text="▶ Play")
                if state in (vlc.State.Paused, vlc.State.Stopped, vlc.State.Ended):
                    self.tts.purge()

        if state == vlc.State.Ended and not self._ended:
            self._ended = True
            self.sub_label.config(text="")
            self.current_cue_idx = None
            self.spoken_idx = -1

        t = max(0, self.player.get_time() or 0)
        dur = max(0, self.player.get_length() or 0)
        self.time_lbl.config(text="%s / %s" % (fmt_time(t), fmt_time(dur)))
        if not self._scrubbing and dur > 0:
            self.seekbar.set_fraction(t / dur)

        # volume readout + mute state (shown on the Mute button itself)
        try:
            vol = self.player.audio_get_volume()
            label = "%d%%" % max(0, vol)
            if label != self._vol_label_cache:
                self._vol_label_cache = label
                self.vol_lbl.config(text=label)
            muted = bool(self.player.audio_get_mute())
            if muted != self._mute_shown:
                self._mute_shown = muted
                if muted:
                    self.mute_btn.config(text="🔇 Muted", bg="#92400e",
                                         fg="#fde68a")
                    self.mute_btn._bg_holder["off"] = "#92400e"
                else:
                    self.mute_btn.config(text="🔊 Mute",
                                         bg=self.THEME["btn"],
                                         fg=self.THEME["fg"])
                    self.mute_btn._bg_holder["off"] = self.THEME["btn"]
        except Exception:
            pass

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
                        self.tts.say(line)
                        self.spoken_idx = idx
        self.sub_label.lift()

# --------------------------------------------------------------------------
# Dependency install (one click)
# --------------------------------------------------------------------------

REQUIREMENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "requirements.txt")

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
    return missing

def pip_install_requirements(log=None):
    """pip-install everything in requirements.txt (next to the app).
    `log` gets every output line. Returns True on success."""
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
    script = os.path.abspath(__file__)
    args = [a for a in sys.argv[1:] if a not in ("--smoke", "--selftest")]
    try:
        subprocess.Popen([sys.executable, script] + args,
                         cwd=os.path.dirname(script))
    except Exception:
        pass

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
        import time; time.sleep(0.1)
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
            import time as _t
            t0 = _t.time()
            voice = PiperVoice.load(pv[0]["model"])
            chunks = list(voice.synthesize("V L C speaker piper check."))
            n = sum(len(c.audio_int16_bytes) for c in chunks)
            print("PASS piper, %d voice(s) on disk; '%s' synthesized "
                  "%d KB of audio in %.1fs"
                  % (len(pv), pv[0]["name"], n // 1024, _t.time() - t0))
        else:
            print("PASS piper installed, no voices downloaded yet "
                  "(File → Get Piper voices)")
    except ImportError as exc:
        print("WARN piper not available (optional): %s" % exc)
    except Exception as exc:
        print("FAIL piper:", exc)
        ok = False

    print("== self test %s ==" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1

def main(argv):
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

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
