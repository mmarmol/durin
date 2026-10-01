"""No hand edit of preemptive_compact_max_tokens, whatever JSON it leaves,
crashes the config load or costs the rest of the configuration.

The value is written as raw JSON text (NaN, Infinity and exponents past a
float are text json.dumps never writes), under agents.defaults or on a
preset, in either spelling of the key and of the presets section, in the
single config file and in the split per-topic layout. Every load must
return a configuration whose providers, presets, other defaults and other
sections are the ones written, and whose cap is one the schema allows: a
value that reads as a number keeps that number's meaning (0 or less: no
cap; under the minimum: the minimum), and anything else is dropped (the
agents.defaults default, or a preset's inheritance). Beyond the listed
values, each seed draws more at random; ``DURIN_INVARIANT_SEEDS`` raises how
many seeds run to a tenth of its value."""

from __future__ import annotations

import json
import math
import os
import random

import pytest

from durin.config.loader import load_config
from durin.config.schema import PREEMPTIVE_COMPACT_MIN_TOKENS

_DEFAULT_CAP = 256_000

# Raw JSON text of each value, as a hand edit could leave it.
_LISTED = [
    # numbers of every sign and size
    "0", "1", "-1", "63999", "64000", "64001", "256000", "1000000", "1000000000000",
    "123456789012345678901234567890", "-123456789012345678901234567890", "1" + "0" * 400, "-" + "1" * 300,
    # more digits than Python reads as an int by default (4,300)
    "1" + "0" * 4_300, "-" + "9" * 5_000,
    "0.5", "-0.5", "63999.9", "64000.0", "150000.75", "1e5", "1E5", "-1e5", "1e308", "-1e308",
    "5e-324", "2.5e-7",
    # past what a float holds, and the non-finite literals JSON parsing accepts
    "1e999", "-1e999", "NaN", "Infinity", "-Infinity",
    # strings
    '"300000"', '" 300000 "', '"0"', '"-1"', '"20000"', '"1e5"', '"1_000_000"', '"+5e5"',
    '"1e999"', '"nan"', '"inf"', '"-inf"', '"Infinity"', '"NaN"', '""', '" "', '"abc"', '"300k"',
    '"0x10"', '"\\u0661\\u0662\\u0663\\u0664\\u0665\\u0666"', '"true"', '"null"', '"12,000"',
    # booleans, null, lists, objects
    "true", "false", "null", "[]", "[300000]", '["300000"]', "[null]", "{}", '{"value": 300000}',
    '{"preemptiveCompactMaxTokens": 300000}',
]

_LOCATIONS = ("defaults", "preset")
_KEYS = ("preemptiveCompactMaxTokens", "preemptive_compact_max_tokens")
_PRESET_SECTIONS = ("modelPresets", "model_presets")
_LAYOUTS = ("file", "split")


def _random_raw(rng: random.Random) -> str:
    kind = rng.randrange(7)
    if kind == 0:
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 60)))
        return rng.choice(("", "-")) + (digits.lstrip("0") or "0")
    if kind == 1:
        mantissa = f"{rng.uniform(0, 10):.{rng.randint(0, 6)}f}"
        return rng.choice(("", "-")) + mantissa + f"e{rng.randint(-400, 400)}"
    if kind == 2:
        return rng.choice(("", "-")) + f"{rng.uniform(0, 500_000):.3f}"
    if kind == 3:
        alphabet = "0123456789 .,_+-eExXabcINFnafy"
        return json.dumps("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12))))
    if kind == 4:
        return json.dumps(str(rng.choice((1, -1)) * rng.randint(0, 2_000_000)))
    if kind == 5:
        return rng.choice(("true", "false", "null"))
    inner = _random_raw(rng)
    return rng.choice((f"[{inner}]", f'{{"n": {inner}}}', f"[{inner}, {inner}]"))


_ANY = object()


def _short(raw: str) -> str:
    return raw if len(raw) <= 32 else f"{raw[:8]}...{len(raw)}chars"


def _expected(raw: str, *, on_preset: bool) -> object:
    """The cap a load must give the value written as *raw*: ``_ANY`` when
    Python cannot read it as JSON at all (an integer of more digits than
    it converts), and any cap the schema allows will do."""
    try:
        value = json.loads(raw)
    except ValueError:
        return _ANY
    if value is None:
        # null: no cap under agents.defaults; a preset inherits the default.
        return None
    number = value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            number = value
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or (isinstance(number, float) and not math.isfinite(number))
    ):
        return None if on_preset else _DEFAULT_CAP
    count = int(number)
    if count <= 0:
        return 0 if on_preset else None
    return max(count, PREEMPTIVE_COMPACT_MIN_TOKENS)


