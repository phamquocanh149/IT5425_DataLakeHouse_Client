import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from client.api_client import APIClient
from client.config import Config


def main():
    api = None
    try:
        config = Config.load(require_key=False)
        api = APIClient(config)
        response = api.health()
        print(f"GET /health: HTTP {response.status_code}")
        text = response.text
        print(text.replace(config.api_key, "[REDACTED]") if config.api_key else text)
        response.raise_for_status()
        schema = api.openapi()
        print(f"GET /openapi.json: HTTP {schema.status_code}")
        if schema.status_code == 200:
            print("Endpoints:", ", ".join(schema.json().get("paths", {})))
        return 0
    except httpx.ConnectError:
        print("Connection failed: check hostname/DNS, server port, VPN and TLS certificate trust.")
    except httpx.TimeoutException:
        print("Server request timed out.")
    except httpx.HTTPError as exc:
        print("Server check failed:", type(exc).__name__)
    except ValueError as exc:
        print(str(exc))
    finally:
        if api:
            api.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
