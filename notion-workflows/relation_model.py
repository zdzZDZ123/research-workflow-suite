"""Small OpenAI-compatible JSON client; credentials never enter project files."""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from secure_credentials import model_api_key


class ModelError(RuntimeError):
    pass


def validate_config(config):
    url = str(config.get('base_url', '')).rstrip('/')
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1'))
            or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ModelError('MODEL_BASE_URL_INVALID: use HTTPS, or HTTP on loopback')
    if config.get('api_style') not in ('chat_completions', 'responses'):
        raise ModelError('MODEL_API_STYLE_INVALID')
    if not isinstance(config.get('model'), str) or not config['model'].strip():
        raise ModelError('MODEL_ID_REQUIRED')
    return {'base_url': url, 'api_style': config['api_style'], 'model': config['model'].strip()}


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise ModelError('MODEL_CONFIGURATION_REQUIRED: configure base URL, model and API style')
    return validate_config(json.loads(path.read_text(encoding='utf-8')))


def parse_output(response, style):
    if style == 'chat_completions':
        message = response['choices'][0]['message']['content']
        if isinstance(message, list):
            message = ''.join(part.get('text', '') for part in message if isinstance(part, dict))
    else:
        message = response.get('output_text')
        if message is None:
            message = ''.join(part.get('text', '') for item in response.get('output', [])
                              for part in item.get('content', []) if part.get('type') in ('output_text', 'text'))
    if not isinstance(message, str) or not message.strip():
        raise ModelError('MODEL_EMPTY_OUTPUT')
    value = message.strip()
    if value.startswith('```'):
        value = value.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        result = json.loads(value)
    except json.JSONDecodeError as error:
        raise ModelError('MODEL_INVALID_JSON') from error
    if not isinstance(result, dict):
        raise ModelError('MODEL_JSON_OBJECT_REQUIRED')
    return result


class CompatibleModel:
    def __init__(self, config, key=None, opener=None):
        self.config = validate_config(config)
        self.key = key if key is not None else model_api_key()
        self.opener = opener or urllib.request.urlopen
        if not self.key:
            raise ModelError('MODEL_API_KEY_REQUIRED: use the Windows credential store or PAPER_RELATIONS_API_KEY')

    def preflight(self):
        result = self.complete_json('Return exactly one JSON object with ok set to true.', 'Connectivity check.')
        if result.get('ok') is not True:
            raise ModelError('MODEL_PREFLIGHT_FAILED: expected {"ok":true}')
        return True

    def complete_json(self, system, content):
        if len(content) > 120000:
            raise ModelError('MODEL_INPUT_TOO_LONG')
        style = self.config['api_style']
        if style == 'chat_completions':
            path = '/chat/completions'
            body = {'model': self.config['model'], 'messages': [
                {'role': 'system', 'content': system}, {'role': 'user', 'content': content}]}
        else:
            path = '/responses'
            body = {'model': self.config['model'], 'instructions': system, 'input': content}
        request = urllib.request.Request(self.config['base_url'] + path,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST',
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=120) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise ModelError(f'MODEL_HTTP_{error.code}') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            # A lost model response may still have incurred a charge. Do not auto-retry.
            raise ModelError('MODEL_OUTCOME_UNKNOWN') from error
        return parse_output(data, style)
