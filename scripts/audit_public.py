"""Fail closed for private artifacts in the tracked release file set."""
import re
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def tracked():
    return subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().strip('\0').split('\0')

def audit():
    names = tracked()
    if not names or names == ['']: raise RuntimeError('No tracked source files')
    forbidden = {'.runtime', 'private', '.venv', '__pycache__', 'models', 'dist', '.git'}
    for name in names:
        p = ROOT / name
        if p.is_symlink(): raise RuntimeError('Symlink forbidden: ' + name)
        if forbidden.intersection(Path(name).parts) or p.name in ('deep-reading-database.json', '.deployment.local.json', '.env'):
            raise RuntimeError('Private path: ' + name)
        if p.suffix.lower() in ('.pdf','.docx','.sqlite','.sqlite3','.xpi','.pem','.key'):
            raise RuntimeError('Private/binary artifact: ' + name)
        s = p.read_text(encoding='utf-8-sig')
        if re.search(r'(?:ntn_|secret_|ghp_|gho_|sk-proj-)[A-Za-z0-9_-]{24,}', s):
            raise RuntimeError('Possible secret: ' + name)
        if re.search(r'[A-Z]:[\\/]+Users[\\/]+Administrator', s, re.I):
            raise RuntimeError('Machine-specific path: ' + name)
    print('Public-source audit passed:', len(names), 'files')
    return names

if __name__ == '__main__': audit()
