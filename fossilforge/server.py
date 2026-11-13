"""Local FastAPI server: uploads, previews and gated exports, bound to the loopback interface."""

from __future__ import annotations

import json
import struct
import threading
import uuid
import zipfile
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .geometry import GeometryError, Settings, Surface, export_ply, thicken

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="Fossil Forge", version="0.3.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
app.mount("/vendor/three", StaticFiles(directory=ROOT / "node_modules" / "three"), name="three")
surfaces = OrderedDict()
results = OrderedDict()
SESSION_LOCK = threading.RLock()
MAX_UPLOAD = 150 * 1024 * 1024


def remember(store, key, value, limit=12):
    with SESSION_LOCK:
        store[key] = value
        while len(store) > limit:
            store.popitem(last=False)


def get_surface(token):
    with SESSION_LOCK:
        if token not in surfaces:
            raise HTTPException(404, "This surface session expired. Reload the file.")
        return surfaces[token]


def geometry_bytes(mesh):
    return (
        struct.pack("<II", len(mesh.vertices), len(mesh.faces))
        + np.asarray(mesh.vertices, dtype="<f4").tobytes()
        + np.asarray(mesh.faces, dtype="<u4").tobytes()
    )


def register(content, name, units):
    try:
        surface = Surface(content, name, units)
        info = surface.info()
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    token = uuid.uuid4().hex
    remember(surfaces, token, surface)
    info["token"] = token
    return info


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "application": "Fossil Forge", "version": "0.3.0"}


@app.post("/api/example")
def example():
    path = ROOT / "examples" / "Example.ply"
    if not path.exists():
        raise HTTPException(404, "examples/Example.ply is missing.")
    return register(path.read_bytes(), path.name, "mm")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), units: str = Form(...)):
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "Choose a mesh smaller than 150 MB.")
    # Filename is display-only; it never becomes a server filesystem path.
    name = (file.filename or "fragment.ply").replace("\\", "/").rsplit("/", 1)[-1]
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(register, content, name, units)


@app.get("/api/surfaces/{token}/geometry")
def source_geometry(token: str):
    return Response(geometry_bytes(get_surface(token).mesh), media_type="application/octet-stream")


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    thickness: float = 2
    side: int = -1
    mode: str = "curve"
    smoothing: float = 5
    adaptive: bool = True
    minimum: float = 0.05
    direction: tuple[float, float, float] = (0, 0, -1)
    exclude_specks: bool = True
    copy_percent: float = 90
    copy_smoothing: int = 0
    edge_rounding: float = 0.4
    selected_face: int | None = None
    bridge_smoothing: float = 0
    backing_shape: str = "mirror"


@app.post("/api/preview")
def preview(request: PreviewRequest):
    surface = get_surface(request.token)
    settings = Settings(**request.model_dump(exclude={"token"}))
    try:
        result = thicken(surface, settings)
    except GeometryError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"Could not create this backing: {exc}") from exc
    token = uuid.uuid4().hex
    remember(results, token, (surface, result))
    report = {k: v for k, v in result.report.items() if k != "source_face_ids"}
    return {
        "token": token,
        "report": report,
        "source_faces": result.source_face_count,
        "backing_faces": result.backing_face_count,
        "collision_faces": result.collision_faces.tolist(),
    }


def get_result(token):
    with SESSION_LOCK:
        if token not in results:
            raise HTTPException(404, "This preview expired. Generate it again.")
        return results[token]


@app.get("/api/results/{token}/geometry")
def result_geometry(token: str):
    _, result = get_result(token)
    return Response(geometry_bytes(result.mesh), media_type="application/octet-stream")


@app.get("/api/results/{token}/export/{kind}")
def export_result(token: str, kind: str):
    surface, result = get_result(token)
    if not result.report["valid_geometry"]:
        raise HTTPException(409, "Export is blocked because this shell failed its geometry checks.")
    stem = Path(surface.filename).stem.replace('"', "")
    if kind == "stl":
        content, filename = result.mesh.export(file_type="stl"), f"{stem}_backed.stl"
    elif kind == "ply":
        content, filename = export_ply(result.mesh), f"{stem}_backed.ply"
    elif kind == "bundle":
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{stem}_backed.stl", result.mesh.export(file_type="stl"))
            z.writestr(f"{stem}_backed.ply", export_ply(result.mesh))
            z.writestr("source/" + surface.filename, surface.content)
            z.writestr("report.json", json.dumps(result.report, indent=2))
        content, filename = buffer.getvalue(), f"{stem}_backing_project.zip"
    else:
        raise HTTPException(404, "Choose STL, PLY, or a project bundle.")
    return Response(
        content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
