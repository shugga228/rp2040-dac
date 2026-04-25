"""
Music Player Generator for RP2040 devices (Pico, Pico W, etc)
Scans music folder, extracts metadata & album art,
converts to RAW format, and generates updated main.py
"""

import os
import sys
from pathlib import Path
from mutagen.mp3 import MP3
from mutagen.id3 import ID3
from PIL import Image
import io

MUSIC_DIR = "music"
ART_OUTPUT_DIR = "album_art"
OUTPUT_SCRIPT = "main.py"

os.makedirs(ART_OUTPUT_DIR, exist_ok=True)

ART_SIZE = 146  # Must match converter

# -----------------------------------------------
# Clean filename for raw file
# -----------------------------------------------
def clean_filename(text):
    """Convert to lowercase, replace spaces with underscores"""
    return text.lower().replace(" ", "_").replace("!", "").strip("_")

# -----------------------------------------------
# Extract metadata from MP3
# -----------------------------------------------
def get_metadata(file_path):
    """Extract title, artist, and album art from MP3"""
    try:
        audio = MP3(file_path, ID3=ID3)
        tags = audio.tags
        
        if not tags:
            return None, None, None
        
        title = str(tags.get("TIT2", "Unknown"))
        artist = str(tags.get("TPE1", "Unknown"))
        
        # Extract album art
        for tag in tags.values():
            if tag.FrameID == "APIC":
                try:
                    img = Image.open(io.BytesIO(tag.data))
                    return title, artist, img
                except:
                    pass
        
        return title, artist, None
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return None, None, None

# -----------------------------------------------
# Convert image to RGB565 RAW
# -----------------------------------------------
def image_to_raw(image, output_filename):
    """Convert PIL Image to RGB565 RAW format"""
    try:
        # Crop to square from center
        w, h = image.size
        min_dim = min(w, h)
        left = (w - min_dim) // 2
        top = (h - min_dim) // 2
        img = image.crop((left, top, left + min_dim, top + min_dim))
        
        # Resize to target
        img = img.resize((ART_SIZE, ART_SIZE), Image.Resampling.LANCZOS)
        img = img.convert('RGB')
        
        output_path = os.path.join(ART_OUTPUT_DIR, output_filename)
        
        with open(output_path, 'wb') as f:
            pixels = img.tobytes()
            for i in range(0, len(pixels), 3):
                r, g, b = pixels[i], pixels[i+1], pixels[i+2]
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                f.write(rgb565.to_bytes(2, 'big'))
        
        print(f"  ✓ Album art: {output_filename}")
        return True
    except Exception as e:
        print(f"  ✗ Art conversion failed: {e}")
        return False

# -----------------------------------------------
# Scan music directory
# -----------------------------------------------
def scan_music_directory():
    """Scan music folder and extract all metadata"""
    if not os.path.exists(MUSIC_DIR):
        print(f"Error: {MUSIC_DIR}/ folder not found!")
        return []
    
    songs = []
    files = [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith('.mp3')]
    files.sort()
    
    if not files:
        print(f"No MP3 files found in {MUSIC_DIR}/")
        return []
    
    print(f"Found {len(files)} MP3 files\n")
    
    for file in files:
        path = os.path.join(MUSIC_DIR, file)
        title, artist, img = get_metadata(path)
        
        if title is None:
            print(f"✗ Skipped: {file} (no metadata)")
            continue
        
        print(f"Processing: {title} - {artist}")
        
        # Generate raw filename
        raw_filename = clean_filename(title) + ".raw"
        
        # Convert art if available
        if img:
            image_to_raw(img, raw_filename)
        else:
            print(f"  ⚠ No album art found")
        
        songs.append({
            'title': title,
            'artist': artist,
            'raw_file': raw_filename
        })
        
        print()
    
    return songs