def _write(tmp_path, raw: str, *, location: str, key: str, presets_key: str, layout: str):
    cap = f'"{key}": {raw}'
    defaults = '"model": "openai/gpt-4.1", "temperature": 0.3'
    roomy = '"model": "gpt-4.1", "provider": "openai", "temperature": 0.6'
    if location == "defaults":
        defaults += ", " + cap
    else:
        roomy += ", " + cap
    sections = {
        "agents": '{"defaults": {' + defaults + "}}",
        presets_key: '{"roomy": {' + roomy + '}, "other": {"model": "gpt-4o-mini", "provider": "openai"}}',
        "providers": '{"openai": {"apiKey": "sk-test-not-real"}}',
        "tools": '{"restrictToWorkspace": true}',
    }
    path = tmp_path / f"{location}-{key}-{presets_key}-{layout}" / "config.json"
    path.parent.mkdir(parents=True)
    if layout == "file":
        path.write_text("{" + ", ".join(f'"{k}": {v}' for k, v in sections.items()) + "}", encoding="utf-8")
    else:
        split = path.with_suffix(".json.d")
        split.mkdir()
        for name, text in sections.items():
            (split / f"{name}.json").write_text(text, encoding="utf-8")
        path.write_text('{"_layout": "split"}', encoding="utf-8")
    return path


def _check(tmp_path, raw: str) -> list[str]:
    problems = []
    for location in _LOCATIONS:
        for key in _KEYS:
            for presets_key in _PRESET_SECTIONS:
                for layout in _LAYOUTS:
                    where = f"{_short(raw)} on {location} as {key} ({presets_key}, {layout})"
                    path = _write(tmp_path, raw, location=location, key=key, presets_key=presets_key, layout=layout)
                    try:
                        cfg = load_config(path)
                    except Exception as exc:  # noqa: BLE001 - a raise is the finding
                        problems.append(f"{where}: load raised {type(exc).__name__}: {exc}")
                        continue
                    kept = (
                        cfg.providers.openai.api_key == "sk-test-not-real"
                        and cfg.agents.defaults.model == "openai/gpt-4.1"
                        and cfg.agents.defaults.temperature == 0.3
                        and {"roomy", "other"} <= set(cfg.model_presets)
                        and cfg.model_presets["roomy"].temperature == 0.6
                        and cfg.model_presets["other"].model == "gpt-4o-mini"
                        and cfg.tools.restrict_to_workspace is True
                    )
                    if not kept:
                        problems.append(f"{where}: the rest of the config was lost")
                        continue
                    on_preset = location == "preset"
                    cap = (cfg.model_presets["roomy"].preemptive_compact_max_tokens if on_preset
                           else cfg.agents.defaults.preemptive_compact_max_tokens)
                    expected = _expected(raw, on_preset=on_preset)
                    allowed = cap is None or (cap == 0 and on_preset) or (
                        isinstance(cap, int) and cap >= PREEMPTIVE_COMPACT_MIN_TOKENS
                    )
                    if not allowed or (expected is not _ANY and cap != expected):
                        shown = "any the schema allows" if expected is _ANY else repr(expected)
                        problems.append(f"{where}: cap {str(cap)[:40]}, expected {shown}")
    return problems


@pytest.mark.parametrize("raw", _LISTED, ids=_short)
def test_a_listed_hand_edited_cap_loads_with_the_rest_of_the_config(tmp_path, raw):
    problems = _check(tmp_path, raw)
    assert problems == [], "\n".join(problems)


_SEEDS = max(10, int(os.environ.get("DURIN_INVARIANT_SEEDS") or 0) // 10)


@pytest.mark.parametrize("seed", range(_SEEDS))
def test_a_random_hand_edited_cap_loads_with_the_rest_of_the_config(tmp_path, seed):
    rng = random.Random(seed)
    problems = []
    for n in range(8):
        raw = _random_raw(rng)
        problems += _check(tmp_path / str(n), raw)
    assert problems == [], "\n".join(problems[:10])
