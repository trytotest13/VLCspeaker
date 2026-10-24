"""Regression check for OCR read-aloud: no repeats from OCR wobble, no stale
re-speak after seeks, and speech re-firing on resume / Read Aloud toggle.
Drives the real _tick_body with a scripted fake player."""
import tkinter as tk
import vlcspeaker as V

# --- caption matching helper ------------------------------------------------
sc = V._ocr_same_caption
assert sc("I'll be back", "I'II be back"), "OCR wobble must match"
assert sc("Hello world.", "Hello world."), "identical must match"
assert not sc("Caption one", "Caption two")
assert not sc("I am tired", "I am hungry")
assert not sc("Hello world.", "How are you?")
assert sc("The quick brown fox jumps over the lazy dog",
          "The qulck brown fox jumps over the lazy dog"), "1-char wobble"
assert not sc("I love you too", "I love you"), "longer new line must split"
assert sc("I love you", "I love you too"), "truncated re-read extends"
assert not sc("", "x") and not sc("x", "")

root = tk.Tk()
app = V.PlayerApp(root)
root.update()
app._tick = lambda: None               # silence the scheduled polling loop

class FakeP:
    def __init__(self):
        self.t = 0
        self.rate = 1.0
        self.paused = False
    def get_time(self): return self.t
    def get_length(self): return 3600000
    def get_state(self):
        return V.vlc.State.Paused if self.paused else V.vlc.State.Playing
    def is_playing(self): return not self.paused
    def set_pause(self, x): self.paused = bool(x)
    def play(self): self.paused = False
    def get_rate(self): return self.rate
    def audio_get_volume(self): return 100
    def audio_get_mute(self): return False
    def set_rate(self, r): self.rate = r
    def video_set_spu(self, x): pass
    def video_take_snapshot(self, *a): return -1

fp = FakeP()
app.player = fp
app._video_path = None
app.ocr_mode = True
app.read_aloud.set(True)

said = []
app.tts.say = lambda text: said.append((fp.t, text))

def tick_at(t):
    fp.t = t
    app._tick_body()

def feed(text, pos):
    app._sub_job_q.put(lambda t=text, p=pos: app._ocr_text_ready(t, p))

def reset():
    said.clear()
    app.ocr_cues, app.ocr_starts = [], []
    app._ocr_last_text = None
    app.current_cue_idx = None
    app.spoken_idx = -1
    app._was_playing = True
    app._resume_skip = False
    fp.paused = False

# --- S1: plain captions -----------------------------------------------------
reset()
feed("Hello world.", 10000); tick_at(11800)
feed("Hello world.", 10800); tick_at(12500)      # exact re-read: no repeat
feed("How are you?", 14000); tick_at(15200)
feed("How are you?", 14800); tick_at(15900)
assert said == [(11800, "Hello world."), (15200, "How are you?")], said
print("S1 plain          OK", said)

# --- S2: OCR wobble on one caption must speak once --------------------------
reset()
feed("I'll be back", 20000); tick_at(21800)
feed("I'II be back", 20800); tick_at(22500)      # 1-char wobble
feed("I'll be back", 21600); tick_at(23200)      # wobble back
assert said == [(21800, "I'll be back")], said
print("S2 wobble         OK", said)

# --- S3: backward seek then re-watch ----------------------------------------
reset()
feed("Caption one", 30000); tick_at(31800)
feed("Caption two", 33000); tick_at(34800)
feed("Caption three", 36000); tick_at(37800)
app._seek_from = 38000
fp.t = 20000
app._after_seek()                                 # backward seek
tick_at(20100)
feed("Caption one", 20500); tick_at(20800)       # fresh re-read: speaks once
tick_at(30500)                                    # old windows must NOT fire
tick_at(33500)
assert said == [(31800, "Caption one"), (34800, "Caption two"),
                (37800, "Caption three"), (20800, "Caption one")], said
print("S3 backward seek  OK", said)

# --- S4: forward seek onto the same line stays silent, next line speaks -----
reset()
feed("Caption one", 30000); tick_at(31800)
feed("Caption two", 33000); tick_at(34800)
app._last_spoken_text = "Caption two"
app._seek_from = 35000
fp.t = 36000
app._after_seek()                                 # forward seek
tick_at(36100)
feed("Caption two", 36300); tick_at(36600)       # still on that same line
feed("Caption three", 36500); tick_at(37000)
assert said == [(31800, "Caption one"), (34800, "Caption two"),
                (37000, "Caption three")], said
print("S4 forward seek   OK", said)

# --- S5: resume from pause re-speaks the caption on screen ------------------
reset()
feed("Still talking.", 40000); tick_at(41800)
assert said == [(41800, "Still talking.")], said
app.toggle_play()                                  # pause
tick_at(42400)
app.toggle_play()                                  # resume mid-caption
tick_at(42500)
assert said == [(41800, "Still talking."), (42500, "Still talking.")], said
print("S5 resume         OK", said)

# --- S6: Read Aloud toggled ON mid-caption speaks it right away -------------
reset()
app.read_aloud.set(False)
feed("Speak me.", 50000); tick_at(51800)
assert said == [], said
app.toggle_read_aloud()                            # ON (flips the var)
tick_at(52000)
assert said == [(52000, "Speak me.")], said
print("S6 read-aloud on  OK", said)

root.destroy()
print("OCR SPEAK CHECK PASS")
