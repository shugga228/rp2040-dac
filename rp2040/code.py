"""
CircuitPython I2S Music Player with SPI Display and SD Card (Max Optimized)
============================================================================

Firmware for a Raspberry Pi Pico (CircuitPython) based portable music player.
Runs entirely on-device and is designed to be driven by the companion desktop
app, which converts a user's MP3 library into the WAV/album-art format this
firmware expects and writes it onto the SD card, then rewrites the `SONGS`
tuple (and default volume/mode/color) below to match.

Responsibilities of this file:

  1. Bring up all hardware: I2S audio out, SPI display (ST7789), SD card,
     and the six physical buttons (up/down/play/prev/next/mode).
  2. Build a flat (non-nested) displayio scene graph up front, then mutate
     individual bitmaps/labels in place every frame instead of rebuilding
     groups, to keep RAM/CPU usage low on the Pico.
  3. Stream album art directly to the display over SPI (bypassing displayio's
     OnDiskBitmap) so large images load without blowing the RAM budget.
  4. Drive a lightweight bar-style audio visualizer and playback progress bar
     using only integer math (no floats), since float ops are comparatively
     expensive on this hardware.
  5. Handle button input, playback control (play/pause/prev/next/shuffle/
     loop), and an in-device settings menu (volume/mode/color/about).

This firmware ships with an empty `SONGS` tuple and default settings
(White highlight color, Default playback mode, normal volume). The song
list and album art folders on the SD card are expected to be empty until
the companion app performs its first sync.

Author:   Sami Uddin
GitHub:   https://github.com/shugga228
"""

import time
import gc
import random
import board
import busio
import digitalio
import sdcardio
import storage
import audiobusio
import audiocore
import audiomixer
import displayio
import fourwire
from adafruit_st7789 import ST7789
from adafruit_display_text import label
import terminalio
from micropython import const

gc.collect()

# ===============================================
# HARDWARE CONFIGURATION
# ===============================================
# Pin assignments for the three SPI/I2S peripherals and the six buttons.
# Adjust these if wiring differs from the reference build.

PIN_I2S_BCK   = board.GP4
PIN_I2S_LCK   = board.GP5
PIN_I2S_DIN   = board.GP6

PIN_DISP_CLK  = board.GP14
PIN_DISP_MOSI = board.GP15
PIN_DISP_CS   = board.GP13
PIN_DISP_DC   = board.GP10
PIN_DISP_RST  = board.GP9
PIN_DISP_BL   = board.GP11

PIN_SD_CLK    = board.GP18
PIN_SD_MOSI   = board.GP19
PIN_SD_MISO   = board.GP20
PIN_SD_CS     = board.GP23

PIN_BTN_UP   = board.GP0
PIN_BTN_DOWN = board.GP3
PIN_BTN_PLAY = board.GP2
PIN_BTN_PREV = board.GP1
PIN_BTN_NEXT = board.GP12
PIN_BTN_MODE = board.GP26

# ===============================================
# SOFTWARE CONFIGURATION (Using const for RAM)
# ===============================================
# `const()` lets CircuitPython inline these values at compile time instead of
# keeping them as live globals, which saves a meaningful amount of RAM on a
# memory-constrained board like the Pico.

FIRMWARE_VERSION = "1.0.0"

# Default volume as an integer percent (0-20 scale used by the settings
# menu; actual mixer level is this value / 100). 5 = "normal" listening
# volume out of the box, and matches the companion app's own default.
DEFAULT_VOL_INT = const(5)   # Represents 0.05 (5%)
AUDIO_BUFFER_SIZE = const(1024)
AUDIO_MIXER_BUFFER_SIZE = const(8192)
DISPLAY_REFRESH_MS = const(100)
IDLE_LOOP_DELAY_S = 0.002

BLACK = const(0x000000)
WHITE = const(0xFFFFFF)
CYAN  = const(0x00FFFF)
GRAY  = const(0x606060)

# Highlight color choices shown in the on-device settings menu, and mirrored
# by the companion app's own color dropdown so indices line up between them.
COLOR_OPTIONS = (
    ("White",  0xFFFFFF),
    ("Cyan",   0x00FFFF),
    ("Pink",   0xFF69B4),
    ("Green",  0x00FF00),
    ("Red",    0xFF0000),
    ("Orange", 0xFF8800),
    ("Purple", 0x9933FF),
    ("Sage",   0x9CB89C)
)
# Default highlight color index. 0 = White, i.e. a neutral out-of-the-box
# look until the user picks something else via the menu or companion app.
DEFAULT_COLOR_IDX = const(0)
HIGHLIGHT_COLOR = COLOR_OPTIONS[DEFAULT_COLOR_IDX][1]

SCREEN_W  = const(320)
SCREEN_H  = const(240)
DIVIDER_X = const(152)
RIGHT_X   = const(155)
RIGHT_W   = const(SCREEN_W - RIGHT_X)
ROW_H     = const(18)
LIST_ROWS = const(12)

ART_SIZE     = const(148)
ART_INNER    = const(ART_SIZE - 2)
ART_X        = const(163)
ART_Y        = const(6)
ART_ROTATION = const(180)

VIZ_Y     = const(ART_Y + ART_SIZE + 4)
VIZ_H     = const(16)
VIZ_BARS  = const(8)
VIZ_BAR_W = const(5)
VIZ_GAP   = const(2)
TITLE_Y   = const(VIZ_Y + VIZ_H + 18)
ARTIST_Y  = const(TITLE_Y + 36)
MODE_Y    = const(SCREEN_H - 12)

