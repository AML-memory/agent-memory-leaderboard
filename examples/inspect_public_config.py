from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = ROOT / "data" / "beam" / "pipeline.py"


def load_pipeline(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("aml_beam_pipeline", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load public pipeline: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    pipeline = load_pipeline(PIPELINE_PATH)
    event_ordering_fields = list(pipeline.event_ordering_metrics([], []).keys())

    public_contract = {
        "benchmark": "beam",
        "pipeline": str(PIPELINE_PATH.relative_to(ROOT)),
        "upstream": {
            "repository": pipeline.UPSTREAM_REPOSITORY,
            "commit": pipeline.UPSTREAM_COMMIT,
        },
        "model": {
            "default_model": pipeline.DEFAULT_MODEL,
            "runtime_fields": [
                "ANSWER_MODEL",
                "JUDGE_MODEL",
                "JUDGE_VERSION",
            ],
        },
        "scoring": {
            "rubric_score_values": [0.0, 0.5, 1.0],
            "aggregate_field": "llm_judge_score",
            "event_ordering_fields": event_ordering_fields,
        },
    }

    print(json.dumps(public_contract, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
