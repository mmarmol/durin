"""Docker container lifecycle for SWE-bench evaluation.

Supports two modes:
  1. "internal" (recommended): Agent runs INSIDE the container.
     No path confusion, no contamination, pre-compiled deps.
  2. "external" (legacy): Agent runs on host, exec routed via docker exec.

Usage:
    mgr = DockerManager()

    # Internal mode (agent inside container)
    result = mgr.run_instance_internal(instance, run_id, config)

    # External mode (agent on host)
    container_id = mgr.start_container(instance, workspace)
    # ... agent runs ...
    mgr.stop_container(container_id)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import docker
from loguru import logger

from swebench.harness.docker_build import (
    build_env_images,
    build_instance_image,
    make_test_spec,
)
from swebench.harness.test_spec.test_spec import TestSpec

_DURIN_ROOT = Path(__file__).resolve().parent.parent
_DURIN_EVAL_IMAGE_PREFIX = "durin.eval"

_DURIN_DEPS = [
    "pydantic>=2.12.0",
    "pydantic-settings>=2.12.0",
    "httpx>=0.28.0",
    "loguru>=0.7.3",
    "tiktoken>=0.12.0",
    "jinja2>=3.1.0",
    "pyyaml>=6.0",
    "json-repair>=0.57.0",
    "chardet>=3.0.2",
    "litellm>=1.40.0",
    "filelock>=3.25.2",
]


class DockerManager:
    """Manages Docker containers for SWE-bench evaluation."""

    def __init__(self) -> None:
        self._client = docker.from_env()
        self._specs_cache: dict[str, TestSpec] = {}

    def get_test_spec(self, instance: dict) -> TestSpec:
        """Get or cache the TestSpec for an instance."""
        iid = instance["instance_id"]
        if iid not in self._specs_cache:
            self._specs_cache[iid] = make_test_spec(instance)
        return self._specs_cache[iid]

    # ------------------------------------------------------------------
    # Image management
    # ------------------------------------------------------------------

    def ensure_env_image(self, instance: dict) -> str:
        """Build the env image if not present. Returns the image key."""
        spec = self.get_test_spec(instance)
        env_key = spec.env_image_key

        try:
            self._client.images.get(env_key)
            logger.debug("Env image already exists: {}", env_key)
        except docker.errors.ImageNotFound:
            logger.info("Building env image: {} (this may take a while)...", env_key)
            build_env_images(
                client=self._client,
                dataset=[instance],
                force_rebuild=False,
                max_workers=1,
                instance_image_tag="latest",
                env_image_tag="latest",
            )
            logger.info("Env image built: {}", env_key)

        return env_key

    def ensure_instance_image(self, instance: dict) -> str:
        """Build the instance image if not present. Returns the image key."""
        spec = self.get_test_spec(instance)
        img_key = spec.instance_image_key

        try:
            self._client.images.get(img_key)
            logger.debug("Instance image already exists: {}", img_key)
        except docker.errors.ImageNotFound:
            logger.info("Building instance image: {}...", img_key)
            self.ensure_env_image(instance)
            build_instance_image(spec, self._client, logger, nocache=False)
            logger.info("Instance image built: {}", img_key)

        return img_key

    def ensure_durin_eval_image(self, instance: dict) -> str:
        """Build the durin eval image (instance + durin deps in separate env).

        This extends the instance image with a conda env 'durin' that has
        the agent's dependencies, completely isolated from 'testbed'.
        """
        instance_key = self.ensure_instance_image(instance)
        iid = instance["instance_id"].replace("/", "_").replace("__", "_")
        durin_key = f"{_DURIN_EVAL_IMAGE_PREFIX}.{iid}:latest"

        try:
            self._client.images.get(durin_key)
            logger.debug("Durin eval image already exists: {}", durin_key)
            return durin_key
        except docker.errors.ImageNotFound:
            pass

        logger.info("Building durin eval image: {}...", durin_key)
        deps_str = " ".join(f'"{d}"' for d in _DURIN_DEPS)
        dockerfile_content = f"""\