# Song list is intentionally empty out of the box. Each entry, once
# populated by the companion app, is a (title, artist, base_filename,
# duration_ms) tuple. `base_filename` matches both
# /sd/music/<base_filename>.wav and /sd/album_art/<base_filename>.raw.
SONGS = ()
NUM_SONGS    = len(SONGS)
NUM_PAGES    = (NUM_SONGS + LIST_ROWS - 1) // LIST_ROWS

MODE_SHUFFLE = const(1)
MODE_LOOP    = const(2)
# Default playback mode index — 0 corresponds to MODE_NAMES[0] ("Default"),
# so the device boots into plain sequential playback until changed.
MODE_DEFAULT = const(0)
MODE_SYMBOLS = (">", "?", "@")
MODE_NAMES   = ("Default", "Shuffle", "Loop")

# "About" is included alongside the tunable settings so firmware version and
# song count can be checked without a computer.
MENU_ITEMS = ("Volume", "Play Mode", "Color", "About", "Back")
VOL_STEP_INT = const(1)
VOL_MIN_INT  = const(0)
VOL_MAX_INT  = const(20)

# ===============================================
# HARDWARE INIT
# ===============================================
i2s = audiobusio.I2SOut(PIN_I2S_BCK, PIN_I2S_LCK, PIN_I2S_DIN)

# All six buttons are wired active-low with internal pull-ups, so a pressed
# button reads False. Stored in a dict keyed by a single-character "action
# code" so the rest of the code can treat input as simple key events.
btn_map = (
    ('w', PIN_BTN_UP),
    ('s', PIN_BTN_DOWN),
    ('p', PIN_BTN_PLAY),
    ('a', PIN_BTN_PREV),
    ('d', PIN_BTN_NEXT),
    ('m', PIN_BTN_MODE),
)
hardware_buttons = {}
for key_char, pin_obj in btn_map:
    btn = digitalio.DigitalInOut(pin_obj)
    btn.direction = digitalio.Direction.INPUT
    btn.pull = digitalio.Pull.UP
    hardware_buttons[key_char] = btn

# Audio playback state, mutated by the audio_* helper functions below.
audio_file    = None
audio_decoder = None
audio_mixer   = None
audio_playing = False
audio_paused  = False
audio_buffer  = bytearray(AUDIO_BUFFER_SIZE)

displayio.release_displays()
raw_spi = busio.SPI(clock=PIN_DISP_CLK, MOSI=PIN_DISP_MOSI)
display_bus = fourwire.FourWire(
    raw_spi, command=PIN_DISP_DC, chip_select=PIN_DISP_CS,
    reset=PIN_DISP_RST, baudrate=20000000,
)
display = ST7789(
    display_bus, width=SCREEN_W, height=SCREEN_H,
    rotation=90, backlight_pin=PIN_DISP_BL
)
display.brightness   = 1.0
# Manual refresh (instead of auto_refresh) lets the main loop batch all
# per-frame drawing and call display.refresh() exactly once, avoiding
# redundant/partial SPI transfers.
display.auto_refresh = False
FONT = terminalio.FONT

# ===============================================
# BUILD UI (Flattened for memory optimization)
# ===============================================
# Every widget that can change over time (list rows, mode indicator, title,
# progress bar, visualizer bars) is created exactly once here and referenced
# by variable from then on. Nothing is added to or removed from `root`
# after this block (aside from the temporary boot splash), since rebuilding
# displayio groups at runtime is comparatively slow and fragments memory.
root = displayio.Group()
display.root_group = root

# Full-screen black background.
bg = displayio.Bitmap(SCREEN_W, SCREEN_H, 1)
bp = displayio.Palette(1); bp[0] = BLACK
root.append(displayio.TileGrid(bg, pixel_shader=bp))

# Vertical divider separating the song list (left) from art/now-playing (right).
dv = displayio.Bitmap(1, SCREEN_H, 1)
dp = displayio.Palette(1); dp[0] = GRAY
root.append(displayio.TileGrid(dv, pixel_shader=dp, x=DIVIDER_X))

# 2px-wide playback progress bar drawn along the divider, filled from the
# bottom up as the current song plays.
prog_bmp = displayio.Bitmap(2, SCREEN_H, 2)
prog_pal = displayio.Palette(2)
prog_pal[0] = GRAY
prog_pal[1] = HIGHLIGHT_COLOR
root.append(displayio.TileGrid(prog_bmp, pixel_shader=prog_pal, x=DIVIDER_X))

# Song list rows: each row is an arrow-indicator label plus a title label,
# pre-created for every visible row and text-only updated afterward.
arrow_labels = []
list_labels  = []
for i in range(LIST_ROWS):
    y = i * ROW_H + 6
    a = label.Label(FONT, text=" ", color=WHITE, x=2,  y=y)
    l = label.Label(FONT, text="",  color=GRAY,  x=14, y=y)
    root.append(a)
    root.append(l)
    arrow_labels.append(a)
    list_labels.append(l)

mode_lbl = label.Label(FONT, text=">", color=HIGHLIGHT_COLOR, x=2, y=MODE_Y)
root.append(mode_lbl)

# Album art frame: a 1px border bitmap plus a black fill bitmap sized for
# the inner area, which the streamed album art is drawn over directly via
# display_bus commands (see load_art_blocking).
ab = displayio.Bitmap(ART_SIZE, ART_SIZE, 2)
ap = displayio.Palette(2); ap[0] = BLACK; ap[1] = WHITE
for px in range(ART_SIZE):
    ab[px, 0]=1; ab[px, ART_SIZE-1]=1
    ab[0, px]=1; ab[ART_SIZE-1, px]=1
