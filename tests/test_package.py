import json
from zipfile import ZipFile

from scripts.package import package


def test_hacs_zip_layout_and_version(tmp_path):
    output = tmp_path / "integration.zip"
    package("0.1.42", output)
    with ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == "0.1.42"
        assert manifest["domain"] == "ieee2030_5"
        assert "config_flow.py" in archive.namelist()
        assert "translations/en.json" in archive.namelist()
        assert "LICENSE" in archive.namelist()
        assert not any(name.endswith((".pem", ".key", ".pyc")) for name in archive.namelist())
        assert not any(name.startswith("custom_components/") for name in archive.namelist())
