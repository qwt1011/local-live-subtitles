"""Exercise address options and reset through the real WebSocket handler, without models."""

import asyncio
import json
import unittest
from types import SimpleNamespace

from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from test_server_settings import options
from app.server import handle


class AddressProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_reset_on_same_connection(self):
        args = options(engine="fake", model="fake", log=None, log_dir=None)
        engine = SimpleNamespace(name="fake", model_name="fake")

        async def handler(ws):
            await handle(ws, args, engine)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"type": "start", "options": {"address_consistency": True}}))
                started = json.loads(await asyncio.wait_for(ws.recv(), 2))
                self.assertTrue(started["address_consistency"])
                await ws.send(json.dumps({"type": "reset_address_memory"}))
                reset = json.loads(await asyncio.wait_for(ws.recv(), 2))
                self.assertEqual(reset["state"], "address_memory_reset")
                await ws.send(json.dumps({"type": "stop"}))


if __name__ == "__main__":
    unittest.main()
