"""An OpenID Connect provider that lives entirely inside this process.

A HELPER, not a suite -- `scripts/run_backend_suites.sh` and
`scripts/coverage_check.sh` both glob `test_*.py`, so this filename is never
executed as one. Same role as `backend/tests/fixtures/`.

There is no IdP to develop against, so the provider is faked at the HTTP
boundary with `httpx.MockTransport` -- the same shape `test_nces.py` uses for
NCES. What that buys is that nothing about the app's own verification is stubbed
out: this module signs a REAL RS256 id_token with a REAL RSA key and publishes a
REAL JWKS, and `app/oidc.py` runs its real joserfc/Authlib path against it. A
test that forges a token with the wrong key, or asks for `alg: none`, is
therefore testing the verifier rather than testing a mock's `return True`.

The keypair is generated ONCE at import (~50-100ms) and reused; a second,
otherwise-identical keypair exists so a forgery can carry the right `kid` and
still fail the signature.

What the fake cannot do, stated so nobody mistakes coverage for interop: it does
not prove this app works with Entra ID, Okta or Keycloak. It proves the app's own
logic is right. Real-provider verification needs a real provider.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt as jose_jwt
from joserfc.jwk import OctKey, RSAKey

ISSUER = "https://idp.example.test"
CLIENT_ID = "ipeds-oracle-test"
CLIENT_SECRET = "test-client-secret"
KID = "test-key-1"
DEFAULT_EMAIL = "jdoe@example.edu"


def _pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


_SIGNING_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
# Same kid, different key: the only way to prove the signature is actually
# checked rather than the kid merely being matched.
_FORGERY_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))

SIGNING_KEY = RSAKey.import_key(_SIGNING_PEM,
                                parameters={"kid": KID, "use": "sig", "alg": "RS256"})
FORGERY_KEY = RSAKey.import_key(_FORGERY_PEM,
                                parameters={"kid": KID, "use": "sig", "alg": "RS256"})
# A symmetric key some providers really do publish in their JWKS. It exists so a
# test can make RS->HS confusion actually REACHABLE: against an RSA-only key set
# joserfc refuses an HS256 token because no key matches the kid, which is the
# right outcome for the wrong reason and would evaporate here.
HS_KID = "oct-1"
SYMMETRIC_KEY = OctKey.import_key("a-symmetric-key-a-sloppy-provider-published",
                                  parameters={"kid": HS_KID, "use": "sig", "alg": "HS256"})
PUBLIC_JWKS = {"keys": [SIGNING_KEY.as_dict(private=False)]}


def jwks_with_symmetric_key() -> dict:
    return {"keys": [SIGNING_KEY.as_dict(private=False), SYMMETRIC_KEY.as_dict()]}


class FakeIdp:
    """One provider, configurable per test.

    The knobs are the things a test needs to break on purpose. Everything else
    behaves like a conforming provider, so a test that changes nothing is
    exercising the happy path.
    """

    def __init__(self) -> None:
        # Echoed into the id_token. A real provider remembers the nonce from the
        # authorize request; the fake never sees that request (the app only
        # BUILDS the URL -- a browser would visit it), so `start_login` below
        # lifts it out of the URL and sets it here.
        self.nonce: str | None = None
        # S256 challenge from the authorization URL. The /token handler checks
        # the submitted verifier against it, so a build that dropped PKCE fails.
        self.expected_challenge: str | None = None

        self.claims: dict = {}          # merged over the defaults
        self.drop_claims: tuple = ()    # claims to remove entirely
        self.sign_with_forgery = False
        self.alg = "RS256"
        self.kid = KID                  # a different kid = a key the app has never seen
        self.publish_symmetric_key = False   # see jwks_with_symmetric_key
        self.discovery: dict | None = None   # replaces the whole document
        self.jwks_status = 200
        self.token_status = 200
        self.token_body: dict | None = None  # replaces the whole token response
        self.omit_id_token = False

        self.token_requests: list[dict] = []
        self.jwks_requests = 0
        self.discovery_requests = 0
        self.seen_urls: list[str] = []

    # --- what the provider serves ------------------------------------------
    def discovery_document(self) -> dict:
        if self.discovery is not None:
            return self.discovery
        return {"issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
                "response_types_supported": ["code"],
                "id_token_signing_alg_values_supported": ["RS256"]}

    def id_token(self) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1",
                  "email": DEFAULT_EMAIL, "email_verified": True,
                  "nonce": self.nonce, "iat": now, "exp": now + 300}
        claims.update(self.claims)
        for name in self.drop_claims:
            claims.pop(name, None)
        if self.alg.startswith("HS"):
            key = SYMMETRIC_KEY
        else:
            key = FORGERY_KEY if self.sign_with_forgery else SIGNING_KEY
        return jose_jwt.encode({"alg": self.alg, "kid": self.kid}, claims, key)

    # --- the transport ------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen_urls.append(str(request.url))
        path = request.url.path

        if path == "/.well-known/openid-configuration":
            self.discovery_requests += 1
            return httpx.Response(200, json=self.discovery_document())

        if path == "/jwks":
            self.jwks_requests += 1
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status, json={"error": "nope"})
            return httpx.Response(
                200, json=jwks_with_symmetric_key() if self.publish_symmetric_key
                else PUBLIC_JWKS)

        if path == "/token":
            body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(body)
            if self.expected_challenge is not None:
                verifier = body.get("code_verifier", "")
                assert create_s256_code_challenge(verifier) == self.expected_challenge, (
                    "the token exchange did not carry the PKCE verifier matching "
                    "the challenge in the authorization URL")
            if self.token_status != 200:
                return httpx.Response(self.token_status,
                                      json={"error": "invalid_grant"})
            if self.token_body is not None:
                return httpx.Response(200, json=self.token_body)
            payload = {"access_token": "fake-access-token", "token_type": "Bearer"}
            if not self.omit_id_token:
                payload["id_token"] = self.id_token()
            return httpx.Response(200, json=payload)

        return httpx.Response(404, json={"error": "no such endpoint"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
