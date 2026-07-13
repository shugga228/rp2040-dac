#!/usr/bin/env python3
"""
DAC Player Companion App
=========================

A desktop GUI utility for syncing a local MP3 library to a CircuitPython-based
DAC music player. The app:

  1. Scans a folder of MP3 files and reads their ID3 metadata (title/artist).
  2. Converts each MP3 to a 44.1kHz/16-bit stereo WAV using ffmpeg, so the
     device's audio pipeline receives a consistent, predictable format.
  3. Extracts embedded album art (if present) and converts it into a raw
     RGB565 pixel buffer sized for the device's display.
  4. Copies the converted WAV/art files onto an SD card in the expected
     `music/` and `album_art/` folder structure.
  5. Rewrites the `SONGS` tuple (and optional default volume/mode/color
     settings) inside `code.py` on the device's CIRCUITPY drive, so the
     firmware knows about the new song list without manual editing.

The GUI itself is built with Tkinter and styled to look like a retro
terminal/CRT interface (dark background, monospace fonts, glowing accent
color, scanline-style static noise, blinking cursor, etc).

Author:   Sami Uddin
GitHub:   https://github.com/shugga228
"""

import os
import io
import re
import sys
import random
import shutil
import hashlib
import threading
import subprocess
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog
from pathlib import Path

# --- Optional third-party dependencies -------------------------------------
# These libraries aren't strictly required for the app to launch, but without
# them certain features degrade gracefully (metadata falls back to filename,
# album art is skipped, etc). We detect availability up front so the rest of
# the code can branch on simple booleans instead of catching ImportError
# everywhere.
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# --- Constants ---------------------------------------------------------------

# Album art is stored on-device as a square image at this resolution (pixels).
ART_SIZE       = 146

# Target WAV format for converted audio: standard CD-quality sample rate,
# stereo output, matching what the device's DAC/amp expects.
WAV_RATE       = 44100
WAV_CHANNELS   = 2

# Available highlight colors on the device, shown in the dropdown and written
# into code.py as an index. The hex values are kept here mainly for reference/
# potential future use (e.g. previewing the color in the UI).
DEVICE_COLORS = (
    ("White",  "0xFFFFFF"),
    ("Cyan",   "0x00FFFF"),
    ("Pink",   "0xFF69B4"),
    ("Green",  "0x00FF00"),
    ("Red",    "0xFF0000"),
    ("Orange", "0xFF8800"),
    ("Purple", "0x9933FF"),
    ("Sage",   "0x9CB89C"),
)
# Playback modes supported by the device firmware.
DEVICE_MODES = ("Default", "Shuffle", "Loop")

# --- UI color palette ---------------------------------------------------------
# Centralized theme colors so the "retro terminal" look stays consistent
# across every widget without repeating hex codes throughout the layout code.
BG        = "#07080a"   # main window background
BG2       = "#0d1013"   # panel background
BG3       = "#151a1e"   # input/control background
BORDER    = "#1f2529"   # subtle borders
LINE_DIM  = "#1a3438"   # divider lines under section labels
TEXT      = "#eaf6f6"   # primary bright text
TEXT_DIM  = "#3f484c"   # de-emphasized text
TEXT_MED  = "#8a9ea3"   # medium-emphasis text (labels, log lines)
SUCCESS   = "#4ade80"   # green - success states
WARNING   = "#facc15"   # yellow - warnings
ERROR     = "#f87171"   # red - errors
CYAN      = "#9CB89C"   # accent color (despite the name, tuned to a sage tone)
CYAN_DIM  = "#4A5F4A"   # dimmed accent (hover/active states)
CYAN_GLOW = "#22301F"   # soft glow halo behind status dots
MONO      = ("Consolas", 9)
MONO_B    = ("Consolas", 9, "bold")


# --- Helper / utility functions ----------------------------------------------

def clean_name(title):
    """Turn a song title into a filesystem-safe, lowercase, underscore-joined
    base name (e.g. "My Song - Live!" -> "my_song_live"). Used to derive
    consistent .wav/.raw filenames from arbitrary ID3 title strings."""
    name = title.strip().lower()
    name = re.sub(r"[^\w\s\-]", "", name)
    name = re.sub(r"[\s\-]+", "_", name)
    return name.strip("_") or "unknown"


