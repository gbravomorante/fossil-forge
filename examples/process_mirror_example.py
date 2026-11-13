"""Reproduce the checked reflected sandwich for examples/Example.ply."""

import json
import zipfile
from io import BytesIO
from pathlib import Path

import trimesh

from fossilforge.geometry import Settings, Surface, export_ply, intersection_mask, thicken


def main():
    root = Path(__file__).resolve().parent
    surface = Surface((root / "Example.ply").read_bytes(), "Example.ply", "mm")
    settings = Settings(
        mode="mirror",
        direction=tuple(surface.info()["suggested_direction"]),
        copy_percent=90,
        thickness=2,
        edge_rounding=0.4,
    )
    result = thicken(surface, settings)
    print(json.dumps({k: v for k, v in result.report.items() if k != "source_face_ids"}, indent=2))
    if not result.report["valid_geometry"]:
        raise SystemExit("Geometry checks failed; no printable mesh exported.")
    stl = result.mesh.export(file_type="stl")
    reloaded = trimesh.load(BytesIO(stl), file_type="stl", process=True)
    if (
        not reloaded.is_watertight
        or not reloaded.is_winding_consistent
        or intersection_mask(reloaded.vertices, reloaded.faces).any()
    ):
        raise SystemExit("STL re-import check failed; no printable mesh exported.")
    output = root / "outputs"
    output.mkdir(exist_ok=True)
    (output / "Example_mirrored.stl").write_bytes(stl)
    (output / "Example_mirrored.ply").write_bytes(export_ply(result.mesh))
    (output / "Example_mirrored_report.json").write_text(
        json.dumps(result.report, indent=2), encoding="utf-8"
    )
    with zipfile.ZipFile(
        output / "Example_mirrored_project.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for name in ("Example_mirrored.stl", "Example_mirrored.ply"):
            archive.write(output / name, name)
        archive.writestr("source/Example.ply", surface.content)
        archive.writestr("report.json", json.dumps(result.report, indent=2))


if __name__ == "__main__":
    main()
