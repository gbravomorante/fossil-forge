"""The HTTP API: invalid input, expired sessions, mirror and flat-back modes, and export gating."""

from fastapi.testclient import TestClient

from fossilforge.server import app

client = TestClient(app)
PLANE = b"v 0 0 0\nv 4 0 0\nv 4 4 0\nv 0 4 0\nf 1 2 3\nf 1 3 4\n"


def upload():
    response = client.post(
        "/api/upload", files={"file": ("plane.obj", PLANE)}, data={"units": "mm"}
    )
    assert response.status_code == 200
    return response.json()["token"]


def test_export_requires_checked_geometry():
    token = upload()
    bad = client.post(
        "/api/preview",
        json={"token": token, "mode": "direction", "direction": [1, 0, 0], "adaptive": False},
    )
    assert bad.status_code == 200
    data = bad.json()
    assert not data["report"]["valid_geometry"]
    assert client.get(f"/api/results/{data['token']}/export/stl").status_code == 409
    good = client.post(
        "/api/preview",
        json={"token": token, "mode": "direction", "direction": [0, 0, -1], "adaptive": False},
    )
    assert good.status_code == 200
    data = good.json()
    assert data["report"]["valid_geometry"]
    response = client.get(f"/api/results/{data['token']}/export/stl")
    assert response.status_code == 200
    assert len(response.content) == 84 + 50 * (4 + 8)


def test_invalid_input_and_missing_sessions():
    assert client.get("/api/surfaces/missing/geometry").status_code == 404
    assert (
        client.post(
            "/api/upload", files={"file": ("bad.ply", b"not a mesh")}, data={"units": "mm"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/upload", files={"file": ("plane.obj", PLANE)}, data={"units": "unknown"}
        ).status_code
        == 422
    )
    token = upload()
    assert client.post("/api/preview", json={"token": token, "thickness": -1}).status_code == 422


def test_mirror_api_parameters_and_export():
    token = upload()
    assert (
        client.post(
            "/api/preview", json={"token": token, "mode": "mirror", "copy_percent": 101}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/preview",
            json={"token": token, "mode": "mirror", "selected_face": 0, "direction": [0, 0, 1]},
        ).status_code
        == 422
    )
    response = client.post(
        "/api/preview",
        json={
            "token": token,
            "mode": "mirror",
            "selected_face": 0,
            "direction": [0, 0, -1],
            "copy_percent": 80,
            "edge_rounding": 0,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["report"]["valid_geometry"] and data["report"]["core_surface_preserved"]
    assert data["report"]["mirrored_copy_percent"] == 80
    assert data["report"]["source_surface_preserved"]
    assert client.get(f"/api/results/{data['token']}/export/bundle").status_code == 200


def test_smooth_flat_back_api():
    token = upload()
    response = client.post(
        "/api/preview",
        json={
            "token": token,
            "mode": "mirror",
            "backing_shape": "flat",
            "bridge_smoothing": 1,
            "selected_face": 0,
            "direction": [0, 0, -1],
            "copy_percent": 80,
            "edge_rounding": 0,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["report"]["valid_geometry"] and data["report"]["source_surface_preserved"]
    assert data["report"]["backing_shape"] == "flat"
    assert data["report"]["bridge_smoothing_mm"] == 1
    assert client.get(f"/api/results/{data['token']}/export/stl").status_code == 200
