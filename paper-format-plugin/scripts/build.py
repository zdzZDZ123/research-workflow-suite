"""Deterministic independent XPI; never bundle models, keys, papers or job records."""
import json
import zipfile
import hashlib
from pathlib import Path

root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'extension/manifest.json').read_text(encoding='utf-8'))
dist=root/'dist'
dist.mkdir(exist_ok=True)
files={path.relative_to(root/'extension').as_posix():path.read_bytes() for path in sorted((root/'extension').rglob('*')) if path.is_file()}
def package(name, contents):
    output=dist/name
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(contents.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data)
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    (dist/(output.name+'.sha256')).write_text(digest+'  '+output.name+'\n',encoding='ascii')
    print(str(output))
    print(digest)
package(f'paper-formatter-{manifest["version"]}.xpi',files)
binding=root/'.deployment.local.json'
if binding.is_file():
    config=json.loads(binding.read_text(encoding='utf-8-sig'))
    if set(config)-{'schema_version','python','project','home'}:
        raise ValueError('Deployment binding must never contain credentials or document data')
    package(f'paper-formatter-{manifest["version"]}-local.xpi',
            {**files,'deployment.json':json.dumps(config,ensure_ascii=False,indent=2).encode('utf-8')})
