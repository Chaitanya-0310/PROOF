import asyncio
from proof.agents.coordinator import ask
from proof.identity import resolve

async def test():
    p = resolve("a.morin")
    print("Testing FIRST request (should hit LLM and cache the result)")
    result1 = await ask("What is the procedure for restarting the flatbread line?", principal=p)
    print("\nResult 1 (LLM):")
    print(result1.answer)
    
    print("\n\nTesting SECOND request (should hit the Semantic Cache)")
    result2 = await ask("Tell me the procedure to restart the flatbread line.", principal=p)
    print("\nResult 2 (Cache Hit):")
    print(result2.answer)

if __name__ == "__main__":
    asyncio.run(test())
