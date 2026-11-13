"""Reproduce either checked smooth-bridge example, retaining provenance."""

import argparse
import json
import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import trimesh

from fossilforge.geometry import Settings, Surface, export_ply, intersection_mask, thicken


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--back-shape", choices=["mirror", "flat"], default="mirror")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    source = Surface((root / "Example.ply").read_bytes(), "Example.ply", "mm")
    settings = Settings(
        mode="mirror",
        backing_shape=args.back_shape,
        bridge_smoothing=1,
        direction=tuple(source.info()["suggested_direction"]),
    )
    result = thicken(source, settings)
    print(json.dumps({k: v for k, v in result.report.items() if k != "source_face_ids"}, indent=2))
    if not result.report["valid_geometry"]:
        raise SystemExit("Geometry checks failed; no printable mesh exported.")
    # Confirm every claimed retained triangle exactly, independent of winding.
    ids = np.array(result.report["source_face_ids"])
    measured = source.mesh.vertices[source.mesh.faces[ids]]
    retained = result.mesh.vertices[result.mesh.faces[: len(ids)]]
    assert np.all(
        np.any(np.all(retained[:, :, None, :] == measured[:, None, :, :], axis=-1), axis=2)
    )
    stl = result.mesh.export(file_type="stl")
    loaded = trimesh.load(BytesIO(stl), file_type="stl", process=True)
    assert loaded.is_watertight and loaded.is_winding_consistent
    assert not intersection_mask(loaded.vertices, loaded.faces).any()
    name = "Example_smooth_mirror" if args.back_shape == "mirror" else "Example_flat_back"
    output = root / "outputs"
    output.mkdir(exist_ok=True)
    (output / f"{name}.stl").write_bytes(stl)
    (output / f"{name}.ply").write_bytes(export_ply(result.mesh))
    (output / f"{name}_report.json").write_text(
        json.dumps(result.report, indent=2), encoding="utf-8"
    )
    with zipfile.ZipFile(
        output / f"{name}_project.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr(f"{name}.stl", stl)
        archive.writestr(f"{name}.ply", export_ply(result.mesh))
        archive.writestr("source/Example.ply", source.content)
        archive.writestr("report.json", json.dumps(result.report, indent=2))


if __name__ == "__main__":
    main()
