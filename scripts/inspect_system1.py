"""Read-only inspection of the real Laya MCP contract."""
import asyncio
import json

from app.config import load_config
from app.mcp import MCPRegistry


async def main():
    registry = MCPRegistry(load_config().mcp)
    try:
        tools = await registry.tools_for(["laya"])
        print(json.dumps([tool.definition() for tool in tools], ensure_ascii=False))
        for question in [
            {"type": "noul", "instructions": "Does message request a refund?"},
            {"type": "choice", "instructions": "What is requested in message?",
             "criteria": {"refund": "money back", "other": "not money back"}},
            {"type": "score", "instructions": "How urgent is message?", "criteria": ["no deadline", "urgent"]},
        ]:
            print(json.dumps(await registry.call_structured("laya", "laya_predict", {
                "state": {"message": "Solicito la devolución del importe."},
                "questions": {"decision": question}, "model": "multilingual",
            }), ensure_ascii=False))
    finally:
        await registry.aclose()


if __name__ == "__main__":
    asyncio.run(main())
