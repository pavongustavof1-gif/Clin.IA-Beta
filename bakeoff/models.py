"""bakeoff/models.yaml: which models run, their prices and parameters."""
from dataclasses import dataclass
from pathlib import Path

import yaml

from bakeoff import paths
from bakeoff.gateway import OUTPUT_MODES


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
    reasoning_effort: str | None = None
    optional: bool = False
    role: str = ''


@dataclass
class ModelsConfig:
    models: dict
    surcharge: float
    chars_per_token: float
    transcript_chars_per_audio_minute: float

    def default_models(self) -> list[ModelSpec]:
        return [m for m in self.models.values() if not m.optional]

    def select(self, names: list[str] | None) -> list[ModelSpec]:
        if not names:
            return self.default_models()
        unknown = [n for n in names if n not in self.models]
        if unknown:
            raise ValueError(f'unknown model(s): {", ".join(unknown)} (see bakeoff/models.yaml)')
        return [self.models[n] for n in names]

    def cost(self, spec: ModelSpec, input_tokens: float, output_tokens: float) -> float:
        base = (input_tokens * spec.price_in + output_tokens * spec.price_out) / 1_000_000
        return base * (1 + self.surcharge)


def load_models(path: Path | None = None) -> ModelsConfig:
    data = yaml.safe_load((path or paths.MODELS_FILE).read_text(encoding='utf-8'))
    models = {}
    for name, m in data['models'].items():
        if m.get('output_mode') not in OUTPUT_MODES:
            raise ValueError(f'models.yaml: {name}: output_mode must be one of {", ".join(OUTPUT_MODES)}')
        timeout = m.get('timeout_s')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f'models.yaml: {name}: timeout_s must be a positive number of seconds')
        models[name] = ModelSpec(
            name=name, model=m['model'], price_in=float(m['price_in']), price_out=float(m['price_out']),
            accepts_temperature=bool(m['accepts_temperature']), est_output_tokens=int(m['est_output_tokens']),
            reasoning_effort=m.get('reasoning_effort'), optional=bool(m.get('optional', False)),
            role=m.get('role', ''), output_mode=m['output_mode'],
            timeout_s=float(timeout),
        )
    est = data.get('estimation', {})
    return ModelsConfig(models=models, surcharge=float(data.get('surcharge', 0)),
                        chars_per_token=float(est.get('chars_per_token', 3.0)),
                        transcript_chars_per_audio_minute=float(est.get('transcript_chars_per_audio_minute', 900)))
