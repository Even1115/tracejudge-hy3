"""LiveCodeBench isolated-execution boundary (official checker in Docker).

The host package only prepares disclosure-safe control files, launches a
hardened container, and reads back the strict raw-result mapping.  Candidate
code is never executed, imported, or compiled on the host; hidden tests are
never decoded on the host.  Both happen only inside the container via the
pinned official LiveCodeBench checker.
"""

from .docker_runner import (
    DEFAULT_LCB_PLATFORM,
    DockerLimits,
    LCBDockerRunner,
    LCBRunnerError,
)

__all__ = [
    "DEFAULT_LCB_PLATFORM",
    "DockerLimits",
    "LCBDockerRunner",
    "LCBRunnerError",
]
