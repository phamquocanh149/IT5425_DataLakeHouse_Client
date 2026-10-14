import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Generate MOCK transport test data, not validated science data")
    parser.add_argument("--source", choices=["usdm", "usgs"], default="usdm")
    args = parser.parse_args()
    path = ROOT / "data" / "pending" / f"sample_{args.source}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print("Sample already exists; preserved:", path.name)
        return 0
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        if args.source == "usdm":
            writer.writerow(["time", "lat", "lon", "drought"])
            writer.writerows([["2021-06-22", 39.25, -95.25, "D1"], ["2021-06-29", 39.25, -95.25, "D2"]])
        else:
            writer.writerow(["time", "site_id", "streamflow_cfs"])
            writer.writerows([["2021-06-22", "MOCK_SITE", 10.5], ["2021-06-29", "MOCK_SITE", 12.0]])
    print("Created MOCK data:", path.name, f"({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
