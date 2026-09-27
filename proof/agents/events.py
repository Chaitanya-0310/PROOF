import asyncio
import json
from typing import Any

class EventStreamer:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def emit(self, event_type: str, data: dict[str, Any]):
        await self.queue.put({"type": event_type, "data": data})

    async def end(self, data: dict[str, Any]):
        await self.queue.put({"type": "final", "data": data})

    async def error(self, msg: str):
        await self.queue.put({"type": "error", "data": {"message": msg}})

    async def stream(self):
        while True:
            event = await self.queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "final" or event["type"] == "error":
                break
