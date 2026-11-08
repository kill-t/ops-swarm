"""仅在演示容器网络提供脚本模型的兼容 HTTP 协议。"""

from fastapi import FastAPI, HTTPException, Request

from scripts.local_demo.config import MODEL
from scripts.local_demo.fixture import completion

app = FastAPI()


@app.get("/health")
def health() -> dict:
    return dict(status="ready", model=MODEL, scripted=True)


@app.post("/v1/chat/completions")
async def complete(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "演示请求过大")
    try:
        import json

        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("演示请求必须为对象")
        return completion(payload)
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise HTTPException(422, "请求不符合脚本演示契约") from error
