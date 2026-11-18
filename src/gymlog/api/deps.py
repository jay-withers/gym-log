"""The passcode gate, from jay-withers/appkit (`appkit.passcode`).

This is reachable from the public internet the moment ingress is external, so the
gate defaults **on** and a missing passcode fails closed rather than open. It is
a shared passcode rather than Entra sign-in: the data behind it is a list of how
much someone lifted, and an Entra round trip on a phone between sets would cost
more than it protects. appkit's module docstring has the rest of the reasoning.
"""

from __future__ import annotations

from appkit.passcode import PasscodeGate

from ..settings import secret, settings

COOKIE_NAME = "gymlog_session"

gate = PasscodeGate(COOKIE_NAME, settings=settings, secret=secret)

issue = gate.issue
valid = gate.valid
check_passcode = gate.check_passcode
require_session = gate.require_session
