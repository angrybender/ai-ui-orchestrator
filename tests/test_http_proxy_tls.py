import asyncio
import datetime
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from http_proxy import ProxyApplication
from settings.http_proxy import check_availability
from tests.test_http_proxy import config, request


def test_selfsigned_wrong_hostname_save_headers_only_and_forwarding(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'wrong-host.example')])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)

    async def run():
        requests = []
        closed = asyncio.Event()
        tasks = set()

        async def handler(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                requests.append(head)
                if head.startswith(b'GET / HTTP'):
                    writer.write(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 9999999\r\n\r\n')
                    await writer.drain()
                    await reader.read()
                    closed.set()
                else:
                    while await reader.readline() != b'0\r\n':
                        pass
                    await reader.readline()
                    writer.write(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\n\r\nok')
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(handler, '127.0.0.1', 0, ssl=context)
        url = f'https://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        values = config(url, methods=['POST'], patterns=[{'operator': '=', 'pattern': '^/object$'}])
        try:
            await asyncio.wait_for(asyncio.to_thread(check_availability, {'proxies': [values]}), 2)
            await asyncio.wait_for(closed.wait(), 1)
            assert b'authorization' not in requests[0].lower() and b'cookie' not in requests[0].lower()
            app = ProxyApplication(values)
            try:
                response = await request(app, 'POST', b'/object', headers=[(b'authorization', b'Bearer sample'), (b'cookie', b'a=1')])
                assert response[0]['status'] == 401
                assert b''.join(m.get('body', b'') for m in response) == b'ok'
                assert b'authorization: Bearer sample' in requests[1]
            finally:
                await app.close()
        finally:
            server.close()
            await server.wait_closed()
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tuple(tasks), return_exceptions=True)
    asyncio.run(run())
