"""Synthetic UI-only recovery + withdrawn-request demo. Never connects to real Zotero."""
import argparse
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citation_sync.server import AppServer
from citation_sync.service import Service
from fixtures import FakeAPI, docx

parser = argparse.ArgumentParser()
parser.add_argument('--home', type=Path, required=True)
parser.add_argument('--port', type=int, default=8766)
args = parser.parse_args()
args.home.mkdir(parents=True, exist_ok=False)
source = docx(args.home / 'synthetic-withdrawn-request.docx')
api = FakeAPI()
api.data['connection']['version'] = 'SYNTHETIC UI TEST — no real Zotero'
service = Service(args.home / 'state', api)
service.scan(source)
api.data['items'][3]['data']['annotationComment'] = '[cite] 不要引用 10.5555/synthetic-b'
service.scan(source)
original = service.store._write_target
count = 0
def interrupt(path, value):
    global count
    count += 1
    if count == 2:
        raise OSError('synthetic interrupted commit')
    original(path, value)
try:
    with patch.object(service.store, '_write_target', side_effect=interrupt):
        with service.store.transaction() as state:
            service.store.stage_json(service.store.home / 'scans' / ('c' * 32 + '.json'), {'synthetic': True})
            state['recovery_fixture_ready'] = True
except OSError:
    pass
server = AppServer(args.home / 'state', args.port, service)
print(str(source.resolve()), flush=True)
print(server.origin, flush=True)
try: server.serve_forever()
finally: server.server_close()
