# RP2040 Based Music Player

A custom music player system with a graphical interface, album artwork rendering, and an animated audio visualizer.

## Overview

This project combines microcontroller UI rendering, display driver control, and offline asset generation to create a fully functional music player on the Pico.

**Key Features:**
-  Graphical UI with song list and album art
-  Album artwork rendering (1:1 square display)
-  Animated audio visualizer
-  Playback progress bar
-  Multiple playback modes (Default, Shuffle, Loop)
-  Keyboard controls

## Hardware

### Display
- **Display Type:** ST7789V-based SPI display
- **Resolution:** 320x240 (landscape mode)
- **Communication:** SPI
- **Driver:** st7789py

### Wiring (Microcontrller → Display)

| Pico Pin | Display Pin | Purpose |
|----------|-------------|---------|
| GP18 | SCK | SPI Clock |
| GP19 | MOSI | SPI Data |
| GP17 | CS | Chip Select |
| GP21 | DC | Data/Command |
| GP20 | RST | Reset |
| GP22 | BL | Backlight |
| GND | GND | Ground |
| 3V3 | VCC | Power |

## Software Architecture

The project is split into two main components:

### 1. Pico Runtime (`main.py`)

Runs directly on any RP2040 based device.

**Responsibilities:**
- Renders the full UI:
  - Song list (left panel)
  - Album art (right panel)
  - Audio visualizer
  - Playback progress bar
- Handles input (keyboard/serial controls)
- Manages playback state:
  - Selected song vs currently playing
  - Pause/resume
  - Shuffle / loop modes
- Draws album art from preprocessed `.raw` files
- Animates the lightweight visualizer

**Key Characteristics:**
- Fully frame-based rendering
- Optimized for low memory + SPI bandwidth
- Uses RGB565 raw image format for fast drawing

### 2. Generator Script (`generate_player.py`)

Runs on your computer as a build step.

**Responsibilities:**
- Scans a folder of `.mp3` files
- Extracts:
  - Song title
  - Artist name
  - Embedded album artwork
- Converts album art into:
  - Square, resized RGB565 `.raw` files
- Automatically generates:
  - The `SONGS` list for the Pico UI
  - A complete `main.py` file ready to deploy

**Why this exists:**

The RP2040 is too limited to decode MP3 metadata and process images, so all heavy processing is done ahead of time on your computer.

