"""GET-only Zotero Local API v3; never reads Zotero's SQLite database."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .util import SyncError, digest, now


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SyncError('LOCAL_API_REDIRECT', '本地 API 重定向被拒绝')


class LocalAPI:
    def __init__(self, base='http://127.0.0.1:23119', library='users/0', timeout=12):
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.username or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or not parsed.port):
            raise SyncError('LOCAL_API_ONLY', '仅允许 127.0.0.1 上的 Zotero API')
        if not re.fullmatch(r'(users|groups)/\d+', library):
            raise SyncError('LIBRARY_INVALID')
        self.base, self.library, self.timeout = base.rstrip('/'), library, timeout
        self.server_id = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, path, **query):
        if not path.startswith('/api/') or '..' in path or '?' in path or '#' in path:
            raise SyncError('API_PATH_INVALID')
        url = self.base + path + ('?' + urllib.parse.urlencode(query) if query else '')
        headers = {'Zotero-API-Version': '3', 'Accept': 'application/json'}
        if self.server_id:
            headers['Zotero-Server-ID'] = self.server_id
        request = urllib.request.Request(url, headers=headers, method='GET')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise SyncError('API_RESPONSE_TOO_LARGE')
                meta = {k.lower(): v for k, v in response.headers.items()}
                seen = meta.get('zotero-server-id')
                if self.server_id and seen != self.server_id:
                    raise SyncError('ZOTERO_INSTANCE_CHANGED')
                if seen:
                    self.server_id = seen
                data = (raw.decode('utf-8') if path == '/api/' else json.loads(raw)) if raw.strip() else None
                return data, meta
        except urllib.error.HTTPError as exc:
            raise SyncError('LOCAL_API_HTTP_' + str(exc.code),
                            '检查 Zotero 高级设置中的本地 API 及当前文库') from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise SyncError('LOCAL_API_UNAVAILABLE', '请启动 Zotero 并启用本地 API') from exc

    def probe(self):
        _, headers = self.get('/api/')
        return {'available': True, 'version': headers.get('x-zotero-version') or headers.get('zotero-version'),
                'api_version': headers.get('zotero-api-version'),
                'server_id_present': bool(self.server_id)}

    def snapshot(self):
        info = self.probe()
        if not self.server_id:
            raise SyncError('SERVER_ID_REQUIRED', '此版本未提供实例身份，请升级到 Zotero 10 后扫描')
        items, keys, first_version, total = [], set(), None, None
        for start in range(0, 100000, 100):
            page, headers = self.get('/api/' + self.library + '/items',
                                     limit=100, start=start, sort='dateAdded', direction='asc')
            version = headers.get('last-modified-version')
            current_total = headers.get('total-results')
            if not version or current_total is None:
                raise SyncError('SNAPSHOT_VERSION_MISSING')
            if not current_total.isdigit() or int(current_total) > 100000:
                raise SyncError('SNAPSHOT_TOTAL_INVALID')
            if first_version is None:
                first_version, total = version, int(current_total)
            if version != first_version or int(current_total) != total:
                raise SyncError('LIBRARY_CHANGED_DURING_SCAN', '文库扫描期间发生变化，请重试')
            if not isinstance(page, list):
                raise SyncError('API_ITEMS_INVALID')
            for item in page:
                if not isinstance(item, dict):
                    raise SyncError('SNAPSHOT_INVALID')
                key = item.get('key')
                if not re.fullmatch(r'[A-Z0-9]{8}', key or '') or key in keys or not isinstance(item.get('data'), dict):
                    raise SyncError('SNAPSHOT_INVALID')
                keys.add(key)
                items.append(item)
            if len(page) < 100 or len(items) == total:
                break
        if len(items) != total:
            raise SyncError('SNAPSHOT_INCOMPLETE')
        _, headers = self.get('/api/' + self.library + '/items', limit=1)
        if headers.get('last-modified-version') != first_version:
            raise SyncError('LIBRARY_CHANGED_DURING_SCAN')
        return {'scope': digest([self.server_id, self.library]), 'library': self.library,
                'server_id': self.server_id, 'version': first_version, 'complete': True,
                'at': now(), 'connection': info, 'items': items}

