from __future__ import annotations

import json
import time
import urllib.request

UA = "Mozilla/5.0 (compatible; buffett-fund/1.0)"


def get(url: str, *, retries: int = 3, timeout: int = 20) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - network is best effort
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET failed: {url}: {last}")


def get_json(url: str) -> dict:
    return json.loads(get(url))
