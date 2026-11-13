"""A reflected, scaled second skin with corresponding borders joined."""

import platform
from dataclasses import asdict
from importlib.metadata import version

import numpy as np
import pymeshlab
import trimesh
from scipy.spatial import cKDTree

from .bridge import connect_skins, flat_back
from .geometry import MESHLAB_LOCK, GeometryError, Result, edge_data, intersection_mask


def repair_border(vertices, faces, materials, original, direction, width):
    """Repair small intersecting strips only; never remove central source faces."""
    if width <= 0:
        return vertices, faces, materials, 0
    boundary = edge_data(original.faces)[2]
    edge_tree = cKDTree(original.vertices[np.unique(boundary)])
    count = 0
    for attempt in range(8):
        mesh = trimesh.Trimesh(vertices, faces, process=False)
        bad = intersection_mask(vertices, faces) | (mesh.area_faces < 1e-14)
        if not bad.any() and mesh.is_watertight:
            break
        # Widen a failing patch by adjacent triangles so hole filling has a
        # clean perimeter instead of recreating the same narrow folded strip.
        proximity = edge_tree.query(vertices)[0]
        editable = (materials != 0) | (proximity[faces].max(axis=1) <= width)
        for _ in range(min(1 + attempt // 2, 3)):
            adjacent = mesh.face_adjacency
            neighbors = adjacent[bad[adjacent].any(axis=1)].ravel()
            bad[neighbors[editable[neighbors]]] = True
        cortical = bad & (materials == 0)
        if cortical.any():
            if (proximity[faces[cortical]].max(axis=1) > width).any():
                break
        count += int(bad.sum())
        colors = np.column_stack(
            (
                materials / 2,
                np.zeros(len(materials)),
                np.zeros(len(materials)),
                np.ones(len(materials)),
            )
        )
        with MESHLAB_LOCK:
            ms = pymeshlab.MeshSet()
            ms.add_mesh(
                pymeshlab.Mesh(
                    np.array(vertices),
                    np.array(faces),
                    f_color_matrix=colors,
                    f_scalar_array=bad.astype(float),
                )
            )
            ms.compute_selection_by_condition_per_face(condselect="fq > 0.5")
            ms.meshing_remove_selected_faces()
            ms.meshing_repair_non_manifold_edges(method=0)
            ms.meshing_repair_non_manifold_vertices(vertdispratio=0)
            v = ms.current_mesh().vertex_matrix()
            f = ms.current_mesh().face_matrix()
            c = ms.current_mesh().face_color_matrix()
            mesh = trimesh.Trimesh(v, f, process=False)
            trimesh.repair.fix_winding(mesh)
            ms = pymeshlab.MeshSet()
            ms.add_mesh(pymeshlab.Mesh(v, np.array(mesh.faces), f_color_matrix=c))
            ms.meshing_close_holes(maxholesize=150, selfintersection=False, newfaceselected=True)
            vertices = np.array(ms.current_mesh().vertex_matrix())
            faces = np.array(ms.current_mesh().face_matrix())
            materials = np.rint(ms.current_mesh().face_color_matrix()[:, 0] * 2).astype(np.int32)
            materials[ms.current_mesh().face_selection_array()] = 2
        # Stabilize generated edge patches at STL precision while retaining
        # every surviving measured vertex exactly. Weld only exact matches.
        source_distance, _ = cKDTree(original.vertices).query(vertices)
        generated = source_distance > 1e-12
        vertices[generated] = vertices[generated].astype(np.float32).astype(np.float64)
        vertices, inverse = np.unique(vertices, axis=0, return_inverse=True)
        faces = inverse[faces]
        mesh = trimesh.Trimesh(vertices, faces, process=False)
        good = mesh.area_faces > 1e-14
        faces = faces[good]
        materials = materials[good]
        _, first = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
        first = np.sort(first)
        faces = faces[first]
        materials = materials[first]
        mesh = trimesh.Trimesh(vertices, faces, process=False)
        # MeshLab can leave a three-edge hole after native cleanup. Closing a
        # triangular or quadrilateral hole introduces no new vertices.
        trimesh.repair.fill_holes(mesh)
        if len(mesh.faces) > len(faces):
            materials = np.r_[materials, np.full(len(mesh.faces) - len(faces), 2, int)]
            faces = np.array(mesh.faces)
    return vertices, faces, np.clip(materials, 0, 2), count


def mirrored_sandwich(surface, settings, data):
    source = data["mesh"]
    n = len(source.vertices)
    nf = len(source.faces)
    direction = np.asarray(settings.direction, dtype=np.float64)
    direction /= np.linalg.norm(direction)
    if settings.selected_face is not None:
        if not 0 <= settings.selected_face < len(surface.mesh.faces):
            raise GeometryError("Select a cortical face on this fragment.")
        normal = surface.mesh.face_normals[settings.selected_face]
        if np.dot(direction, normal * settings.side) <= 0:
            raise GeometryError(
                "The arrow points toward the selected cortical side. Reverse the arrow or choose a direction behind the cortex."
            )
    center = np.array(source.centroid)
    scale = settings.copy_percent / 100
    if settings.backing_shape == "flat":
        vertices, back_faces, walls, backing, shift = flat_back(
            source,
            data["boundary"],
            center,
            direction,
            scale,
            settings.thickness,
            settings.bridge_smoothing,
        )
    else:
        reflected = (source.vertices - center) - 2 * ((source.vertices - center) @ direction)[
            :, None
        ] * direction
        backing = center + scale * reflected
        if settings.copy_smoothing:
            with MESHLAB_LOCK:
                ms = pymeshlab.MeshSet()
                ms.add_mesh(pymeshlab.Mesh(np.array(backing), np.array(source.faces)))
                ms.apply_coord_taubin_smoothing(stepsmoothnum=int(settings.copy_smoothing))
                backing = np.array(ms.current_mesh().vertex_matrix())
        # Separate the bounding planes along the chosen arrow. Curvature
        # contributes to total depth beyond the requested minimum gap.
        axial = (source.vertices - center) @ direction
        shift = float(axial.max() - ((backing - center) @ direction).min() + settings.thickness)
        backing += shift * direction
        vertices = np.vstack((source.vertices, backing))
        vertices, walls = connect_skins(vertices, data["boundary"], n, settings.bridge_smoothing)
        back_faces = source.faces[:, ::-1] + n
    faces = np.vstack((source.faces, back_faces, walls))
    materials = np.r_[np.zeros(nf, int), np.ones(len(back_faces), int), np.full(len(walls), 2, int)]
    if settings.backing_shape == "flat":
        # Stabilize the generated planar cap before intersection checks.
        # Near-coplanar double arithmetic can produce inconsistent triangle
        # predicates; both checks use the actual generated STL coordinates.
        vertices[n:] = vertices[n:].astype(np.float32).astype(np.float64)
    vertices, faces, materials, repairs = repair_border(
        vertices, faces, materials, source, direction, settings.edge_rounding
    )
    order = np.argsort(materials, kind="stable")
    faces = faces[order]
    materials = materials[order]
    shell = trimesh.Trimesh(vertices, faces, process=False)
    if shell.volume < 0:
        shell.invert()
    floatv = vertices.astype(np.float32).astype(np.float64)
    floatmesh = trimesh.Trimesh(floatv, np.array(shell.faces), process=False)
    bad = intersection_mask(vertices, shell.faces) | intersection_mask(floatv, shell.faces)
    degenerate = int((floatmesh.area_faces < 1e-14).sum())
    with MESHLAB_LOCK:
        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(floatv, np.array(shell.faces, dtype=np.int32)))
        ms.compute_selection_by_non_manifold_per_vertex()
        nonmanifold = ms.current_mesh().selected_vertex_number()
    # Retain a face-level audit. Vertex correspondence also permits both skins
    # to be compared with their intended geometry, independent of reindexing.
    distances, mapping = cKDTree(source.vertices).query(vertices)
    mapped = mapping[np.array(shell.faces[materials == 0])]
    trustworthy = (distances[np.array(shell.faces[materials == 0])] < 1e-9).all(axis=1)
    keys = {tuple(row) for row in np.sort(mapped[trustworthy], axis=1)}
    retained = np.array([tuple(row) in keys for row in np.sort(source.faces, axis=1)])
    boundary_tree = cKDTree(source.vertices[np.unique(data["boundary"])])
    proximity = boundary_tree.query(source.vertices)[0]
    core_faces = (proximity[source.faces] > settings.edge_rounding).all(axis=1)
    core_preserved = bool(retained[core_faces].all())
    valid = bool(
        floatmesh.is_watertight
        and floatmesh.is_winding_consistent
        and not bad.any()
        and not degenerate
        and not nonmanifold
        and floatmesh.volume > 0
        and core_preserved
    )
    source_faces = int((materials == 0).sum())
    back_faces = int((materials == 1).sum())
    excluded = (
        surface.speck_mask if settings.exclude_specks else np.zeros(len(surface.mesh.faces), bool)
    )
    removed_vertices = np.unique(source.faces[~retained])
    changed_band = float(proximity[removed_vertices].max()) if len(removed_vertices) else 0.0
    valid = valid and changed_band <= settings.edge_rounding + 1e-12
    copy_rounding = (
        float(np.max(np.abs(backing.astype(np.float32).astype(np.float64) - backing)))
        if repairs or settings.backing_shape == "flat"
        else 0.0
    )
    report = dict(
        application="Fossil Forge",
        version="0.3.0",
        python_version=platform.python_version(),
        software_versions={
            name: version(name) for name in ("numpy", "scipy", "trimesh", "pymeshlab")
        },
        source_filename=surface.filename,
        source_sha256=surface.sha256,
        source_units=surface.units,
        output_units="mm",
        settings=asdict(settings),
        valid_geometry=valid,
        watertight=bool(floatmesh.is_watertight),
        consistent_winding=bool(floatmesh.is_winding_consistent),
        self_intersection_faces=int(bad.sum()),
        nonmanifold_vertices=int(nonmanifold),
        degenerate_faces=degenerate,
        volume_mm3=float(floatmesh.volume),
        source_surface_preserved=bool(retained.all()),
        core_surface_preserved=core_preserved,
        preserved_faces=int(retained.sum()),
        source_face_ids=data["face_ids"][retained].tolist(),
        changed_edge_faces=int((~retained).sum()),
        edge_cleanup_width_mm=settings.edge_rounding,
        edge_repair_faces=repairs,
        changed_edge_area_mm2=float(source.area_faces[~retained].sum()),
        changed_edge_band_max_mm=changed_band,
        mirrored_coordinate_rounding_max_mm=copy_rounding,
        excluded_speck_faces=int(excluded.sum()),
        excluded_speck_area_mm2=float(surface.mesh.area_faces[excluded].sum()),
        mirrored_copy_percent=settings.copy_percent,
        copy_smoothed=bool(settings.copy_smoothing and settings.backing_shape == "mirror"),
        bridge_smoothing_mm=settings.bridge_smoothing,
        backing_shape=settings.backing_shape,
        mirror_direction=direction.tolist(),
        mirror_center_mm=center.tolist(),
        copy_center_shift_mm=shift,
        minimum_skin_gap_mm=settings.thickness,
        total_axial_depth_mm=float((vertices @ direction).max() - (vertices @ direction).min()),
        target_displacement_mm=settings.thickness,
        displacement_min_mm=settings.thickness,
        displacement_median_mm=settings.thickness,
        tapered_vertex_percent=0.0,
        taper_iterations=0,
        stl_rounding_max_mm=float(np.max(np.abs(floatv - vertices))),
        checked_representations=["double-precision PLY", "float32 STL"],
        notes=[
            "The second skin is geometrically reflected, uniformly scaled and translated along the chosen arrow."
            if settings.backing_shape == "mirror"
            else "The generated back is a flat, scaled, smoothed projection of the cut outline perpendicular to the arrow.",
            "The gap is between bounding planes along the arrow; total thickness also includes surface curvature.",
            "Source cut-edge cleanup is limited to the requested band. Retained source triangles stay fixed.",
            "Edge repairs may replace small patches on either skin and the joining wall. Generated coordinates are stabilized at STL precision when repair is needed.",
            "Bridge smoothing uses intermediate boundary rings; it never moves original skin vertices.",
            "No local thickness taper is applied to the generated back.",
        ],
    )
    return Result(shell, report, np.flatnonzero(bad), source_faces, back_faces)
