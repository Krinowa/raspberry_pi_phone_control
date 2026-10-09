"""Configuration for the localhost-only Withdraw Bot v2 mock."""

from pathlib import Path

HOST = "127.0.0.1"
HTTP_PORT = 9100
WS_PORT = 9101
APP_KEY = "mock-withdraw-v2"
APP_SECRET = "mock-withdraw-v2-secret"
API_PREFIX = "/api/admin-withdraw/v2"
AUTH_ENDPOINT = API_PREFIX + "/broadcasting/auth"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "password123"
LOCK_TTL_SECONDS = 600
SHORT_LOCK_TTL_SECONDS = 15
TOKEN_TTL_SECONDS = 3600
SESSION_MAX_SECONDS = 8 * 3600
IDLE_TIMEOUT_SECONDS = 30 * 60
PROOF_ROOT = Path(__file__).resolve().parent / "proofs"
