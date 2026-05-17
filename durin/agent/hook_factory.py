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
    from durin.telemetry.logger import TelemetryLogger

_POSTURE_DRIFT_THRESHOLD = 0.15


def build_hooks_from_config(
    config: Config,
    session_key: str | None = None,
) -> list[AgentHook]:
    """Build posture + deliberation + plan hooks based on config flags.

    Returns an empty list if all systems are disabled.
    """
    defaults = config.agents.defaults
    hooks: list[AgentHook] = []
    telemetry = _make_telemetry(session_key)

    posture_hook = _maybe_build_posture_hook(defaults.posture, telemetry)
    if posture_hook:
        hooks.append(posture_hook)

    delib_hook = _maybe_build_deliberation_hook(
        defaults.deliberation, posture_hook, telemetry, config=config,
    )
    if delib_hook:
        hooks.append(delib_hook)

    plan_hook = _maybe_build_plan_hook(config, session_key)
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
                media=axis_cfg.media,
                varianza=axis_cfg.varianza,
                fuerza_retorno=axis_cfg.fuerza_retorno,
                valor_actual=axis_cfg.valor_actual if axis_cfg.valor_actual is not None else axis_cfg.media,
            )
        else:
            axes[name] = AxisState(
                media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.5,
            )

    vector = PostureVector(axes=axes)
    hook = PostureHook(vector=vector, telemetry=telemetry)
    logger.info("PostureHook enabled — 5 axes initialized")
    return hook


def _maybe_build_deliberation_hook(
    delib_config: Any,
    posture_hook: AgentHook | None,
    telemetry: TelemetryLogger | None,
    config: Any = None,
) -> AgentHook | None:
    if not delib_config.enabled:
        return None

    from durin.deliberation.engine import DeliberationEngine
    from durin.deliberation.evaluator import LLMEvaluator
    from durin.deliberation.generator import GeneratorConfig
    from durin.deliberation.hook import DeliberationHook
    from durin.deliberation.types import GeneratorRole

    provider = _make_deliberation_provider(delib_config.provider, config=config)
    if provider is None:
        return None

    generators = _build_generators(delib_config.generators)

    engine = DeliberationEngine(
        provider=provider,
        generators=generators,
        evaluators=[],  # V2: no evaluators, perspectives injected directly
        max_rounds=1,   # V2: single round, no evolution
    )

    posture_snapshot_fn = None
    if posture_hook is not None:
        from durin.posture.hook import PostureHook
        if isinstance(posture_hook, PostureHook):
            posture_snapshot_fn = lambda: posture_hook.current_vector.snapshot()

    hook = DeliberationHook(
        engine=engine,
        posture_snapshot_fn=posture_snapshot_fn,
        telemetry=telemetry,
    )
    logger.info(
        "DeliberationHook enabled — {} generators, V2 (no evaluators), max_rounds=1",
        len(generators),
    )
    return hook


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
                default_model="glm-5-turbo",
            )
        logger.warning("Deliberation provider 'custom' lacks api_key/api_base")
        return None

    logger.warning("Unknown deliberation provider '{}', skipping", provider_name)
    return None


def _build_generators(gen_configs: dict[str, Any]) -> list:
    from durin.deliberation.generator import GeneratorConfig
    from durin.deliberation.types import GeneratorRole

    role_map = {
        "pragmatico": GeneratorRole.PRAGMATICO,
        "explorador": GeneratorRole.EXPLORADOR,
        "critico": GeneratorRole.CRITICO,
    }

    prompt_templates = {
        "pragmatico": (
            "Sos el generador pragmático. Proponé un camino concreto "
            "usando lo conocido y probado. Explicá brevemente por qué "
            "es la opción más directa para este problema."
        ),
        "explorador": (
            "Sos el generador explorador. Proponé un camino alternativo "
            "que los demás no están considerando. Explicá brevemente qué "
            "se gana explorando esta dirección."
        ),
        "critico": (
            "Sos el generador crítico. Proponé el camino más seguro y "
            "reversible. Explicá brevemente qué riesgos evitás con esta "
            "dirección."
        ),
    }

    generators = []
    for name, cfg in gen_configs.items():
        if not cfg.enabled:
            continue
        role = role_map.get(name)
        if role is None:
            continue
        generators.append(GeneratorConfig(
            role=role,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            prompt_template=prompt_templates.get(name, ""),
        ))
    return generators


def _build_evaluators(eval_configs: dict[str, Any], provider) -> list:
    from durin.deliberation.evaluator import LLMEvaluator

    prompt_templates = {
        "avance": (
            "Del 0 al 10, cuanto avanza esta propuesta hacia el objetivo? "
            "Responde SOLO el numero."
        ),
        "reversibilidad": (
            "Del 0 al 10, si esta propuesta falla, cuan facil es volver atras? "
            "Responde SOLO el numero."
        ),
    }

    evaluators = []
    for name, cfg in eval_configs.items():
        template = prompt_templates.get(name, "Score 0-10.")
        evaluators.append(LLMEvaluator(
            _name=name,
            _provider=provider,
            _model=cfg.model,
            _prompt_template=template,
            _max_tokens=cfg.max_tokens,
            _temperature=cfg.temperature,
        ))
    return evaluators


def _maybe_build_plan_hook(
    config: Config,
    session_key: str | None,
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
    )
    logger.info("PlanHook enabled — 3-tier execution model")
    return hook
