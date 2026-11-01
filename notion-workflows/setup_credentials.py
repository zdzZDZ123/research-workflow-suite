"""Single-use, loopback-only form to move an authorized API token to Windows vault.

Never prints or stores the token in a file. No third-party assets or telemetry.
The unpredictable URL is a short-lived form nonce, not the API token.
"""
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from secure_credentials import save_token

nonce = secrets.token_urlsafe(32)
saved = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def reply(self, text, code=200):
        data = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def valid(self):
        return self.path == '/' + nonce and self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def do_GET(self):
        if not self.valid(): return self.reply('Not found', 404)
        self.reply('<!doctype html><html lang="zh"><meta charset="utf-8"><title>本机 Notion 凭据</title>'
                   '<h1>Paper Deep Reading 本机凭据</h1><p>仅保存到当前 Windows 用户的凭据管理器；不会写入项目文件。</p>'
                   '<form method="post"><label>Notion API Token <input type="password" name="token" autocomplete="off" required></label>'
                   '<button type="submit">保存到 Windows 凭据管理器</button></form></html>')

    def do_POST(self):
        global saved
        if not self.valid() or saved: return self.reply('Not found', 404)
        expected = f'http://127.0.0.1:{self.server.server_port}'
        if self.headers.get('Origin') != expected: return self.reply('Invalid origin', 403)
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size < 4096: return self.reply('Invalid request', 400)
        token = urllib.parse.parse_qs(self.rfile.read(size).decode()).get('token', [''])[0]
        try: save_token(token)
        except Exception: return self.reply('保存失败；凭据格式或系统凭据管理器不可用。', 400)
        saved = True
        self.reply('<h1>已安全保存</h1><p>凭据位于 Windows 凭据管理器：PaperDeepReading/Notion。</p>')


if __name__ == '__main__':
    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.timeout = 2
        print(f'http://127.0.0.1:{server.server_port}/{nonce}', flush=True)
        deadline = time.monotonic() + 600
        while not saved and time.monotonic() < deadline: server.handle_request()
        print('CREDENTIAL_SAVED' if saved else 'SETUP_EXPIRED', flush=True)
