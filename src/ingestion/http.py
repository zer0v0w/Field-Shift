"""HTTP helpers with bounded retries and explicit timeouts."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_session(
    user_agent: str,
    retries: int = 3,
    backoff: float = 0.6,
    pool_size: int = 16,
) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "POST"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    return session


def get_json(
    session: requests.Session,
    url: str,
    *,
    timeout: float = 60,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = session.get(url, timeout=timeout, headers=headers, params=params)
    response.raise_for_status()
    return response.json()


def post_json(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 60,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    response = session.post(url, json=payload, timeout=timeout, headers=merged)
    response.raise_for_status()
    return response.json()
