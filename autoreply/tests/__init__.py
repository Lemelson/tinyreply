"""Test package guard: nothing in the test suite may ever reach a real service.

Any code path that forgets to point at a local fake fails to connect instead of calling Meta or Telegram.
"""
import os

os.environ.setdefault("IG_API_BASE", "http://127.0.0.1:9")
os.environ.setdefault("TG_API_BASE", "http://127.0.0.1:9")
for _base in ("IG_API_BASE", "TG_API_BASE"):
    if not os.environ[_base].startswith("http://127.0.0.1:"):
        raise RuntimeError(f"{_base} must point at 127.0.0.1 during tests, got {os.environ[_base]!r}")
