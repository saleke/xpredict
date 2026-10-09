"""Run existing Scalper regressions against the research candidate in this process."""
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'engine'))


def main():
    from batched_repository_candidate import BatchedRepository
    import lisa.scalper.repository
    import lisa.scalper.service
    import lisa.scalper.browser
    import lisa.providers.scalper
    import pytest
    with ExitStack() as stack:
        for module in (lisa.scalper.repository, lisa.scalper.service, lisa.scalper.browser, lisa.providers.scalper):
            stack.enter_context(patch.object(module, 'ScalperRepository', BatchedRepository))
        return pytest.main([str(ROOT / 'engine/tests/test_scalper_unittest.py'),
            str(ROOT / 'engine/tests/test_scalper_browser.py'), *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
