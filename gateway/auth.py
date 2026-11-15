"""Admin login for the dashboard. One password, one signed cookie, stdlib only.

    ADMIN_PASSWORD in .env. If it is missing, a random one is made for this run and
    logged once, so the gateway never runs with the operator controls open.

Guarded: the operator's phone (texts and reply codes), replying, reset. The feed, plant
view and agent log stay readable without login so a judge can watch.

The cookie is "<expiry>.<hmac>", signed with a key made at start-up (a restart logs
everyone out). HttpOnly and SameSite=Strict; replies must be JSON, so a foreign page
cannot post one. Wrong passwords are rate-limited per client address.
Demo-grade: plain HTTP on the hotspot (no Secure flag), one shared password, no users.
"""
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, Response

log = logging.getLogger(__name__)

COOKIE = "backflow_admin"
SESSION_S = 12 * 3600
MAX_FAILS, FAIL_WINDOW_S = 5, 60.0


class Auth:
    def __init__(self, password: str | None = None, clock=time.time):
        password = password if password is not None else os.getenv("ADMIN_PASSWORD", "")
        if not password:
            password = secrets.token_urlsafe(9)
            log.warning("ADMIN_PASSWORD is not set. Password for this run: %s", password)
        self._password = password.encode()
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._fails: dict[str, deque] = defaultdict(deque)

    def _sign(self, expiry: int) -> str:
        return hmac.new(self._key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def token(self) -> str:
        expiry = int(self._clock()) + SESSION_S
        return f"{expiry}.{self._sign(expiry)}"

    def valid(self, token: str | None) -> bool:
        try:
            expiry, sig = (token or "").split(".", 1)
            return hmac.compare_digest(sig, self._sign(int(expiry))) and int(expiry) > self._clock()
        except ValueError:
            return False

    def login(self, password: str, client: str, response: Response) -> None:
        now, q = self._clock(), self._fails[client]
        while q and now - q[0] > FAIL_WINDOW_S:
            q.popleft()
        if len(q) >= MAX_FAILS:
            raise HTTPException(429, "too many attempts, wait a minute")
        if not hmac.compare_digest(password.encode(), self._password):
            q.append(now)
            raise HTTPException(401, "wrong password")
        q.clear()
        response.set_cookie(COOKIE, self.token(), max_age=SESSION_S, httponly=True,
                            samesite="strict", path="/")

    def is_admin(self, request: Request) -> bool:
        return self.valid(request.cookies.get(COOKIE))

    def require(self, request: Request) -> None:
        """FastAPI dependency: 401 unless the request carries a valid admin cookie."""
        if not self.is_admin(request):
            raise HTTPException(401, "admin login required")
