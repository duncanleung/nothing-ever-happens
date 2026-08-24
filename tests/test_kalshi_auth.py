import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bot.exchange.kalshi_auth import KalshiAuthError, KalshiAuthSession, load_private_key, sign_message


@pytest.fixture
def rsa_key_path(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "kalshi_private.pem"
    path.write_bytes(pem)
    return path, private_key


def test_load_private_key_missing_file(tmp_path):
    with pytest.raises(KalshiAuthError, match="not found"):
        load_private_key(str(tmp_path / "missing.pem"))


def test_load_private_key_rejects_non_rsa_key(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "ed25519.pem"
    path.write_bytes(pem)

    with pytest.raises(KalshiAuthError, match="not an RSA private key"):
        load_private_key(str(path))


def test_sign_message_produces_verifiable_signature(rsa_key_path):
    _, private_key = rsa_key_path
    message = "1700000000000GET/portfolio/balance"

    signature_b64 = sign_message(private_key, message)

    public_key = private_key.public_key()
    public_key.verify(
        base64.b64decode(signature_b64),
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_kalshi_auth_session_requires_api_key_id(rsa_key_path):
    path, _ = rsa_key_path
    with pytest.raises(KalshiAuthError, match="KALSHI_API_KEY_ID"):
        KalshiAuthSession(api_key_id="", private_key_path=str(path), base_url="https://example.test")


def test_kalshi_auth_session_signs_request_headers(rsa_key_path, monkeypatch):
    path, private_key = rsa_key_path
    session = KalshiAuthSession(
        api_key_id="key-123",
        private_key_path=str(path),
        base_url="https://example.test/trade-api/v2",
    )

    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["kwargs"] = kwargs
        return "response"

    monkeypatch.setattr(session._session, "request", fake_request)

    result = session.get("/portfolio/balance", params={"ticker": "FOO"})

    assert result == "response"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://example.test/trade-api/v2/portfolio/balance"
    assert captured["kwargs"]["params"] == {"ticker": "FOO"}
    headers = captured["headers"]
    assert headers["KALSHI-ACCESS-KEY"] == "key-123"
    timestamp_ms = headers["KALSHI-ACCESS-TIMESTAMP"]
    assert timestamp_ms.isdigit()

    message = f"{timestamp_ms}GET/portfolio/balance"
    public_key = private_key.public_key()
    public_key.verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
