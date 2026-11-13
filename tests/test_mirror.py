"""Mirror sandwich: preservation of the measured cortex and the edge-repair band."""

from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import trimesh
from test_geometry import patch, surface

from fossilforge.geometry import (
    GeometryError,
    Settings,
    Surface,
    edge_data,
    intersection_mask,
    thicken,
)


@pytest.mark.parametrize("curved", [False, True])
@pytest.mark.parametrize("side", [-1, 1])
def test_reflected_scaled_skin_and_outward_faces(curved, side):
    source = surface(patch(curved=curved))
    d = np.array([0, 0, side], float)
    result = thicken(
        source,
        Settings(
            mode="mirror",
            side=side,
            direction=tuple(d),
            copy_percent=75,
            thickness=1.5,
            edge_rounding=0,
            selected_face=0,
        ),
    )
    assert result.report["valid_geometry"]
    assert result.report["source_surface_preserved"]
    assert result.report["changed_edge_faces"] == 0
    original = source.prepare(True)["mesh"]
    n = len(original.vertices)
    nf = len(original.faces)
    assert np.array_equal(result.mesh.vertices[:n], original.vertices)
    center = original.centroid
    reflected = center + 0.75 * (
        (original.vertices - center) - 2 * ((original.vertices - center) @ d)[:, None] * d
    )
    shift = ((original.vertices - center) @ d).max() - ((reflected - center) @ d).min() + 1.5
    assert np.allclose(result.mesh.vertices[n:], reflected + shift * d)
    assert result.report["minimum_skin_gap_mm"] == 1.5
    assert result.mesh.face_normals[:nf] @ d == pytest.approx(-np.abs(original.face_normals @ d))
    assert np.all(result.mesh.face_normals[nf : 2 * nf] @ d > 0)
    reloaded = trimesh.load(
        BytesIO(result.mesh.export(file_type="stl")), file_type="stl", process=True
    )
    assert reloaded.is_watertight and reloaded.is_winding_consistent
    assert not intersection_mask(reloaded.vertices, reloaded.faces).any()


def test_arrow_cannot_grow_towards_picked_cortex():
    source = surface(patch())
    with pytest.raises(GeometryError, match="selected cortical side"):
        thicken(source, Settings(mode="mirror", side=-1, selected_face=0, direction=(0, 0, 1)))
    with pytest.raises(GeometryError, match="Select a cortical face"):
        thicken(source, Settings(mode="mirror", selected_face=999999))


def test_copy_smoothing_leaves_source_fixed():
    source = surface(patch(curved=True))
    result = thicken(
        source, Settings(mode="mirror", direction=(0, 0, -1), copy_smoothing=3, edge_rounding=0)
    )
    assert result.report["valid_geometry"]
    assert result.report["source_surface_preserved"]
    assert result.report["copy_smoothed"]
    assert np.array_equal(result.mesh.vertices[: len(source.mesh.vertices)], source.mesh.vertices)


def test_mirror_settings_and_small_gap():
    for settings in [
        Settings(mode="mirror", copy_percent=0),
        Settings(copy_percent=101),
        Settings(copy_smoothing=1.2),
        Settings(edge_rounding=-0.1),
        Settings(mode="mirror", direction=(0, 0, 0)),
        Settings(bridge_smoothing=-1),
        Settings(bridge_smoothing=6),
        Settings(backing_shape="sphere"),
    ]:
        with pytest.raises(GeometryError):
            settings.validate()
    Settings(mode="mirror", thickness=0.01).validate()


@pytest.mark.parametrize("shape", ["mirror", "flat"])
@pytest.mark.parametrize("side", [-1, 1])
def test_smoothed_bridge_and_flat_back_both_sides(shape, side):
    source = surface(patch(curved=True))
    direction = np.array((0, 0, side), float)
    result = thicken(
        source,
        Settings(
            mode="mirror",
            backing_shape=shape,
            bridge_smoothing=1,
            direction=tuple(direction),
            side=side,
            edge_rounding=0,
        ),
    )
    assert result.report["valid_geometry"]
    assert result.report["source_surface_preserved"]
    assert np.array_equal(result.mesh.vertices[: len(source.mesh.vertices)], source.mesh.vertices)
    assert result.report["bridge_smoothing_mm"] == 1
    assert result.mesh.is_winding_consistent and result.mesh.is_watertight
    if shape == "flat":
        first = result.source_face_count
        last = first + result.backing_face_count
        cap = result.mesh.vertices[np.unique(result.mesh.faces[first:last])]
        assert np.ptp(cap @ direction) < 1e-5
        assert (result.mesh.face_normals[first:last] @ direction > 0.999).all()
        assert result.report["total_axial_depth_mm"] == pytest.approx(
            source.mesh.extents[2] + 2, abs=1e-5
        )
    reloaded = trimesh.load(
        BytesIO(result.mesh.export(file_type="stl")), file_type="stl", process=True
    )
    assert reloaded.is_watertight and reloaded.is_winding_consistent
    assert not intersection_mask(reloaded.vertices, reloaded.faces).any()


def test_bridge_smoothing_changes_generated_wall_geometry():
    from fossilforge.bridge import smooth_loop

    angles = np.linspace(0, 2 * np.pi, 240, endpoint=False)
    radius = 10 + 0.25 * np.cos(36 * angles)
    jagged = np.column_stack(
        (radius * np.cos(angles), radius * np.sin(angles), np.zeros_like(angles))
    )
    smooth = smooth_loop(jagged, 1)
    assert np.std(np.linalg.norm(smooth[:, :2], axis=1)) < 0.1 * np.std(radius)


def test_flat_back_multiple_loops_fails_visibly():
    source = surface(trimesh.util.concatenate([patch(), patch().apply_translation([20, 0, 0])]))
    with pytest.raises(GeometryError, match="one cut-boundary loop"):
        thicken(source, Settings(mode="mirror", backing_shape="flat", bridge_smoothing=1))


@pytest.mark.slow
def test_irregular_example_repairs_only_allowed_edge_and_stl_roundtrip():
    source = Surface(
        (Path(__file__).parents[1] / "examples" / "Example.ply").read_bytes(), "Example.ply", "mm"
    )
    result = thicken(
        source, Settings(mode="mirror", direction=tuple(source.info()["suggested_direction"]))
    )
    assert result.report["valid_geometry"]
    assert result.report["core_surface_preserved"]
    assert 0 < result.report["changed_edge_faces"] < 50
    assert result.report["excluded_speck_faces"] == 1
    original = source.prepare(True)["mesh"]
    ids = np.array(result.report["source_face_ids"])
    actual = result.mesh.vertices[result.mesh.faces[: len(ids)]]
    # Match whole vertex coordinates, allowing winding reversal and reindexing.
    expected = source.mesh.vertices[source.mesh.faces[ids]]
    assert np.all(np.any(np.all(actual[:, :, None, :] == expected[:, None, :, :], axis=-1), axis=2))
    from scipy.spatial import cKDTree

    boundary = edge_data(original.faces)[2]
    proximity = cKDTree(original.vertices[np.unique(boundary)]).query(source.mesh.vertices)[0]
    removed = np.setdiff1d(source.prepare(True)["face_ids"], ids)
    assert (proximity[source.mesh.faces[removed]] <= 0.4).all()
    reloaded = trimesh.load(
        BytesIO(result.mesh.export(file_type="stl")), file_type="stl", process=True
    )
    assert reloaded.is_watertight and reloaded.is_winding_consistent
    assert not intersection_mask(reloaded.vertices, reloaded.faces).any()
