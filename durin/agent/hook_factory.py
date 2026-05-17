"""Factory that builds lifecycle hooks from configuration.

Reads PostureConfig and DeliberationConfig, instantiates the full hook chain.
Called from AgentLoop.from_config to wire everything automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from durin.agent.hook import AgentHook

if TYPE_CHECKING:
    from durin.config.schema import Config
    from durin.deliberation.service import DeliberationService
    from durin.telemetry.logger import TelemetryLogger

_POSTURE_DRIFT_THRESHOLD = 0.15


def build_hooks_from_config(
    config: Config,
    session_key: str | None = None,
) -> list[AgentHook]:
    """Build posture + plan hooks based on config flags.

    Deliberation V3 is a service injected into PlanHook (not a standalone hook).
    Returns an empty list if all systems are disabled.
    """
    defaults = config.agents.defaults
    hooks: list[AgentHook] = []
    telemetry = _make_telemetry(session_key)

    posture_hook = _maybe_build_posture_hook(defaults.posture, telemetry)
    if posture_hook:
        hooks.append(posture_hook)

    delib_service = _maybe_build_deliberation_service(
        defaults.deliberation, telemetry, config=config,
    )

    posture_snapshot_fn = None
    if posture_hook is not None:
        from durin.posture.hook import PostureHook
        if isinstance(posture_hook, PostureHook):
            posture_snapshot_fn = lambda: posture_hook.current_vector.snapshot()

    plan_hook = _maybe_build_plan_hook(
        config, session_key, delib_service, posture_snapshot_fn,
    )
    if plan_hook:
        hooks.append(plan_hook)

    return hooks


def _make_telemetry(session_key: str | None) -> TelemetryLogger | None:
    if not session_key:
        return None
    try:
        from durin.telemetry.logger import get_session_logger
        return get_session_logger(session_key)
    except Exception:
        logger.debug("Failed to create telemetry logger")
        return None


def _maybe_build_posture_hook(
    posture_config: Any,
    telemetry: TelemetryLogger | None,
) -> AgentHook | None:
    if not posture_config.enabled:
        return None

    from durin.posture.vector import AxisName, AxisState, PostureVector
    from durin.posture.hook import PostureHook

    axes = {}
    for name in AxisName:
        axis_cfg = posture_config.axes.get(name.value)
        if axis_cfg:
            axes[name] = AxisState(
                mean=axis_cfg.mean,
                variance=axis_cfg.variance,
                return_force=axis_cfg.return_force,
                current_value=axis_cfg.current_value if axis_cfg.current_value is not None else axis_cfg.mean,
            )
        else:
            axes[name] = AxisState(
                mean=0.5, variance=0.15, return_force=0.3, current_value=0.5,
            )

    vector = PostureVector(axes=axes)
    hook = PostureHook(vector=vector, telemetry=telemetry)
    logger.info("PostureHook enabled — 5 axes initialized")
    return hook


def _maybe_build_deliberation_service(
    delib_config: Any,
    telemetry: TelemetryLogger | None,
    config: Any = None,
) -> "DeliberationService | None":
    if not delib_config.enabled:
        return None

    from durin.deliberation.engine import DeliberationEngine
    from durin.deliberation.service import DeliberationService

    provider = _make_deliberation_provider(delib_config.provider, config=config)
    if provider is None:
        return None

    model = getattr(delib_config, "model", None) or "glm-5.1"
    engine = DeliberationEngine(
        provider=provider,
        model=model,
        temperature=0.4,
        max_tokens=2048,
    )

    service = DeliberationService(engine=engine, telemetry=telemetry)
    logger.info("DeliberationService V3 enabled — single-call multi-perspective, model={}", model)
    return service


def _make_deliberation_provider(provider_name: str, config: Any = None):
    """Create the LLM provider for deliberation."""
    if provider_name == "local":
        try:
            from durin.providers.local_llama_provider import LocalLlamaProvider
            return LocalLlamaProvider()
        except ImportError:
            logger.warning(
                "local provider requested but llama-cpp-python not installed. "
                "Install with: pip install 'durin[local]'"
            )
            return None

    if provider_name == "ollama":
        try:
            from durin.providers.openai_compat_provider import OpenAICompatProvider
            return OpenAICompatProvider(
                api_key="ollama",
                api_base="http://localhost:11434/v1",
                default_model="qwen2.5:7b",
            )
        except Exception:
            logger.warning(
                "Deliberation disabled — Ollama not reachable at localhost:11434. "
                "Start with: ollama serve"
            )
            return None

    if provider_name == "custom" and config is not None:
        p = getattr(config.providers, "custom", None)
        if p and p.api_key and p.api_base:
            from durin.providers.openai_compat_provider import OpenAICompatProvider
            return OpenAICompatProvider(
                api_key=p.api_key,
                api_base=p.api_base,
                default_model="glm-5.1",
            )
        logger.warning("Deliberation provider 'custom' lacks api_key/api_base")
        return None

    logger.warning("Unknown deliberation provider '{}', skipping", provider_name)
    return None


def _maybe_build_plan_hook(
    config: Config,
    session_key: str | None,
    deliberation: "DeliberationService | None" = None,
    posture_snapshot_fn: Any = None,
) -> AgentHook | None:
    plan_config = getattr(config.agents.defaults, "plan", None)
    if plan_config is None:
        return None
    enabled = getattr(plan_config, "enabled", None)
    if enabled is not True:
        return None

    from pathlib import Path
    from durin.plan.hook import PlanHook

    workspace = Path(getattr(config, "workspace_dir", "."))
    hook = PlanHook(
        workspace=workspace,
        session_key=session_key or "default",
        deliberation=deliberation,
        posture_snapshot_fn=posture_snapshot_fn,
    )
    logger.info("PlanHook enabled — 3-tier execution model")
    return hook