FROM {instance_key}
RUN /opt/miniconda3/bin/conda create -n durin python=3.11 -y 2>/dev/null && \\
    /opt/miniconda3/envs/durin/bin/pip install --quiet {deps_str}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            df_path = Path(tmpdir) / "Dockerfile"
            df_path.write_text(dockerfile_content)
            self._client.images.build(
                path=tmpdir,
                tag=durin_key,
                rm=True,
                platform="linux/x86_64",
            )

        logger.info("Durin eval image built: {}", durin_key)
        return durin_key

    # ------------------------------------------------------------------
    # Internal mode: agent runs inside container
    # ------------------------------------------------------------------

    def run_instance_internal(
        self,
        instance: dict,
        run_id: str,
        api_key: str,
        api_base: str,
        model: str = "glm-5.1",
        deliberation: bool = True,
        max_iterations: int = 100,
        agent: str = "durin",
    ) -> dict:
        """Run agent inside container. Returns result dict with patch + stats.

        Args:
            agent: "durin" or "nanobot" — which agent to run.
        """
        iid = instance["instance_id"]
        durin_image = self.ensure_durin_eval_image(instance)
        container_name = f"durin_eval_{iid.replace('/', '_')}_{run_id}"

        self._cleanup_existing(container_name)

        output_dir = Path(tempfile.mkdtemp(prefix=f"durin_out_{iid}_"))

        problem_file = output_dir / "problem.txt"
        problem_file.write_text(instance["problem_statement"])

        durin_root = str(_DURIN_ROOT)
        spec = self.get_test_spec(instance)

        if agent == "nanobot":
            entrypoint = self._nanobot_entrypoint(instance, run_id, model, api_key, api_base, max_iterations)
        else:
            entrypoint = (
                f"source /opt/miniconda3/bin/activate && "
                f"conda activate durin && "
                f"cd /testbed && "
                f"python /opt/durin/scripts/swebench_run_inside.py "
                f"--instance-id {iid} "
                f"--repo {instance['repo']} "
                f"--problem-statement /output/problem.txt "
                f"--api-key {api_key} "
                f"--api-base {api_base} "
                f"--model {model} "
                f"--max-iterations {max_iterations} "
                f"--run-id {run_id} "
                f"{'--no-deliberation' if not deliberation else ''}"
            )

        logger.info("[{}] Starting container (agent={})...", iid, agent)
        start = time.time()

        try:
            container = self._client.containers.run(
                image=durin_image,
                name=container_name,
                detach=True,
                command=["bash", "-c", entrypoint],
                volumes={
                    durin_root: {"bind": "/opt/durin", "mode": "ro"},
                    str(output_dir): {"bind": "/output", "mode": "rw"},
                },
                working_dir="/testbed",
                platform=spec.platform,
                environment={"ZAI_API_KEY": api_key, "ZAI_API_BASE": api_base},
            )

            exit_status = container.wait(timeout=960)
            elapsed = time.time() - start
            exit_code = exit_status.get("StatusCode", -1)

            logs = container.logs(tail=50).decode("utf-8", errors="replace")
            logger.info("[{}] Container exited {} in {:.0f}s", iid, exit_code, elapsed)

            container.remove(force=True)
        except Exception as e:
            elapsed = time.time() - start
            logger.error("[{}] Container error: {}", iid, str(e)[:200])
            self._cleanup_existing(container_name)
            return {
                "instance_id": iid,
                "model_patch": "",
                "error": f"container_error: {e}",
                "elapsed_s": round(elapsed, 1),
            }

        return self._collect_results(iid, output_dir, elapsed)

    def _nanobot_entrypoint(
        self,
        instance: dict,
        run_id: str,
        model: str,
        api_key: str,
        api_base: str,
        max_iterations: int,
    ) -> str:
        """Build entrypoint command for running nanobot (no posture/delib)."""
        iid = instance["instance_id"]
        return (
            f"source /opt/miniconda3/bin/activate && "
            f"conda activate durin && "
            f"cd /testbed && "
            f"python /opt/durin/scripts/swebench_run_inside.py "
            f"--instance-id {iid} "
            f"--repo {instance['repo']} "
            f"--problem-statement /output/problem.txt "
            f"--api-key {api_key} "
            f"--api-base {api_base} "
            f"--model {model} "
            f"--max-iterations {max_iterations} "
            f"--run-id {run_id} "
            f"--no-deliberation "
            f"--agent nanobot"
        )

    def _collect_results(self, iid: str, output_dir: Path, elapsed: float) -> dict:
        """Collect results from the /output volume after container exits."""
        result_file = output_dir / "result.json"
        patch_file = output_dir / "patch.diff"

        if result_file.exists():
            result = json.loads(result_file.read_text())
            result["elapsed_s"] = round(elapsed, 1)
            return result

        patch = patch_file.read_text() if patch_file.exists() else ""
        return {
            "instance_id": iid,
            "model_patch": patch,
            "elapsed_s": round(elapsed, 1),
            "error": "no_result_json" if not patch else None,
        }

    # ------------------------------------------------------------------
    # External mode: agent on host, exec via docker exec (legacy)
    # ------------------------------------------------------------------

    def start_container(
        self,
        instance: dict,
        workspace: Path,
        run_id: str = "eval",
    ) -> str:
        """Start a container with workspace mounted at /testbed (legacy mode).

        Uses the env image and mounts the workspace for host-based agent.
        Returns the container ID.
        """
        spec = self.get_test_spec(instance)
        env_key = self.ensure_env_image(instance)
        iid = instance["instance_id"]
        container_name = f"durin_eval_{iid.replace('/', '_')}_{run_id}"

        self._cleanup_existing(container_name)

        ws = str(Path(workspace).resolve())
        logger.info("Starting container {} from image {}", container_name, env_key)

        container = self._client.containers.run(
            image=env_key,
            name=container_name,
            detach=True,
            command="tail -f /dev/null",
            volumes={ws: {"bind": "/testbed", "mode": "rw"}},
            working_dir="/testbed",
            platform=spec.platform,
        )

        self._install_package(container, instance)
        logger.info("Container ready: {} ({})", container_name, container.short_id)
        return container.id

    def _install_package(self, container, instance: dict) -> None:
        """Run pip install -e . inside the container (for external mode)."""
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

        repo = instance["repo"]
        version = instance.get("version", "")
        specs = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
        install_cmd = specs.get("install", "pip install -e .")
        pre_install = specs.get("pre_install", [])

        commands = [
            "source /opt/miniconda3/bin/activate && conda activate testbed",
            "cd /testbed",
        ]
        commands.extend(pre_install)
        commands.append(install_cmd)

        full_cmd = " && ".join(commands)
        logger.debug("Installing package in container: {}", install_cmd)

        exit_code, output = container.exec_run(
            ["bash", "-c", full_cmd], workdir="/testbed",
        )

        if exit_code != 0:
            out_text = output.decode("utf-8", errors="replace")[-500:]
            logger.warning("Package install exited {}: ...{}", exit_code, out_text)
        else:
            logger.debug("Package installed successfully")

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def run_eval_in_container(self, instance: dict, container_id: str) -> bool:
        """Run the official SWE-bench eval script inside a container.

        Returns True if the instance was resolved.
        """
        spec = self.get_test_spec(instance)
        eval_script = spec.eval_script

        container = self._client.containers.get(container_id)
        exit_code, output = container.exec_run(
            ["bash", "-c", eval_script], workdir="/testbed",
        )

        out_text = output.decode("utf-8", errors="replace")
        resolved = exit_code == 0 and "PASSED" in out_text
        logger.info("Eval {}: exit={}, resolved={}", instance["instance_id"], exit_code, resolved)
        return resolved

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def stop_container(self, container_id: str) -> None:
        """Stop and remove a container."""
        try:
            container = self._client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
            logger.debug("Container removed: {}", container_id[:12])
        except docker.errors.NotFound:
            pass
        except Exception as e:
            logger.warning("Error stopping container {}: {}", container_id[:12], e)

    def _cleanup_existing(self, name: str) -> None:
        """Remove any existing container with this name."""
        try:
            existing = self._client.containers.get(name)
            existing.stop(timeout=2)
            existing.remove(force=True)
        except docker.errors.NotFound:
            pass
