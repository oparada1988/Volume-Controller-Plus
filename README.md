# WaveController for StreamController

<img width="987" height="347" alt="WaveController-plugin-banner" src="https://github.com/user-attachments/assets/4bd0d764-d93e-4613-8f4b-5578e6cf5b23" />

Official WaveController plugin for StreamController. Allows you to add Channel, Mixes, and Submixes to the StreamDeck Plus Dials.

## This plugin is going through major refactoring. Please continue using the Main branch for the last stable release ##

## Features
* **Dial Adjustment**: Smoothly turn dials to raise or lower PipeWire volume.
* **Mute Toggle**: Press the dial to quickly mute/unmute.
* **Dual-Device Switching**: Connect two separate PipeWire devices (any combination of inputs/sources or outputs/sinks) to a single dial control. When active, tap the touchscreen to instantly switch control between the two devices.
* **Touchscreen Controls**: Touchscreen tap transitions between devices, with smart input lockouts when the switch is disabled to prevent accidental gestures.
* **VU Meter**: Real-time peak monitor with smooth 40 FPS animations, a professional VU peak-hold floating marker, and a visual red warning when the audio peak reaches 100%.
* **Custom Presentation**: Clean layout with separate name entries, configurable fonts, custom device icons, active/inactive device status indicators in the corner, and automated text truncation to prevent layout overlaps.

## Privacy & Desktop Indicators
* **GNOME Microphone Icon Bypass**: The Live Peak Meter measures playback audio levels in real-time by starting a background `parecord` helper stream. To prevent GNOME Shell from displaying a persistent orange recording indicator (microphone icon) on your desktop panel, the stream's application ID is spoofed as `org.PulseAudio.pavucontrol`. Since this ID is on GNOME's hardcoded recording exclusion list, the Live Peak Meter can run cleanly without triggering system-wide privacy notifications.

---
Notice: Plugin was written/updated with assistance of Google Antigravity
