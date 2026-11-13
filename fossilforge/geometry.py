"""Surface loading, curve-following and directional growth, tapering and geometry checks."""

from __future__ import annotations

import hashlib
import platform
import threading
from dataclasses import asdict, dataclass
from importlib.metadata import version
from io import BytesIO

import numpy as np
import pymeshlab
import trimesh
from scipy.sparse import coo_matrix, diags, eye
from scipy.sparse.linalg import spsolve
from scipy.spatial import cKDTree

# MeshLab plugins use native state. Serialize calls even across HTTP workers.
MESHLAB_LOCK = threading.RLock()
UNIT_SCALE = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}


class GeometryError(ValueError):
    pass


@dataclass
class Settings:
    thickness: float = 2.0
    side: int = -1
    mode: str = "curve"
    smoothing: float = 5.0
    adaptive: bool = True
    minimum: float = 0.05
    direction: tuple[float, float, float] = (0, 0, -1)
    exclude_specks: bool = True
    copy_percent: float = 90.0
    copy_smoothing: int = 0
    edge_rounding: float = 0.4
    selected_face: int | None = None
    bridge_smoothing: float = 0.0
    backing_shape: str = "mirror"

    def validate(self):
        values = [
            self.thickness,
            self.smoothing,
            self.minimum,
            self.copy_percent,
            self.copy_smoothing,
            self.edge_rounding,
            self.bridge_smoothing,
            *self.direction,
        ]
        if not np.isfinite(values).all():
            raise GeometryError("All settings must be finite numbers.")
        if not 0.01 <= self.thickness <= 100:
            raise GeometryError("Choose a thickness between 0.01 and 100 mm.")
        if not 0 <= self.smoothing <= 50:
            raise GeometryError("Growth smoothing must be between 0 and 50 mm.")
        if self.mode != "mirror" and not 0.001 <= self.minimum <= self.thickness:
            raise GeometryError(
                "The taper limit must be positive and no greater than the target thickness."
            )
        if self.side not in (-1, 1) or self.mode not in ("curve", "direction", "mirror"):
            raise GeometryError("Invalid growth mode or side.")
        if self.mode in ("direction", "mirror") and np.linalg.norm(self.direction) < 1e-8:
            raise GeometryError("The growth arrow needs a nonzero direction.")
        if not 1 <= self.copy_percent <= 100:
            raise GeometryError("The copy size must be between 1% and 100% of the original.")
        if not 0 <= self.copy_smoothing <= 100 or int(self.copy_smoothing) != self.copy_smoothing:
            raise GeometryError("Choose 0 to 100 copy-smoothing steps.")
        if not 0 <= self.edge_rounding <= 5:
            raise GeometryError("Edge cleanup must be between 0 and 5 mm.")
        if not 0 <= self.bridge_smoothing <= 5:
            raise GeometryError("Bridge smoothing must be between 0 and 5 mm.")
        if self.backing_shape not in ("mirror", "flat"):
            raise GeometryError("Choose a mirrored skin or a flat back.")


def intersection_mask(vertices, faces):
    with MESHLAB_LOCK:
        ms = pymeshlab.MeshSet()
        ms.add_mesh(
            pymeshlab.Mesh(np.array(vertices, dtype=np.float64), np.array(faces, dtype=np.int32))
        )
        ms.compute_selection_by_self_intersections_per_face()
        return np.array(ms.current_mesh().face_selection_array(), dtype=bool)


def edge_data(faces):
    directed = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges, inverse, counts = np.unique(
        np.sort(directed, axis=1), axis=0, return_inverse=True, return_counts=True
    )
    return edges, counts, directed[counts[inverse] == 1]


def export_ply(mesh):
    """Double precision PLY retains the source coordinates, including OBJ inputs."""
    v = np.asarray(mesh.vertices, dtype="<f8")
    f = np.empty(len(mesh.faces), dtype=[("count", "u1"), ("indices", "<i4", (3,))])
    f["count"] = 3
    f["indices"] = mesh.faces
    header = (
        "ply\nformat binary_little_endian 1.0\ncomment Fossil Forge; coordinates in millimetres\n"
        f"element vertex {len(v)}\nproperty double x\nproperty double y\nproperty double z\n"
        f"element face {len(f)}\nproperty list uchar int vertex_indices\nend_header\n"
    )
    return header.encode("ascii") + v.tobytes() + f.tobytes()


