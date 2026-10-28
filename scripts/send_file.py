import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client.api_client import APIClient
from client.config import Config
from client.uploader import UploadError, upload_file
from client.locking import JobLocked


def main():
    parser = argparse.ArgumentParser(description="Upload raw climate data using the existing Server API")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--source", required=True, choices=["usdm", "noaa", "usgs", "nldas"])
    parser.add_argument("--date", required=True)
    parser.add_argument("--dataset-key", help="Stable product/station/indicator identity; defaults to original filename")
    args = parser.parse_args()
    api = None
    try:
        config = Config.load()
        api = APIClient(config)
        result = upload_file(api, args.file, args.source, args.date, dataset_key=args.dataset_key)
        output = json.dumps({k: v for k, v in result.items() if k != "server_metadata"}, indent=2)
        print(output.replace(config.api_key, "[REDACTED]"))
        return 0
    except JobLocked:
        print("Job is being handled by another worker; inspect pending/uploaded metadata for confirmation.")
        return 1
    except (ValueError, OSError, UploadError) as exc:
        print("Upload failed:", str(exc) if not isinstance(exc, OSError) else type(exc).__name__)
        return 1
    finally:
        if api:
            api.close()


if __name__ == "__main__":
    sys.exit(main())
