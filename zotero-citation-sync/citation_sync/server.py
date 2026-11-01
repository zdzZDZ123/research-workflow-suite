from __future__ import annotations

import json
import platform
import secrets
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .environment import diagnose
from .service import Service
from .util import SyncError
from . import __version__

ASSETS = Path(__file__).resolve().parent / 'web'


def choose_docx():
    if platform.system() == 'Darwin':
        result = subprocess.run(['/usr/bin/osascript', '-e',
                                 'POSIX path of (choose file with prompt "选择 Word 论文 DOCX")'],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise SyncError('FILE_SELECTION_CANCELLED')
        return result.stdout.strip()
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        result = filedialog.askopenfilename(filetypes=[('Word DOCX', '*.docx')])
        root.destroy()
        return result
    except ImportError as exc:
        raise SyncError('PICKER_UNAVAILABLE', '请在输入框填写本地 DOCX 路径') from exc


class AppServer(HTTPServer):
    def __init__(self, home, port=0, service=None):
        self.service = service or Service(home)
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, data, mime='application/json; charset=utf-8'):
        body = json.dumps(data, ensure_ascii=False).encode() if mime.startswith('application/json') else data
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            return False
        return secrets.compare_digest(self.headers.get('X-Citation-Sync-Token', ''), self.server.token)

    def do_GET(self):
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            self.send(403, {'error': 'HOST_REJECTED'})
            return
        if self.path in ('/', '/app.js', '/style.css'):
            name = 'index.html' if self.path == '/' else self.path[1:]
            payload = (ASSETS / name).read_bytes().replace(b'__SESSION_TOKEN__', self.server.token.encode())
            mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8',
                    'style.css': 'text/css; charset=utf-8'}[name]
            self.send(200, payload, mime)
            return
        if not self.authorized():
            self.send(403, {'error': 'TOKEN_OR_ORIGIN_REJECTED'})
            return
        try:
            if self.path == '/api/status':
                service = self.server.service
                recovery = service.store.inspect_recovery()
                history, history_error = [], None
                if not recovery['required'] and not recovery['blockers']:
                    try:
                        history = service.history()
                    except SyncError as exc:
                        history_error = {'error': exc.code, 'detail': exc.detail}
                data = {'host': platform.system(), 'version': __version__, 'mode': 'official_confirmation_preview',
                        'bulk_write_enabled': False, 'mac_word_validation': 'pending', 'history': history,
                        'history_error': history_error, 'recovery': recovery}
            elif self.path == '/api/recovery/status':
                data = self.server.service.store.inspect_recovery()
            else:
                self.send(404, {'error': 'NOT_FOUND'})
                return
            self.send(200, data)
        except SyncError as exc:
            self.send(409, {'error': exc.code, 'detail': exc.detail})

    def do_POST(self):
        if not self.authorized():
            # Drain a bounded, already-announced body before closing the socket.
            # On Windows an unread body can reset the connection and discard the 403.
            previous_timeout = self.connection.gettimeout()
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if 0 < length <= 2 * 1024 * 1024:
                    self.connection.settimeout(1)
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            finally:
                self.connection.settimeout(previous_timeout)
            self.send(403, {'error': 'TOKEN_OR_ORIGIN_REJECTED'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2 * 1024 * 1024 or self.headers.get_content_type() != 'application/json':
                raise SyncError('REQUEST_INVALID')
            body = json.loads(self.rfile.read(length))
            service = self.server.service
            if self.path == '/api/connect':
                result = service.api.probe()
            elif self.path == '/api/pick':
                result = {'path': choose_docx()}
            elif self.path == '/api/scan':
                result = service.scan(body['path'])
            elif self.path == '/api/search':
                result = service.search(body['scan_id'], body['query'])
            elif self.path == '/api/resolve':
                result = service.resolve(body['scan_id'], body['task_id'], body['item_identity'], body['target_quote'], body['paragraph'])
            elif self.path == '/api/conflict':
                result = service.decide_conflict(body['scan_id'], body['conflict_id'], body['decision'], body.get('confirmed', False))
            elif self.path == '/api/recovery/apply':
                result = service.store.recover(body['plan_id'])
            elif self.path == '/api/prepare':
                result = service.prepare(body['scan_id'], body['approvals'], body['style'], body['saved_in_word'])
            elif self.path == '/api/verify':
                result = service.verify(body['session_id'], body.get('attestations', {}))
            elif self.path == '/api/restore':
                result = service.restore(body['session_id'])
            elif self.path == '/api/open-word':
                result = service.open_word(body['session_id'])
            elif self.path == '/api/shutdown':
                result = {'stopped': True}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send(404, {'error': 'NOT_FOUND'})
                return
            self.send(200, result)
        except SyncError as exc:
            self.send(409, {'error': exc.code, 'detail': exc.detail})
        except (KeyError, TypeError, ValueError) as exc:
            self.send(400, {'error': 'REQUEST_INVALID', 'detail': str(exc)[:150]})
        except Exception as exc:
            self.server.service.store.log('error', error_type=type(exc).__name__)
            self.send(500, {'error': 'LOCAL_OPERATION_FAILED', 'detail': '操作未完成；请保留文件并查看本地日志'})


def serve(home, port=0, open_browser=True):
    server = AppServer(home, port)
    print('Zotero Citation Sync: ' + server.origin, flush=True)
    if open_browser:
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

