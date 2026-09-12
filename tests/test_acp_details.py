import io
import threading

import pytest
import agent_remote as remote


def test_acp_error_preserves_data_details():
    message = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"Internal error","data":{"details":"Connection error."}}}\n'
    with pytest.raises(remote.ACPError, match=r'Internal error: Connection error\.'):
        remote._request(io.BytesIO(), io.BytesIO(message), 1, 'session/prompt', {}, remote._Diagnostics([]), threading.Event())


def test_acp_error_with_method_and_id_preserves_details():
    message = b'{"jsonrpc":"2.0","id":3,"method":"session/prompt","error":{"code":-32603,"message":"Internal error","data":{"details":"Connection error."}}}\n'
    with pytest.raises(remote.ACPError, match=r'session/prompt: Internal error: Connection error\.'):
        remote._request(io.BytesIO(), io.BytesIO(message), 3, 'session/prompt', {}, remote._Diagnostics([]), threading.Event())
