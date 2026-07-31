"""ScriptMem answer and evaluation pipeline.

Answer prompt source:
- Dataset-specific answer instructions supplied by the benchmark runner owner.

Evaluation source:
- https://github.com/memorax-ai/ScriptMem/blob/main/src/evaluate.py

ScriptMem's upstream repository provides the official exact option evaluator.
It does not define this memory-search answer prompt, so outputs from this script
must label the answer prompt as user-specified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api_config import (ANSWER_API_BASE, ANSWER_API_KEY, ANSWER_MODEL, JUDGE_API_BASE, JUDGE_API_KEY, JUDGE_MODEL, JUDGE_VERSION)


DATASET_FILES = ("angry.json", "enemy.json", "friends.json", "man_earth.json")
PREDICTION_FIELDS = ("predicted_answer", "prediction", "answer", "response")


CHOICE_ANSWER_TEMPLATE = """
You are asked to answer a multiple-choice question based on your memories of a conversation.

<instructions>
1. Use only the provided memories. Prefer the memories that answer the question most directly.
2. Your memories are episodic raw observations. Reason about what they imply. Do not refuse just because the answer is not stated verbatim.
3. The question may contain typos. Match it to the most relevant memories even if the wording differs.
4. The question may be single-choice, multi-select, or ordering. The required output format is different for each type. Obey the format stated in the question, not a default format you invent.
5. Do not add unsupported options. Do not omit supported options. Do not hedge.
6. Preserve option letters exactly. Do not rewrite the answer as option text unless the question explicitly asks for that.
7. If memories conflict, prefer the most recent supported memory.
8. Choose "Cannot infer" only when no memory contains any relevant evidence after scanning all memories. Partial or indirect evidence requires a supported answer, not a refusal.
9. When memories conflict in direction, prefer the one semantically closest to the question's core—not the one with the highest keyword overlap.
10. For multi-select: check every option independently. Include if any memory supports it; exclude only if clearly contradicted or out of scope.
11. For "most plausible", "underlying", or "most strongly implies" questions: compare the top candidates directly before choosing; do not default to the option with the most shared vocabulary.
12. Keep reasoning internal. The visible output must be just the answer string required by the question.
</instructions>

<memories>
Memories for user {{speaker_1_name}}:

{{speaker_1_memories}}

Memories for user {{speaker_2_name}}:

{{speaker_2_memories}}
</memories>

