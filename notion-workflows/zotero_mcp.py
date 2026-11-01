"""Call the installed local Zotero MCP. Credential must be provided by environment."""
import json
import os
import urllib.request


def call(name, arguments):
    token = os.environ.get('ZOTERO_MCP_TOKEN')
    if not token:
        raise RuntimeError('ZOTERO_MCP_TOKEN is required')
    request = urllib.request.Request(
        'http://127.0.0.1:23119/llm-for-zotero/mcp',
        data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                         'params': {'name': name, 'arguments': arguments}}).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        envelope = json.load(response)
    if 'error' in envelope:
        raise RuntimeError(str(envelope['error']))
    result = envelope['result']
    entries = [json.loads(c['text']) for c in result.get('content', []) if c['type'] == 'text']
    if result.get('isError') or any(e.get('ok') is False for e in entries):
        raise RuntimeError(json.dumps(entries, ensure_ascii=False))
    return entries
