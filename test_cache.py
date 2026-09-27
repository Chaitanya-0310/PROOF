import asyncio
from proof.agents.coordinator import ask
from proof.identity import resolve

async def test():
    p = resolve("a.morin")
    result = await ask("What is the procedure for restarting the flatbread line?", principal=p)
    print("SUCCESS")
    print(result.answer)

if __name__ == "__main__":
    asyncio.run(test())
