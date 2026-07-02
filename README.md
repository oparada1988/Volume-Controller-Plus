# Volume Control Plus for StreamController
<img width="1000" height="360" alt="g31" src="https://github.com/user-attachments/assets/254e6175-572e-4f63-beaa-333d7a278b89" />


Volume Control plugin inspired by the official Elgato Volumen Mixer plugin. Mimics the visual UI for a clean look. Allows volume adjustment via dials, mute toggle via dial push, and touchscreen drag gestures to mute/unmute the audio.

## Features
* **Dial Adjustment**: Smoothly turn dials to raise or lower PipeWire volume.
* **Mute Toggle**: Press the dial or tap/drag on the touchscreen area to quickly mute/unmute.
* **VU Meter**: Real-time peak monitor with smooth 40 FPS animations, a professional VU peak-hold floating marker, and a visual red warning when the audio peak reaches 100%.
* **Custom Presentation**: Clean presentation with custom device names, fonts, and a custom SVG/PNG icon.

## Privacy & Desktop Indicators
* **GNOME Microphone Icon Bypass**: The Live Peak Meter measures playback audio levels in real-time by starting a background `parecord` helper stream. To prevent GNOME Shell from displaying a persistent orange recording indicator (microphone icon) on your desktop panel, the stream's application ID is spoofed as `org.PulseAudio.pavucontrol`. Since this ID is on GNOME's hardcoded recording exclusion list, the Live Peak Meter can run cleanly without triggering system-wide privacy notifications.