class Surface:
    def __init__(self, content: bytes, filename: str, units: str):
        extension = filename.rsplit(".", 1)[-1].lower()
        if extension not in ("ply", "stl", "obj"):
            raise GeometryError("Open a triangular PLY, STL, or OBJ surface.")
        if units not in UNIT_SCALE:
            raise GeometryError("Choose the units of the source file.")
        try:
            loaded = trimesh.load(
                BytesIO(content), file_type=extension, force="mesh", process=False
            )
        except Exception as exc:
            raise GeometryError(f"Could not read this mesh: {exc}") from exc
        if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
            raise GeometryError(
                "This file has no triangular surface. Point clouds need meshing first."
            )
        if len(loaded.faces) > 2_000_000:
            raise GeometryError("This version supports up to two million triangles per file.")
        v = np.asarray(loaded.vertices, dtype=np.float64) * UNIT_SCALE[units]
        f = np.asarray(loaded.faces, dtype=np.int64)
        if not np.isfinite(v).all():
            raise GeometryError("The file contains invalid vertex coordinates.")
        if f.min() < 0 or f.max() >= len(v):
            raise GeometryError("The file contains invalid triangle indices.")
        # STL stores a triangle soup; weld exact coordinates without rounding.
        unique, inverse = np.unique(v, axis=0, return_inverse=True)
        f = inverse[f]
        self.mesh = trimesh.Trimesh(unique, f, process=False)
        self.winding_repaired = not self.mesh.is_winding_consistent
        if self.winding_repaired:
            # Reorient triangles without moving vertices, before side picking.
            trimesh.repair.fix_winding(self.mesh)
        self.filename = filename
        self.content = content
        self.units = units
        self.sha256 = hashlib.sha256(content).hexdigest()
        self.prepared = {}
        self._source_intersections = None
        labels = trimesh.graph.connected_component_labels(
            self.mesh.face_adjacency, node_count=len(f)
        )
        areas = np.bincount(labels, weights=self.mesh.area_faces)
        # Only detached face fans smaller than 0.01 mm² and <0.1% of the main fan.
        cutoff = min(0.01, float(areas.max()) * 0.001)
        self.speck_mask = (areas[labels] < cutoff) & (areas[labels] < areas.max())
        self.component_count = len(areas)

    def prepare(self, exclude_specks=True):
        if exclude_specks in self.prepared:
            return self.prepared[exclude_specks]
        keep = ~self.speck_mask if exclude_specks else np.ones(len(self.mesh.faces), dtype=bool)
        ids = np.flatnonzero(keep)
        faces = np.array(self.mesh.faces[ids])
        used, inverse = np.unique(faces, return_inverse=True)
        mesh = trimesh.Trimesh(
            np.array(self.mesh.vertices[used]), inverse.reshape(-1, 3), process=False
        )
        if (mesh.area_faces < 1e-14).any():
            raise GeometryError(
                "The scan has degenerate triangles. Repair them in the source editor first."
            )
        edges, counts, boundary = edge_data(mesh.faces)
        if (counts > 2).any():
            raise GeometryError(
                "Some edges belong to more than two triangles. Repair this non-manifold surface first."
            )
        if not len(boundary):
            raise GeometryError(
                "This mesh is already closed. Export the open cortical surface before filling holes."
            )
        if not mesh.is_winding_consistent:
            trimesh.repair.fix_winding(mesh)
        # Boundary fans must form loops; touching fans need explicit exclusion/repair.
        edges, counts, boundary = edge_data(mesh.faces)
        _, degrees = np.unique(boundary, return_counts=True)
        if (degrees != 2).any():
            raise GeometryError(
                "Boundary loops touch at a vertex. Enable scan-speck exclusion or repair the boundary first."
            )
        with MESHLAB_LOCK:
            ms = pymeshlab.MeshSet()
            ms.add_mesh(pymeshlab.Mesh(np.array(mesh.vertices), np.array(mesh.faces)))
            ms.compute_selection_by_non_manifold_per_vertex()
            if ms.current_mesh().selected_vertex_number():
                raise GeometryError(
                    "The source has a vertex shared by separate face fans. Repair the surface first."
                )
        bad = intersection_mask(mesh.vertices, mesh.faces)
        if bad.any():
            raise GeometryError(
                f"The source itself has {int(bad.sum()):,} intersecting triangles. Repair them before thickening."
            )
        n = len(mesh.vertices)
        rows, cols = np.r_[edges[:, 0], edges[:, 1]], np.r_[edges[:, 1], edges[:, 0]]
        adj = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)).tocsr()
        degree = np.asarray(adj.sum(axis=1)).ravel()
        lap = eye(n, format="csr") - diags(1 / degree) @ adj
        data = dict(
            mesh=mesh,
            edges=edges,
            boundary=boundary,
            adj=adj,
            lap=lap,
            face_ids=ids,
            directions={},
            tree=cKDTree(mesh.vertices),
        )
        self.prepared[exclude_specks] = data
        return data

    def info(self):
        edges, counts, boundary = edge_data(self.mesh.faces)
        _, _, vh = np.linalg.svd(
            self.mesh.vertices - self.mesh.vertices.mean(axis=0), full_matrices=False
        )
        direction = vh[-1]
        if np.dot(direction, self.mesh.face_normals.mean(axis=0)) < 0:
            direction = -direction
        if self._source_intersections is None:
            self._source_intersections = int(
                intersection_mask(self.mesh.vertices, self.mesh.faces).sum()
            )
        return dict(
            filename=self.filename,
            units=self.units,
            vertices=len(self.mesh.vertices),
            faces=len(self.mesh.faces),
            bounds=self.mesh.bounds.tolist(),
            size_mm=self.mesh.extents.tolist(),
            boundary_edges=len(boundary),
            nonmanifold_edges=int((counts > 2).sum()),
            source_intersections=self._source_intersections,
            winding_consistent=bool(self.mesh.is_winding_consistent),
            closed=bool(self.mesh.is_watertight),
            winding_repaired=self.winding_repaired,
            components=self.component_count,
            speck_faces=int(self.speck_mask.sum()),
            speck_area_mm2=float(self.mesh.area_faces[self.speck_mask].sum()),
            suggested_direction=(-direction).tolist(),
            face_normals=self.mesh.face_normals.astype(np.float32).tolist(),
        )


