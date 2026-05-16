"""CO2 -> HomeKit AirQuality (1-5) mapping for the cabinet.

Mapping per docs/server-api-spec.md §7.3:

    < 600 ppm    -> 1  Excellent
    600 - 800    -> 2  Good
    800 - 1000   -> 3  Fair
    1000 - 1200  -> 4  Inferior
    > 1200 ppm   -> 5  Poor

Use as a library:

    from co2_quality import co2_to_homekit_quality
    quality = co2_to_homekit_quality(750)   # -> 2

Use as a CLI (for plugins that exec a script and read stdout, e.g. a
custom homebridge plugin or a wrapper around homebridge-script):

    python3 co2_quality.py                  # fetch live CO2 from Flask
    python3 co2_quality.py --co2 750        # explicit ppm
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

# HomeKit Characteristic.AirQuality values
HOMEKIT_UNKNOWN   = 0
HOMEKIT_EXCELLENT = 1
HOMEKIT_GOOD      = 2
HOMEKIT_FAIR      = 3
HOMEKIT_INFERIOR  = 4
HOMEKIT_POOR      = 5


def co2_to_homekit_quality(co2_ppm: float | int | None) -> int:
    """Return HomeKit AirQuality (0=Unknown, 1=Excellent ... 5=Poor)."""
    if co2_ppm is None:
        return HOMEKIT_UNKNOWN
    if co2_ppm < 600:   return HOMEKIT_EXCELLENT
    if co2_ppm < 800:   return HOMEKIT_GOOD
    if co2_ppm < 1000:  return HOMEKIT_FAIR
    if co2_ppm < 1200:  return HOMEKIT_INFERIOR
    return HOMEKIT_POOR


def fetch_current_co2(api_url: str) -> float | None:
    """GET the Flask readings endpoint and pull out co2_ppm. None on any failure."""
    try:
        with urllib.request.urlopen(api_url, timeout=5) as resp:
            return json.load(resp).get("co2_ppm")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Map a CO2 reading (ppm) to a HomeKit AirQuality value (1-5)."
    )
    parser.add_argument(
        "--co2", type=float, default=None,
        help="CO2 in ppm. If omitted, fetched from --api-url.",
    )
    parser.add_argument(
        "--api-url", default="http://localhost:5000/api/readings",
        help="Flask readings endpoint. Default: http://localhost:5000/api/readings.",
    )
    args = parser.parse_args()

    co2 = args.co2 if args.co2 is not None else fetch_current_co2(args.api_url)
    print(co2_to_homekit_quality(co2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
