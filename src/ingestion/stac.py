"""Minimal STAC client for the two public endpoints used by this prototype.

* AWS Earth Search (Sentinel-2 L2A) — no authentication.
* Microsoft Planetary Computer (MODIS, Landsat) — public items, SAS-signed
  asset URLs obtained from the Planetary Computer token endpoint.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from .cache import Cache
from .http import post_json

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
PLANETARY_COMPUTER = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_SIGN_ENDPOINT = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"


class StacClient:
    def __init__(
        self,
        cache: Cache,
        session: requests.Session,
        *,
        timeout: float = 60,
        max_age_s: float = 6 * 3600,
    ):
        self.cache = cache
        self.session = session
        self.timeout = timeout
        self.max_age_s = max_age_s
        self._sas_cache: dict[str, tuple[str, float]] = {}

    def search(
        self,
        collection: str,
        bbox: tuple[float, float, float, float],
        start: str,
        end: str,
        *,
        cloud_cover_max: float | None = None,
        limit: int = 100,
        max_items: int = 1000,
        endpoint: str = EARTH_SEARCH,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        key = json.dumps(
            {
                "collection": collection,
                "bbox": [round(v, 6) for v in bbox],
                "start": start,
                "end": end,
                "cloud": cloud_cover_max,
                "limit": limit,
                "max_items": max_items,
                "endpoint": endpoint,
            },
            sort_keys=True,
        )
        cached = self.cache.get_json("stac_search", key, max_age_s=self.max_age_s)
        if cached is not None:
            return cached["items"], {**cached.get("meta", {}), "cached": True}

        body: dict[str, Any] = {
            "collections": [collection],
            "bbox": list(bbox),
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
            "limit": limit,
        }
        if cloud_cover_max is not None:
            body["query"] = {"eo:cloud_cover": {"lt": cloud_cover_max}}

        items: list[dict[str, Any]] = []
        url = f"{endpoint}/search"
        pages = 0
        while url and len(items) < max_items and pages < 50:
            response = post_json(self.session, url, body, timeout=self.timeout)
            features = response.get("features", [])
            items.extend(features)
            pages += 1
            next_link = next(
                (link for link in response.get("links", []) if link.get("rel") == "next"), None
            )
            if not next_link:
                break
            if next_link.get("body"):
                if next_link.get("merge"):
                    body = {**body, **next_link["body"]}
                    url = f"{endpoint}/search"
                else:
                    body = next_link["body"]
                    url = f"{endpoint}/search"
            elif next_link.get("href"):
                url = next_link["href"]
            else:
                break

        items = items[:max_items]
        meta = {
            "collection": collection,
            "bbox": list(bbox),
            "datetime": body.get("datetime"),
            "endpoint": endpoint,
            "pages": pages,
            "returned": len(items),
            "cached": False,
            "accessed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        self.cache.set_json("stac_search", key, {"items": items, "meta": meta})
        return items, meta

    def sign_pc_asset(self, collection: str, href: str) -> str:
        if "blob.core.windows.net" not in href:
            return href
        # The per-collection token endpoint is not sufficient for collections
        # whose items live in more than one storage account (MODIS does), so
        # sign each asset href through the generic signer instead.
        now = time.time()
        cached = self._sas_cache.get(href)
        if cached and cached[1] > now + 60:
            return f"{href}{'&' if '?' in href else '?'}{cached[0]}"

        data, _ = self.cache.fetch_json(
            self.session,
            PC_SIGN_ENDPOINT,
            namespace="pc_sign",
            key=href,
            timeout=self.timeout,
            max_age_s=30 * 60,
            params={"href": href},
        )
        signed = data.get("href")
        if not signed:
            raise RuntimeError(f"Planetary Computer did not sign asset: {href}")
        token = signed.split("?", 1)[1] if "?" in signed else ""
        self._sas_cache[href] = (token, now + 25 * 60)
        return signed

    @staticmethod
    def item_date(item: dict[str, Any]) -> str | None:
        props = item.get("properties", {})
        for field in ("datetime", "start_datetime", "end_datetime"):
            value = props.get(field)
            if value:
                return str(value)[:10]
        item_id = item.get("id", "")
        # MODIS: MYD13Q1.A2024057.h20v05...
        for token in item_id.split("."):
            if len(token) == 7 and token.startswith("A") and token[1:].isdigit():
                year = int(token[1:5])
                doy = int(token[5:])
                return datetime.strptime(f"{year}-{doy:03d}", "%Y-%j").date().isoformat()
        return None

    @staticmethod
    def cloud_cover(item: dict[str, Any]) -> float | None:
        value = item.get("properties", {}).get("eo:cloud_cover")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def asset_href(item: dict[str, Any], *names: str) -> str | None:
        assets = item.get("assets", {})
        for name in names:
            if name in assets:
                return assets[name].get("href")
        return None

    @staticmethod
    def asset_band_meta(item: dict[str, Any], name: str) -> dict[str, Any]:
        asset = item.get("assets", {}).get(name, {})
        bands = asset.get("raster:bands") or []
        return bands[0] if bands else {}

    @staticmethod
    def sort_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: StacClient.item_date(item) or "")
