# Changelog

All notable changes to **Volume Controller Plus** will be documented in this file.

## [1.2.0] - 2026-08-23

### 🎛️ WaveController Suite Integration
- **Three Dedicated Actions**:
  - `ChannelMaster`: Individual channel master fader with real-time VU peak metering and mute toggle.
  - `SubMix`: Independent per-mix send level fader (1:1 with WaveController matrix cells) for custom routing into Personal, Chat, and Hardware sub-mixes.
  - `MixMaster`: Overall virtual mix bus output level controller with physical audio output switching.
- **Ultra Low-Latency Persistent IPC Socket**: Persistent line-buffered Unix Domain Socket streaming with sub-millisecond query latency and automatic reconnection.
- **Physical Dial & Touch Interaction**: Fixed `TURN_CW` / `TURN_CCW` event dispatching, tap mute toggling, and swipe/long-press device cycling.
- **Dynamic Icon Resolution**: Built-in GTK theme symbolic icon rasterizer with fallback to bundled high-DPI assets.
- **Adwaita Settings Safety**: Protected `Adw.ComboRow` models from recursive re-entrancy crashes during user selection.

## [1.1.2] - 2026-08-22

### ⚡ Engine & Peak Monitoring Enhancements
- **Flatpak Host Audio Bridge**: Added automatic Flatpak environment detection with `flatpak-spawn --host pw-record` bridge, allowing seamless PipeWire peak monitoring within containerized environments.
- **Active Sound Card Monitor Prioritization**: Filtered out silent digital IEC958 microphone ports from sink monitor link discovery, ensuring accurate live VU meter rendering for Spotify and desktop media playback.
- **Calibrated Noise Floor Cutoff**: Lowered noise gate threshold from `0.04` to `0.005` with high-responsiveness exponential decay physics.

## [1.1.1] - 2026-08-22

### 🎨 Visual & UI Design Enhancements
- **Calibrated Knob & Dial Geometry**: Lowered knob center position to $(x=70\text{px}, y=104\text{px})$ with calibrated radiuses ($r_{\text{inner}}=39\text{px}$, $r_{\text{outer}}=44\text{px}$, $r_{\text{arc}}=51\text{px}$) for a sleek, low-profile dial appearance.
- **Hierarchical 9 Radial Ticks**: Implemented 5 tall major markers at $0\%$, $25\%$, $50\%$, $75\%$, and $100\%$ ($r=54\text{px}\rightarrow 63\text{px}$), and 4 shorter minor markers in between ($r=56\text{px}\rightarrow 61\text{px}$).
- **Recessed Dark Groove Track**: Styled the inactive base track as a subtle dark groove (`#141418` / `RGB: 20, 20, 24`) that is clearly and subtly darker than the widget background, creating an etched channel depth.
- **Vivid Emerald Green Meter**: Configured active live audio VU meter to a modern emerald green (`#3db356` / `RGB: 61, 179, 86`), smoothly transitioning into yellow/red at upper volume thresholds ($>80\%$).
- **Dynamic White Adjustment Curve**: Rotating the dial displays a solid bright white curve filling from $210^\circ$ to the current volume level, which automatically reverts to the dark track and live audio meter after $1.2\text{s}$ of inactivity.
- **Mute State & Layout Spacing**: Retained the perimeter red border and red `"MUTE"` text centered at $(165\text{px}, 64\text{px})$, with the device switch icon positioned at the top-right corner $(168\text{px}, 10\text{px})$ providing $24\text{px}$ of vertical clearance above the status text.
- **Settings UI Icon Filename Display**: Added clean filename display (`os.path.basename`) as the subtitle for custom icon selection rows in the configuration page, preventing blank entries.

### ⚡ Performance & Engine Optimizations
- **Pre-Baked Static Knob Core in Midground Cache**: Pre-rendered the static outer bezel chord, inner disc chord, and top highlight arc into the midground cache, eliminating 3 geometry drawing operations per frame and speeding up rendering to **$\approx 0.5\text{ms}$ per frame** ($\approx 2,000\text{ FPS}$ capability).
- **Pre-Computed Trigonometry Constants**: Pre-calculated fixed $210^\circ$ start cap coordinates and sub-canvas offsets once at startup, eliminating per-frame trigonometric calculations.
- **Off-Screen Background Throttling**: Suspends 40 FPS meter loops when the widget is on an inactive profile or background page, reducing idle background CPU usage to $0\%$.
- **Non-Blocking Asynchronous Peak Monitor Restart**: Peak monitor stop/start operations during device switching now run in background worker threads for instant, zero-delay UI response.
- **Debounced Event Worker**: Replaced ad-hoc thread spawning on system volume change events with a persistent, reusable `threading.Event()` polling worker.
