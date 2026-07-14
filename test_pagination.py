import asyncio
import httpx

async def test():
    async with httpx.AsyncClient() as client:
        # Assuming dev server is running on 8000, we can try to start it and test
        pass
