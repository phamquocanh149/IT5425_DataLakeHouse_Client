import httpx


class APIClient:
    def __init__(self, config, transport=None):
        self.config = config
        self.http = httpx.Client(timeout=config.timeout, verify=True, follow_redirects=False, transport=transport)

    def close(self):
        self.http.close()

    def health(self):
        return self.http.get(self.config.base_url + "/health")

    def openapi(self):
        return self.http.get(self.config.base_url + "/openapi.json")

    def uploads(self):
        return self.http.get(self.config.base_url + "/uploads", headers={"X-API-Key": self.config.api_key})

    def upload(self, path, filename, source, source_date):
        # Reopen on every attempt so retries start at byte zero. HTTPX streams file objects.
        with path.open("rb") as stream:
            return self.http.post(self.config.base_url + "/upload",
                headers={"X-API-Key": self.config.api_key},
                data={"source": source, "source_date": source_date},
                files={"file": (filename, stream, "application/octet-stream")})
