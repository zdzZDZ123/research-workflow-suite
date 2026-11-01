"""Local browser test harness. Synthetic metadata; no real Word compatibility claims."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citation_sync.server import AppServer
from citation_sync.service import Service
from fixtures import FakeAPI, docx

parser = argparse.ArgumentParser()
parser.add_argument('--home', type=Path, required=True)
parser.add_argument('--port', type=int, default=8766)
args = parser.parse_args()
args.home.mkdir(parents=True, exist_ok=True)
source = args.home / 'synthetic-browser-test.docx'
if not source.exists(): docx(source)
api = FakeAPI()
api.data['connection']['version'] = 'SYNTHETIC UI TEST — no real Zotero'
server = AppServer(args.home / 'state', args.port, Service(args.home / 'state', api))
print(str(source.resolve()), flush=True)
print(server.origin, flush=True)
try: server.serve_forever()
finally: server.server_close()
