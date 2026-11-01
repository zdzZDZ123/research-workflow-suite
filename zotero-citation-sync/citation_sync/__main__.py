from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

from .docx_read import read_docx
from .environment import diagnose, write_report
from .server import serve
from .store import Store
from .util import SyncError, write_json


def main():
    parser = argparse.ArgumentParser(description='Zotero Citation Sync 本地确认式引文工作台')
    parser.add_argument('--home', type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    sub = parser.add_subparsers(dest='command')
    diagnostic = sub.add_parser('diagnose')
    diagnostic.add_argument('--output', type=Path, default=Path.cwd())
    inspect = sub.add_parser('inspect')
    inspect.add_argument('document', type=Path)
    inspect.add_argument('--output', type=Path, required=True)
    recovery = sub.add_parser('recover', help='检查中断记录；提供检查所得 plan_id 才执行恢复')
    recovery.add_argument('--apply-plan')
    args = parser.parse_args()
    default_home = (Path.home() / 'Library/Application Support/Zotero Citation Sync' if platform.system() == 'Darwin'
                    else Path(__file__).resolve().parents[1] / 'private/app')
    try:
        if args.command == 'diagnose':
            args.output.mkdir(parents=True, exist_ok=True)
            result = diagnose()
            write_report(args.output, result)
            print(args.output / 'environment_report.md')
        elif args.command == 'inspect':
            result = read_docx(args.document)
            write_json(args.output, result)
            print(json.dumps({'issues': result['issues'], 'citation_fields': len(result['citations']),
                              'word_dynamic_verified': False, 'sha256': result['sha256']}))
        elif args.command == 'recover':
            store = Store(args.home or default_home)
            report = store.recover(args.apply_plan) if args.apply_plan else store.inspect_recovery()
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            serve(args.home or default_home, args.port, not args.no_browser)
    except SyncError as exc:
        parser.exit(1, f'{exc.code}: {exc.detail}\n')


if __name__ == '__main__':
    main()
