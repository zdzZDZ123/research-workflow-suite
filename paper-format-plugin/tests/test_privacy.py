import json
import socket

import pytest

from paper_formatter.contracts import FormatterError
from paper_formatter.rules import RuleInput, ModelClient, validate_public_url
from paper_formatter.offline import offline_processing


def test_rule_input_cannot_include_document_or_template_text():
    for field in ("attachment", "annotations", "manuscript", "template_text", "references", "source_path"):
        with pytest.raises(FormatterError, match="RULE_INPUT_FIELD"):
            RuleInput.from_dict({"instruction": "A4", field: "PRIVATE_MANUSCRIPT_SENTINEL"})


def test_remote_model_receives_only_format_request():
    requests = []
    def transport(url, body, headers):
        requests.append((url, body, headers))
        return {"choices": [{"message": {"content": json.dumps({"rules": {"page": {"columns": 2}}, "unresolved": []})}}]}
    client = ModelClient({"base_url": "https://model.example/v1", "model": "rules-model", "api_style": "chat_completions"}, "fixture-key", transport=transport)
    result = client.resolve(RuleInput.from_dict({"instruction": "双栏"}))
    assert result["rules"]["page"]["columns"] == 2
    payload = requests[0][1]
    assert "双栏" in json.dumps(payload, ensure_ascii=False)
    assert set(payload) == {"model", "messages", "temperature"}
    assert "attachment" not in json.dumps(payload)


@pytest.mark.parametrize("url", ["file:///C:/private.txt", "http://127.0.0.1/x", "https://10.0.0.1/x", "https://user:pass@example.org/x", "https://example.org:8443/x"])
def test_public_guideline_fetch_cannot_read_local_resources(url):
    with pytest.raises(FormatterError):
        validate_public_url(url, resolve_dns=False)


def test_document_processing_has_no_socket_egress():
    original = socket.socket.connect
    with offline_processing():
        with socket.socket() as sock:
            with pytest.raises(FormatterError, match="DOCUMENT_NETWORK_DISABLED"):
                sock.connect(("1.1.1.1", 443))
    assert socket.socket.connect is original


def test_invalid_model_output_cannot_become_executable_rule():
    def transport(url, body, headers):
        return {"output_text": '{"rules":{"shell":"powershell dangerous"},"unresolved":[]}'}
    client = ModelClient({"base_url": "https://model.example/v1", "model": "x", "api_style": "responses"}, "key", transport=transport)
    with pytest.raises(FormatterError):
        client.resolve(RuleInput.from_dict({"instruction": "双栏"}))
def test_document_udp_egress_is_also_blocked():
    import socket
    import pytest
    from paper_formatter.offline import offline_processing
    from paper_formatter.contracts import FormatterError
    with offline_processing(),socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        with pytest.raises(FormatterError,match='DOCUMENT_NETWORK_DISABLED'):
            sock.sendto(b'PRIVATE-SENTINEL',('127.0.0.1',9))

