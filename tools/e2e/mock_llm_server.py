"""Mock llama-server for tools\e2e-smoke.ps1 (NOT shipped in any image; mounted into the agent image at run time).

Speaks the two routes the agent uses (GET /health, POST /v1/chat/completions) and answers with the agent's own
deterministic ScriptedLLM, so no model is needed. It honours response_format.json_schema like the real server's
grammar would (a reply that does not satisfy the schema becomes an empty plan)."""
import asyncio
import json

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent.mock import ScriptedLLM

llm = ScriptedLLM(constrained=True)


async def health(_):
    return JSONResponse({"status": "ok"})


async def completions(request):
    body = await request.json()
    rf = body.get("response_format") or {}
    schema = (rf.get("json_schema") or {}).get("schema")
    res = await llm.complete(body["messages"], schema)
    return JSONResponse({"choices": [{"message": {"role": "assistant", "content": res.content}, "finish_reason": "stop"}],
                         "timings": res.timings})


app = Starlette(routes=[Route("/health", health), Route("/v1/chat/completions", completions, methods=["POST"])])
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
