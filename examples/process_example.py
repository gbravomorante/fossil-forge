"""Reproduce the checked Example.ply backing without opening the UI."""

import json
import zipfile
from pathlib import Path

from fossilforge.geometry import Settings, Surface, export_ply, thicken


def main():
    root = Path(__file__).resolve().parent
    surface = Surface((root / "Example.ply").read_bytes(), "Example.ply", "mm")
    result = thicken(surface, Settings(thickness=2, side=-1, smoothing=5, minimum=0.05))
    summary = {k: v for k, v in result.report.items() if k not in ("source_face_ids", "notes")}
    print(json.dumps(summary, indent=2))
    if not result.report["valid_geometry"]:
        raise SystemExit("Geometry checks failed. No printable mesh was exported.")
    output = root / "outputs"
    output.mkdir(exist_ok=True)
    (output / "Example_backed.ply").write_bytes(export_ply(result.mesh))
    (output / "Example_backed.stl").write_bytes(result.mesh.export(file_type="stl"))
    (output / "Example_report.json").write_text(
        json.dumps(result.report, indent=2), encoding="utf-8"
    )
    with zipfile.ZipFile(
        output / "Example_backing_project.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for name in ("Example_backed.stl", "Example_backed.ply"):
            archive.write(output / name, name)
        archive.writestr("source/Example.ply", surface.content)
        archive.writestr("report.json", json.dumps(result.report, indent=2))


if __name__ == "__main__":
    main()
