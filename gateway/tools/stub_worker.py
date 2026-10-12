"""Stub worker for gateway dev: echoes the trusted headers, burns `cpu_ms` of CPU, sleeps `sleep_ms`.

Run: python tools/stub_worker.py --name w0 --port 8100
"""
from __future__ import annotations

import argparse
import os
import time

import uvicorn
from fastapi import FastAPI, Request, Response
from tenantguard_common.contracts import H_CLASS, H_CPU_MS, H_REQUEST_ID, H_TENANT

app = FastAPI()
NAME = os.environ.get("SERVER_NAME", "stub")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def echo(path: str, request: Request, response: Response, cpu_ms: float = 0.5, sleep_ms: float = 0.0):
    c0 = time.thread_time()
    end = c0 + cpu_ms / 1000
    while time.thread_time() < end:
        pass
    if sleep_ms:
        time.sleep(sleep_ms / 1000)
    response.headers[H_CPU_MS] = f"{(time.thread_time() - c0) * 1000:.3f}"
    return {"server": NAME, "path": "/api/" + path, "tenant": request.headers.get(H_TENANT),
            "cls": request.headers.get(H_CLASS), "request_id": request.headers.get(H_REQUEST_ID)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=NAME)
    p.add_argument("--port", type=int, default=8100)
    a = p.parse_args()
    NAME = a.name
    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")
