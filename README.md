# VLCspeaker

A simple Windows video player built for watching **foreign-language movies by listening**.

It plays your video like a normal player, and it has one extra big button:
**Read Aloud**: it speaks every subtitle/caption line out loud with Windows
text-to-speech voices, while the subtitle text is also shown on the video.

Video playback is powered by VLC, so it plays almost every format
(mp4, mkv, avi, webm, mov ...).

---

## Quick start

1. Double-click **run.bat**.
   (First run installs two small Python packages automatically.
   Needs: Python 3 (64-bit) and VLC (both are already on this PC).)
2. Click the big green **📂 Open Video** button on the start screen, or
   **File → Open Video** (Ctrl+O), and pick a movie, or simply
   **drag & drop** the movie file from Explorer onto the
   VLCspeaker window.
3. If a subtitle file with the same name is in the same folder
   (movie.srt next to movie.mkv), it loads automatically.
   Otherwise click **📄 Add Subtitles** (or **Ctrl+U**) and pick a
   `.srt`, `.vtt`, `.ass` or `.ssa` file.
4. Click **🔊 Read Aloud: OFF**, it turns green **ON** and speaks every line.

## Install all dependencies (one click)

Missing a Python package (moved the app to another PC, broken install)?
Two ways to install **everything** in `requirements.txt` with one click:

- the **⬇ Install dependencies** button in the bottom-right status bar, or
- **File → ⬇ Install all dependencies…**

It runs pip in the background and shows the live output. When packages
were missing at startup, the status bar lists them. Restart VLCspeaker
afterwards so newly installed packages load (the dialog offers a
**↻ Restart** button for this). Needs internet.

---

## Subtitles already inside the movie

Many MKV/MP4 movies have subtitle **tracks inside the file**. VLCspeaker
finds them automatically:

1. Open the movie.
2. Look at the **"Subtitles inside this movie"** dropdown (bottom bar).
   - If the movie has exactly one text track, it loads by itself;
     just press **Read Aloud**.
   - Otherwise pick the track you want (it shows the language and type).
3. The track is extracted to text, shown on the video, and spoken by
   Read Aloud like any subtitle file.

Notes:

- Needs **ffmpeg** on the PC (already installed here).
- **Picture subtitles** (PGS / VobSub / DVB, pictures burned as images,
  common on Blu-ray/DVD rips) have **no text inside**, so Read Aloud
  cannot speak them. VLCspeaker will still show them on the video via
  VLC, and tells you when this happens. For those movies, download a
  text `.srt` file (e.g. opensubtitles.org) and use Add Subtitles.
- File → **Close Subtitles** removes the loaded subtitles.

## Read burned-in captions (OCR / hardsubs)

For movies where the subtitles are part of the picture (hardsubs),
there is an **👁 OCR** button:

1. Open the movie and press **OCR: ON** (pick the caption language in
   the small box next to it first).
2. VLCspeaker reads the bottom of the video picture about once a
   second, shows the caption text on the player, and, with
   **Read Aloud** ON, speaks it.
3. The captured lines are kept as a subtitle track and, with
   **Read Aloud** ON, are spoken like any subtitle file.

OCR languages available on your PC: shown in the dropdown (this PC
has English). To read other-language captions, add that language in
**Windows Settings → Time & Language → Language & region** (tick the
language pack features), or install Tesseract:
`winget install UB-Mannheim.TesseractOCR`. OCR reads only captions in
the bottom area of the picture and works best with normal white or
yellow hardsubs; it cannot be 100% perfect; small mistakes are
possible.

**Smooth viewing + disk safety:** while OCR runs, a short-lived invisible
ffmpeg grabs **one frame at the current position** about once per second,
so the video you are watching is never interrupted (no flashes, no stutter),
and captions are picked up within a second, even right after a seek.
Disk use is minimal: ONE small temp file (`%TEMP%\VLCspeaker\ocr_snap.png`,
~100 to 200 KB) that is overwritten every capture, it never grows, never
multiplies. The temp folder (snapshots + extracted subtitle tracks)
is wiped automatically every time the player starts and on exit, so
nothing is ever left on your disk.

## Fullscreen

- Click the **⛶ Fullscreen** button, or press **F** / **F11**.
- Exit with **Esc** (or press **F** / **F11** again).

## How to watch another-language movie

1. Download subtitles in a language **you understand**
   (for example from opensubtitles.org) and put the `.srt` file in the
   same folder as the movie, with the same name.
2. Open the movie in VLCspeaker.
3. Press the **Read Aloud** button.
4. In the bottom bar choose a **Voice** you like. The voice speed
   follows the video speed automatically: slow the video down
   (**Speed −**) and the voice speaks slower too.
5. Tip: turn the movie volume down a little (**Vol −** or ↓ key) so you
   can hear the voice clearly over the film sound.

