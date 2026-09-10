"""Run isolated business-control tests. Never load or clear real employee data."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_enterprise")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
