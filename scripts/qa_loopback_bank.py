"""Serve the public foreground Bank strictly on ephemeral IPv4 loopback."""
import asyncio
from contextlib import asynccontextmanager

import httpx

from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL
from scripts.verify_windows_customer import Bank


@asynccontextmanager
async def bank_for(host, *, supplier_port):
    if type(supplier_port) is not int or not (0 < supplier_port < 65536):
        raise ValueError('invalid loopback supplier')
    bank = Bank(host)
    hits = []

    async def serve(reader, writer):
        try:
            async with asyncio.timeout(5) as deadline:
                raw = await reader.readuntil(b'\r\n\r\n')
                head = raw.decode('ascii').split('\r\n')
                method, path, protocol = head[0].split()
                route = path.partition('?')[0]
                if route in {prefix + leaf for prefix in ('/_host', '/integrations/native')
                             for leaf in ('/v1/responses', '/v1/messages', '/v1/messages/count_tokens')}:
                    remote, out = await asyncio.open_connection('127.0.0.1', supplier_port)
                    async def copy(src, dst):
                        while chunk := await src.read(65536):
                            dst.write(chunk)
                            await dst.drain()
                    out.write(raw)
                    flows = [asyncio.create_task(copy(reader, out)), asyncio.create_task(copy(remote, writer))]
                    try:
                        deadline.reschedule(asyncio.get_running_loop().time() + 300)
                        await asyncio.wait(flows, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for flow in flows:
                            flow.cancel()
                        await asyncio.gather(*flows, return_exceptions=True)
                        out.close()
                    return
                items = [line.split(':', 1) for line in head[1:] if line]
                headers = httpx.Headers([(k, v.strip()) for k, v in items])
                count = int(headers.get('content-length', '0'))
                if not (protocol == 'HTTP/1.1' and path.startswith('/integrations/')
                        and 0 <= count <= 32768 and 'transfer-encoding' not in headers):
                    raise ValueError
                body = await reader.readexactly(count)
                # The public bank validates an absolute URL. This constructs an
                # in-memory request; there is no network client or origin dial.
                response = bank(httpx.Request(method, FALLBACK_DEFAULT_BACKEND_URL + path,
                                              headers=headers, content=body))
                hits.append(response.status_code)
                payload = response.content
                writer.write(f'HTTP/1.1 {response.status_code} QA\r\nContent-Type: application/json\r\n'
                             f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode() + payload)
                await writer.drain()
        except Exception:
            hits.append(0)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, '127.0.0.1', 0, limit=8192)
    async with server:
        yield bank, hits, server.sockets[0].getsockname()[1]
