"""Local integration harness. Real Postgres/PostgREST + guest routes/workers; fake auth and disk-backed R2.
Run with SUPABASE_URL=http://127.0.0.1:55441 and a disposable PostgREST on :55440.
Never mount this app in production. This deliberately exposes fixture controls on loopback.
"""
import asyncio
import base64
import json
import os
import tempfile
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

if os.environ.get("SUPABASE_URL") != "http://127.0.0.1:55441":
    raise RuntimeError("This harness only uses the disposable local Supabase endpoint")

from app import storage, guest_lifecycle
from app.database import get_supabase
from app.routers import guest_upload

ROOT = Path(tempfile.mkdtemp(prefix="guest-test-r2-")).resolve()


class LocalR2:
    def file(self, key):
        path = (ROOT / key).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError("Invalid fixture path")
        return path

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f'http://127.0.0.1:55441/storage/{Params["Key"]}?{urlencode({"size": Params.get("ContentLength", ""), "type": Params.get("ContentType", "")})}'

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        path = self.file(Key)
        if not path.exists():
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": path.stat().st_size}

    def get_object(self, Bucket, Key):
        return {"Body": self.file(Key).open("rb")}

    def upload_file(self, path, Bucket, Key, ExtraArgs=None, Callback=None):
        target = self.file(Key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(path).read_bytes())
        if Callback:
            Callback(target.stat().st_size)

    def get_paginator(self, name):
        return self

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": str(p.relative_to(ROOT))} for p in ROOT.rglob("*") if p.is_file() and str(p.relative_to(ROOT)).startswith(Prefix)]}

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.file(item["Key"]).unlink(missing_ok=True)
        return {}


r2 = LocalR2()
storage.get_r2_client = lambda: r2
storage.R2_BUCKET_NAME = "local"
guest_lifecycle.get_r2_client = lambda: r2
guest_lifecycle.R2_BUCKET_NAME = "local"
guest_upload.INTERNAL_PRESIGN_SECRET = "local-test"


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(guest_lifecycle.guest_archive_worker())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    shutil.rmtree(ROOT)


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3107", "http://127.0.0.1:3107"], allow_methods=["*"], allow_headers=["*"])
app.include_router(guest_upload.router, prefix="/api/guest-upload")


@app.api_route("/rest/v1/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "HEAD"])
async def rest(path: str, request: Request):
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "prefer", "range", "range-unit", "accept")}
    async with httpx.AsyncClient() as client:
        response = await client.request(request.method, f"http://127.0.0.1:55440/{path}", params=request.query_params, headers=headers, content=await request.body())
    return Response(response.content, status_code=response.status_code, headers={k: v for k, v in response.headers.items() if k in ("content-type", "content-range", "preference-applied")})


@app.get("/auth/v1/user")
def user(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        uid = payload["sub"]
        if uid not in ["11111111-1111-4111-8111-111111111111", "99999999-9999-4999-8999-999999999999"]:
            raise ValueError()
    except Exception:
        return Response(status_code=401)
    return {"id": uid, "aud": "authenticated", "role": "authenticated", "email": "guest-test@example.test", "app_metadata": {}, "user_metadata": {}}


@app.put("/storage/{key:path}")
async def put(key: str, request: Request):
    data = await request.body()
    if len(data) != int(request.query_params["size"]) or request.headers.get("content-type") != request.query_params["type"]:
        return Response(status_code=403)
    path = r2.file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return Response(status_code=200)


@app.get("/storage/{key:path}")
def get(key: str):
    path = r2.file(key)
    if not path.exists():
        return Response(status_code=404)
    return FileResponse(path, filename="guest-photos.zip" if key.endswith(".zip") else None)


@app.post("/_test/sweep")
async def sweep():
    await asyncio.to_thread(guest_lifecycle.sweep)
    return {"keys": [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]}


@app.post("/_test/age-delete/{album_id}")
def age(album_id: str):
    get_supabase().table("guest_albums").update({"deleting_at": (datetime.now(timezone.utc)-timedelta(hours=3)).isoformat()}).eq("id", album_id).execute()
    return {"ok": True}
