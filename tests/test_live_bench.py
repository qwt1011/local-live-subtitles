"""Benchmark protocol and completeness checks without loading ASR models."""

import asyncio
import json
import sys
import unittest
from pathlib import Path

import numpy as np
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.live_bench import analyze, stream


class BenchmarkTest(unittest.IsolatedAsyncioTestCase):
    async def test_audio_waits_for_language_ready(self):
        received = []

        async def handler(ws):
            received.append(json.loads(await ws.recv()))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.05)
            await ws.send(json.dumps({"type": "status", "state": "listening"}))
            received.append(await ws.recv())
            await ws.send(json.dumps({"type": "event", "segment_id": 0,
                                      "is_final": True, "text": "Hello", "audio_end": 0.1}))
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            events = await stream(port, np.zeros(1600, dtype="<i2"), "en", 0.1)
        self.assertEqual(received[0]["language"], "en")
        self.assertEqual(len(received[1]), 3200)
        self.assertEqual(len(events), 1)
        self.assertIn("_recv", events[0])

    def test_missing_translation_matched_by_segment(self):
        events = [dict(segment_id=0, is_final=True, text="Hello", audio_end=1, _recv=2),
                  dict(segment_id=1, is_final=True, text="Bye", audio_end=2, _recv=3),
                  dict(segment_id=0, is_final=True, text="Hello", translation="Hi",
                       audio_end=1, _recv=3)]
        result = analyze(events)
        self.assertEqual(result["finals"], 2)
        self.assertEqual(result["translated"], 1)
        self.assertEqual(result["missing_translations"], 1)


if __name__ == "__main__":
    unittest.main()
