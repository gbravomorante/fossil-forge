"""Growth, tapering, units and STL/PLY round trips on synthetic and real surfaces."""

from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import trimesh

from fossilforge.geometry import (
    GeometryError,
    Settings,
    Surface,
    export_ply,
    intersection_mask,
    thicken,
)


def patch(nx=12, ny=10, curved=False):
    x, y = np.meshgrid(np.linspace(-5, 5, nx), np.linspace(-4, 4, ny))
    z = 0.045 * x * x - 0.065 * y * y if curved else np.zeros_like(x)
    vertices = np.column_stack((x.ravel(), y.ravel(), z.ravel()))
    faces = []
    for row in range(ny - 1):
        for col in range(nx - 1):
            a = row * nx + col
            b = a + 1
            c = a + nx
            d = c + 1
            faces.extend([[a, b, d], [a, d, c]])
    return trimesh.Trimesh(vertices, np.array(faces), process=False)


def surface(mesh, units="mm"):
    return Surface(export_ply(mesh), "fragment.ply", units)


@pytest.mark.parametrize("curved", [False, True])
@pytest.mark.parametrize("side", [-1, 1])
def test_flat_and_double_curvature_preserve_surface(curved, side):
    source = surface(patch(curved=curved))
    result = thicken(source, Settings(thickness=0.8, side=side, smoothing=1, minimum=0.05))
    assert result.report["valid_geometry"]
    assert result.report["source_surface_preserved"]
    assert np.array_equal(result.mesh.vertices[: len(source.mesh.vertices)], source.mesh.vertices)
    assert result.mesh.is_watertight and result.mesh.is_winding_consistent
    assert not intersection_mask(result.mesh.vertices, result.mesh.faces).any()
    reloaded = trimesh.load(BytesIO(export_ply(result.mesh)), file_type="ply", process=False)
    assert np.array_equal(reloaded.vertices, result.mesh.vertices)


def test_directional_growth_and_units():
    source = surface(patch(), "cm")
    result = thicken(
        source, Settings(thickness=2, mode="direction", direction=(0, 0, -7), adaptive=False)
    )
    assert result.report["valid_geometry"]
    n = len(source.mesh.vertices)
    assert np.allclose(result.mesh.vertices[n:] - result.mesh.vertices[:n], [0, 0, -2])
    assert np.allclose(source.mesh.extents, [100, 80, 0])
    assert result.report["volume_mm3"] == pytest.approx(16000)


def test_closed_mesh_rejected():
    source = surface(trimesh.creation.icosphere(subdivisions=1))
    with pytest.raises(GeometryError, match="already closed"):
        thicken(source, Settings())


def test_source_intersection_rejected():
    vertices = np.array(
        [[0, 0, 0], [3, 0, 0], [1, 3, 0], [1, -1, -1], [1, 2, 1], [1, 2, -1]], float
    )
    source = surface(trimesh.Trimesh(vertices, [[0, 1, 2], [3, 4, 5]], process=False))
    with pytest.raises(GeometryError, match="source itself"):
        thicken(source, Settings(exclude_specks=False))


def test_invalid_parameters_rejected():
    for settings in [
        Settings(thickness=-1),
        Settings(thickness=float("nan")),
        Settings(side=0),
        Settings(minimum=3),
        Settings(mode="direction", direction=(0, 0, 0)),
    ]:
        with pytest.raises(GeometryError):
            settings.validate()


def test_stl_exact_welding():
    mesh = patch()
    source = Surface(mesh.export(file_type="stl"), "fragment.stl", "mm")
    result = thicken(source, Settings(mode="direction", adaptive=False))
    assert result.report["valid_geometry"]
    assert len(source.mesh.vertices) == len(mesh.vertices)


@pytest.mark.slow
def test_real_example_and_stl_roundtrip():
    source = Surface(
        (Path(__file__).parents[1] / "examples" / "Example.ply").read_bytes(), "Example.ply", "mm"
    )
    result = thicken(source, Settings(thickness=2))
    assert result.report["valid_geometry"]
    assert result.report["excluded_speck_faces"] == 1
    assert result.report["preserved_faces"] == 126197
    assert result.report["displacement_min_mm"] >= 0.05
    assert result.report["displacement_median_mm"] > 1.9
    prepared = source.prepare(True)["mesh"]
    assert np.array_equal(result.mesh.vertices[: len(prepared.vertices)], prepared.vertices)
    # Verify each retained source triangle geometrically, allowing winding reversal.
    ids = np.array(result.report["source_face_ids"])
    actual = result.mesh.vertices[result.mesh.faces[: len(ids)]]
    expected = source.mesh.vertices[source.mesh.faces[ids]]
    same = np.all(actual == expected, axis=(1, 2))
    reversed_winding = np.all(actual == expected[:, ::-1], axis=(1, 2))
    assert np.all(same | reversed_winding)
    reloaded = trimesh.load(
        BytesIO(result.mesh.export(file_type="stl")), file_type="stl", process=True
    )
    assert reloaded.is_watertight
    assert reloaded.is_winding_consistent
    assert not intersection_mask(reloaded.vertices, reloaded.faces).any()
