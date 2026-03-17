# Train Connectivity Tracker

Records GPS position, WiFi network (ÖBB / Westbahn / phone hotspot), and
connection quality during train journeys in Austria.  Generates an interactive
colour-coded map you can open in any browser.

## Prerequisites

- **Windows 10/11** (uses `netsh` for WiFi detection and WinRT for GPS)
- **Python 3.10+**
- **Windows Positionsdienste** enabled (all three toggles):
  _Einstellungen → Datenschutz & Sicherheit → Position_
  1. **Positionsdienste** → Ein
  2. **Apps den Zugriff auf Ihren Standort erlauben** → Ein
  3. **Desktop-Apps den Zugriff auf Ihren Standort erlauben** → Ein (scroll down — this is the critical one for Python)

## Setup

```bash
# Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Configuration

Edit `config.json` before your first trip:

| Key | Purpose |
|-----|---------|
| `known_networks` | Map of SSID → label for train WiFi (`OEBB`, `WESTlan` already set) |
| `hotspot_ssids` | List of your phone hotspot SSID(s), e.g. `["iPhone von Max"]` |
| `interval_seconds` | Sampling interval (default 10 s ≈ one point every 280–560 m) |
| `ping_host` | ICMP ping target (default `1.1.1.1`) |
| `http_test_url` | URL for HTTP latency test |
| `download_test_url` | URL for download speed test |

## Usage

### 1. Record a trip

```bash
python tracker.py record              # uses config.json interval
python tracker.py record -i 5         # override: sample every 5 seconds
```

Press **Ctrl+C** when you arrive.  Data is saved to `data/<timestamp>.csv`.

### 2. Generate the map

```bash
python tracker.py map                 # latest recording
python tracker.py map -f data/2026-03-17_14-30-00.csv   # specific file
python tracker.py map -o mytrip.html  # custom output path
```

The map opens automatically in your default browser.

## Reading the map

- **Line colour** = connection quality  
  🟢 Excellent · 🟡 Good · 🟠 Fair · 🔴 Poor · ⚪ No connection

- **Dot colour** = network type  
  🔵 ÖBB · 🟣 Westbahn · 🟠 Phone hotspot · ⚫ Other · ⚪ Disconnected

- **Dashed segments** indicate GPS gaps (e.g. tunnels)

- **Click any dot** for details: timestamp, SSID, signal %, ping, HTTP latency,
  download speed, GPS source & accuracy.

## Quality thresholds

| Level     | Ping     | HTTP latency |
|-----------|----------|--------------|
| Excellent | < 50 ms  | < 200 ms     |
| Good      | < 100 ms | < 500 ms     |
| Fair      | < 200 ms | < 1 000 ms   |
| Poor      | ≥ 200 ms | ≥ 1 000 ms   |

## Known limitations

- **Laptop GPS accuracy** – Many laptops lack a GPS chip.  Windows falls back
  to WiFi/IP positioning (~100–1 000 m accuracy).  A USB GPS dongle improves
  this significantly.
- **IP geolocation on train WiFi** – When the IP fallback is used, the
  position may reflect the ISP's data centre, not the train.  The
  `position_source` column and popup tell you which source was used.
- **Captive portals** – If train WiFi requires login, connectivity tests will
  correctly report "connected to WiFi but no internet" until you accept the
  portal.