def file_md5(path):
    """Compute the MD5 checksum of a file, reading it in chunks to avoid
    loading large files entirely into memory. Currently unused by the sync
    flow but kept available for future duplicate-detection/caching logic."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def find_drive(label):
    """Search all mounted Windows drive letters for one whose volume label
    (or drive letter itself) matches the given label. Returns the drive path
    (e.g. "E:\\") or None if not found. Windows-only; returns None elsewhere."""
    if sys.platform != "win32":
        return None
    import ctypes
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    # GetLogicalDrives returns a bitmask where bit i corresponds to drive
    # letter chr(65 + i), i.e. bit 0 = "A:", bit 1 = "B:", etc.
    for i in range(26):
        if bitmask & (1 << i):
            drives.append(chr(65 + i) + ":\\")
    for drive in drives:
        try:
            vol_buf  = ctypes.create_unicode_buffer(261)
            ctypes.windll.kernel32.GetVolumeInformationW(
                drive, vol_buf, 261, None, None, None, None, 0)
            drive_vol_name = vol_buf.value.strip().upper()
            target_label = label.strip().upper()
            if target_label in drive_vol_name or target_label in drive.upper():
                return drive
        except Exception:
            # Some drives (e.g. empty optical drives) raise on query; skip them.
            pass
    return None


def find_circuitpy():
    """Locate the CIRCUITPY drive exposed by the device when it's plugged in
    via USB (CircuitPython boards mount themselves under this volume label)."""
    return find_drive("CIRCUITPY")


def find_sd_by_contents():
    """Fallback SD-card detection: instead of relying on a volume label
    (which varies by card/reader), look for a drive whose root contains both
    a `music` and `album_art` folder — the exact layout this app expects."""
    if sys.platform != "win32":
        return None
    import ctypes
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask & (1 << i)):
            continue
        drive = chr(65 + i) + ":\\"
        try:
            entries = {name.lower() for name in os.listdir(drive)}
        except Exception:
            continue
        if "music" in entries and "album_art" in entries:
            return drive
    return None


def find_sd_drive():
    """Best-effort SD card discovery: first try matching by folder contents
    (most reliable), then fall back to a list of common volume labels."""
    d = find_sd_by_contents()
    if d:
        return d
    search_labels = ["SD", "SDCARD", "SD_CARD", "USB DRIVE", "F:"]
    for label in search_labels:
        d = find_drive(label)
        if d:
            return d
    return None


def get_metadata(path):
    """Read title/artist/duration from an MP3's ID3 tags. Falls back to the
    filename (and "Unknown" artist / 0ms duration) if mutagen is unavailable
    or the tags can't be parsed, so a bad/missing tag never crashes the sync."""
    if not HAS_MUTAGEN:
        return Path(path).stem, "Unknown", 0
    try:
        audio  = MP3(path, ID3=ID3)
        duration_ms = int(round(audio.info.length * 1000))
        tags   = audio.tags
        if not tags:
            return Path(path).stem, "Unknown", duration_ms
        title  = str(tags.get("TIT2", Path(path).stem)).strip()
        artist = str(tags.get("TPE1", "Unknown")).strip()
        return title, artist, duration_ms
    except Exception:
        return Path(path).stem, "Unknown", 0


def get_album_art(path):
    """Extract the embedded cover art image (APIC frame) from an MP3's ID3
    tags, returning a PIL Image, or None if there's no art or a dependency
    (mutagen/Pillow) is missing."""
    if not HAS_MUTAGEN or not HAS_PIL:
        return None
    try:
        audio = MP3(path, ID3=ID3)
        tags  = audio.tags
        if not tags:
            return None
        for tag in tags.values():
            if hasattr(tag, "FrameID") and tag.FrameID == "APIC":
                return Image.open(io.BytesIO(tag.data))
    except Exception:
        pass
    return None


def convert_to_wav(mp3_path, wav_path, log_fn):
    """Convert an MP3 file to a WAV file using ffmpeg as an external process,
    forcing the sample rate/channel count/bit depth the device expects.
    Returns True on success; logs and returns False on any failure (missing
    ffmpeg binary, non-zero exit code, or timeout)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path),
             "-ar", str(WAV_RATE), "-ac", str(WAV_CHANNELS),
             "-sample_fmt", "s16", str(wav_path)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return True
        # Only the tail of stderr is logged, since ffmpeg's output can be very
        # long and the most relevant error info is usually at the end.
        log_fn(f"ffmpeg error: {result.stderr[-300:]}", "error")
        return False
    except FileNotFoundError:
        log_fn("ffmpeg not found — install ffmpeg and add to PATH", "error")
        return False
    except subprocess.TimeoutExpired:
        log_fn("ffmpeg timed out", "error")
        return False
    except Exception as e:
        log_fn(f"Conversion error: {e}", "error")
        return False


def convert_art_to_raw(image, output_path, log_fn):
    """Crop the given image to a centered square, resize it to the device's
    display resolution (ART_SIZE x ART_SIZE), and write it out as a raw
    big-endian RGB565 pixel buffer — the pixel format the device's display
    driver reads directly, with no header/metadata."""
    try:
        w, h    = image.size
        m       = min(w, h)
        # Center-crop to a square using the smaller dimension.
        img     = image.crop(((w-m)//2, (h-m)//2, (w+m)//2, (h+m)//2))
        img     = img.resize((ART_SIZE, ART_SIZE), Image.Resampling.LANCZOS)
        img     = img.convert("RGB")
        pixels  = img.tobytes()
        with open(output_path, "wb") as f:
            # Pack each 8-bit RGB triplet into a 16-bit RGB565 value:
            # 5 bits red, 6 bits green, 5 bits blue.
            for i in range(0, len(pixels), 3):
                r, g, b = pixels[i], pixels[i+1], pixels[i+2]
                rgb565  = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                f.write(rgb565.to_bytes(2, "big"))
        return True
    except Exception as e:
        log_fn(f"Art conversion error: {e}", "error")
        return False


def build_songs_block(songs):
    """Render the list of (title, artist, base_filename, duration) tuples as
    a Python source-code literal — the exact `SONGS = (...)` block that gets
    spliced into the device's code.py."""
    lines = ["SONGS = (\n"]
    for title, artist, base, duration in songs:
        # Escape any embedded double quotes so the generated code stays valid.
        t = title.replace('"', '\\"')
        a = artist.replace('"', '\\"')
        b = base.replace('"', '\\"')
        lines.append(f'    ("{t}", "{a}", "{b}", {duration}),\n')
    lines.append(")\n")
    return "".join(lines)


