import io
import threading

import pytest
import agent_remote as remote


def test_complete_value_does_not_disable_stream_secret_protection():
    diagnostics = remote._Diagnostics(['abcdef-secret'])
    assert diagnostics.safe('session-abc', partial_suffix=False) == 'session-abc'
    assert diagnostics.safe('session-abc') == 'session-[REDACTED]'
    assert diagnostics.safe('session-', partial_suffix=False) == 'session-'
    assert diagnostics.safe('session-') == 'session[REDACTED]'
    assert diagnostics.safe('id-abcdef-secret-tail', partial_suffix=False) == 'id-[REDACTED]-tail'
    pem = '-----BEGIN PRIVATE KEY-----\nkey material\n-----END PRIVATE KEY-----'
    assert diagnostics.safe(pem, partial_suffix=False) == '[REDACTED]'


def test_acp_error_preserves_data_details():
    message = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"Internal error","data":{"details":"Connection error."}}}\n'
    with pytest.raises(remote.ACPError, match=r'Internal error: Connection error\.'):
        remote._request(io.BytesIO(), io.BytesIO(message), 1, 'session/prompt', {}, remote._Diagnostics([]), threading.Event())


def test_acp_error_with_method_and_id_preserves_details():
    message = b'{"jsonrpc":"2.0","id":3,"method":"session/prompt","error":{"code":-32603,"message":"Internal error","data":{"details":"Connection error."}}}\n'
    with pytest.raises(remote.ACPError, match=r'session/prompt: Internal error: Connection error\.'):
        remote._request(io.BytesIO(), io.BytesIO(message), 3, 'session/prompt', {}, remote._Diagnostics([]), threading.Event())
