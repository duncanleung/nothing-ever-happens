"""RSA-PSS request signing for the Kalshi trade API (v2).

Kalshi requires three headers per request: KALSHI-ACCESS-KEY (API key id),
KALSHI-ACCESS-TIMESTAMP (unix ms), and KALSHI-ACCESS-SIGNATURE (base64
RSA-PSS signature over ``f"{timestamp_ms}{METHOD}{path}"`` — path only, no
query string, no host).
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

DEFAULT_TIMEOUT_SEC = 10.0
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


class KalshiAuthError(RuntimeError):
    pass


class KalshiApiError(RuntimeError):
    """A non-2xx response from the Kalshi API, with the parsed error body attached."""

    def __init__(self, method: str, path: str, status_code: int, body: str) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(f"Kalshi API error {status_code} on {method} {path}: {body}")


def _extract_error_body(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:500]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code", "")
            message = error.get("message", "")
            return f"{code}: {message}".strip(": ")
        return str(payload)[:500]
    return str(payload)[:500]


def load_private_key(private_key_path: str) -> RSAPrivateKey:
    path = Path(private_key_path)
    if not path.exists():
        raise KalshiAuthError(f"Kalshi private key not found at {private_key_path}")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise KalshiAuthError(f"Could not parse Kalshi private key at {private_key_path}: {exc}") from exc
    if not isinstance(key, RSAPrivateKey):
        raise KalshiAuthError(f"Key at {private_key_path} is not an RSA private key")
    return key


def sign_message(private_key: RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


class KalshiAuthSession:
    """Signs every request with the RSA-PSS scheme Kalshi's trade API requires.

    Callers must pass query parameters via the ``params`` kwarg (never
    embedded in ``path``) — the signature covers the path only, so a
    caller-embedded query string would sign correctly but Kalshi would
    still reject the mismatched request.
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        if not api_key_id:
            raise KalshiAuthError("KALSHI_API_KEY_ID is required")
        self._key_id = api_key_id
        self._private_key = load_private_key(private_key_path)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self._request("DELETE", path, **kwargs)

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KalshiApiError(method, path, response.status_code, _extract_error_body(response)) from exc
        if not response.content:
            return {}
        return response.json()

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method}{path}"
        headers = {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": sign_message(self._private_key, message),
            "Content-Type": "application/json",
        }
        headers.update(kwargs.pop("headers", None) or {})
        url = f"{self._base_url}{path}"
        kwargs.setdefault("timeout", self._timeout)
        return self._session.request(method, url, headers=headers, **kwargs)
