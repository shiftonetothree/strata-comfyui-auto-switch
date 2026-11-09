"""auto-switch: an HTTP/WebSocket relay that keeps exactly one of several
mutually-exclusive GPU services running.

The relay owns the public ports of the services it front-ends and forwards each
request to the matching service, switching the active one only when the current
service has been idle for ``cooldown_seconds``.
"""

__version__ = "1.0.0"