def update_code_py(circuitpy_drive, songs, log_fn):
    """Replace the existing `SONGS = (...)` block inside code.py on the
    CIRCUITPY drive with a freshly generated one matching the current song
    list. Uses a regex match spanning the whole tuple literal, so the block
    can safely be regenerated regardless of its previous contents/length."""
    code_path = Path(circuitpy_drive) / "code.py"
    if not code_path.exists():
        log_fn("code.py not found on CIRCUITPY drive", "error")
        return False
    try:
        content     = code_path.read_text(encoding="utf-8")
        new_block   = build_songs_block(songs)
        new_content = re.sub(
            r"SONGS\s*=\s*\(.*?\)\n",
            new_block,
            content,
            flags=re.DOTALL
        )
        if new_content == content:
            # The regex found nothing to replace, meaning code.py doesn't
            # contain the expected SONGS block — don't silently write a
            # no-op file, just warn so the user can investigate.
            log_fn("SONGS block not found in code.py — skipping update", "warning")
            return False
        code_path.write_text(new_content, encoding="utf-8")
        log_fn(f"code.py updated with {len(songs)} songs", "success")
        return True
    except Exception as e:
        log_fn(f"Failed to update code.py: {e}", "error")
        return False


def update_device_setting(circuitpy_drive, setting_name, value, log_fn):
    """Patch a single device setting constant (volume, default mode, or
    default highlight color) directly inside code.py using a targeted regex
    per setting. Each setting corresponds to a specific `const(...)`
    declaration that the firmware reads at boot."""
    code_path = Path(circuitpy_drive) / "code.py"
    if not code_path.exists():
        log_fn("code.py not found on CIRCUITPY drive", "error")
        return False
    try:
        content = code_path.read_text(encoding="utf-8")
        if setting_name == "volume":
            # Volume is stored on-device as an integer percentage (0-100),
            # while the UI works in a 0.0-1.0 float range.
            vol_int = int(round(float(value) * 100))
            pattern = r"DEFAULT_VOL_INT\s*=\s*const\(\s*[0-9]+\s*\)"
            replacement = f"DEFAULT_VOL_INT = const({vol_int})"
        elif setting_name == "mode":
            pattern = r"(MODE_DEFAULT\s*=\s*const\()([0-9]+)(\))"
            replacement = f"\\g<1>{value}\\g<3>"
        elif setting_name == "color":
            pattern = r"DEFAULT_COLOR_IDX\s*=\s*const\(\s*[0-9]+\s*\)"
            replacement = f"DEFAULT_COLOR_IDX = const({value})"
        else:
            log_fn(f"Unknown setting: {setting_name}", "error")
            return False
        new_content = re.sub(pattern, replacement, content)
        if new_content == content:
            log_fn(f"Setting '{setting_name}' pattern not found in code.py", "warning")
            return False
        code_path.write_text(new_content, encoding="utf-8")
        log_fn(f"Updated {setting_name} in code.py", "success")
        return True
    except Exception as e:
        log_fn(f"Failed to update {setting_name}: {e}", "error")
        return False


# --- Core sync workflow -------------------------------------------------------