root.append(displayio.TileGrid(ab, pixel_shader=ap, x=ART_X, y=ART_Y))

art_fill   = displayio.Bitmap(ART_INNER, ART_INNER, 1)
art_fill_p = displayio.Palette(1); art_fill_p[0] = BLACK
root.append(displayio.TileGrid(art_fill, pixel_shader=art_fill_p, x=ART_X+1, y=ART_Y+1))

# Bar visualizer bitmap: each bar is VIZ_BAR_W pixels wide with a gap column
# between bars so bar heights can be redrawn independently.
viz_total_w = VIZ_BARS * (VIZ_BAR_W + VIZ_GAP)
viz_x0      = RIGHT_X + (RIGHT_W - viz_total_w) // 2
viz_bmp     = displayio.Bitmap(viz_total_w, VIZ_H, 2)
viz_pal     = displayio.Palette(2); viz_pal[0] = BLACK; viz_pal[1] = HIGHLIGHT_COLOR
root.append(displayio.TileGrid(viz_bmp, pixel_shader=viz_pal, x=viz_x0, y=VIZ_Y))

# Now-playing title (wraps across two lines) and artist labels. These use
# HIGHLIGHT_COLOR (not a hardcoded color) so a color change from the menu
# or companion app is reflected immediately via apply_highlight_color().
title_lbl1 = label.Label(FONT, text="",               color=HIGHLIGHT_COLOR, x=RIGHT_X+4, y=TITLE_Y,     scale=2)
title_lbl2 = label.Label(FONT, text="",               color=HIGHLIGHT_COLOR, x=RIGHT_X+4, y=TITLE_Y + 16, scale=2)
artist_lbl = label.Label(FONT, text="No song playing", color=GRAY,  x=RIGHT_X+4, y=ARTIST_Y,    scale=1)
root.append(title_lbl1)
root.append(title_lbl2)
root.append(artist_lbl)

gc.collect()

# ===============================================
# ALBUM ART (Fully Optimized X/Y commands)
# ===============================================
# Album art is streamed straight to the display controller over SPI, one row
# at a time, instead of going through displayio.OnDiskBitmap. This avoids
# holding a full decoded image in RAM and lets a 148x148 image load quickly
# on hardware with limited memory. The four rotation branches below exist
# because the raw file is stored row-major in its original orientation, but
# the physical panel is mounted rotated relative to it.
_art_row_buf = bytearray(ART_INNER * 2)
x_cmd = bytearray(4)
y_cmd = bytearray(4)

def load_art_blocking(base_name):
    """Stream `/sd/album_art/<base_name>.raw` (a raw big-endian RGB565
    buffer produced by the companion app) directly onto the display over
    SPI, row by row, positioned inside the album-art frame and rotated to
    match ART_ROTATION. Blocks the main loop for the duration of the
    transfer — acceptable since it's only called on song change, not every
    frame. Silently no-ops (leaving the black placeholder fill visible) if
    the file is missing or can't be read."""
    base_y = (SCREEN_H - ART_SIZE - ART_Y) if ART_ROTATION in (90, 180, 270) else ART_Y
    path = "/sd/album_art/" + base_name + ".raw"
    try:
        with open(path, "rb") as f:
            if ART_ROTATION == 180:
                x0 = ART_X + 1
                x1 = x0 + ART_INNER - 1
                x_cmd[0] = x0 >> 8; x_cmd[1] = x0 & 0xFF; x_cmd[2] = x1 >> 8; x_cmd[3] = x1 & 0xFF
                for row in range(ART_INNER):
                    if f.readinto(_art_row_buf) != ART_INNER * 2: break
                    y = base_y + 1 + (ART_INNER - 1 - row)
                    y_cmd[0] = y >> 8; y_cmd[1] = y & 0xFF; y_cmd[2] = y >> 8; y_cmd[3] = y & 0xFF
                    display_bus.send(0x2B, x_cmd)
                    display_bus.send(0x2A, y_cmd)
                    display_bus.send(0x2C, _art_row_buf)
            elif ART_ROTATION == 0:
                x0 = ART_X + 1
                x1 = x0 + ART_INNER - 1
                x_cmd[0] = x0 >> 8; x_cmd[1] = x0 & 0xFF; x_cmd[2] = x1 >> 8; x_cmd[3] = x1 & 0xFF
                for row in range(ART_INNER):
                    if f.readinto(_art_row_buf) != ART_INNER * 2: break
                    y = base_y + 1 + row
                    y_cmd[0] = y >> 8; y_cmd[1] = y & 0xFF; y_cmd[2] = y >> 8; y_cmd[3] = y & 0xFF
                    display_bus.send(0x2B, x_cmd)
                    display_bus.send(0x2A, y_cmd)
                    display_bus.send(0x2C, _art_row_buf)
            elif ART_ROTATION == 90:
                y0 = base_y + 1
                y1 = y0 + ART_INNER - 1
                y_cmd[0] = y0 >> 8; y_cmd[1] = y0 & 0xFF; y_cmd[2] = y1 >> 8; y_cmd[3] = y1 & 0xFF
                for row in range(ART_INNER):
                    if f.readinto(_art_row_buf) != ART_INNER * 2: break
                    x = ART_X + 1 + (ART_INNER - 1 - row)
                    x_cmd[0] = x >> 8; x_cmd[1] = x & 0xFF; x_cmd[2] = x >> 8; x_cmd[3] = x & 0xFF
                    display_bus.send(0x2B, x_cmd)
                    display_bus.send(0x2A, y_cmd)
                    display_bus.send(0x2C, _art_row_buf)
            elif ART_ROTATION == 270:
                y0 = base_y + 1
                y1 = y0 + ART_INNER - 1
                y_cmd[0] = y0 >> 8; y_cmd[1] = y0 & 0xFF; y_cmd[2] = y1 >> 8; y_cmd[3] = y1 & 0xFF
                for row in range(ART_INNER):
                    if f.readinto(_art_row_buf) != ART_INNER * 2: break
                    x = ART_X + 1 + row
                    x_cmd[0] = x >> 8; x_cmd[1] = x & 0xFF; x_cmd[2] = x >> 8; x_cmd[3] = x & 0xFF
                    display_bus.send(0x2B, x_cmd)
                    display_bus.send(0x2A, y_cmd)
                    display_bus.send(0x2C, _art_row_buf)
    except Exception as e:
        # Missing/unreadable art (e.g. no album_art file for this track yet)
        # is expected and non-fatal — just leave the black placeholder.
        print("Art error:", e)

