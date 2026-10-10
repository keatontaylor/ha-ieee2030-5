"""Build the HACS install ZIP with a release-specific manifest version."""

import argparse
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "ieee2030_5"


def package(version: str, output: Path) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("A numeric semantic version is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(COMPONENT.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            content = path.read_bytes()
            if path.name == "manifest.json":
                manifest = json.loads(content)
                manifest["version"] = version
                content = (json.dumps(manifest, indent=2) + "\n").encode()
            archive.writestr(str(path.relative_to(COMPONENT)), content)
        archive.write(ROOT / "LICENSE", "LICENSE")
        archive.write(ROOT / "NOTICE", "NOTICE")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "ieee2030_5.zip")
    args = parser.parse_args()
    package(args.version, args.output)