def sync(music_folder, circuitpy_drive, sd_drive, log_fn, progress_fn, done_fn,
         default_volume=0.05, default_mode=0, default_color=0):
    """Full sync pipeline: convert every MP3 in `music_folder` to WAV +
    RGB565 album art, copy both onto the SD card, then rewrite the SONGS
    list (and any non-default settings) in code.py on the CIRCUITPY drive.

    Designed to be run on a background thread — `log_fn`, `progress_fn`, and
    `done_fn` are callbacks used to report status back to the GUI thread
    rather than touching Tkinter widgets directly from this thread.
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        mp3s  = sorted(Path(music_folder).glob("*.mp3"))
        total = len(mp3s)
        if not mp3s:
            log_fn("No MP3 files found in selected folder.", "warning")
            done_fn([])
            return
        log_fn(f"Found {total} MP3 file(s).", "info")
        music_dir = Path(sd_drive) / "music"
        art_dir   = Path(sd_drive) / "album_art"
        music_dir.mkdir(exist_ok=True)
        art_dir.mkdir(exist_ok=True)
        songs = []
        for i, mp3 in enumerate(mp3s):
            progress_fn(i, total, f"Processing: {mp3.name}")
            title, artist, duration = get_metadata(str(mp3))
            base     = clean_name(title)
            wav_name = base + ".wav"
            raw_name = base + ".raw"
            log_fn(f"[{i+1}/{total}] {title} — {artist} ({duration}ms)", "info")

            # --- Audio conversion + copy to SD card ---
            progress_fn(i, total, f"Converting: {mp3.name} -> {wav_name}")
            wav_tmp = Path(tmp_dir) / wav_name
            wav_ok  = convert_to_wav(str(mp3), str(wav_tmp), log_fn)
            if wav_ok:
                dest_wav = music_dir / wav_name
                progress_fn(i, total, f"Copying: {wav_name}")
                try:
                    shutil.copy2(str(wav_tmp), str(dest_wav))
                    log_fn(f"  OK WAV: {wav_name} ({dest_wav.stat().st_size // 1024} KB)", "success")
                except Exception as e:
                    log_fn(f"  FAIL Copy: {e}", "error")
            else:
                log_fn(f"  FAIL Conversion for {mp3.name}", "error")

            # --- Album art conversion + copy to SD card (best-effort) ---
            if HAS_PIL:
                img = get_album_art(str(mp3))
                if img:
                    raw_tmp  = Path(tmp_dir) / raw_name
                    dest_raw = art_dir / raw_name
                    if convert_art_to_raw(img, str(raw_tmp), log_fn):
                        try:
                            shutil.copy2(str(raw_tmp), str(dest_raw))
                            log_fn(f"  OK Art: {raw_name}", "success")
                        except Exception as e:
                            log_fn(f"  FAIL Art copy: {e}", "error")
                    else:
                        log_fn(f"  FAIL Art conversion", "error")
                else:
                    log_fn(f"  No embedded art in {mp3.name}", "warning")
            else:
                log_fn("  Pillow not installed — skipping art", "warning")

            songs.append((title, artist, base, duration))

        # --- Write the updated song list + any changed settings to code.py ---
        progress_fn(total, total, "Updating code.py...")
        update_code_py(circuitpy_drive, songs, log_fn)
        # Only write settings that differ from the firmware defaults, to
        # avoid touching code.py more than necessary.
        if default_volume != 0.05:
            update_device_setting(circuitpy_drive, "volume", f"{default_volume:.2f}", log_fn)
        if default_mode != 0:
            update_device_setting(circuitpy_drive, "mode", str(default_mode), log_fn)
        if default_color != 0:
            update_device_setting(circuitpy_drive, "color", str(default_color), log_fn)

        progress_fn(total, total, "Done!")
        log_fn(f"Sync complete — {len(songs)} song(s).", "success")
        done_fn(songs)
    except Exception as e:
        import traceback
        log_fn(f"Sync error: {e}\n{traceback.format_exc()}", "error")
        done_fn([])
    finally:
        # Always clean up temp conversion files, even if the sync failed partway.
        shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_settings_only(circuitpy_drive, log_fn, progress_fn, done_fn,
                        volume, mode_idx, color_idx):
    """Lightweight variant of `sync()` that only pushes the volume/mode/color
    settings to code.py, leaving the song list and SD card contents
    untouched. Useful for quickly tweaking device behavior without
    re-running the (much slower) full audio conversion pipeline."""
    try:
        log_fn("-" * 52, "info")
        log_fn("Applying settings only (songs untouched)...", "info")
        progress_fn(0, 3, "Writing volume...")
        update_device_setting(circuitpy_drive, "volume", f"{volume:.2f}", log_fn)
        progress_fn(1, 3, "Writing default mode...")
        update_device_setting(circuitpy_drive, "mode", str(mode_idx), log_fn)
        progress_fn(2, 3, "Writing highlight color...")
        update_device_setting(circuitpy_drive, "color", str(color_idx), log_fn)
        progress_fn(3, 3, "Done!")
        log_fn("Settings updated — no songs were changed.", "success")
        done_fn([])
    except Exception as e:
        import traceback
        log_fn(f"Settings update error: {e}\n{traceback.format_exc()}", "error")
        done_fn([])


# --- Custom themed Tkinter widgets -------------------------------------------

class GlowDot(tk.Canvas):
    """A small circular status indicator (red/green dot with a soft glow
    halo behind it) used to show whether a drive has been detected."""

    def __init__(self, parent, color=ERROR, **kw):
        super().__init__(parent, width=18, height=18, bg=BG2,
                          highlightthickness=0, **kw)
        self._halo = self.create_oval(2, 2, 16, 16, fill=CYAN_GLOW, outline="")
        self._dot  = self.create_oval(6, 6, 12, 12, fill=color, outline="")
        self.set_color(color)

    def set_color(self, color):
        """Update both the dot and its halo. The error-state halo uses a
        dedicated dark red so it doesn't clash with the default accent glow."""
        halo = color if color != ERROR else "#3a1414"
        self.itemconfig(self._halo, fill=halo)
        self.itemconfig(self._dot, fill=color)