def growth_directions(data, settings):
    mesh = data["mesh"]
    n = len(mesh.vertices)
    if settings.mode == "direction":
        direction = np.array(settings.direction, dtype=float)
        return np.tile(direction / np.linalg.norm(direction), (n, 1))
    key = round(settings.smoothing, 4)
    if key not in data["directions"]:
        lengths = np.linalg.norm(
            mesh.vertices[data["edges"][:, 0]] - mesh.vertices[data["edges"][:, 1]], axis=1
        )
        spacing = float(np.median(lengths))
        lam = (settings.smoothing / max(spacing, 1e-8)) ** 2
        raw = np.array(mesh.vertex_normals)
        normals = spsolve(eye(n, format="csr") + lam * data["lap"], raw) if lam > 0 else raw
        length = np.linalg.norm(normals, axis=1)
        normals[length < 1e-10] = raw[length < 1e-10]
        normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-15)
        # Smooth only the growth field. Keep growth on the proper side of every
        # incident source triangle, including folded scan edges.
        for _ in range(30):
            for corner in range(3):
                ids = mesh.faces[:, corner]
                dots = np.einsum("ij,ij->i", normals[ids], mesh.face_normals)
                np.add.at(normals, ids, np.maximum(0.1 - dots, 0)[:, None] * mesh.face_normals)
            normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-15)
        alignment = np.min(np.sum(normals[mesh.faces] * mesh.face_normals[:, None], axis=2))
        if alignment < -1e-5:
            raise GeometryError(
                "Some folds have conflicting growth directions. Try a different surface cut."
            )
        data["directions"][key] = normals
    return data["directions"][key] * settings.side


@dataclass
class Result:
    mesh: trimesh.Trimesh
    report: dict
    collision_faces: np.ndarray
    source_face_count: int
    backing_face_count: int