Question: {{question}}
Return only the answer, exactly in the format requested by the question:
""".strip()


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_id(item: dict[str, Any], index: int) -> str:
    for key in ("id", "qa_id", "question_id", "qid"):
        if item.get(key) is not None:
            return str(item[key])
    return str(index)


def render_answer_prompt(item: dict[str, Any]) -> str:
    return (
        CHOICE_ANSWER_TEMPLATE.replace("{{speaker_1_name}}", str(item.get("speaker_1_name", "speaker 1")))
        .replace("{{speaker_1_memories}}", str(item.get("speaker_1_memories", "")))
        .replace("{{speaker_2_name}}", str(item.get("speaker_2_name", "speaker 2")))
        .replace("{{speaker_2_memories}}", str(item.get("speaker_2_memories", "")))
        .replace("{{question}}", str(item["question"]))
    )


def dataset_name(filename: str) -> str:
    return filename[:-5]


def load_gold_records(data_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for filename in DATASET_FILES:
        source = dataset_name(filename)
        path = data_dir / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        for sample_index, sample in enumerate(data):
            sample_id = sample.get("sample_id") or f"{source}-{sample_index}"
            for qa_index, qa in enumerate(sample.get("qa", [])):
                qa_id = f"{source}:{sample_id}#q{qa_index:04d}"
                records.append(
                    {
                        "qa_id": qa_id,
                        "dataset": source,
                        "sample_id": sample_id,
                        "qa_index": qa_index,
                        "qa_type": qa["qa_type"],
                        "question": qa["question"],
                        "answer": qa["answer"],
                    }
                )
    return records


def load_submission(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    predictions: dict[str, str] = {}

    def prediction_text(item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            for field in PREDICTION_FIELDS:
                if field in item:
                    return str(item.get(field) or "")
        return ""

    def add_result(item: Any, *, dataset: str | None = None, index: int | None = None) -> None:
        if isinstance(item, dict):
            qa_id = item.get("qa_id") or item.get("question_id")
            if not qa_id and dataset is not None and index is not None:
                qa_id = f"{dataset}:conv-0#q{index:04d}"
            if qa_id:
                predictions[str(qa_id)] = prediction_text(item)
        elif dataset is not None and index is not None:
            predictions[f"{dataset}:conv-0#q{index:04d}"] = prediction_text(item)

    if isinstance(payload, list):
        for group in payload:
            if not isinstance(group, dict):
                continue
            dataset = str(group.get("dataset") or group.get("source") or group.get("corpus") or "")
            results = (
                group.get("qa_results")
                or group.get("results")
                or group.get("predictions")
                or group.get("answers")
                or []
            )
            if isinstance(results, list):
                for index, item in enumerate(results):
                    add_result(item, dataset=dataset or None, index=index)
    elif isinstance(payload, dict):
        for dataset, value in payload.items():
            results = value
            if isinstance(value, dict):
                results = (
                    value.get("qa_results")
                    or value.get("results")
                    or value.get("predictions")
                    or value.get("answers")
                    or []
                )
            if isinstance(results, list):
                for index, item in enumerate(results):
                    add_result(item, dataset=str(dataset), index=index)
    else:
        raise ValueError("submission must be a JSON list or object")

    return predictions


def gold_letters(answer: Any) -> list[str]:
    parts = answer if isinstance(answer, list) else [answer]
    letters: list[str] = []
    for part in parts:
        match = re.match(r"\s*([A-F])\.", str(part))
        if match:
            letters.append(match.group(1))
    return letters


def normalize_prediction_text(text: str) -> str:
    cleaned = str(text or "").strip()
    box_matches = list(re.finditer(r"\\box(?:ed)?\{([^}]*)(?:\}|$)", cleaned))
    if box_matches:
        return box_matches[-1].group(1).strip()
    lower = cleaned.lower()
    if "final answer:" in lower:
        index = lower.index("final answer:")
        cleaned = cleaned[index + len("final answer:") :].strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1].strip()
    return cleaned


def predicted_letters(prediction: str, qa_type: str = "") -> tuple[list[str], bool]:
    normalized = normalize_prediction_text(prediction)
    if not normalized:
        return [], False

    if qa_type in {"multi_select", "ordering"}:
        return predicted_ordered_letters(normalized)

    return predicted_option_letters(normalized)


def predicted_ordered_letters(prediction: str) -> tuple[list[str], bool]:
    paren_matches = list(re.finditer(r"[\(\[]([^\)\]]*)[\)\]]", prediction))
    content = paren_matches[-1].group(1) if paren_matches else prediction
    letters = [letter.upper() for letter in re.findall(r"[A-Fa-f]", content)]
    if letters:
        return letters, len(set(letters)) != len(letters)
    return [], False


def predicted_option_letters(prediction: str) -> tuple[list[str], bool]:
    if re.fullmatch(r"[A-Fa-f]{1,5}", prediction):
        return [letter.upper() for letter in prediction], False
    if re.search(r"\(\s*[A-Fa-f]\s*\)\(\s*[A-Fa-f]\s*\)", prediction) or re.search(
        r"\[\s*[A-Fa-f]\s*\]\[\s*[A-Fa-f]\s*\]",
        prediction,
    ):
        return [], True

    options: set[str] = set()
    token_re = re.compile(r"\([^)]*\)|\[[^\]]*\]")
    for match in token_re.finditer(prediction):
        inner = match.group(0)[1:-1].strip()
        if not inner:
            continue

        single_letter_match = re.fullmatch(r"([A-Fa-f])", inner)
        if single_letter_match:
            options.add(single_letter_match.group(1).upper())
            continue

        labeled_text_match = re.match(r"^([A-Fa-f])\s*[.:]\s*.+$", inner)
        if labeled_text_match:
            options.add(labeled_text_match.group(1).upper())
            continue

        letters_only = re.sub(r"[^A-Za-z]", "", inner)
        if (
            letters_only
            and len(letters_only) <= 5
            and inner[0].upper() in {"A", "B", "C", "D", "E", "F"}
            and re.fullmatch(r"[A-Za-z ]+", inner)
        ):
            options.add(inner[0].upper())
    return sorted(options), False


def score_item(qa_type: str, gold: list[str], pred: list[str], malformed: bool) -> float:
    if malformed:
        return 0.0
    if qa_type == "single_choice":
        return 1.0 if len(gold) == 1 and len(pred) == 1 and gold[0] == pred[0] else 0.0
    if qa_type == "multi_select":
        return 1.0 if bool(gold) and set(gold) == set(pred) and len(pred) == len(set(pred)) else 0.0
    if qa_type == "ordering":
        return 1.0 if bool(gold) and gold == pred else 0.0
    raise ValueError(f"unsupported qa_type: {qa_type}")


async def post_chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float | None,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    response = await client.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


async def answer(args: argparse.Namespace) -> None:
    args.api_key = ANSWER_API_KEY
    args.base_url = ANSWER_API_BASE
    args.model = ANSWER_MODEL
    records = read_jsonl(args.input)
    existing = {str(row.get("id")) for row in read_jsonl(args.output)}
    api_key = args.api_key
    base_url = args.base_url or os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = args.model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=args.timeout) as client, output_path.open("a", encoding="utf-8") as out:
        for index, item in enumerate(records):
            ident = row_id(item, index)
            if ident in existing:
                continue
            generated = await post_chat(
                client,
                base_url=base_url,
                api_key=api_key,
                model=model,
                prompt=render_answer_prompt(item),
                temperature=args.temperature,
            )
            out.write(
                json.dumps(
                    {
                        "id": ident,
                        "dataset": item.get("dataset"),
                        "generated_answer": generated,
                        "answer_prompt_source": "published dataset answer instructions",
                        "eval_source": "ScriptMem src/evaluate.py",
                        "model": model,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()


def evaluate_official(data_dir: Path, submission_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    gold_records = load_gold_records(data_dir)
    predictions = load_submission(submission_path)
    details: list[dict[str, Any]] = []
    by_dataset: dict[str, Counter[str]] = defaultdict(Counter)
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    missing_predictions: list[str] = []

    for record in gold_records:
        qa_id = record["qa_id"]
        prediction = predictions.get(qa_id, "")
        if qa_id not in predictions:
            missing_predictions.append(qa_id)
        gold = gold_letters(record["answer"])
        pred, malformed = predicted_letters(prediction, record["qa_type"])
        item_score = score_item(record["qa_type"], gold, pred, malformed)
        dataset_counter = by_dataset[record["dataset"]]
        type_counter = by_type[record["qa_type"]]
        for counter in (dataset_counter, type_counter):
            counter["count"] += 1
            counter["score"] += item_score
        details.append(
            {
                "qa_id": qa_id,
                "dataset": record["dataset"],
                "qa_index": record["qa_index"],
                "qa_type": record["qa_type"],
                "gold": gold,
                "predicted": pred,
                "score": item_score,
                "missing_prediction": qa_id not in predictions,
                "malformed_prediction": malformed,
            }
        )

    total_count = len(gold_records)
    total_score = sum(item["score"] for item in details)
    extra_predictions = sorted(set(predictions) - {record["qa_id"] for record in gold_records})
    summary: dict[str, Any] = {
        "count": total_count,
        "score": total_score,
        "accuracy": total_score / total_count if total_count else 0.0,
        "missing_prediction_count": len(missing_predictions),
        "extra_prediction_count": len(extra_predictions),
        "by_dataset": {
            key: {
                "count": int(value["count"]),
                "score": float(value["score"]),
                "accuracy": float(value["score"]) / int(value["count"]) if value["count"] else 0.0,
            }
            for key, value in sorted(by_dataset.items())
        },
        "by_qa_type": {
            key: {
                "count": int(value["count"]),
                "score": float(value["score"]),
                "accuracy": float(value["score"]) / int(value["count"]) if value["count"] else 0.0,
            }
            for key, value in sorted(by_type.items())
        },
    }
    if missing_predictions:
        summary["missing_prediction_examples"] = missing_predictions[:20]
    if extra_predictions:
        summary["extra_prediction_examples"] = extra_predictions[:20]
    return summary, details


def evaluate(args: argparse.Namespace) -> None:
    summary, details = evaluate_official(Path(args.data_dir), Path(args.submission))
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.details:
        details_path = Path(args.details)
        details_path.parent.mkdir(parents=True, exist_ok=True)
        details_path.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def convert_jsonl_answers(args: argparse.Namespace) -> None:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for index, item in enumerate(read_jsonl(args.answers)):
        qa_id = str(item.get("qa_id") or item.get("id") or row_id(item, index))
        dataset = str(item.get("dataset") or qa_id.split(":", 1)[0])
        groups[dataset].append(
            {
                "qa_id": qa_id,
                "predicted_answer": str(item.get("generated_answer", item.get("predicted_answer", item.get("prediction", "")))),
            }
        )
    submission = [{"dataset": dataset, "qa_results": results} for dataset, results in sorted(groups.items())]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(submission, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    answer_parser = sub.add_parser("answer")
    answer_parser.add_argument("--input", required=True)
    answer_parser.add_argument("--output", required=True)
    answer_parser.add_argument("--model")
    answer_parser.add_argument("--base-url")
    answer_parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    answer_parser.add_argument("--temperature", type=float, default=None)
    answer_parser.add_argument("--timeout", type=float, default=120.0)
    answer_parser.set_defaults(func=answer)

    eval_parser = sub.add_parser("evaluate")
    eval_parser.add_argument("--data-dir", default="data/raw")
    eval_parser.add_argument("--submission", required=True)
    eval_parser.add_argument("--output")
    eval_parser.add_argument("--details")
    eval_parser.set_defaults(func=evaluate)

    convert_parser = sub.add_parser("convert-jsonl-answers")
    convert_parser.add_argument("--answers", required=True)
    convert_parser.add_argument("--output", required=True)
    convert_parser.set_defaults(func=convert_jsonl_answers)

    args = parser.parse_args()
    if args.cmd == "answer":
        asyncio.run(args.func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
