"""Operator channel: one text out per incident, one verified reply back.

    channel.send(event_id, text) -> {"sent": True, ...}       the text, with a one-time code
    channel.reply(event_id, "ALLOW"|"BLOCK", code) -> (ok, why)
    channel.take_approval(event_id) -> bool                   consumed by a release, once, and
                                                              only within APPROVAL_TTL_S of the ALLOW

Why a code: texts are easy to forge, so a bare "ALLOW" proves nothing. Each question
carries a fresh 6-digit code, tied to its event id, good for one reply and for CODE_TTL_S.
Five wrong codes void the question (fail closed: nothing is released). A reply never
releases anything by itself: release still re-runs the safety checks (agent/tools_act.py).

Backend: the "phone" panel on the dashboard (GET /operator/inbox shows the texts and
codes, as a phone would). A real SMS backend would send `text + code` and parse
"ALLOW 123456"; it must use the same reply() so the rules stay in one place.
Demo-grade: questions live in memory; a restart voids them (safe side).
"""
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

CODE_TTL_S = 15 * 60
APPROVAL_TTL_S = 5 * 60   # a verified ALLOW that is not used for a release within this is void
MAX_BAD_CODES = 5
MAX_TEXT = 320
REPLIES = ("ALLOW", "BLOCK")


@dataclass
class Question:
    event_id: str
    text: str
    code: str
    ts: float
    expires: float
    bad: int = 0
    reply: str | None = None      # set once, by a verified reply
    void: bool = False


class OperatorChannel:
    def __init__(self, store=None, ttl_s: float = CODE_TTL_S, clock=time.time,
                 approval_ttl_s: float = APPROVAL_TTL_S):
        self.store, self.ttl_s, self._clock = store, ttl_s, clock
        self.approval_ttl_s = approval_ttl_s
        self._q: dict[str, Question] = {}
        self._approved: dict[str, float] = {}   # event id -> time of the verified ALLOW
        self._lock = threading.Lock()
        self.on_reply = None      # callable(event_id, reply), set by the runner

    def send(self, event_id: str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"sent": False, "error": "empty text"}
        if len(text) > MAX_TEXT:
            return {"sent": False, "error": f"text is {len(text)} characters, the limit is {MAX_TEXT}"}
        now = self._clock()
        with self._lock:
            if event_id in self._q:
                return {"sent": False, "error": "the operator was already texted about this event"}
            q = Question(event_id, text, f"{secrets.randbelow(10**6):06d}", now, now + self.ttl_s)
            self._q[event_id] = q
        if self.store is not None:     # the feed shows the text; the code stays on the "phone"
            self.store.save_agent_step(event_id, "text", text)
        return {"sent": True, "event_id": event_id, "characters": len(text)}

    def inbox(self) -> list[dict]:
        """What the operator's phone shows, oldest first."""
        now = self._clock()
        with self._lock:
            return [{"event_id": q.event_id, "text": q.text, "code": q.code, "ts": q.ts,
                     "expires_in_s": max(0, round(q.expires - now)), "reply": q.reply,
                     "open": q.reply is None and not q.void and q.expires > now}
                    for q in self._q.values()]

    def reply(self, event_id: str, reply: str, code: str) -> tuple[bool, str]:
        with self._lock:
            q = self._q.get(event_id)
            if q is None:
                return False, "no question is open for this event"
            if q.reply is not None:
                return False, "this question was already answered"
            if q.void:
                return False, "this question is void"
            if q.expires <= self._clock():
                q.void = True
                return False, "the code has expired"
            if reply not in REPLIES:
                return False, "reply must be ALLOW or BLOCK"
            if not hmac.compare_digest(str(code).encode(), q.code.encode()):
                q.bad += 1
                if q.bad >= MAX_BAD_CODES:
                    q.void = True
                return False, "wrong code"
            q.reply = reply
            if reply == "ALLOW":
                self._approved[event_id] = self._clock()
        if self.store is not None:
            self.store.save_agent_step(event_id, "reply", f"Operator replied {reply}")
        if self.on_reply:
            self.on_reply(event_id, reply)
        return True, "accepted"

    def decision(self, event_id: str) -> str | None:
        with self._lock:
            q = self._q.get(event_id)
            return q.reply if q else None

    def take_approval(self, event_id: str) -> bool:
        """True once per verified ALLOW, and only within approval_ttl_s of it. A release
        consumes it; an expired one is dropped and releases nothing."""
        with self._lock:
            given = self._approved.pop(event_id, None)
            return given is not None and self._clock() - given <= self.approval_ttl_s

    def clear(self) -> None:
        with self._lock:
            self._q.clear()
            self._approved.clear()
