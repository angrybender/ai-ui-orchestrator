import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

import main


@pytest.mark.parametrize('method,invalid', [('POST', False), ('POST', True), ('PUT', False), ('PUT', True)])
def test_multipart_uploads_are_closed(monkeypatch, method, invalid):
    closed = []
    original = UploadFile.close

    async def close(upload):
        await original(upload)
        closed.append(upload.file.closed)

    monkeypatch.setattr(UploadFile, 'close', close)
    with TestClient(main.app) as client:
        url = '/api/board/tasks'
        if method == 'PUT':
            response = client.post(url, json={'task_id': 'CLOSE-1', 'title': 'Task', 'description': 'Description'})
            assert response.status_code == 201
            url += '/CLOSE-1'
        data = {'task_id': 'CLOSE-1', 'title': 'Task', 'description': 'Description', 'status': 'BACKLOG'}
        if invalid:
            if method == 'POST':
                data['title'] = ''
            else:
                data['remove_attachment_ids'] = 'not-json'
        response = client.request(method, url, data=data, files=[
            ('files', ('one.txt', b'one', 'text/plain')),
            ('files', ('two.txt', b'two', 'text/plain')),
        ])
        assert response.status_code == (422 if invalid else 201 if method == 'POST' else 200)
        assert closed == [True, True]
