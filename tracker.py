#!/usr/bin/env python3
"""Train Connectivity Tracker

Records GPS position, WiFi network, and connectivity quality during train
travel, then generates an interactive color-coded map.

Usage:
    python tracker.py record          # start recording (Ctrl+C to stop)
    python tracker.py map             # generate map from latest recording
    python tracker.py map -f FILE     # generate map from specific CSV
"""

import argparse
import asyncio
import csv
import json
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

try:
    import requests

    _REQUESTS = True
except ImportError:
    _REQUESTS = False

try:
    import winrt.windows.devices.geolocation as wdg

    _WINRT = True
except ImportError:
    _WINRT = False

try:
    import geocoder as _geocoder

    _GEOCODER = True
except ImportError:
    _GEOCODER = False

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------

DATA_DIR = Path("data")
MAPS_DIR = Path("maps")
CONFIG_FILE = Path("config.json")

DEFAULT_CONFIG = {
    "known_networks": {"OEBB": "obb", "WESTlan": "westbahn"},
    "hotspot_ssids": [],
    "interval_seconds": 10,
    "ping_host": "1.1.1.1",
    "ping_timeout_ms": 3000,
    "http_test_url": "http://www.gstatic.com/generate_204",
    "http_timeout_seconds": 3,
    "download_test_url": "https://speed.cloudflare.com/__down?bytes=102400",
    "download_timeout_seconds": 5,
    "download_size_bytes": 102400,
}

CSV_FIELDS = [
    "timestamp",
    "latitude",
    "longitude",
    "accuracy_m",
    "position_source",
    "speed_kmh",
    "ssid",
    "signal_strength_pct",
    "network_type",
    "connected",
    "ping_ms",
    "http_latency_ms",
    "download_kbps",
]

QUALITY_COLORS = {
    "excellent": "#2ecc71",
    "good": "#82e0aa",
    "fair": "#f4d03f",
    "poor": "#e74c3c",
    "none": "#95a5a6",
}

NETWORK_COLORS = {
    "obb": "#2471a3",
    "westbahn": "#8e44ad",
    "hotspot": "#e67e22",
    "other": "#7f8c8d",
    "disconnected": "#bdc3c7",
}

