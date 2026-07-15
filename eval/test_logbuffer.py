"""The admin log ring buffer must never retain secrets.

The Logs admin view is readable by any admin, and in dev the mailer logs full
email bodies (with magic-link tokens). This guards that (a) the mail logger is
dropped from the buffer and (b) token/bearer values are redacted from whatever
else is captured.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.logbuffer import RingBufferHandler

FAILURES = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗'} {name}")
    if not cond:
        FAILURES.append(name)


def _rec(name, msg):
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, None)


def run():
    print("log ring-buffer secret scrubbing:")
    h = RingBufferHandler(capacity=50)
    h.emit(_rec("ipeds.mail", "[DEV] link https://x/api/auth/verify?token=SECRET123 body"))
    h.emit(_rec("ipeds.app", "consumed verify?token=ABCDEF12345 for user"))
    h.emit(_rec("ipeds.app", "header Authorization: Bearer sk-or-v1-abc123 kept-tail"))
    recs = h.records()
    blob = str(recs)

    check("mail logger is excluded entirely", not any(r["name"] == "ipeds.mail" for r in recs))
    check("no mail-token leaks", "SECRET123" not in blob)
    check("token= value redacted", "ABCDEF12345" not in blob)
    check("bearer/api-key value redacted", "abc123" not in blob and "sk-or-v1-abc123" not in blob)
    check("non-secret content retained", any("kept-tail" in r["msg"] for r in recs))
    check("bounded capacity", h._buf.maxlen == 50)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL LOGBUFFER TESTS PASSED")


if __name__ == "__main__":
    run()
