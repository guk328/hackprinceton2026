"""Sources the gateway refuses to talk to. Checked by the proxy before anything is parsed.

In memory: a restart clears it (demo-grade; a real gateway would persist it and also push
the rule to the firewall in front).
"""
import ipaddress
import threading
import time

MAX_MINUTES = 24 * 60


class Blocklist:
    def __init__(self, clock=time.time):
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def block(self, ip: str, minutes: float = 60) -> float:
        """Block `ip` for `minutes` (1 to 1440). Returns the expiry time. Raises ValueError
        on something that is not an IP address."""
        ip = str(ipaddress.ip_address(ip))
        until = self._clock() + 60 * max(1.0, min(float(minutes), MAX_MINUTES))
        with self._lock:
            self._until[ip] = max(until, self._until.get(ip, 0))
            return self._until[ip]

    def is_blocked(self, ip: str) -> bool:
        with self._lock:
            until = self._until.get(ip)
            if until is None:
                return False
            if until <= self._clock():
                del self._until[ip]
                return False
            return True

    def all(self) -> dict[str, float]:
        now = self._clock()
        with self._lock:
            return {ip: t for ip, t in self._until.items() if t > now}

    def clear(self) -> None:
        with self._lock:
            self._until.clear()
