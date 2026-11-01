"""Build a portable source release from audited tracked files only."""
import hashlib
import zipfile
from pathlib import Path
from audit_public import ROOT, audit
names = audit()
out = ROOT / 'dist'
out.mkdir(exist_ok=True)
archive = out / 'research-workflow-suite-v0.1.1.zip'
manifest = []
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
    for name in sorted(names):
        data = (ROOT / name).read_bytes()
        z.writestr('research-workflow-suite/' + name, data)
        manifest.append(hashlib.sha256(data).hexdigest() + '  ' + name)
    z.writestr('research-workflow-suite/SHA256SUMS', '\n'.join(manifest) + '\n')
(archive.with_suffix('.zip.sha256')).write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n', encoding='ascii')
print(archive)