**Dialogues are never cut:** the line being spoken always finishes
fully before the next one starts. If a very long dialogue makes the
voice fall behind the movie, the oldest unheard line is skipped so
the voice can catch up. Slow the video down a little (**Speed −**)
if the subtitles come too quickly.

### More voices (more languages)

VLCspeaker uses the voices installed in Windows. To add a language:

- Windows **Settings → Time & Language → Speech → Add voices**,
  download the language you want.
- New voices appear in the **Voice** dropdown (restart VLCspeaker).

---

## Piper neural voices (natural narration, recommended)

Windows voices sound robotic. **Piper** voices are small neural
networks that sound much more natural, run fully offline, and exist
for ~30 languages. VLCspeaker ships with Piper support:

1. **File → Get Piper voices (neural TTS)…**: a list of ~400 voices
   opens (downloaded from huggingface.co the first time).
2. Pick one, for example **en_US-lessac-medium** (English, 63 MB),
   and click **⬇ Download selected**. Use the filter box to find a
   language (e.g. type `hi_IN` for Hindi).
3. When the download finishes, pick **Piper: en_US-lessac-medium** in
   the **Voice** dropdown (bottom bar) and use **Read Aloud** as
   usual. The voice speed follows the video speed with Piper too.

Notes:

- Needs the Python packages **piper-tts** and **PyAudio** (installed
  automatically on first run; if missing, use the **⬇ Install
  dependencies** button or run `py -3 -m pip install piper-tts PyAudio`).
- Voices are saved in the **piper\voices** folder next to the app
  and keep working offline forever. Delete a voice's two files
  (`.onnx` + `.onnx.json`) to remove it.
- The first line after picking a voice may pause a moment while the
  model loads into memory (~1 to 2 s); after that it keeps up easily.
- If the download list will not load (no internet / blocked site),
  download a voice manually from
  [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main)
  (the `.onnx` and `.onnx.json` files of a voice) into `piper\voices`.

---

## Controls

| Button | What it does |
|---|---|
| ▶ Play / ❚❚ Pause | play or pause (Space) |
| ■ Stop | stop |
| « 10s / 10s » | jump 10 seconds back / forward (← / →) |
| Speed − / Speed + | slower / faster playback (0.25x steps, 0.25x to 4x); the Read Aloud voice speed follows |
| green **1.00x** | click to reset speed to normal (also [ and ] keys) |
| Vol − / Vol + | volume down / up (↓ / ↑) |
| Mute | mute / unmute (M) |
| Drag & drop | drop a video **or** subtitle file from Explorer anywhere on the window, it plays / loads immediately |
| Subtitles: ON/OFF | show or hide the subtitle text on video (C) |
| 📄 Add Subtitles | open a subtitle file (Ctrl+U) |
| Subtitles inside this movie | pick a subtitle track stored inside the MKV/MP4 file |
| 🔊 Read Aloud | speak every subtitle line (S) |
| Voice | Windows voices + **Piper: …** neural voices you downloaded (File → Get Piper voices) |

| Key | Action |
|---|---|
| Space | Play / Pause |
| ← / → | seek 10s back / forward |
| ↑ / ↓ | volume up / down |
| [ / ] | speed down / up |
| M | mute |
| S | Read Aloud on / off |
| C | show / hide subtitles |
| F / F11 / Esc | fullscreen on / off |
| Ctrl+O | open video |
| Ctrl+U | open subtitles |

---

## Troubleshooting

- **"Could not load VLC (libvlc.dll)"**, install normal (not Microsoft
  Store) VLC from videolan.org, 64-bit, and start VLCspeaker again.
- **"Python packages are not installed"**, answer **Yes** to the install
  question in the dialog, or use the **⬇ Install dependencies** button
  (bottom-right status bar / File menu).
- **Read Aloud button is grey / "unavailable"**, use the **⬇ Install
  dependencies** button (bottom-right status bar / File menu), or run
  `py -3 -m pip install comtypes` in a terminal.
- **No voice for my language**, add it in Windows speech settings
  (see above). English voices David / Zira are always available.
- **Voice reads the wrong language**, pick a voice that matches the
  language of your subtitle file in the Voice dropdown.
- **Female voice**, the player picks a female voice automatically when
  one is installed (voices marked "(female)" in the dropdown).
  More/better female voices: Windows Settings → Time & Language →
  Speech → Add voices, or install natural voices via Narrator settings.

## Files

- `vlcspeaker.py`, the whole application
- `run.bat`, double-click to start
- `requirements.txt`, Python packages (python-vlc, comtypes, tkinterdnd2)

Tip: while a movie is playing, drop files onto the control bar at the
bottom or the window edges, the video picture itself belongs to VLC,
which does not accept drops there.

Check everything is OK: open a terminal in this folder and run
`py -3 vlcspeaker.py --selftest`
"# VLCspeaker" 