def thicken(surface: Surface, settings: Settings) -> Result:
    settings.validate()
    data = surface.prepare(settings.exclude_specks)
    if settings.mode == "mirror":
        from .mirror import mirrored_sandwich

        return mirrored_sandwich(surface, settings, data)
    mesh, boundary = data["mesh"], data["boundary"]
    n, nf = len(mesh.vertices), len(mesh.faces)
    directions = growth_directions(data, settings)
    a, b = boundary.T
    walls = np.vstack((np.column_stack((b, a, a + n)), np.column_stack((b, a + n, b + n))))
    faces = np.vstack((mesh.faces, mesh.faces[:, ::-1] + n, walls))
    # Dataclasses accept whole-number inputs too; tapering must never truncate.
    thickness = np.full(n, settings.thickness, dtype=np.float64)
    iterations = 0
    for step in range(19 if settings.adaptive else 1):
        vertices = np.vstack((mesh.vertices, mesh.vertices + thickness[:, None] * directions))
        # Validate the STL representation, which is float32. PLY keeps doubles.
        stl_vertices = vertices.astype(np.float32).astype(np.float64)
        bad = intersection_mask(stl_vertices, faces)
        iterations = step
        if not bad.any() or not settings.adaptive:
            break
        affected = np.unique(faces[bad] % n)
        next_t = thickness.copy()
        next_t[affected] = np.maximum(settings.minimum, next_t[affected] * 0.6)
        distance, neighbors = data["tree"].query(mesh.vertices[affected], k=min(80, n))
        if distance.ndim == 1:
            distance, neighbors = distance[:, None], neighbors[:, None]
        # Spread a taper rather than introducing new abrupt steps. These are
        # displacement lengths, not guaranteed normal wall thicknesses.
        for j in range(neighbors.shape[1]):
            np.minimum.at(next_t, neighbors[:, j], next_t[affected] + distance[:, j] * 0.6)
        if np.max(np.abs(next_t - thickness)) < 1e-9:
            break
        if step == 18:
            break
        thickness = next_t
    shell = trimesh.Trimesh(vertices, faces, process=False)
    # PLY retains doubles; check it too rather than trusting STL rounding.
    bad |= intersection_mask(vertices, faces)
    if shell.volume < 0:
        shell.invert()
    float_mesh = trimesh.Trimesh(stl_vertices, np.array(shell.faces), process=False)
    degenerates = int((float_mesh.area_faces < 1e-14).sum())
    watertight = bool(float_mesh.is_watertight)
    winding = bool(float_mesh.is_winding_consistent)
    volume = float(float_mesh.volume)
    # Vertex links catch closed-looking meshes that only meet at a point.
    with MESHLAB_LOCK:
        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(stl_vertices, np.array(shell.faces, dtype=np.int32)))
        ms.compute_selection_by_non_manifold_per_vertex()
        bad_vertices = ms.current_mesh().selected_vertex_number()
    valid = (
        watertight
        and winding
        and not bad.any()
        and not degenerates
        and not bad_vertices
        and volume > 0
    )
    excluded = (
        surface.speck_mask
        if settings.exclude_specks
        else np.zeros(len(surface.mesh.faces), dtype=bool)
    )
    # The first n vertices and first nf triangles remain the measured surface.
    preserved = bool(np.array_equal(vertices[:n], mesh.vertices))
    report = dict(
        application="Fossil Forge",
        version="0.1.0",
        source_filename=surface.filename,
        software_versions={
            name: version(name) for name in ("numpy", "scipy", "trimesh", "pymeshlab")
        },
        python_version=platform.python_version(),
        source_sha256=surface.sha256,
        source_units=surface.units,
        output_units="mm",
        source_faces=len(surface.mesh.faces),
        source_size_mm=surface.mesh.extents.tolist(),
        source_winding_repaired=surface.winding_repaired,
        settings=asdict(settings),
        valid_geometry=bool(valid),
        watertight=watertight,
        checked_representations=["double-precision PLY", "float32 STL"],
        consistent_winding=winding,
        self_intersection_faces=int(bad.sum()),
        nonmanifold_vertices=int(bad_vertices),
        degenerate_faces=degenerates,
        volume_mm3=volume,
        source_surface_preserved=preserved,
        preserved_faces=nf,
        excluded_speck_faces=int(excluded.sum()),
        excluded_speck_area_mm2=float(surface.mesh.area_faces[excluded].sum()),
        source_face_ids=data["face_ids"].tolist(),
        target_displacement_mm=settings.thickness,
        displacement_min_mm=float(thickness.min()),
        displacement_median_mm=float(np.median(thickness)),
        displacement_percentiles_mm=np.percentile(thickness, [5, 25, 50, 75, 95]).tolist(),
        tapered_vertex_percent=float(np.mean(thickness < settings.thickness - 0.01) * 100),
        taper_iterations=iterations,
        stl_rounding_max_mm=float(np.max(np.abs(stl_vertices - vertices))),
        notes=[
            "Added backing is synthetic; it does not reconstruct missing anatomy.",
            "Displacement is measured along the growth field; normal wall thickness and printer suitability need slicer review.",
            "The retained source vertices are unchanged. Excluded speck faces are recorded separately.",
        ],
    )
    return Result(shell, report, np.flatnonzero(bad), nf, nf)
