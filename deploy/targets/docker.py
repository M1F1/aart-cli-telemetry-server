"""The image under Docker on one host: this machine, or another one through a Docker context.

The image is pulled from its registry unless `PULL=false`, for an image built on the host. The
container replaces the one of the same name; the data stays on its volume. Its port is
published on `PUBLISH`, loopback unless set otherwise, for whatever the host puts in front to reach:
that front decides who reaches the server, as `--behind-authenticating-proxy` says it does. A
PostgreSQL URL, which can carry a password, comes from `ENV_FILE`: a file of `KEY=VALUE` lines
Docker reads on the machine running this, which nothing here reads.
"""

from __future__ import annotations

from collections.abc import Mapping

from deploy import Refused, Step

NAME = "docker"
REQUIRED = ("IMAGE",)
OPTIONAL = {
    "CONTAINER": "aart-cli-telemetry-server",
    "DOCKER_CONTEXT": "",
    "PUBLISH": "127.0.0.1:8000",
    "VOLUME": "aart-cli-telemetry",
    "ENV_FILE": "",
    "PULL": "true",
}


def plan(settings: Mapping[str, str]) -> tuple[Step, ...]:
    docker = (
        ("docker", "--context", settings["DOCKER_CONTEXT"])
        if settings["DOCKER_CONTEXT"]
        else ("docker",)
    )
    container, image = settings["CONTAINER"], settings["IMAGE"]
    env_file = ("--env-file", settings["ENV_FILE"]) if settings["ENV_FILE"] else ()
    if settings["PULL"] not in ("true", "false"):
        raise Refused("PULL is true or false")
    pull = (Step((*docker, "pull", image), "the image, from its registry"),)
    return (
        *(pull if settings["PULL"] == "true" else ()),
        Step(
            (*docker, "stop", "--time", "30", container),
            "the running container stops, finishing what it serves; absent the first time",
            may_fail=True,
        ),
        Step(
            (*docker, "rm", container),
            "the stopped container goes; its volume and the data on it stay",
            may_fail=True,
        ),
        Step(
            (
                *docker,
                "run",
                "--detach",
                "--name",
                container,
                "--restart",
                "unless-stopped",
                "--read-only",
                "--publish",
                f"{settings['PUBLISH']}:8000",
                "--volume",
                f"{settings['VOLUME']}:/data",
                "--env",
                "AART_CLI_TELEMETRY_BEHIND_AUTHENTICATING_PROXY=true",
                *env_file,
                image,
            ),
            "the new container, on the same volume",
        ),
        Step(
            (*docker, "inspect", "--format", "{{.State.Health.Status}}", container),
            "the image's health check, GET /healthz, reports healthy",
            until="healthy",
            attempts=90,
        ),
    )
