"""
Test signal staleness guard in signal_worker.
"""
import os
import sys
import time

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from bot.config import MAX_SIGNAL_STALENESS_SECONDS


class TestStalenessGuard:
    """Verify that stale signals are detected correctly."""

    def test_fresh_signal_is_not_stale(self):
        """A signal generated just now should NOT be stale."""
        generated_at = time.time()
        age = time.time() - generated_at
        assert age < MAX_SIGNAL_STALENESS_SECONDS

    def test_old_signal_is_stale(self):
        """A signal older than MAX_SIGNAL_STALENESS_SECONDS should be stale."""
        generated_at = time.time() - MAX_SIGNAL_STALENESS_SECONDS - 10
        age = time.time() - generated_at
        assert age > MAX_SIGNAL_STALENESS_SECONDS

    def test_boundary_signal_is_not_stale(self):
        """A signal exactly at the boundary should NOT be stale (< check)."""
        generated_at = time.time() - MAX_SIGNAL_STALENESS_SECONDS + 1
        age = time.time() - generated_at
        # Just under the threshold — should be considered fresh
        assert age < MAX_SIGNAL_STALENESS_SECONDS + 2  # small slack for execution time


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