_WINRT_POSITION_SOURCES = {
    0: "cellular",
    1: "satellite",
    2: "wifi",
    3: "ip_address",
    4: "unknown",
    5: "default",
    6: "obfuscated",
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            config.update(json.load(fh))
    return config


# ---------------------------------------------------------------------------
# GPS – Train portal (best) → WinRT → IP geolocation (fallback)
# ---------------------------------------------------------------------------

TRAIN_GPS_ENDPOINTS = {
    "obb": {
        "gps_url": "https://railnet.oebb.at/api/gps",
        "speed_url": "https://railnet.oebb.at/api/speed",
    },
}


def _get_location_train_portal(network_type: str) -> dict | None:
    """Get GPS from the train's own onboard portal (most accurate source)."""
    endpoints = TRAIN_GPS_ENDPOINTS.get(network_type)
    if not endpoints:
        return None
    try:
        if _REQUESTS:
            r = requests.get(endpoints["gps_url"], timeout=3)
            data = r.json()
        else:
            from urllib.request import urlopen
            import json as _json

            with urlopen(endpoints["gps_url"], timeout=3) as r:
                data = _json.loads(r.read())

        lat = float(data.get("Latitude") or data.get("latitude", 0))
        lon = float(data.get("Longitude") or data.get("longitude", 0))
        if lat == 0 and lon == 0:
            return None

        speed = None
        try:
            if _REQUESTS:
                sr = requests.get(endpoints["speed_url"], timeout=2)
                speed = float(sr.text.strip())
            else:
                with urlopen(endpoints["speed_url"], timeout=2) as sr:
                    speed = float(sr.read().decode().strip())
        except Exception:
            pass

        return {
            "latitude": lat,
            "longitude": lon,
            "accuracy_m": 10.0,
            "position_source": "train_gps",
            "speed_kmh": speed,
        }
    except Exception:
        return None


async def _get_location_winrt() -> dict | None:
    """Retrieve location via the Windows Location API (WinRT)."""
    if not _WINRT:
        return None
    try:
        access = await wdg.Geolocator.request_access_async()
        if access != wdg.GeolocationAccessStatus.ALLOWED:
            return None

        locator = wdg.Geolocator()
        locator.desired_accuracy = wdg.PositionAccuracy.HIGH
        pos = await locator.get_geoposition_async()
        coord = pos.coordinate

        source_val = (
            coord.position_source.value
            if hasattr(coord, "position_source") and coord.position_source is not None
            else 4
        )
        accuracy = None
        if hasattr(coord, "accuracy") and coord.accuracy is not None:
            accuracy = round(coord.accuracy, 1)

        return {
            "latitude": coord.point.position.latitude,
            "longitude": coord.point.position.longitude,
            "accuracy_m": accuracy,
            "position_source": _WINRT_POSITION_SOURCES.get(source_val, "unknown"),
        }
    except Exception as exc:
        print(f"  [GPS] WinRT error: {exc}")
        return None


def _get_location_ip() -> dict | None:
    """Fallback: approximate location via public IP geolocation."""
    if not _GEOCODER:
        return None
    try:
        g = _geocoder.ip("me")
        if g.ok and g.lat and g.lng:
            return {
                "latitude": g.lat,
                "longitude": g.lng,
                "accuracy_m": None,
                "position_source": "ip",
            }
    except Exception:
        pass
    return None


async def get_location(network_type: str = "") -> dict:
    """Best-effort location: train portal → WinRT → IP fallback → null."""
    loc = _get_location_train_portal(network_type)
    if loc:
        return loc
    loc = await _get_location_winrt()
    if loc:
        return loc
    loc = _get_location_ip()
    if loc:
        return loc
    return {
        "latitude": None,
        "longitude": None,
        "accuracy_m": None,
        "position_source": "none",
    }


# ---------------------------------------------------------------------------
# Network detection  (netsh wlan show interfaces)
# ---------------------------------------------------------------------------

_SUBPROCESS_FLAGS = 0
if sys.platform == "win32":
    _SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW


def get_wifi_info() -> tuple[str | None, int | None]:
    """Return (ssid, signal_strength_percent) of the currently connected WiFi."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            encoding="oem",
            errors="replace",
            timeout=5,
            creationflags=_SUBPROCESS_FLAGS,
        )
        if result.returncode != 0:
            return None, None

        ssid = None
        signal = None

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("SSID") and not stripped.startswith("BSSID"):
                parts = stripped.split(":", 1)
                if len(parts) == 2:
                    val = parts[1].strip()
                    ssid = val if val else None
            if stripped.startswith("Signal"):
                m = re.search(r"(\d+)\s*%", stripped)
                if m:
                    signal = int(m.group(1))

        return ssid, signal
    except Exception:
        return None, None


def classify_network(ssid: str | None, config: dict) -> str:
    if not ssid:
        return "disconnected"
    if ssid in config.get("known_networks", {}):
        return config["known_networks"][ssid]
    if ssid in config.get("hotspot_ssids", []):
        return "hotspot"
    return "other"


# ---------------------------------------------------------------------------
# Connectivity tests
# ---------------------------------------------------------------------------


def test_ping(host: str, timeout_ms: int) -> float | None:
    """Single ICMP ping; returns round-trip ms or None."""
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True,
            encoding="oem",
            errors="replace",
            timeout=timeout_ms / 1000 + 2,
            creationflags=_SUBPROCESS_FLAGS,
        )
        if result.returncode == 0:
            m = re.search(r"[=<](\d+)\s*ms", result.stdout)
            if m:
                return int(m.group(1))
        return None
    except Exception:
        return None


def test_http_latency(url: str, timeout: float) -> float | None:
    """HTTP GET latency in milliseconds or None."""
    try:
        t0 = time.monotonic()
        if _REQUESTS:
            resp = requests.get(url, timeout=timeout, allow_redirects=False)
            elapsed_ms = (time.monotonic() - t0) * 1000
            if resp.status_code in (200, 204, 301, 302):
                return round(elapsed_ms, 1)
        else:
            from urllib.request import urlopen, Request

            req = Request(url, method="GET")
            with urlopen(req, timeout=timeout) as resp:
                elapsed_ms = (time.monotonic() - t0) * 1000
                if resp.status in (200, 204, 301, 302):
                    return round(elapsed_ms, 1)
        return None
    except Exception:
        return None


def test_download_speed(url: str, timeout: float) -> float | None:
    """Estimated download speed in kbps, or None."""
    try:
        t0 = time.monotonic()
        if _REQUESTS:
            resp = requests.get(url, timeout=timeout)
            elapsed = time.monotonic() - t0
            if resp.status_code == 200 and elapsed > 0:
                return round((len(resp.content) * 8) / elapsed / 1000, 1)
        else:
            from urllib.request import urlopen

            with urlopen(url, timeout=timeout) as resp:
                data = resp.read()
                elapsed = time.monotonic() - t0
                if resp.status == 200 and elapsed > 0:
                    return round((len(data) * 8) / elapsed / 1000, 1)
        return None
    except Exception:
        return None


def run_connectivity_tests(config: dict) -> dict:
    ping_ms = test_ping(config["ping_host"], config["ping_timeout_ms"])
    http_ms = test_http_latency(
        config["http_test_url"], config["http_timeout_seconds"]
    )
    dl_kbps = None
    if http_ms is not None:
        dl_kbps = test_download_speed(
            config["download_test_url"],
            config["download_timeout_seconds"],
        )
    connected = ping_ms is not None or http_ms is not None
    return {
        "connected": connected,
        "ping_ms": ping_ms,
        "http_latency_ms": http_ms,
        "download_kbps": dl_kbps,
    }


# ---------------------------------------------------------------------------
# Quality score
# ---------------------------------------------------------------------------


def get_quality(ping_ms: float | None, http_ms: float | None) -> str:
    if ping_ms is None and http_ms is None:
        return "none"
    p = ping_ms if ping_ms is not None else float("inf")
    h = http_ms if http_ms is not None else float("inf")
    if p < 50 and h < 200:
        return "excellent"
    if p < 100 and h < 500:
        return "good"
    if p < 200 and h < 1000:
        return "fair"
    return "poor"


# ---------------------------------------------------------------------------
# Recording loop
# ---------------------------------------------------------------------------


async def record(config: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)

    ts_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    csv_path = DATA_DIR / f"{ts_str}.csv"

    print(f"Recording to {csv_path}")
    print(f"Sampling every {config['interval_seconds']}s")
    print("Press Ctrl+C to stop.\n")

    if _WINRT:
        print("[GPS] Windows Location API available")
    else:
        print("[GPS] winrt not installed – using IP geolocation fallback only")
    if _GEOCODER:
        print("[GPS] IP geolocation fallback available")
    elif not _WINRT:
        print("[GPS] WARNING: no GPS source available (install winrt or geocoder)")
    print()

    sample_n = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        fh.flush()

        while True:
            t_start = time.monotonic()
            sample_n += 1
            now = datetime.now().isoformat(timespec="seconds")

            ssid, signal = get_wifi_info()
            network_type = classify_network(ssid, config)
            location = await get_location(network_type)
            conn = run_connectivity_tests(config)
            quality = get_quality(conn["ping_ms"], conn["http_latency_ms"])

            speed = location.get("speed_kmh")
            row = {
                "timestamp": now,
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "accuracy_m": location["accuracy_m"],
                "position_source": location["position_source"],
                "speed_kmh": speed if speed is not None else "",
                "ssid": ssid or "",
                "signal_strength_pct": signal if signal is not None else "",
                "network_type": network_type,
                "connected": conn["connected"],
                "ping_ms": conn["ping_ms"] if conn["ping_ms"] is not None else "",
                "http_latency_ms": (
                    conn["http_latency_ms"]
                    if conn["http_latency_ms"] is not None
                    else ""
                ),
                "download_kbps": (
                    conn["download_kbps"]
                    if conn["download_kbps"] is not None
                    else ""
                ),
            }
            writer.writerow(row)
            fh.flush()

            # Pretty console line
            lat_s = (
                f"{location['latitude']:.4f}" if location["latitude"] else "\u2014"
            )
            lon_s = (
                f"{location['longitude']:.4f}" if location["longitude"] else "\u2014"
            )
            acc_s = (
                f"\u00b1{location['accuracy_m']:.0f}m"
                if location["accuracy_m"]
                else ""
            )
            ssid_s = ssid or "\u2014"
            sig_s = f"{signal}%" if signal is not None else "\u2014"
            ping_s = (
                f"{conn['ping_ms']}ms" if conn["ping_ms"] is not None else "\u2014"
            )
            http_s = (
                f"{conn['http_latency_ms']}ms"
                if conn["http_latency_ms"] is not None
                else "\u2014"
            )
            dl_s = (
                f"{conn['download_kbps']}kbps"
                if conn["download_kbps"] is not None
                else "\u2014"
            )

            spd_s = f"{speed:.0f}km/h" if speed is not None else ""

            print(
                f"[{now}] #{sample_n:>4} | "
                f"GPS: {lat_s}, {lon_s} {acc_s} {spd_s} ({location['position_source']}) | "
                f"WiFi: {ssid_s} ({sig_s}) [{network_type}] | "
                f"Ping: {ping_s} | HTTP: {http_s} | DL: {dl_s} | "
                f"{quality.upper()}"
            )

            elapsed = time.monotonic() - t_start
            await asyncio.sleep(max(0, config["interval_seconds"] - elapsed))


# ---------------------------------------------------------------------------
# Map generation
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            row = {
                "timestamp": raw["timestamp"],
                "latitude": float(raw["latitude"]) if raw["latitude"] else None,
                "longitude": float(raw["longitude"]) if raw["longitude"] else None,
                "accuracy_m": (
                    float(raw["accuracy_m"]) if raw["accuracy_m"] else None
                ),
                "position_source": raw["position_source"],
                "speed_kmh": (
                    float(raw["speed_kmh"])
                    if raw.get("speed_kmh")
                    else None
                ),
                "ssid": raw["ssid"] or None,
                "signal_strength_pct": (
                    int(raw["signal_strength_pct"])
                    if raw["signal_strength_pct"]
                    else None
                ),
                "network_type": raw["network_type"],
                "connected": raw["connected"] == "True",
                "ping_ms": (
                    float(raw["ping_ms"]) if raw["ping_ms"] else None
                ),
                "http_latency_ms": (
                    float(raw["http_latency_ms"]) if raw["http_latency_ms"] else None
                ),
                "download_kbps": (
                    float(raw["download_kbps"]) if raw["download_kbps"] else None
                ),
            }
            rows.append(row)
    return rows


def _legend_html() -> str:
    parts: list[str] = []
    parts.append(
        '<div style="position:fixed;bottom:30px;left:30px;z-index:1000;'
        "background:white;padding:14px 18px;border-radius:8px;"
        'box-shadow:0 2px 8px rgba(0,0,0,.3);font-family:Arial,sans-serif;font-size:13px;">'
    )
    parts.append('<b style="font-size:14px;">Connection Quality</b><br>')
    for label, key in [
        ("Excellent", "excellent"),
        ("Good", "good"),
        ("Fair", "fair"),
        ("Poor", "poor"),
        ("No connection", "none"),
    ]:
        c = QUALITY_COLORS[key]
        parts.append(
            f'<span style="background:{c};width:14px;height:14px;'
            f'display:inline-block;margin:2px 6px 2px 0;border-radius:2px;">'
            f"</span>{label}<br>"
        )
    parts.append('<br><b style="font-size:14px;">Network Type</b><br>')
    for label, key in [
        ("\u00d6BB", "obb"),
        ("Westbahn", "westbahn"),
        ("Phone Hotspot", "hotspot"),
        ("Other", "other"),
        ("Disconnected", "disconnected"),
    ]:
        c = NETWORK_COLORS[key]
        parts.append(
            f'<span style="background:{c};width:14px;height:14px;'
            f'display:inline-block;margin:2px 6px 2px 0;border-radius:50%;">'
            f"</span>{label}<br>"
        )
    parts.append("</div>")
    return "\n".join(parts)


def _popup_html(p: dict) -> str:
    quality = get_quality(p["ping_ms"], p["http_latency_ms"])
    ping_s = f'{p["ping_ms"]:.0f} ms' if p["ping_ms"] is not None else "N/A"
    http_s = (
        f'{p["http_latency_ms"]:.0f} ms'
        if p["http_latency_ms"] is not None
        else "N/A"
    )
    dl_s = f'{p["download_kbps"]:.0f} kbps' if p["download_kbps"] is not None else "N/A"
    sig_s = (
        f'{p["signal_strength_pct"]}%'
        if p["signal_strength_pct"] is not None
        else "N/A"
    )
    acc_part = ""
    if p["accuracy_m"] is not None:
        acc_part = f'<br><b>Accuracy:</b> \u00b1{p["accuracy_m"]:.0f} m'
    spd_part = ""
    if p.get("speed_kmh") is not None:
        spd_part = f'<br><b>Speed:</b> {p["speed_kmh"]:.0f} km/h'

    return (
        '<div style="font-family:Arial,sans-serif;font-size:12px;min-width:180px;">'
        f'<b>{p["timestamp"]}</b><br><br>'
        f'<b>Network:</b> {p["ssid"] or "None"} ({p["network_type"]})<br>'
        f"<b>Signal:</b> {sig_s}<br>"
        f"<b>Quality:</b> {quality.upper()}<br><br>"
        f"<b>Ping:</b> {ping_s}<br>"
        f"<b>HTTP Latency:</b> {http_s}<br>"
        f"<b>Download:</b> {dl_s}<br><br>"
        f'<b>GPS Source:</b> {p["position_source"]}'
        f"{acc_part}{spd_part}</div>"
    )


def _summary_stats(data: list[dict], geo: list[dict]) -> str:
    """Build HTML panel with summary statistics."""
    from collections import Counter

    total = len(data)
    quals = Counter(get_quality(r["ping_ms"], r["http_latency_ms"]) for r in data)
    nets = Counter(r["network_type"] for r in data)
    connected = sum(1 for r in data if r["connected"])

    pings = [r["ping_ms"] for r in data if r["ping_ms"] is not None]
    https_ = [r["http_latency_ms"] for r in data if r["http_latency_ms"] is not None]
    speeds = [r.get("speed_kmh") for r in geo if r.get("speed_kmh") is not None]

    ts_list = [r["timestamp"] for r in data]
    time_span = f"{ts_list[0][11:19]} – {ts_list[-1][11:19]}" if ts_list else "N/A"

    q_rows = ""
    for label, key in [
        ("Excellent", "excellent"),
        ("Good", "good"),
        ("Fair", "fair"),
        ("Poor", "poor"),
        ("No conn.", "none"),
    ]:
        c = quals.get(key, 0)
        pct = c / total * 100 if total else 0
        bar_w = int(pct)
        q_rows += (
            f"<tr><td>{label}</td>"
            f'<td style="width:110px"><div style="background:{QUALITY_COLORS[key]};'
            f'width:{bar_w}%;height:12px;border-radius:2px;display:inline-block;">'
            f"</div></td>"
            f"<td style='text-align:right'>{c}</td>"
            f"<td style='text-align:right'>{pct:.0f}%</td></tr>"
        )

    net_rows = ""
    for name, count in nets.most_common():
        label = {"obb": "\u00d6BB", "westbahn": "Westbahn", "hotspot": "Hotspot",
                 "other": "Other", "disconnected": "Disconn."}.get(name, name)
        net_rows += f"<tr><td>{label}</td><td style='text-align:right'>{count}</td></tr>"

    ping_s = (
        f"{min(pings):.0f} / {sum(pings)/len(pings):.0f} / {max(pings):.0f}"
        if pings else "N/A"
    )
    http_s = (
        f"{min(https_):.0f} / {sum(https_)/len(https_):.0f} / {max(https_):.0f}"
        if https_ else "N/A"
    )
    speed_s = (
        f"{min(speeds):.0f} – {max(speeds):.0f} km/h"
        if speeds else "N/A"
    )

    return (
        '<div style="position:fixed;top:15px;right:15px;z-index:1000;'
        "background:white;padding:14px 18px;border-radius:8px;"
        "box-shadow:0 2px 8px rgba(0,0,0,.3);font-family:Arial,sans-serif;"
        'font-size:12px;max-width:300px;max-height:90vh;overflow-y:auto;">'
        '<b style="font-size:14px;">Trip Summary</b><br>'
        f"<span style='color:#666'>{time_span}</span><br><br>"
        f"<b>Samples:</b> {total} ({len(geo)} with GPS)<br>"
        f"<b>Connected:</b> {connected}/{total} ({connected*100//total}%)<br>"
        f"<b>Speed:</b> {speed_s}<br><br>"
        '<b>Connection Quality</b>'
        '<table style="width:100%;border-collapse:collapse;margin:4px 0 8px 0;">'
        f"{q_rows}</table>"
        '<b>Latency</b> <span style="color:#888">(min / avg / max)</span><br>'
        f"Ping: {ping_s} ms<br>"
        f"HTTP: {http_s} ms<br><br>"
        f'<b>Networks</b><table style="border-collapse:collapse;">{net_rows}</table>'
        "</div>"
    )


def _compute_regions(geo: list[dict], cell_deg: float = 0.01) -> list[dict]:
    """Aggregate GPS points into grid cells and compute quality stats per cell.

    cell_deg ~0.01 degree ≈ ~1.1 km latitude, ~0.7 km longitude in Austria.
    """
    from collections import defaultdict

    cells: dict[tuple[float, float], list[dict]] = defaultdict(list)
    for p in geo:
        key = (round(p["latitude"] / cell_deg) * cell_deg,
               round(p["longitude"] / cell_deg) * cell_deg)
        cells[key].append(p)

    regions = []
    for (clat, clon), points in cells.items():
        n = len(points)
        quals = [get_quality(p["ping_ms"], p["http_latency_ms"]) for p in points]
        bad = sum(1 for q in quals if q in ("poor", "none"))
        bad_pct = bad / n * 100

        pings = [p["ping_ms"] for p in points if p["ping_ms"] is not None]
        avg_ping = sum(pings) / len(pings) if pings else None

        regions.append({
            "lat": clat, "lon": clon,
            "count": n,
            "bad_pct": bad_pct,
            "bad_count": bad,
            "avg_ping": avg_ping,
            "cell_deg": cell_deg,
        })

    regions.sort(key=lambda r: r["bad_pct"], reverse=True)
    return regions


def generate_map(csv_path: Path, output_path: str | None = None) -> Path | None:
    try:
        import folium
        from branca.element import Element
    except ImportError:
        print(
            "ERROR: folium is not installed. Install it with:\n"
            "  pip install folium branca"
        )
        return None

    data = _read_csv(csv_path)
    geo = [d for d in data if d["latitude"] is not None and d["longitude"] is not None]

    if not geo:
        print("No GPS data in the recording – cannot generate map.")
        return None

    n_conn = sum(1 for d in data if d["connected"])
    print(
        f"Loaded {len(data)} samples "
        f"({len(geo)} with GPS, {n_conn} with connectivity)"
    )

    avg_lat = sum(p["latitude"] for p in geo) / len(geo)
    avg_lon = sum(p["longitude"] for p in geo) / len(geo)

    m = folium.Map(location=[avg_lat, avg_lon], zoom_start=11, tiles="OpenStreetMap")

    # Colored polyline segments (quality)
    for i in range(len(geo) - 1):
        p1, p2 = geo[i], geo[i + 1]
        quality = get_quality(p1["ping_ms"], p1["http_latency_ms"])
        color = QUALITY_COLORS[quality]

        try:
            gap = (
                datetime.fromisoformat(p2["timestamp"])
                - datetime.fromisoformat(p1["timestamp"])
            ).total_seconds()
        except (ValueError, TypeError):
            gap = 0

        folium.PolyLine(
            [
                [p1["latitude"], p1["longitude"]],
                [p2["latitude"], p2["longitude"]],
            ],
            color=color,
            weight=5,
            opacity=0.85,
            dash_array="10 10" if gap > 30 else None,
        ).add_to(m)

    # Circle markers (network type)
    for p in geo:
        net_color = NETWORK_COLORS.get(p["network_type"], NETWORK_COLORS["other"])
        folium.CircleMarker(
            [p["latitude"], p["longitude"]],
            radius=6,
            color=net_color,
            fill=True,
            fill_color=net_color,
            fill_opacity=0.7,
            popup=folium.Popup(_popup_html(p), max_width=300),
        ).add_to(m)

    # Region quality overlay
    regions = _compute_regions(geo)
    for reg in regions:
        if reg["count"] < 2:
            continue
        half = reg["cell_deg"] / 2
        bad_pct = reg["bad_pct"]
        good_pct = 100 - bad_pct

        if bad_pct >= 50:
            color = "#e74c3c"
        elif bad_pct >= 25:
            color = "#f39c12"
        elif bad_pct >= 10:
            color = "#f4d03f"
        else:
            color = "#2ecc71"

        opacity = 0.20 if bad_pct < 10 else min(0.50, 0.20 + bad_pct / 150)

        ping_s = f'{reg["avg_ping"]:.0f} ms' if reg["avg_ping"] else "N/A"
        label = (
            "Excellent" if bad_pct < 10
            else "Good" if bad_pct < 25
            else "Degraded" if bad_pct < 50
            else "Problem Zone"
        )
        tip = (
            f'<div style="font-family:Arial,sans-serif;font-size:12px;">'
            f"<b>{label}</b><br>"
            f"Samples: {reg['count']}<br>"
            f"Good/Excellent: {good_pct:.0f}%<br>"
            f'Poor/No-conn: {reg["bad_count"]}/{reg["count"]} '
            f"({bad_pct:.0f}%)<br>"
            f"Avg ping: {ping_s}</div>"
        )

        folium.Rectangle(
            bounds=[
                [reg["lat"] - half, reg["lon"] - half],
                [reg["lat"] + half, reg["lon"] + half],
            ],
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=opacity,
            weight=1,
            popup=folium.Popup(tip, max_width=220),
        ).add_to(m)

    # Summary statistics panel
    stats_html = _summary_stats(data, geo)
    m.get_root().html.add_child(Element(stats_html))

    # Legend
    m.get_root().html.add_child(Element(_legend_html()))

    # Fit bounds with padding
    lats = [p["latitude"] for p in geo]
    lons = [p["longitude"] for p in geo]
    m.fit_bounds(
        [[min(lats), min(lons)], [max(lats), max(lons)]],
        padding=[30, 30],
    )

    MAPS_DIR.mkdir(exist_ok=True)
    if output_path is None:
        out = MAPS_DIR / f"{csv_path.stem}.html"
    else:
        out = Path(output_path)

    m.save(str(out))
    print(f"Map saved to {out}")
    return out


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _latest_csv() -> Path | None:
    if not DATA_DIR.exists():
        return None
    csvs = sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def cmd_record(args, config: dict) -> None:
    if args.interval:
        config["interval_seconds"] = args.interval
    try:
        asyncio.run(record(config))
    except KeyboardInterrupt:
        print("\n\nRecording stopped.")
        latest = _latest_csv()
        if latest:
            print(f"Data saved to {latest}")
            print("Generate map with:  python tracker.py map")


def cmd_map(args, config: dict) -> None:
    if args.file:
        csv_path = Path(args.file)
    else:
        csv_path = _latest_csv()

    if csv_path is None or not csv_path.exists():
        print("No recording found. Record a trip first:  python tracker.py record")
        sys.exit(1)

    print(f"Generating map from {csv_path} ...")
    out = generate_map(csv_path, args.output)
    if out:
        webbrowser.open(str(out.resolve()))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train Connectivity Tracker – record and visualize "
        "WiFi quality on Austrian train trips",
    )
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("record", help="Start recording connectivity data")
    rec.add_argument(
        "-i",
        "--interval",
        type=int,
        help="Sampling interval in seconds (overrides config.json)",
    )

    mp = sub.add_parser("map", help="Generate interactive map from recorded data")
    mp.add_argument(
        "-f", "--file", help="Path to CSV file (default: latest recording)"
    )
    mp.add_argument("-o", "--output", help="Output HTML file path")

    args = parser.parse_args()
    config = load_config()

    if args.command == "record":
        cmd_record(args, config)
    elif args.command == "map":
        cmd_map(args, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