class LedBar(tk.Canvas):
    """A segmented progress bar styled like an LED VU meter, drawn as a row
    of rectangles that light up (accent color) proportionally to progress."""

    SEGMENTS = 42

    def __init__(self, parent, **kw):
        super().__init__(parent, height=14, bg=BG2, highlightthickness=0, **kw)
        # Redraw whenever the canvas is resized, since segment widths are
        # computed from the current canvas width.
        self.bind("<Configure>", lambda e: self.draw(self._ratio))
        self._ratio = 0.0

    def set_ratio(self, ratio):
        """Set the fraction of segments to light up (0.0 - 1.0)."""
        self._ratio = max(0.0, min(1.0, ratio))
        self.draw(self._ratio)

    def draw(self, ratio):
        self.delete("all")
        w = self.winfo_width() or 600
        n = self.SEGMENTS
        gap = 3
        seg_w = (w - gap * (n - 1)) / n
        lit = int(round(ratio * n))
        for i in range(n):
            x0 = i * (seg_w + gap)
            x1 = x0 + seg_w
            on = i < lit
            color = CYAN if on else BG3
            self.create_rectangle(x0, 1, x1, 13, fill=color, outline="")


class StaticNoise(tk.Canvas):
    """Purely decorative canvas that continuously redraws random dim pixels
    to mimic old-CRT static/scanline noise beneath the app header."""

    def __init__(self, parent, height=26, **kw):
        super().__init__(parent, height=height, bg=BG, highlightthickness=0, **kw)
        self._running = True
        # Stop the animation loop once the widget is destroyed, so it doesn't
        # keep scheduling `after` callbacks against a dead widget.
        self.bind("<Destroy>", lambda e: setattr(self, "_running", False))
        self.after(50, self._tick)

    def _tick(self):
        if not self._running:
            return
        self.delete("all")
        w = self.winfo_width() or 700
        h = int(self["height"])
        for _ in range(60):
            x = random.randint(0, w)
            y = random.randint(0, h)
            size = random.choice((1, 1, 1, 2))
            shade = random.choice([TEXT_DIM, BORDER, LINE_DIM, "#0f1518"])
            self.create_rectangle(x, y, x + size, y + size, fill=shade, outline="")
        if self._running:
            self.after(220, self._tick)


def section_label(parent, text):
    """Small helper to render a section header row: a triangle marker,
    label text, and a horizontal divider line filling the remaining space."""
    row = tk.Frame(parent, bg=BG2)
    row.pack(fill="x")
    tk.Label(row, text="\u25b8", font=MONO_B, fg=CYAN, bg=BG2).pack(side="left")
    tk.Label(row, text=f" {text}", font=MONO, fg=TEXT_DIM, bg=BG2).pack(side="left")
    line = tk.Frame(row, bg=LINE_DIM, height=1)
    line.pack(side="left", fill="x", expand=True, padx=(8, 0), pady=(2, 0))
    return row


def flat_button(parent, text, command, primary=False):
    """Create a flat, borderless button matching the retro theme. `primary`
    controls whether it uses the bright accent-filled style (for the main
    call-to-action) or the darker outline style (for secondary actions),
    and wires up hover-highlight behavior that's skipped while disabled."""
    fg = BG if primary else CYAN
    bg = CYAN if primary else BG3
    active_bg = "#B9D2B9" if primary else CYAN_DIM
    active_fg = BG if primary else TEXT
    btn = tk.Button(
        parent, text=text, font=MONO_B, fg=fg, bg=bg,
        relief="flat", bd=0, cursor="hand2",
        activebackground=active_bg, activeforeground=active_fg,
        highlightthickness=1, highlightbackground=CYAN_DIM if not primary else bg,
        pady=10, command=command
    )

    def on_enter(_):
        if btn["state"] == "disabled":
            return
        btn.config(bg=active_bg, fg=active_fg)

    def on_leave(_):
        if btn["state"] == "disabled":
            return
        btn.config(bg=bg, fg=fg)

    btn.bind("<Enter>", on_enter)
    btn.bind("<Leave>", on_leave)
    return btn


# --- Main application ----------------------------------------------------------

