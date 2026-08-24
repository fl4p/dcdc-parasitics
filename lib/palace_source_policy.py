#!/usr/bin/env python3
"""Reviewed Palace source identities authorized for build attestation."""
try:
    from .palace_source_identity import validate_palace_source_identity
except ImportError:
    from palace_source_identity import validate_palace_source_identity


AUTHORIZED_PALACE_SOURCE_IDENTITIES = frozenset({
    "9a50e1ad35a4b6b644afdff791f312c75b8d58ff7bc01cc5669fe7faa16be94b",
})

AUTHORIZED_PALACE_BUILDS = {
    "9a50e1ad35a4b6b644afdff791f312c75b8d58ff7bc01cc5669fe7faa16be94b": {
        "build_directory": "/Users/fab/dev/vendor/palace-build-qualification-make",
        "executable": "/Users/fab/dev/vendor/palace-build-qualification-make/bin/palace",
        "binaries": {
            "/Users/fab/dev/vendor/palace-build-qualification-make/bin/palace": (
                "ad43ec030f51435f32150a5c72cb324ca03083d2ffc2f91405bbf41c6bc2240f"
            ),
            "/Users/fab/dev/vendor/palace-build-qualification-make/bin/palace-arm64.bin": (
                "1bef63615fc4279f4fc09fa9bcd6b70ee9220f6d0c7c13cd8087c355eb54c9e2"
            ),
        },
        "mpi_launcher": "/opt/homebrew/Cellar/open-mpi/5.0.9_1/bin/mpirun",
        "mpi_launcher_sha256": (
            "c6505f9774a983ad72232217254707ea37c763848f34f5e2a33fb7713837637c"
        ),
    },
}


def validate_authorized_palace_source_identity(path):
    manifest = validate_palace_source_identity(path)
    digest = manifest["content_sha256"]
    if digest not in AUTHORIZED_PALACE_SOURCE_IDENTITIES:
        raise ValueError("Palace source identity is not authorized for build attestation")
    return manifest


def authorized_palace_build(source_identity_sha256):
    try:
        return AUTHORIZED_PALACE_BUILDS[source_identity_sha256]
    except KeyError as error:
        raise ValueError("Palace source identity has no authorized build") from error