# -----------------------------------------------
# Generate main.py
# -----------------------------------------------
def generate_main_py(songs):
    """Generate updated main.py with song list"""
    
    if not songs:
        print("No songs to process!")
        return
    
    # Build SONGS list
    songs_list = "SONGS = [\n"
    for song in songs:
        title = song['title'].replace('"', '\\"')
        artist = song['artist'].replace('"', '\\"')
        raw_file = song['raw_file']
        
        songs_list += f'    ("{title}", "{artist}", "{raw_file}"),\n'
    
    songs_list += "]\n"
    
    # Read template (current main.py structure)
    template = '''import time
import sys
import random
from machine import Pin, SPI
import st7789py as st7789
import vga2_8x16 as font_small
import vga2_16x32 as font_large

# -----------------------------------------------
# DISPLAY SETUP
# -----------------------------------------------
spi = SPI(0, baudrate=40000000, polarity=1, phase=1,
          sck=Pin(18), mosi=Pin(19))

tft = st7789.ST7789(
    spi, 240, 320,
    reset=Pin(20, Pin.OUT),
    dc=Pin(21, Pin.OUT),
    cs=Pin(17, Pin.OUT),
    backlight=Pin(22, Pin.OUT),
    rotation=1
)

# -----------------------------------------------
# COLORS
# -----------------------------------------------
BLACK = st7789.color565(0,   0,   0)
WHITE = st7789.color565(255, 255, 255)
GRAY  = st7789.color565(120, 120, 120)

# -----------------------------------------------
# LAYOUT (320x240 landscape)
# -----------------------------------------------
SCREEN_W = 320
SCREEN_H = 240

LEFT_W   = 155
DIVIDER  = 157
RIGHT_X  = 159
RIGHT_W  = SCREEN_W - RIGHT_X

ROW_H    = 16
TOP_PAD  = 4
MAX_ROWS = SCREEN_H // ROW_H

# Right panel zones
ART_SIZE = 148
ART_Y    = 6
ART_X    = RIGHT_X + (RIGHT_W - ART_SIZE) // 2

VIZ_Y    = ART_Y + ART_SIZE + 4
VIZ_H    = 24

TITLE_Y  = VIZ_Y + VIZ_H + 4
ARTIST_Y = TITLE_Y + 34

# Mode icon — bottom right corner of LEFT panel
# Drawn last so list rows never overwrite it
MODE_ICON_X = 0 # 300 for right side
MODE_ICON_Y = 220

# Progress bar — the divider column fills bottom to top
SONG_DURATION = 60.0  # seconds for a full bar

# -----------------------------------------------
# SONGS  (title, artist, raw_filename)
# -----------------------------------------------
''' + songs_list + '''
NUM_SONGS = len(SONGS)

# -----------------------------------------------
# PLAYBACK MODE
# -----------------------------------------------
MODE_DEFAULT = 0
MODE_SHUFFLE = 1
MODE_LOOP    = 2
MODE_SYMBOLS = [">", "?", "@"]

# -----------------------------------------------
# VISUALIZER STATE
# -----------------------------------------------
VIZ_BARS    = 12
VIZ_BAR_W   = 3
VIZ_BAR_GAP = 2
viz_h       = [0] * VIZ_BARS
viz_target  = [0] * VIZ_BARS
viz_prev    = [-1] * VIZ_BARS
viz_frame   = 0

def reset_visualizer():
    global viz_h, viz_target, viz_prev, viz_frame
    viz_h      = [0] * VIZ_BARS
    viz_target = [0] * VIZ_BARS
    viz_prev   = [-1] * VIZ_BARS
    viz_frame  = 0

def update_visualizer():
    global viz_frame, viz_h, viz_target
    viz_frame += 1
    if viz_frame % 5 == 0:
        for i in range(VIZ_BARS):
            viz_target[i] = random.randint(4, VIZ_H - 2)
    for i in range(VIZ_BARS):
        diff = viz_target[i] - viz_h[i]
        if diff > 0:
            viz_h[i] += max(1, diff // 3)
        elif diff < 0:
            viz_h[i] -= max(1, abs(diff) // 4)
        viz_h[i] = max(0, min(VIZ_H - 2, viz_h[i]))

def stop_visualizer():
    """Decay visualizer to zero when paused"""
    global viz_h, viz_target
    for i in range(VIZ_BARS):
        viz_target[i] = 0
        viz_h[i] = max(0, viz_h[i] - 2)

# -----------------------------------------------
# HELPERS
# -----------------------------------------------
def fit(text, max_chars):
    return text[:max_chars]

def txt_s(text, x, y, fg, bg, max_chars=40):
    try:
        tft.text(font_small, fit(text, max_chars), x, y, fg, bg)
    except:
        pass

def txt_l(text, x, y, fg, bg, max_chars=40):
    try:
        tft.text(font_large, fit(text, max_chars), x, y, fg, bg)
    except:
        pass

def clr(x, y, w, h, color):
    tft.fill_rect(x, y, w, h, color)

# -----------------------------------------------
# RAW IMAGE RENDERING
# RGB565 big-endian, no byte swap
# -----------------------------------------------
def draw_raw_image(filename, x, y, width, height):
    try:
        with open("/album_art/" + filename, 'rb') as f:
            for row in range(height):
                line_data = f.read(width * 2)
                if len(line_data) < width * 2:
                    break
                tft.blit_buffer(line_data, x, y + row, width, 1)
    except Exception as e:
        print("Art error:", e)

# -----------------------------------------------
# INPUT
# -----------------------------------------------
def read_key():
    try:
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if line:
                s = line.strip()
                if s:
                    return s[0].lower()
    except:
        pass
    return None

# -----------------------------------------------
# PLAYBACK LOGIC
# -----------------------------------------------
def next_song_index(current, mode, direction=1):
    if mode == MODE_LOOP:
        return current
    if mode == MODE_SHUFFLE:
        if NUM_SONGS > 1:
            return random.choice([i for i in range(NUM_SONGS) if i != current])
        return current
    return (current + direction) % NUM_SONGS

# -----------------------------------------------
# PROGRESS BAR
# Divider column fills white bottom to top
# -----------------------------------------------
prev_filled_px = -1

def draw_progress(progress):
    global prev_filled_px
    filled_px   = int(progress * SCREEN_H)
    filled_px   = max(0, min(SCREEN_H, filled_px))
    if filled_px == prev_filled_px:
        return
    unfilled_px = SCREEN_H - filled_px
    if unfilled_px > 0:
        tft.vline(DIVIDER, 0, unfilled_px, GRAY)
    if filled_px > 0:
        tft.vline(DIVIDER, unfilled_px, filled_px, WHITE)
    prev_filled_px = filled_px

def reset_progress():
    global prev_filled_px
    prev_filled_px = -1

# -----------------------------------------------
# MODE ICON
# Bottom right of left panel, drawn after draw_list
# so list rows never overwrite it
# -----------------------------------------------
def draw_mode_icon(mode):
    # Clear a small area then draw symbol
    clr(MODE_ICON_X - 2, MODE_ICON_Y - 1, 18, 14, BLACK)
    txt_s(MODE_SYMBOLS[mode], MODE_ICON_X, MODE_ICON_Y, WHITE, BLACK, 1)

# -----------------------------------------------
# LEFT PANEL
# Note: leaves bottom row blank for mode icon
# -----------------------------------------------
def draw_list(selected, playing_idx):
    start    = max(0, selected - MAX_ROWS // 2)
    # Reserve last row for mode icon — only draw MAX_ROWS - 1 rows
    draw_rows = 13

    for i in range(draw_rows):
        y   = TOP_PAD + i * ROW_H
        if y + ROW_H > MODE_ICON_Y:  # stop before icon row
            break
        idx = start + i

        if idx >= NUM_SONGS:
            clr(0, y, LEFT_W, ROW_H, BLACK)
            continue

        title, _, _ = SONGS[idx]
        is_hov  = (idx == selected)
        is_play = (idx == playing_idx)

        if is_play:
            clr(0, y, LEFT_W, ROW_H, WHITE)
            if is_hov:
                txt_s(">", 2, y + 1, BLACK, WHITE, 1)
            txt_s(title, 14, y + 1, BLACK, WHITE, 17)
        elif is_hov:
            clr(0, y, LEFT_W, ROW_H, BLACK)
            txt_s(">", 2, y + 1, WHITE, BLACK, 1)
            txt_s(title, 14, y + 1, WHITE, BLACK, 17)
        else:
            clr(0, y, LEFT_W, ROW_H, BLACK)
            txt_s(title, 14, y + 1, GRAY, BLACK, 17)

    # Clear bottom row area (mode icon lives here)
    clr(0, TOP_PAD + draw_rows * ROW_H, LEFT_W, ROW_H, BLACK)

# -----------------------------------------------
# RIGHT PANEL
# -----------------------------------------------
def draw_art(playing_idx):
    clr(ART_X, ART_Y, ART_SIZE, ART_SIZE, BLACK)
    tft.rect(ART_X, ART_Y, ART_SIZE, ART_SIZE, WHITE)
    if playing_idx is None:
        txt_s("NO ART", ART_X + 44, ART_Y + 68, GRAY, BLACK, 6)
        return
    _, _, filename = SONGS[playing_idx]
    draw_raw_image(filename, ART_X + 1, ART_Y + 1, ART_SIZE - 2, ART_SIZE - 2)

def draw_visualizer_frame():
    global viz_prev
    total = VIZ_BARS * (VIZ_BAR_W + VIZ_BAR_GAP)
    x0    = RIGHT_X + (RIGHT_W - total) // 2

    for i in range(VIZ_BARS):
        if viz_h[i] == viz_prev[i]:
            continue
        h_new = viz_h[i]
        h_old = viz_prev[i] if viz_prev[i] >= 0 else 0
        x     = x0 + i * (VIZ_BAR_W + VIZ_BAR_GAP)

        if h_new < h_old:
            clr(x, VIZ_Y + (VIZ_H - h_old), VIZ_BAR_W, h_old - h_new, BLACK)
        else:
            clr(x, VIZ_Y, VIZ_BAR_W, VIZ_H, BLACK)

        if h_new > 0:
            clr(x, VIZ_Y + (VIZ_H - h_new), VIZ_BAR_W, h_new, WHITE)

        viz_prev[i] = h_new

def draw_info(playing_idx):
    clr(RIGHT_X, TITLE_Y, RIGHT_W, SCREEN_H - TITLE_Y, BLACK)
    if playing_idx is None:
        txt_s("No song playing", RIGHT_X + 4, TITLE_Y + 8, GRAY, BLACK, 20)
        return
    title, artist, _ = SONGS[playing_idx]
    txt_l(title,  RIGHT_X + 4, TITLE_Y,  WHITE, BLACK, RIGHT_W // 16)
    txt_s(artist, RIGHT_X + 4, ARTIST_Y, GRAY,  BLACK, RIGHT_W // 8)

def draw_right(playing_idx):
    draw_art(playing_idx)
    clr(RIGHT_X, VIZ_Y, RIGHT_W, VIZ_H, BLACK)
    draw_visualizer_frame()
    draw_info(playing_idx)

# -----------------------------------------------
# MAIN
# -----------------------------------------------
def main():
    global prev_filled_px

    selected    = 0
    playing_idx = None
    paused      = False
    mode        = MODE_DEFAULT

    prev_sel    = -1
    prev_play   = -1
    prev_paused = False

    song_start_ms = 0
    elapsed_ms    = 0

    tft.fill(BLACK)
    draw_list(selected, playing_idx)
    draw_mode_icon(mode)
    draw_right(playing_idx)
    draw_progress(0.0)

    print("Ready. w=up  s=down  a=prev  d=next  p=play/pause  m=mode  q=quit")

    while True:
        now = time.ticks_ms()
        key = read_key()

        if key == 'w':
            selected = (selected - 1) % NUM_SONGS

        elif key == 's':
            selected = (selected + 1) % NUM_SONGS

        elif key == 'p':
            if playing_idx is None:
                playing_idx   = selected
                paused        = False
                elapsed_ms    = 0
                song_start_ms = now
                reset_visualizer()
                reset_progress()
            elif selected == playing_idx:
                if paused:
                    song_start_ms = time.ticks_add(now, -elapsed_ms)
                    paused = False
                    print(f"Resumed: {SONGS[playing_idx][0]}")
                else:
                    elapsed_ms = time.ticks_diff(now, song_start_ms)
                    paused = True
                    print(f"Paused: {SONGS[playing_idx][0]}")
                    reset_visualizer()
            else:
                playing_idx   = selected
                paused        = False
                elapsed_ms    = 0
                song_start_ms = now
                reset_visualizer()
                reset_progress()

        elif key == 'a':
            playing_idx   = next_song_index(
                playing_idx if playing_idx is not None else selected, mode, -1)
            selected      = playing_idx
            paused        = False
            elapsed_ms    = 0
            song_start_ms = now
            reset_visualizer()
            reset_progress()
            print(f"Prev: {SONGS[playing_idx][0]}")

        elif key == 'd':
            playing_idx   = next_song_index(
                playing_idx if playing_idx is not None else selected, mode, 1)
            selected      = playing_idx
            paused        = False
            elapsed_ms    = 0
            song_start_ms = now
            reset_visualizer()
            reset_progress()
            print(f"Next: {SONGS[playing_idx][0]}")

        elif key == 'm':
            mode = (mode + 1) % 3
            print(f"Mode: {['Default','Shuffle','Loop'][mode]}")

        elif key == 'q':
            tft.fill(BLACK)
            break

        # ---- PROGRESS ----
        if playing_idx is not None and not paused:
            current_elapsed = time.ticks_diff(now, song_start_ms)
            progress = min(1.0, current_elapsed / (SONG_DURATION * 1000))
            draw_progress(progress)

            # Auto advance when song ends
            if progress >= 1.0:
                playing_idx   = next_song_index(playing_idx, mode, 1)
                selected      = playing_idx
                elapsed_ms    = 0
                song_start_ms = now
                reset_visualizer()
                reset_progress()
                prev_play = -1  # force right panel redraw

        # ---- VISUALIZER ----
        # Only animate if playing (not paused)
        if playing_idx is not None and not paused:
            update_visualizer()
            draw_visualizer_frame()
        elif paused:
            # Decay visualizer when paused
            stop_visualizer()
            draw_visualizer_frame()

        # ---- REDRAWS ----
        if selected != prev_sel or playing_idx != prev_play:
            draw_list(selected, playing_idx)
            draw_mode_icon(mode)   # always redraw icon after list
            prev_sel = selected

        if playing_idx != prev_play or paused != prev_paused:
            draw_right(playing_idx)
            prev_play = playing_idx
            prev_paused = paused

        # Redraw mode icon when mode changes
        if key == 'm':
            draw_mode_icon(mode)

        time.sleep(0.03)

main()
'''
    
    # Write main.py
    with open(OUTPUT_SCRIPT, 'w', encoding='utf-8') as f:
        f.write(template)
    
    print(f"\n✓ Generated {OUTPUT_SCRIPT}")

# -----------------------------------------------
# Main
# -----------------------------------------------
def main():
    print("=" * 60)
    print("Music Player Generator for Pico")
    print("=" * 60)
    print()
    
    # Scan music directory
    songs = scan_music_directory()
    
    if not songs:
        print("No valid songs found!")
        return
    
    print(f"\n{'='*60}")
    print(f"Successfully processed {len(songs)} songs")
    print(f"{'='*60}\n")
    
    # List songs
    print("Songs to be added:")
    for i, song in enumerate(songs, 1):
        print(f"  {i}. {song['title']} - {song['artist']}")
        print(f"     → {song['raw_file']}")
    
    print()
    
    # Generate main.py
    generate_main_py(songs)
    
    print(f"\n{'='*60}")
    print("NEXT STEPS:")
    print(f"{'='*60}")
    print(f"1. Copy album_art/ folder to Pico's /album_art/")
    print(f"2. Copy generated main.py to Pico's root")
    print(f"3. Run main.py on Pico!")
    print()

if __name__ == "__main__":
    main()