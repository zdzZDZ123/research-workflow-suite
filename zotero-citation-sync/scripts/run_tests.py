"""Run portable tests and write auditable, machine-readable evidence."""
import argparse
import io
import json
import platform
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, default=ROOT / 'private/test-results.json')
args = parser.parse_args()
stream = io.StringIO()
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
args.output.parent.mkdir(parents=True, exist_ok=True)
payload = {'checked_at': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
           'python': platform.python_version(), 'tests': result.testsRun, 'passed': result.wasSuccessful(),
           'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
           'official_mac_word_tested': False, 'log': stream.getvalue()}
args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k: v for k, v in payload.items() if k != 'log'}, ensure_ascii=False))
if not result.wasSuccessful(): print(stream.getvalue())
sys.exit(0 if result.wasSuccessful() else 1)
