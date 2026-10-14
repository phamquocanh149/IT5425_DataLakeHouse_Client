import time

import httpx

RETRYABLE = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


def backoff(retry_index, sleep=time.sleep):
    sleep(min(2 ** retry_index, 30))
