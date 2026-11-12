"""bakeoff/models.yaml: which models run, their prices and parameters."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bakeoff import paths
from bakeoff.gateway import MAX_TOKENS, OUTPUT_MODES


@dataclass
class ModelSpec:
    name: str                    # variant name used everywhere (may carry "@low")
    model: str                   # Gateway model id actually sent
    price_in: float              # USD / 1M input tokens (listed rate)
    price_out: float
    accepts_temperature: bool
    est_output_tokens: int
    output_mode: str = 'none'     # gateway.OUTPUT_MODES
    timeout_s: float = 120.0      # read timeout per attempt
    max_tokens: int = MAX_TOKENS  # output cap sent to the Gateway (reasoning counts toward it)
    reasoning_effort: str | None = None
    optional: bool = False
    role: str = ''


@dataclass
class ModelsConfig:
    models: dict
    surcharge: float
    chars_per_token: float
    transcript_chars_per_audio_minute: float
    judges: dict = field(default_factory=dict)     # name → ModelSpec (models.yaml `judges`)
    note_chars: float = 5000.0

    def default_models(self) -> list[ModelSpec]:
        return [m for m in self.models.values() if not m.optional]

    def select(self, names: list[str] | None) -> list[ModelSpec]:
        if not names:
            return self.default_models()
        unknown = [n for n in names if n not in self.models]
        if unknown:
            raise ValueError(f'unknown model(s): {", ".join(unknown)} (see bakeoff/models.yaml)')
        return [self.models[n] for n in names]

    def select_judges(self, names: list[str] | None) -> list[ModelSpec]:
        if not names:
            return list(self.judges.values())
        unknown = [n for n in names if n not in self.judges]
        if unknown:
            raise ValueError(f'unknown judge(s): {", ".join(unknown)} (see bakeoff/models.yaml judges)')
        return [self.judges[n] for n in names]

    def all_names(self) -> set:
        """Every model and judge name and Gateway id: words a judge must never see."""
        specs = list(self.models.values()) + list(self.judges.values())
        return {s.name for s in specs} | {s.model for s in specs}

    def cost(self, spec: ModelSpec, input_tokens: float, output_tokens: float) -> float:
        base = (input_tokens * spec.price_in + output_tokens * spec.price_out) / 1_000_000
        return base * (1 + self.surcharge)


def _spec(name: str, m: dict, section: str = 'models') -> ModelSpec:
    where = f'models.yaml: {section}.{name}'
    if m.get('output_mode') not in OUTPUT_MODES:
        raise ValueError(f'{where}: output_mode must be one of {", ".join(OUTPUT_MODES)}')
    timeout = m.get('timeout_s')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f'{where}: timeout_s must be a positive number of seconds')
    max_tokens = m.get('max_tokens', MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError(f'{where}: max_tokens must be a positive whole number of tokens')
    return ModelSpec(
        name=name, model=m['model'], price_in=float(m['price_in']), price_out=float(m['price_out']),
        accepts_temperature=bool(m['accepts_temperature']), est_output_tokens=int(m['est_output_tokens']),
        reasoning_effort=m.get('reasoning_effort'), optional=bool(m.get('optional', False)),
        role=m.get('role', ''), output_mode=m['output_mode'],
        timeout_s=float(timeout), max_tokens=max_tokens,
    )


def load_models(path: Path | None = None) -> ModelsConfig:
    data = yaml.safe_load((path or paths.MODELS_FILE).read_text(encoding='utf-8'))
    models = {name: _spec(name, m) for name, m in data['models'].items()}
    judges = {name: _spec(name, m, 'judges') for name, m in (data.get('judges') or {}).items()}
    est = data.get('estimation', {})
    return ModelsConfig(models=models, surcharge=float(data.get('surcharge', 0)),
                        chars_per_token=float(est.get('chars_per_token', 3.0)),
                        transcript_chars_per_audio_minute=float(est.get('transcript_chars_per_audio_minute', 900)),
                        judges=judges, note_chars=float(est.get('note_chars', 5000)))