# ===============================================
# VISUALIZER (Zero Float Implementation)
# ===============================================
# A fake "VU meter" style visualizer (not derived from actual audio signal —
# the hardware has no line-in/analysis path) that eases bar heights toward
# randomly chosen targets. Implemented with only integers to keep frame
# updates cheap on the Pico.
viz_h    = bytearray(VIZ_BARS)
viz_tgt  = bytearray(VIZ_BARS)
viz_prev = bytearray([255] * VIZ_BARS)  # 255 = "not yet drawn" sentinel

def reset_visualizer():
    """Zero out all bar heights/targets and mark them as undrawn, e.g. when
    starting a new song."""
    for i in range(VIZ_BARS): viz_h[i]=0; viz_tgt[i]=0; viz_prev[i]=255

def update_visualizer():
    """Advance one animation tick: occasionally reroll each bar's target
    height, then ease every bar's current height toward its target (faster
    when rising than when falling, for a punchier look)."""
    for i in range(VIZ_BARS):
        if random.getrandbits(1): viz_tgt[i] = random.randrange(3, VIZ_H-1)
    for i in range(VIZ_BARS):
        d = viz_tgt[i]-viz_h[i]
        if d>0: viz_h[i]+=max(1,d//3)
        elif d<0: viz_h[i]-=max(1,(-d)//4)
        viz_h[i]=max(0,min(VIZ_H-1,viz_h[i]))

def stop_visualizer():
    """Drive all bar targets to zero and let them decay by one pixel per
    call, used while paused so the bars settle rather than freezing."""
    for i in range(VIZ_BARS): viz_tgt[i]=0; viz_h[i]=max(0,viz_h[i]-1)

def draw_visualizer():
    """Redraw only the pixel rows that changed since the last frame for
    each bar (comparing against `viz_prev`), instead of clearing and
    repainting the whole visualizer bitmap every tick."""
    for i in range(VIZ_BARS):
        new_h = viz_h[i]
        old_h = viz_prev[i]
        if new_h == old_h: continue
        if old_h == 255: old_h = 0
        x0 = i * (VIZ_BAR_W + VIZ_GAP)
        if new_h > old_h:
            for py in range(VIZ_H - new_h, VIZ_H - old_h):
                for px in range(VIZ_BAR_W):
                    viz_bmp[x0 + px, py] = 1
                if i < VIZ_BARS - 1: viz_bmp[x0 + VIZ_BAR_W, py] = 0
        elif new_h < old_h:
            for py in range(VIZ_H - old_h, VIZ_H - new_h):
                for px in range(VIZ_BAR_W):
                    viz_bmp[x0 + px, py] = 0
                if i < VIZ_BARS - 1: viz_bmp[x0 + VIZ_BAR_W, py] = 0
        viz_prev[i] = new_h

# ===============================================
# PROGRESS BAR (Zero Float Implementation)
# ===============================================
prev_prog = 0

def draw_progress(elapsed_ms, duration_ms):
    """Fill the progress bar bottom-up in proportion to elapsed/duration,
    using only integer math, and only touch the pixel rows that changed
    since the last call."""
    global prev_prog
    if duration_ms <= 0: return
    filled = (elapsed_ms * SCREEN_H) // duration_ms
    if filled > SCREEN_H: filled = SCREEN_H
    elif filled < 0: filled = 0

    if filled == prev_prog: return
    if filled > prev_prog:
        for py in range(SCREEN_H-filled, SCREEN_H-prev_prog):
            prog_bmp[0,py]=1; prog_bmp[1,py]=1
    else:
        for py in range(SCREEN_H-prev_prog, SCREEN_H-filled):
            prog_bmp[0,py]=0; prog_bmp[1,py]=0
    prev_prog = filled

def reset_progress():
    """Clear the progress bar entirely, e.g. when starting a new song."""
    global prev_prog
    for py in range(SCREEN_H): prog_bmp[0,py]=0; prog_bmp[1,py]=0
    prev_prog = 0

# ===============================================
# PAGED SONG LIST
# ===============================================
# The list only ever shows LIST_ROWS songs at once; `page_offset` is the
# index of the song shown in row 0. These "prev_*" trackers let draw_list
# skip re-rendering rows whose displayed state (song index, hover, playing)
# hasn't actually changed since the last call.
prev_list_idx  = [-1] * LIST_ROWS
prev_list_hov  = [False] * LIST_ROWS
prev_list_play = [False] * LIST_ROWS
prev_page_off  = -1

def draw_list(selected, playing_idx, page_offset):
    """Render the visible page of the song list: highlights the selected
    (hovered) row and marks whichever row holds the currently playing song,
    if any. Rows beyond the end of `SONGS` (including every row when the
    list is empty) are simply rendered blank. Only rows whose state
    changed since the last call are actually redrawn."""
    global prev_page_off
    force = (page_offset != prev_page_off)
    if force:
        prev_page_off = page_offset
        for i in range(LIST_ROWS): prev_list_idx[i] = -1

    for i in range(LIST_ROWS):
        idx     = page_offset + i
        is_hov  = (idx == selected)
        is_play = (idx == playing_idx)

        if not force and prev_list_idx[i] == idx and prev_list_hov[i] == is_hov and prev_list_play[i] == is_play:
            continue

        prev_list_idx[i]  = idx
        prev_list_hov[i]  = is_hov
        prev_list_play[i] = is_play

        if idx >= NUM_SONGS or idx < 0:
            # Covers both "past the end of the list" and, defensively, any
            # stray negative offset — either way the row stays blank.
            arrow_labels[i].text = " "
            list_labels[i].text  = ""
            continue

        title = SONGS[idx][0]
        if len(title) > 23: title = title[:22] + "."

        if is_play and is_hov:
            arrow_labels[i].color = HIGHLIGHT_COLOR; list_labels[i].color = HIGHLIGHT_COLOR
            arrow_labels[i].text  = ">"
        elif is_play:
            arrow_labels[i].color = HIGHLIGHT_COLOR; list_labels[i].color = HIGHLIGHT_COLOR
            arrow_labels[i].text  = "-"
        elif is_hov:
            arrow_labels[i].color = WHITE; list_labels[i].color = WHITE
            arrow_labels[i].text  = ">"
        else:
            arrow_labels[i].color = BLACK; list_labels[i].color = GRAY
            arrow_labels[i].text  = " "
        list_labels[i].text = title

def clear_list_cache():
    """Force the next draw_list() call to redraw every row, e.g. after
    returning from the settings menu where the rows were repurposed."""
    global prev_page_off
    prev_page_off = -1
    for i in range(LIST_ROWS): prev_list_idx[i] = -1

def apply_highlight_color(new_color):
    """Push a new accent color to every widget that uses HIGHLIGHT_COLOR
    (progress bar, visualizer, mode indicator, now-playing title) and
    invalidate the list-row cache so hover/playing rows repaint in the new
    color too."""
    prog_pal[1]     = new_color
    viz_pal[1]      = new_color
    mode_lbl.color  = new_color
    title_lbl1.color = new_color
    title_lbl2.color = new_color
    clear_list_cache()

def draw_mode(mode):
    """Update the small mode-indicator glyph in the bottom-left corner."""
    mode_lbl.text = MODE_SYMBOLS[mode]

def draw_info(playing_idx):
    """Update the now-playing title/artist labels. Wraps long titles across
    the two title label rows, preferring to break on a space. Shows a
    placeholder message when nothing is playing (including when the song
    list is still empty)."""
    if playing_idx is None:
        title_lbl1.text = ""
        title_lbl2.text = ""
        artist_lbl.text = "No song playing"
        return
    t, a, _, _dur = SONGS[playing_idx]
    if len(t) <= 13:
        title_lbl1.text = t
        title_lbl2.text = ""
    else:
        split_pos = t.rfind(" ", 0, 13)
        if split_pos == -1: split_pos = t.find(" ", 13, 26)
        if split_pos == -1:
            title_lbl1.text = t[:13]
            title_lbl2.text = t[13:26] + ("." if len(t) > 26 else "")
        else:
            title_lbl1.text = t[:split_pos]
            remainder = t[split_pos+1:]
            if len(remainder) <= 13: title_lbl2.text = remainder
            else: title_lbl2.text = remainder[:12] + "."
    artist_lbl.text = a[:24] if len(a) <= 24 else a[:23] + "."

def draw_menu(menu_sel, cur_mode, cur_vol_int, cur_color_idx):
    """Repurpose the song-list rows to show the settings menu (Volume,
    Play Mode, Color, About, Back), reflecting each setting's current
    value next to its label."""
    for i in range(LIST_ROWS):
        if i < len(MENU_ITEMS):
            is_hov = (i == menu_sel)
            name   = MENU_ITEMS[i]
            if   name == "Volume":    text = f"Volume: {cur_vol_int * 5}%"
            elif name == "Play Mode": text = f"Mode: {MODE_NAMES[cur_mode]}"
            elif name == "Color":     text = f"Color: {COLOR_OPTIONS[cur_color_idx][0]}"
            elif name == "About":     text = "About"
            else:                     text = "Back"

            arrow_labels[i].text  = ">" if is_hov else " "
            arrow_labels[i].color = HIGHLIGHT_COLOR if is_hov else WHITE
            list_labels[i].text   = text
            list_labels[i].color  = HIGHLIGHT_COLOR if is_hov else GRAY
        else:
            arrow_labels[i].text = " "; list_labels[i].text = ""

def draw_about():
    """Draws version info in the list panel — reuses arrow/list label rows."""
    lines = (
        "m.a.r.i.e",
        "",
        f"Firmware v{FIRMWARE_VERSION}",
        f"{NUM_SONGS} songs loaded",
        "",
        "CircuitPython Player",
        "",
        "Press settings to go back",
    )
    for i in range(LIST_ROWS):
        arrow_labels[i].text = " "
        if i < len(lines):
            list_labels[i].text  = lines[i]
            list_labels[i].color = HIGHLIGHT_COLOR if i == 0 else GRAY
        else:
            list_labels[i].text = ""

# ===============================================
# SD CARD + SPLASH
# ===============================================
print("Starting up...")
usb_connected = False
try:
    import supervisor; usb_connected = supervisor.runtime.usb_connected
except: pass

sd_spi = busio.SPI(clock=PIN_SD_CLK, MOSI=PIN_SD_MOSI, MISO=PIN_SD_MISO)
while not sd_spi.try_lock(): pass
sd_spi.configure(baudrate=25000000)
sd_spi.unlock()
sd_card = sdcardio.SDCard(sd_spi, PIN_SD_CS)

# Temporary boot splash group, shown while the SD card mounts and then
# discarded — this is the one place a group is added to `root` after
# initial setup, since it's removed again before the main loop starts.
splash = displayio.Group(); root.append(splash)
LOGO_OY = 15
name_y = LOGO_OY + 130  # fallback text position if logo fails to load

try:
    logo_odb  = displayio.OnDiskBitmap("/logo.bmp")
    logo_pal2 = logo_odb.pixel_shader
    # Recolor: bitmap value 0 = background (made transparent), value 1 = highlight color
    try:
        logo_pal2.make_transparent(0)
    except Exception:
        pass
    try:
        logo_pal2[1] = HIGHLIGHT_COLOR
    except Exception:
        pass
    logo_x = (SCREEN_W - logo_odb.width) // 2
    splash.append(displayio.TileGrid(logo_odb, pixel_shader=logo_pal2,
                                     x=logo_x, y=LOGO_OY))
    name_y = LOGO_OY + logo_odb.height + 20
except Exception as e:
    print("Logo load error:", e)

name_text="m.a.r.i.e"; ns=3
nx=(SCREEN_W-len(name_text)*6*ns)//2
splash.append(label.Label(FONT,text=name_text,color=HIGHLIGHT_COLOR,x=nx,y=name_y,scale=ns))
display.refresh()

try:
    vfs = storage.VfsFat(sd_card)
    storage.mount(vfs, "/sd", readonly=usb_connected)
    print("SD OK!")
except Exception as e:
    print("SD error!", e)

display.refresh()
if usb_connected:
    # Give the user time to read the splash / notice USB mass-storage mode
    # (mounted read-only on-device while the companion app has it open).
    time.sleep(20)
root.remove(splash); del splash
try:
    del logo_odb, logo_pal2
except NameError:
    pass
gc.collect()

# ===============================================
# INITIALIZE GLOBAL AUDIO MIXER
# ===============================================
print("Initializing global mixer...")
try:
    # Probe the first song's WAV header to configure the mixer with the
    # actual sample rate/channel count/bit depth on the SD card, rather
    # than assuming a fixed format.
    with open("/sd/music/" + SONGS[0][2] + ".wav", "rb") as f:
        temp_dec = audiocore.WaveFile(f, audio_buffer)
        m_rate = temp_dec.sample_rate
        m_chan = temp_dec.channel_count
        m_bits = temp_dec.bits_per_sample
except Exception:
    # No songs yet (empty SONGS -> IndexError) or the file is missing/
    # unreadable — fall back to the standard format the companion app
    # always converts to (44.1kHz/16-bit stereo), so the mixer still
    # comes up cleanly with zero songs loaded.
    m_rate, m_chan, m_bits = 44100, 2, 16

audio_mixer = audiomixer.Mixer(
    voice_count=1,
    sample_rate=m_rate,
    channel_count=m_chan,
    bits_per_sample=m_bits,
    samples_signed=True,
    buffer_size=AUDIO_MIXER_BUFFER_SIZE
)
audio_mixer.voice[0].level = DEFAULT_VOL_INT / 100.0
i2s.play(audio_mixer)

draw_list(0, None, 0); draw_mode(MODE_DEFAULT); draw_info(None); reset_progress()
display.refresh()

# ===============================================
# AUDIO
# ===============================================
def audio_start(song_idx):
    """Stop whatever's playing, open the WAV file for `SONGS[song_idx]`,
    and hand it to the mixer voice. Falls back to a clean stopped state if
    the file can't be opened (e.g. it's missing from the SD card)."""
    global audio_file, audio_decoder, audio_playing, audio_paused
    audio_stop()
    gc.collect()

    path = "/sd/music/" + SONGS[song_idx][2] + ".wav"
    try:
        audio_file    = open(path,"rb")
        audio_decoder = audiocore.WaveFile(audio_file, audio_buffer)
        audio_mixer.voice[0].play(audio_decoder)
        audio_playing = True
        audio_paused = False
    except Exception as e:
        print("Cannot open:", path, e)
        audio_stop()

def audio_stop():
    """Stop the mixer voice (if playing), release the decoder, and close
    the underlying file handle. Safe to call even if nothing is playing."""
    global audio_file, audio_decoder, audio_playing, audio_paused
    if audio_mixer and audio_mixer.voice[0].playing:
        audio_mixer.voice[0].stop()
    audio_decoder = None
    if audio_file:
        try: audio_file.close()
        except: pass
        audio_file = None
    audio_playing = False
    audio_paused = False

def audio_pause():
    """Pause the I2S output in place (sample position is preserved) and
    mark the paused state."""
    global audio_paused
    if audio_mixer and audio_mixer.voice[0].playing:
        i2s.pause()
    audio_paused = True

def audio_resume():
    """Resume I2S output from where it was paused."""
    global audio_paused
    if audio_paused and audio_mixer:
        i2s.resume()
    audio_paused = False

def audio_check():
    """Return whether audio is still actively playing, updating
    `audio_playing` to False the moment the mixer voice reports it has
    finished (used to detect natural end-of-track for auto-advance)."""
    global audio_playing
    if audio_playing and not audio_paused:
        if audio_mixer and not audio_mixer.voice[0].playing:
            audio_playing = False; return False
    return audio_playing

def start_song(idx, page_offset):
    """Full "switch to song `idx`" sequence: stop current playback, reset
    the visualizer/progress bar, update the now-playing/list UI, load the
    new album art, and begin playback — in that order, so the display
    reflects the new song immediately even though art loading briefly
    blocks the loop."""
    audio_stop()
    reset_visualizer(); reset_progress()
    draw_info(idx); draw_list(idx, idx, page_offset)
    display.refresh()
    load_art_blocking(SONGS[idx][2])
    audio_start(idx)

# ===============================================
# INPUT & TIMING HELPERS
# Buttons only — serial input removed to save RAM
# ===============================================
last_key_time = 0
DEBOUNCE_MS   = const(150)
VIZ_INTERVAL  = const(50)

def ticks_ms(): return time.monotonic_ns() // 1000000

def read_key(_tms):
    """Poll all buttons and return the action code of the first one found
    pressed, respecting a debounce window so a single press isn't read as
    several rapid-fire key events. Returns None if nothing is pressed or
    still within the debounce window."""
    global last_key_time
    now = _tms()
    if now - last_key_time < DEBOUNCE_MS: return None
    for key_char, btn in hardware_buttons.items():
        if not btn.value:
            last_key_time=now; return key_char
    return None

def next_song(current, mode, direction=1):
    """Compute the next song index given the current playback mode:
    stays put in Loop mode, picks a random different track in Shuffle
    mode, or steps sequentially (wrapping) otherwise. `direction` is only
    meaningful for sequential mode (+1 = next, -1 = previous)."""
    if mode == MODE_LOOP: return current
    if mode == MODE_SHUFFLE:
        if NUM_SONGS <= 1: return current
        while True:
            candidate = random.randrange(NUM_SONGS)
            if candidate != current: return candidate
    return (current + direction) % NUM_SONGS

# ===============================================
# MAIN
# ===============================================
def main():
    """Main event loop: polls buttons, updates playback/navigation/menu
    state, redraws only the UI elements that changed, and throttles both
    the display refresh rate and idle CPU usage. Runs until the 'q' action
    is triggered (not reachable from physical buttons; reserved for a
    future/debug exit path)."""
    global HIGHLIGHT_COLOR

    selected      = 0
    page_offset   = 0
    playing_idx   = None
    paused        = False
    mode          = MODE_DEFAULT
    song_finished = False

    prev_sel        = -1
    prev_play       = -2
    prev_page       = -1
    song_start_ms   = 0
    elapsed_ms      = 0
    last_prog_ms    = 0
    last_viz_ms     = 0
    last_refresh_ms = 0
    song_started_ms = 0

    in_menu       = False
    in_about      = False
    menu_selected = 0
    color_idx     = DEFAULT_COLOR_IDX
    cur_vol_int   = DEFAULT_VOL_INT

    print("Ready. Buttons: up/down/play/prev/next/mode")

    refresh = display.refresh
    _ticks_ms = ticks_ms
    _sleep = time.sleep

    while True:
        now = _ticks_ms()
        key = read_key(_ticks_ms)

        if key == 'q':
            audio_stop(); break

        elif in_about:
            if key == 'p' or key == 'm':
                in_about = False; clear_list_cache()
                draw_list(selected, playing_idx, page_offset)

        elif in_menu:
            if key == 'w':
                menu_selected=(menu_selected-1)%len(MENU_ITEMS)
                draw_menu(menu_selected,mode,cur_vol_int,color_idx)
            elif key == 's':
                menu_selected=(menu_selected+1)%len(MENU_ITEMS)
                draw_menu(menu_selected,mode,cur_vol_int,color_idx)
            elif key == 'a':
                item=MENU_ITEMS[menu_selected]
                if item=="Volume":
                    cur_vol_int = max(VOL_MIN_INT, cur_vol_int - VOL_STEP_INT)
                    if audio_mixer: audio_mixer.voice[0].level = cur_vol_int / 100.0
                    draw_menu(menu_selected,mode,cur_vol_int,color_idx)
                elif item=="Play Mode":
                    mode=(mode-1)%3; draw_mode(mode)
                    draw_menu(menu_selected,mode,cur_vol_int,color_idx)
                elif item=="Color":
                    color_idx=(color_idx-1)%len(COLOR_OPTIONS)
                    HIGHLIGHT_COLOR=COLOR_OPTIONS[color_idx][1]
                    apply_highlight_color(HIGHLIGHT_COLOR)
                    draw_menu(menu_selected,mode,cur_vol_int,color_idx)
            elif key == 'd':
                item=MENU_ITEMS[menu_selected]
                if item=="Volume":
                    cur_vol_int = min(VOL_MAX_INT, cur_vol_int + VOL_STEP_INT)
                    if audio_mixer: audio_mixer.voice[0].level = cur_vol_int / 100.0
                    draw_menu(menu_selected,mode,cur_vol_int,color_idx)
                elif item=="Play Mode":
                    mode=(mode+1)%3; draw_mode(mode)
                    draw_menu(menu_selected,mode,cur_vol_int,color_idx)
                elif item=="Color":
                    color_idx=(color_idx+1)%len(COLOR_OPTIONS)
                    HIGHLIGHT_COLOR=COLOR_OPTIONS[color_idx][1]
                    apply_highlight_color(HIGHLIGHT_COLOR)
                    draw_menu(menu_selected,mode,cur_vol_int,color_idx)
            elif key == 'p':
                item = MENU_ITEMS[menu_selected]
                if item == "About":
                    in_menu = False; in_about = True
                    draw_about()
                elif item == "Back":
                    in_menu=False; clear_list_cache()
                    draw_list(selected,playing_idx,page_offset)
            elif key == 'm':
                in_menu=False; clear_list_cache()
                draw_list(selected,playing_idx,page_offset)

        else:
            # Song-list navigation and playback controls only make sense
            # once at least one song has been synced onto the device —
            # guarding on NUM_SONGS here keeps the list/playback logic from
            # ever indexing into the (currently empty) SONGS tuple.
            if key == 'w' and NUM_SONGS > 0:
                row = selected - page_offset
                if row > 0: selected -= 1
                else:
                    if page_offset > 0:
                        page_offset -= LIST_ROWS
                        selected = min(page_offset + LIST_ROWS - 1, NUM_SONGS - 1)
                    else:
                        page_offset = (NUM_PAGES - 1) * LIST_ROWS
                        selected    = NUM_SONGS - 1

            elif key == 's' and NUM_SONGS > 0:
                row = selected - page_offset
                if row < LIST_ROWS - 1 and selected < NUM_SONGS - 1:
                    selected += 1
                else:
                    next_offset = page_offset + LIST_ROWS
                    if next_offset < NUM_SONGS:
                        page_offset = next_offset; selected = page_offset
                    else:
                        page_offset = 0; selected = 0

            elif key == 'p' and NUM_SONGS > 0:
                if playing_idx is None or selected != playing_idx:
                    playing_idx=selected; paused=False; elapsed_ms=0
                    song_finished=False; song_start_ms=now; song_started_ms=now
                    start_song(playing_idx, page_offset)
                else:
                    if paused:
                        song_start_ms = now - elapsed_ms
                        paused=False; audio_resume()
                    else:
                        elapsed_ms = now - song_start_ms
                        paused=True; audio_pause(); stop_visualizer()

            elif key == 'a' and NUM_SONGS > 0:
                playing_idx=next_song(playing_idx if playing_idx is not None else selected,mode,-1)
                selected=playing_idx
                page_offset=(selected//LIST_ROWS)*LIST_ROWS
                paused=False; elapsed_ms=0
                song_finished=False; song_start_ms=now; song_started_ms=now
                start_song(playing_idx, page_offset)

            elif key == 'd' and NUM_SONGS > 0:
                playing_idx=next_song(playing_idx if playing_idx is not None else selected,mode,1)
                selected=playing_idx
                page_offset=(selected//LIST_ROWS)*LIST_ROWS
                paused=False; elapsed_ms=0
                song_finished=False; song_start_ms=now; song_started_ms=now
                start_song(playing_idx, page_offset)

            elif key == 'm':
                # The settings menu is always reachable, even with an
                # empty song list, since volume/mode/color/about don't
                # depend on SONGS.
                in_menu=True; menu_selected=0
                draw_menu(menu_selected,mode,cur_vol_int,color_idx)

        # AUTO ADVANCE
        if (playing_idx is not None and not paused and not audio_paused
                and not song_finished and now - song_started_ms > 3000):
            if not audio_check():
                song_finished=True
                playing_idx=next_song(playing_idx,mode,1)
                selected=playing_idx
                page_offset=(selected//LIST_ROWS)*LIST_ROWS
                elapsed_ms=0; song_finished=False
                song_start_ms=now; song_started_ms=now
                start_song(playing_idx, page_offset); prev_play=-2

        # PROGRESS
        ui_dirty=False
        if playing_idx is not None and not paused and audio_playing:
            if now - last_prog_ms >= 2000:
                draw_progress(now - song_start_ms, SONGS[playing_idx][3])
                last_prog_ms = now; ui_dirty = True

        # VISUALIZER
        if playing_idx is not None and not paused and audio_playing:
            if now - last_viz_ms >= VIZ_INTERVAL:
                update_visualizer(); draw_visualizer(); ui_dirty=True
                last_viz_ms=now
        elif paused:
            if now - last_viz_ms >= VIZ_INTERVAL:
                stop_visualizer(); draw_visualizer(); ui_dirty=True
                last_viz_ms=now

        if not in_menu and not in_about and (selected!=prev_sel or playing_idx!=prev_play or page_offset!=prev_page):
            draw_list(selected,playing_idx,page_offset)
            prev_sel=selected; prev_page=page_offset; ui_dirty=True
        if playing_idx!=prev_play:
            draw_info(playing_idx); prev_play=playing_idx; ui_dirty=True

        if key is not None or ui_dirty:
            if now - last_refresh_ms >= DISPLAY_REFRESH_MS:
                refresh()
                last_refresh_ms = now
        else:
            if now - last_refresh_ms < DISPLAY_REFRESH_MS:
                _sleep(IDLE_LOOP_DELAY_S)

main()