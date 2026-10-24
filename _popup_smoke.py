"""Smoke test for the desktop-space combobox popup.

event_generate() into an overrideredirect Toplevel does not reliably dispatch
bindings on Windows, so the key handlers are invoked directly via the popup
dict the app stores in _choice_popup.
"""
import tkinter as tk
from tkinter import ttk
import vlcspeaker

root = tk.Tk()
root.geometry("700x90+300+500")
app = object.__new__(vlcspeaker.PlayerApp)
app.root = root
app.THEME = vlcspeaker.PlayerApp.THEME
app._choice_popup = None

vals = ["Microsoft David Desktop", "Microsoft Hazel Desktop",
        "Microsoft Zira Desktop", "Piper: en_US-lessac-medium"]
combo = ttk.Combobox(root, state="readonly", width=24, font=("Segoe UI", 8),
                     values=vals)
combo.pack(side="bottom")
combo.current(1)
picked = []
app._wire_popup_combo(combo, lambda: picked.append(combo.current()))
root.update()

# the old ttk popdown was confined to this 90px-tall window; the new popup
# must be free to drop below the window onto desktop space
app._show_choice_popup(combo, lambda: picked.append(combo.current()))
pop = app._choice_popup
top, lb = pop["top"], pop["lb"]
root.update()
px, py = top.winfo_x(), top.winfo_y()
below = combo.winfo_rooty() + combo.winfo_height()
print("popup %dx%d at +%d+%d; combo bottom y=%d; screen %dx%d"
      % (top.winfo_reqwidth(), top.winfo_reqheight(), px, py, below,
         top.winfo_screenwidth(), top.winfo_screenheight()))
assert py >= below - 1, "popup should open below the combo, got %d < %d" % (py, below)
assert py + top.winfo_reqheight() <= top.winfo_screenheight(), "clipped at screen bottom"
assert lb.size() == 4 and lb.curselection() == (1,), "selection not pre-set"

# pick the 4th item
lb.selection_clear(0, "end")
lb.selection_set(3)
pop["pick"]()
root.update()
assert app._choice_popup is None, "popup should close after pick"
assert picked == [3], "on_change fired with %r" % picked
assert combo.current() == 3

# regression: the release of the OPENING click (no matching press inside the
# listbox, because the popup grabbed the pointer mid-click) must not commit
# or close the popup
app._show_choice_popup(combo, lambda: picked.append(combo.current()))
pop = app._choice_popup
lb = pop["lb"]
lb.selection_clear(0, "end")
lb.selection_set(2)
pop["pick_mouse"]()                  # no ButtonPress happened first
assert app._choice_popup is not None, "orphan release must not close popup"
assert picked == [3], "orphan release must not commit, got %r" % picked
pop["press"]()                         # real user press inside the list
pop["pick_mouse"]()
assert app._choice_popup is None and picked[-1] == 2, "press+release must pick"
assert combo.current() == 2

# arrow keys move the highlight without committing
# (popup re-opens pre-selected at the current value = index 2)
app._show_choice_popup(combo, lambda: picked.append(combo.current()))
pop = app._choice_popup
lb = pop["lb"]
pop["nav"](-1)
assert lb.curselection() == (1,), "nav should move highlight, got %r" % (lb.curselection(),)
pop["nav"](-3)                       # clamp at the top
assert lb.curselection() == (0,), "nav should clamp, got %r" % (lb.curselection(),)
pop["pick"]()
assert combo.current() == 0 and picked[-1] == 0, "commit after nav"

# re-open, then Escape cancels without picking
app._show_choice_popup(combo, lambda: picked.append(combo.current()))
pop = app._choice_popup
pop["close"]()
root.update()
assert app._choice_popup is None and picked[-1] == 0, "escape should cancel"

# open via the wired path, toggle closed via the same combo event handler
combo._popup_open_it()
assert app._choice_popup is not None, "open_it should open"
combo._popup_open_it()
assert app._choice_popup is None, "open_it should toggle closed"

# after close, the combo's open binding is restored (Up/Down/space re-wired)
app._show_choice_popup(combo, lambda: picked.append(combo.current()))
app._hide_choice_popup()
assert bool(combo.bind("<KeyPress-Down>")), "open binding lost after close"

# disabled combo must not open
combo.config(state="disabled")
r = combo._popup_open_it()
assert r == "break" and app._choice_popup is None, "disabled must not open"
combo.config(state="readonly")

# --- fullscreen-bar regression --------------------------------------------
# in fullscreen the pointer poll re-lifts the floating bar on every mouse
# move; bar and popup share the topmost band (the list flips upward over the
# bottom-docked bar), so the bar must not end up above the open dropdown
import ctypes
from ctypes import wintypes

fs_bar = tk.Toplevel(root)
fs_bar.overrideredirect(True)
fs_bar.attributes("-topmost", True)   # the real fullscreen bar is topmost too
fs_bar.geometry("420x80+140+500")
tk.Label(fs_bar, text="bar").pack()
app_fs = object.__new__(vlcspeaker.PlayerApp)
app_fs.root = root
app_fs.THEME = vlcspeaker.PlayerApp.THEME
app_fs._choice_popup = None
app_fs._fs_win = fs_bar
combo2 = ttk.Combobox(root, state="readonly", width=24,
                      values=["v1", "v2", "v3", "v4"])