class App:
    """Top-level Tkinter application: builds the full window layout, wires
    up drive detection, and kicks off sync operations on background threads
    so the GUI stays responsive during long-running conversions."""

    def __init__(self, root):
        self.root           = root
        self.music_folder   = tk.StringVar(value="")
        self.circuitpy_var  = tk.StringVar(value="Not found")
        self.sd_var         = tk.StringVar(value="Not found")
        self.volume_var     = tk.DoubleVar(value=0.05)
        self.mode_var       = tk.StringVar(value="Default")
        self.color_var      = tk.StringVar(value="White")
        self.syncing        = False
        self._circuitpy     = None
        self._sd            = None
        self._cursor_on     = True

        root.title("DAC Player Companion")
        root.configure(bg=BG)
        root.resizable(True, True)

        self._init_ttk_style()
        self._build()

        # Fixed generous default size instead of relying purely on
        # winfo_reqwidth/height, which can under-report before every
        # widget (especially the bottom button row) has been mapped.
        root.update_idletasks()
        req_w = max(root.winfo_reqwidth(), 620)
        req_h = max(root.winfo_reqheight() + 40, 780)
        root.geometry(f"{req_w}x{req_h}")
        root.minsize(620, 700)

        self._scan_drives()
        self._blink_cursor()

    def _init_ttk_style(self):
        """Configure ttk widget styling (combobox colors/fonts) to match the
        rest of the hand-themed Tkinter widgets, since ttk doesn't expose
        the same simple bg/fg options as classic Tkinter widgets."""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Retro.TCombobox",
                         fieldbackground=BG3, background=BG3,
                         foreground=TEXT_MED, arrowcolor=CYAN,
                         bordercolor=BORDER, lightcolor=BG3, darkcolor=BG3,
                         padding=6)
        style.map("Retro.TCombobox",
                  fieldbackground=[("readonly", BG3)],
                  foreground=[("readonly", TEXT_MED)])
        self.root.option_add("*TCombobox*Listbox.background", BG3)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT_MED)
        self.root.option_add("*TCombobox*Listbox.selectBackground", CYAN_DIM)
        self.root.option_add("*TCombobox*Listbox.font", MONO)

    def _build(self):
        """Construct the full widget tree: a fixed bottom action-button row,
        with a scrollable-feeling stack of panels (drive selectors, folder
        picker, device settings, progress bar, and log) filling the rest."""
        r = self.root

        # Use a single outer container with the button row packed at the
        # BOTTOM first, and the scrollable content filling the remaining
        # space above it. This guarantees the buttons are always visible
        # regardless of how much content is above them.
        outer = tk.Frame(r, bg=BG)
        outer.pack(fill="both", expand=True)

        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(side="bottom", fill="x", padx=24, pady=(10, 20))

        self.settings_btn = flat_button(
            btn_row, "SETTINGS ONLY", self._start_settings_only, primary=False)
        self.settings_btn.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=4)

        self.sync_btn = flat_button(
            btn_row, "FULL SYNC", self._start_full_sync, primary=True)
        self.sync_btn.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=4)

        content = tk.Frame(outer, bg=BG)
        content.pack(side="top", fill="both", expand=True)

        # --- Header: title, blinking cursor, and decorative static noise ---
        hdr = tk.Frame(content, bg=BG, pady=16)
        hdr.pack(fill="x", padx=24)
        title_row = tk.Frame(hdr, bg=BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text="//", font=MONO_B, fg=CYAN_DIM, bg=BG).pack(side="left")
        tk.Label(title_row, text=" DAC", font=("Consolas", 26, "bold"),
                 fg=CYAN, bg=BG).pack(side="left")
        tk.Label(title_row, text="_PLAYER", font=("Consolas", 26),
                 fg=TEXT, bg=BG).pack(side="left")
        self.cursor_lbl = tk.Label(title_row, text="\u2588", font=("Consolas", 20),
                                    fg=CYAN, bg=BG)
        self.cursor_lbl.pack(side="left", padx=(2, 0))
        tk.Label(title_row, text="  companion.exe", font=MONO,
                 fg=TEXT_DIM, bg=BG).pack(side="left", pady=(10, 0))

        StaticNoise(hdr, height=20).pack(fill="x", pady=(10, 0))

        # --- Main panels, in top-to-bottom order ---
        self._panel(content, "CIRCUITPY DRIVE // code.py", self._build_circuitpy)
        self._panel(content, "SD CARD DRIVE // music + album_art", self._build_sd)
        self._panel(content, "MUSIC FOLDER // source MP3s", self._build_folder)
        self._panel(content, "DEVICE SETTINGS", self._build_settings)
        self._panel(content, "PROGRESS", self._build_progress)
        self._panel(content, "LOG", self._build_log, expand=True)

        # --- Startup dependency checks, reported into the log panel ---
        if not HAS_MUTAGEN:
            self._log("mutagen not installed — pip install mutagen", "warning")
        if not HAS_PIL:
            self._log("Pillow not installed — pip install Pillow", "warning")
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=3)
            self._log("ffmpeg found OK", "success")
        except FileNotFoundError:
            self._log("ffmpeg not found — install ffmpeg and add to PATH", "warning")

    def _blink_cursor(self):
        """Toggle the header's block cursor visibility on a timer to
        simulate a blinking terminal caret."""
        self._cursor_on = not self._cursor_on
        self.cursor_lbl.config(fg=CYAN if self._cursor_on else BG)
        self.root.after(560, self._blink_cursor)

    def _panel(self, parent, title, builder_fn, expand=False):
        """Create a titled panel section (divider line, header label, and a
        body frame) and delegate populating its contents to `builder_fn`."""
        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="both" if expand else "x", expand=expand, padx=24, pady=(0, 10))
        top_line = tk.Frame(outer, bg=LINE_DIM, height=1)
        top_line.pack(fill="x")
        frame = tk.Frame(outer, bg=BG2, pady=10, padx=14)
        frame.pack(fill="both" if expand else "x", expand=expand)
        section_label(frame, title)
        body = tk.Frame(frame, bg=BG2)
        body.pack(fill="both" if expand else "x", expand=expand)
        builder_fn(body)

    def _drive_row(self, parent, var, dot_attr, browse_cmd, scan_cmd):
        """Shared layout for a drive-path row: status dot, path label, and
        Scan/Browse buttons. Used for both the CIRCUITPY and SD card rows."""
        row = tk.Frame(parent, bg=BG2)
        row.pack(fill="x", pady=(6, 0))
        dot = GlowDot(row, color=ERROR)
        dot.pack(side="left", padx=(0, 8))
        setattr(self, dot_attr, dot)
        tk.Label(row, textvariable=var, font=MONO,
                 fg=TEXT_MED, bg=BG2, anchor="w").pack(side="left", fill="x", expand=True)
        flat_button(row, "SCAN", scan_cmd).pack(side="right", padx=(6, 0), ipadx=2, ipady=0)
        flat_button(row, "BROWSE", browse_cmd).pack(side="right", ipadx=2, ipady=0)

    def _build_circuitpy(self, f):
        self._drive_row(f, self.circuitpy_var, "cp_dot",
                        self._browse_circuitpy, self._scan_drives)

    def _build_sd(self, f):
        self._drive_row(f, self.sd_var, "sd_dot",
                        self._browse_sd, self._scan_drives)

    def _build_folder(self, f):
        row = tk.Frame(f, bg=BG2)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, textvariable=self.music_folder,
                 font=MONO, fg=TEXT_MED, bg=BG2,
                 width=48, anchor="w").pack(side="left", fill="x", expand=True)
        flat_button(row, "BROWSE", self._browse_music).pack(side="right", ipadx=2, ipady=0)

    def _build_settings(self, f):
        """Volume slider, default-mode dropdown, and highlight-color
        dropdown — the three settings pushed to code.py."""
        vol_row = tk.Frame(f, bg=BG2)
        vol_row.pack(fill="x", pady=(8, 0))
        tk.Label(vol_row, text="VOLUME", font=MONO,
                 fg=TEXT_DIM, bg=BG2, width=14, anchor="w").pack(side="left")
        vol_scale = tk.Scale(vol_row, from_=0.0, to=0.2, resolution=0.01,
                             orient=tk.HORIZONTAL, variable=self.volume_var,
                             bg=BG2, fg=CYAN, troughcolor=BG3,
                             activebackground=CYAN, highlightthickness=0,
                             bd=0, sliderrelief="flat", showvalue=False)
        vol_scale.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tk.Label(vol_row, textvariable=self.volume_var, font=MONO,
                 fg=CYAN, bg=BG2, width=5, anchor="e").pack(side="left")

        mode_row = tk.Frame(f, bg=BG2)
        mode_row.pack(fill="x", pady=(10, 0))
        tk.Label(mode_row, text="DEFAULT MODE", font=MONO,
                 fg=TEXT_DIM, bg=BG2, width=14, anchor="w").pack(side="left")
        mode_menu = ttk.Combobox(mode_row, textvariable=self.mode_var,
                                  values=DEVICE_MODES, state="readonly",
                                  style="Retro.TCombobox", font=MONO)
        mode_menu.pack(side="left", fill="x", expand=True)

        color_row = tk.Frame(f, bg=BG2)
        color_row.pack(fill="x", pady=(10, 0))
        tk.Label(color_row, text="HIGHLIGHT COLOR", font=MONO,
                 fg=TEXT_DIM, bg=BG2, width=14, anchor="w").pack(side="left")
        color_names = [c[0] for c in DEVICE_COLORS]
        color_menu = ttk.Combobox(color_row, textvariable=self.color_var,
                                   values=color_names, state="readonly",
                                   style="Retro.TCombobox", font=MONO)
        color_menu.pack(side="left", fill="x", expand=True)

    def _build_progress(self, f):
        self.prog_label = tk.Label(f, text="Ready.",
                                   font=MONO, fg=TEXT_MED, bg=BG2)
        self.prog_label.pack(anchor="w", pady=(6, 8))
        self.prog_bar = LedBar(f)
        self.prog_bar.pack(fill="x")

    def _build_log(self, f):
        self.log_box = tk.Text(
            f, font=MONO, fg=TEXT_MED, bg=BG2,
            relief="flat", state="disabled", height=8,
            insertbackground=CYAN, selectbackground=CYAN_DIM,
            highlightthickness=0, bd=0)
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))
        for tag, color in [("info", TEXT_MED), ("success", SUCCESS),
                           ("warning", WARNING), ("error", ERROR)]:
            self.log_box.tag_config(tag, foreground=color)

    def _scan_drives(self):
        """Attempt to auto-detect both the CIRCUITPY and SD card drives, and
        update the UI/log with the result. Can be re-triggered manually via
        each row's SCAN button (e.g. after plugging in a device)."""
        cp = find_circuitpy()
        sd = find_sd_drive()
        self._set_circuitpy(cp)
        self._set_sd(sd)
        if cp:
            self._log(f"Auto-detected CIRCUITPY at {cp}", "success")
        else:
            self._log("CIRCUITPY not found — plug in Feather or click Browse", "warning")
        if sd:
            self._log(f"Auto-detected SD Card at {sd}", "success")
        else:
            self._log("SD drive not found — use Browse to select it manually", "warning")

    def _set_circuitpy(self, path):
        """Update the stored CIRCUITPY path, its display label, and the
        status dot color together, so they never fall out of sync."""
        if path:
            self._circuitpy = path
            self.circuitpy_var.set(path)
            self.cp_dot.set_color(SUCCESS)
        else:
            self._circuitpy = None
            self.circuitpy_var.set("Not found")
            self.cp_dot.set_color(ERROR)

    def _set_sd(self, path):
        if path:
            self._sd = path
            self.sd_var.set(path)
            self.sd_dot.set_color(SUCCESS)
        else:
            self._sd = None
            self.sd_var.set("Not found")
            self.sd_dot.set_color(ERROR)

    def _browse_circuitpy(self):
        d = filedialog.askdirectory(title="Select CIRCUITPY drive root")
        if d:
            self._set_circuitpy(d)
            self._log(f"CIRCUITPY set to: {d}", "info")

    def _browse_sd(self):
        d = filedialog.askdirectory(title="Select SD card drive root")
        if d:
            self._set_sd(d)
            self._log(f"SD drive set to: {d}", "info")

    def _browse_music(self):
        d = filedialog.askdirectory(title="Select folder containing MP3 files")
        if d:
            self.music_folder.set(d)
            count = len(list(Path(d).glob("*.mp3")))
            self._log(f"Music folder: {d}  ({count} MP3s)", "info")

    def _current_settings(self):
        """Read the current volume/mode/color selections out of their
        Tkinter variables and convert them into the plain values (float,
        mode index, color index) that the sync functions expect."""
        volume = round(self.volume_var.get(), 2)
        mode_idx = DEVICE_MODES.index(self.mode_var.get())
        color_idx = next(i for i, c in enumerate(DEVICE_COLORS) if c[0] == self.color_var.get())
        return volume, mode_idx, color_idx

    def _set_buttons_busy(self, busy, label=""):
        """Enable/disable both action buttons and swap their colors to a
        muted "disabled" look while a sync is in progress, preventing the
        user from starting an overlapping second sync."""
        state = "disabled" if busy else "normal"
        self.sync_btn.config(state=state)
        self.settings_btn.config(state=state)
        if busy:
            self.sync_btn.config(bg=BORDER, fg=TEXT_DIM)
            self.settings_btn.config(bg=BORDER, fg=TEXT_DIM)
        else:
            self.sync_btn.config(bg=CYAN, fg=BG)
            self.settings_btn.config(bg=BG3, fg=CYAN)

    def _start_full_sync(self):
        """Validate that a music folder, CIRCUITPY drive, and SD drive are
        all selected, then launch the full sync pipeline on a background
        thread so the GUI event loop keeps running during conversion."""
        if self.syncing:
            return
        music = self.music_folder.get()
        cp    = self._circuitpy
        sd    = self._sd

        errors = []
        if not music or not os.path.isdir(music):
            errors.append("No music folder selected")
        if not cp:
            errors.append("CIRCUITPY drive not found")
        if not sd:
            errors.append("SD card drive not found")
        if errors:
            for e in errors:
                self._log(e, "error")
            return

        self.syncing = True
        self._set_buttons_busy(True)
        self._log("-" * 52, "info")
        self._log("Starting full sync (songs + settings)...", "info")

        volume, mode_idx, color_idx = self._current_settings()

        threading.Thread(
            target=sync,
            args=(music, cp, sd, self._log_safe, self._prog_safe, self._done_safe,
                  volume, mode_idx, color_idx),
            daemon=True
        ).start()

    def _start_settings_only(self):
        """Push just the current volume/mode/color settings to code.py,
        without touching the song list or SD card contents."""
        if self.syncing:
            return
        cp = self._circuitpy
        if not cp:
            self._log("CIRCUITPY drive not found — settings need code.py", "error")
            return

        self.syncing = True
        self._set_buttons_busy(True)

        volume, mode_idx, color_idx = self._current_settings()

        threading.Thread(
            target=sync_settings_only,
            args=(cp, self._log_safe, self._prog_safe, self._done_safe,
                  volume, mode_idx, color_idx),
            daemon=True
        ).start()

    def _log(self, msg, level="info"):
        """Append a line to the log text box, colored according to `level`.
        Must only be called from the main/GUI thread — see `_log_safe`."""
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n", level)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _log_safe(self, msg, level="info"):
        """Thread-safe wrapper around `_log`: schedules the update on the
        Tkinter main loop instead of touching widgets from a worker thread."""
        self.root.after(0, lambda: self._log(msg, level))

    def _set_prog(self, current, total, text):
        self.prog_label.config(text=text)
        ratio = (current / total) if total > 0 else 0
        self.prog_bar.set_ratio(ratio)

    def _prog_safe(self, current, total, text):
        """Thread-safe wrapper around `_set_prog`, used from the sync worker
        thread to report progress back to the GUI."""
        self.root.after(0, lambda: self._set_prog(current, total, text))

    def _done(self, songs):
        """Reset the busy state and show a final summary once a sync
        (full or settings-only) has finished."""
        self.syncing = False
        self._set_buttons_busy(False)
        label = f"Done — {len(songs)} song(s) synced" if songs else "Done."
        self._set_prog(1, 1, label)
        self._log("-" * 52, "info")

    def _done_safe(self, songs):
        """Thread-safe wrapper around `_done`."""
        self.root.after(0, lambda: self._done(songs))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()