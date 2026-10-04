"""Run current offline lifecycle checks. Original audit evidence stays in docs."""
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "engine"))
suite = unittest.defaultTestLoader.discover(str(root / "engine/tests"), pattern="test_daily_service_unittest.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