combo2.pack()
combo2.current(0)
root.update()
app_fs._show_choice_popup(combo2, lambda: None)
fs_pop = app_fs._choice_popup
root.update()
# drop the bar over the popup, like the bottom-docked fullscreen bar does
fs_bar.geometry("+%d+%d" % (fs_pop["top"].winfo_x() + 30,
                            fs_pop["top"].winfo_y() + 10))
root.update()

u32 = ctypes.windll.user32
GA_ROOT = 2

def root_hwnd_at(x, y):
    return u32.GetAncestor(u32.WindowFromPoint(wintypes.POINT(x, y)), GA_ROOT)

pop_hwnd = u32.GetAncestor(fs_pop["top"].winfo_id(), GA_ROOT)
bar_hwnd = u32.GetAncestor(fs_bar.winfo_id(), GA_ROOT)
assert pop_hwnd != bar_hwnd
# probe a point covered by BOTH windows, like the flipped-up list covering
# the bottom-docked bar in fullscreen
px = fs_pop["top"].winfo_rootx() + 60
py = fs_pop["top"].winfo_rooty() + 25
assert root_hwnd_at(px, py) == pop_hwnd, "popup should open above the bar"

app_fs._fs_show_bar()                  # what the pointer poll does per move
root.update()
assert app_fs._choice_popup is not None, "bar wake must not close the popup"
assert root_hwnd_at(px, py) == pop_hwnd, (
    "bar re-lift must not cover the open dropdown")
# the list must still be clickable after the wake
fs_pop["press"]()
fs_pop["pick_mouse"]()
assert app_fs._choice_popup is None, "list must still pick after bar wake"
assert combo2.current() == 0
root.update()

# --- fullscreen-bar drag-to-move -------------------------------------------
from types import SimpleNamespace

fs_bar2 = tk.Toplevel(root)
fs_bar2.overrideredirect(True)
fs_bar2.geometry("300x60+100+400")
drag_lbl = tk.Label(fs_bar2, text="00:00")
drag_lbl.pack(side="left")
drag_btn = tk.Button(fs_bar2, text="play")
drag_btn.pack(side="left")
drag_cv = tk.Canvas(fs_bar2, width=40, height=20)
drag_cv.pack(side="left")
app_d = object.__new__(vlcspeaker.PlayerApp)
app_d.root = root
app_d.THEME = vlcspeaker.PlayerApp.THEME
app_d._choice_popup = None
app_d._fs_win = fs_bar2
app_d._make_fs_bar_draggable(fs_bar2)
root.update()

def ev(widget, x, y):
    return SimpleNamespace(widget=widget, x_root=x, y_root=y)

# dragging from a label (empty background) moves the bar and remembers it
fs_bar2._fs_drag_start(ev(drag_lbl, 150, 420))
fs_bar2._fs_drag_move(ev(drag_lbl, 260, 430))
root.update()
assert (fs_bar2.winfo_x(), fs_bar2.winfo_y()) == (210, 410), (
    fs_bar2.winfo_x(), fs_bar2.winfo_y())
assert app_d._fs_bar_pos == (210, 410), app_d._fs_bar_pos
fs_bar2._fs_drag_stop(ev(drag_lbl, 260, 430))

# buttons and canvases (seekbar / volume wedge) must not drag the bar
before = fs_bar2.winfo_x()
fs_bar2._fs_drag_start(ev(drag_btn, 160, 420))
fs_bar2._fs_drag_move(ev(drag_btn, 400, 500))
root.update()
assert fs_bar2.winfo_x() == before, "button press must not drag the bar"
fs_bar2._fs_drag_start(ev(drag_cv, 160, 420))
fs_bar2._fs_drag_move(ev(drag_cv, 400, 500))
root.update()
assert fs_bar2.winfo_x() == before, "canvas press must not drag the bar"

# re-showing the bar keeps the dragged position instead of re-centering
app_d._fs_place_bar()
root.update()
assert (fs_bar2.winfo_x(), fs_bar2.winfo_y()) == (210, 410), (
    fs_bar2.winfo_x(), fs_bar2.winfo_y())
fs_bar2.destroy()
root.update()

# --- speed toast -----------------------------------------------------------
toast_area = tk.Frame(root, bg="#000000", width=500, height=200)
toast_area.place(x=100, y=100)
app_t = object.__new__(vlcspeaker.PlayerApp)
app_t.root = root
app_t.THEME = vlcspeaker.PlayerApp.THEME
app_t.video_area = toast_area
app_t._speed_toast = None
app_t._speed_toast_after = None
root.update()
app_t._show_toast("8.00x", big=True)
root.update()
t = app_t._speed_toast
assert t is not None and t.winfo_viewable(), "toast must be visible"
assert app_t.speed_toast_lbl.cget("text") == "8.00x", app_t.speed_toast_lbl.cget("text")
ax, ay = toast_area.winfo_rootx(), toast_area.winfo_rooty()
assert abs(t.winfo_x() - (ax + 500 - t.winfo_width() - 16)) <= 2, ("toast x", t.winfo_x())
assert abs(t.winfo_y() - (ay + 16)) <= 2, ("toast y", t.winfo_y())
assert app_t._speed_toast_after is not None, "auto-hide timer missing"
app_t._show_toast("6.00x", big=True)  # repeat reuses the same toplevel
assert app_t._speed_toast is t and app_t.speed_toast_lbl.cget("text") == "6.00x"
app_t._hide_toast()
root.update()
assert not t.winfo_viewable() and app_t._speed_toast_after is None, "toast must hide"
t.destroy()

root.destroy()
print("SMOKE PASS")
