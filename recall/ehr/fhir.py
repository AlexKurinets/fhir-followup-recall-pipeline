"""Read-only FHIR R4 source: capability check, paged searches, single reads."""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from .smart import SmartBackendClient

FHIR_JSON = "application/fhir+json"


class FhirError(RuntimeError):
    pass


class FhirSource:
    def __init__(self, base_url: str, auth: SmartBackendClient, http: httpx.Client, page_size: int = 200):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.http = http
        self.page_size = page_size
        self.requests_made = 0

    def _get(self, url: str, params: dict | None = None, authed: bool = True) -> dict:
        headers = {"Accept": FHIR_JSON}
        if authed:
            headers["Authorization"] = f"Bearer {self.auth.token()}"
        response = self.http.get(url, params=params, headers=headers)
        self.requests_made += 1
        if response.status_code != 200:
            raise FhirError(f"GET {url} returned {response.status_code}: {response.text[:200]}")
        return response.json()

    def capability_statement(self) -> dict:
        return self._get(f"{self.base_url}/metadata", authed=False)

    def search(self, resource_type: str, elements: str | None = None) -> Iterator[dict]:
        params = {"_count": str(self.page_size)}
        if elements:
            params["_elements"] = elements
        bundle = self._get(f"{self.base_url}/{resource_type}", params=params)
        while True:
            for entry in bundle.get("entry") or []:
                resource = entry.get("resource") or {}
                if resource.get("resourceType") == resource_type:
                    yield resource
            next_url = next((link["url"] for link in bundle.get("link") or [] if link.get("relation") == "next"), None)
            if not next_url:
                return
            bundle = self._get(next_url)

    def read(self, resource_type: str, resource_id: str) -> dict:
        return self._get(f"{self.base_url}/{resource_type}/{resource_id}")


def supported_resources(capability: dict) -> set[str]:
    supported = set()
    for rest in capability.get("rest") or []:
        if rest.get("mode", "server") != "server":
            continue
        for res in rest.get("resource") or []:
            interactions = {i.get("code") for i in res.get("interaction") or []}
            if not interactions or "search-type" in interactions or "read" in interactions:
                supported.add(res.get("type"))
    return supported
