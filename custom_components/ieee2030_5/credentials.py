"""Private on-disk credentials, manipulated only in HA's executor."""

import shutil
import uuid
from pathlib import Path

from .const import DOMAIN


def store_credentials(
    config_dir: str, certificate: bytes, key: bytes, ca: bytes | None = None
) -> str:
    """Allocate an immutable credential set; failed reconfiguration cannot clobber it."""
    identifier = uuid.uuid4().hex
    directory = Path(config_dir) / ".storage" / DOMAIN / identifier
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    try:
        for name, content in (("client.pem", certificate), ("key.pem", key), ("ca.pem", ca)):
            if content is not None:
                path = directory / name
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    stream.write(content)
    except Exception:
        shutil.rmtree(directory)
        raise
    return identifier


def credential_paths(config_dir: str, identifier: str) -> tuple[str, str, str | None]:
    if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
        raise ValueError("Invalid credential identifier")
    directory = Path(config_dir) / ".storage" / DOMAIN / identifier
    ca = directory / "ca.pem"
    return (
        str(directory / "client.pem"),
        str(directory / "key.pem"),
        str(ca) if ca.exists() else None,
    )


def remove_credentials(config_dir: str, identifier: str) -> None:
    cert, _, _ = credential_paths(config_dir, identifier)
    shutil.rmtree(Path(cert).parent, ignore_errors=True)
