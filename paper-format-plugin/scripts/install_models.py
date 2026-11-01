"""Network access is permitted only during explicit environment installation."""
import argparse
from pathlib import Path
from paper_formatter.contracts import write_json,utc_now

parser=argparse.ArgumentParser()
parser.add_argument('--directory',required=True)
parser.add_argument('--verify-only',action='store_true')
args=parser.parse_args()
directory=Path(args.directory).resolve()
if not args.verify_only:
    from docling.utils.model_downloader import download_models
    download_models(output_dir=directory,with_picture_classifier=False,with_code_formula=True,progress=True)
required=('docling-project--docling-layout-heron','docling-project--docling-models','RapidOcr')
for name in required:
    if not (directory/name).is_dir() or not any((directory/name).rglob('*')):
        raise SystemExit('Missing model group: '+name)
files=[{'name':p.relative_to(directory).as_posix(),'size':p.stat().st_size} for p in directory.rglob('*') if p.is_file() and p.name!='paper-formatter-models.json']
write_json(directory/'paper-formatter-models.json',{'installed_at':utc_now(),'files':files,'required':required})
print('Local model resources ready: '+str(directory))
