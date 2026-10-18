"""pytest entry for the unittest suite (``python -m unittest discover -s tests``
is the canonical runner; this lets ``pytest`` run the same tests safely)."""
import _netguard

_netguard.install()


def pytest_sessionfinish(session, exitstatus):
    if _netguard.VIOLATIONS:
        print("\nNETGUARD: refused network attempts:\n  "
              + "\n  ".join(_netguard.VIOLATIONS))
        session.exitstatus = 3
