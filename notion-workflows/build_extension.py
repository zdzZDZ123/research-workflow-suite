"""Package this machine's Zotero extension without credentials or private notes."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
source = root / 'zotero-extension'
version = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))['version']
destination = root / 'dist' / ('paper-deep-reading-' + version + '.xpi')
destination.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(source.rglob('*')):
        if file.name == 'manifest.json':
            manifest = json.loads(file.read_text(encoding='utf-8'))
            # Zotero requires HTTPS even for local-only extensions. The reserved
            # .invalid domain cannot serve code; updates are installed locally.
            manifest['applications']['zotero']['update_url'] = 'https://updates.paper-deep-reading.invalid/updates.json'
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        elif file.is_file(): archive.write(file, file.relative_to(source).as_posix())
    deployment = {'python': sys.executable, 'runner': str(root / 'menu_runner.py'),
                  'relationRunner': str(root / 'paper_relations.py'), 'project': str(root)}
    archive.writestr('deployment.json', json.dumps(deployment, ensure_ascii=False))
    archive.writestr('deployment.js', 'var DEPLOYMENT = ' + json.dumps(deployment, ensure_ascii=False) + ';')
(destination.parent / 'updates.json').write_text(json.dumps({'addons': {'paper-deep-reading@local.research': {'updates': []}}}), encoding='utf-8')
print(json.dumps({'path': str(destination), 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}))
