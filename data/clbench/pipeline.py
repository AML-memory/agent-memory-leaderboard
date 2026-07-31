"""CL-Bench retrieval-template answer and rubric evaluation pipeline.

This pipeline intentionally follows the project CL-Bench implementation supplied
with this repository task, not Tencent-Hunyuan/CL-bench's standalone infer.py:

* ``prompts.py``: ``_CLBENCH_ANSWER_PROMPT_TEMPLATE``;
* ``memory_search.py``: structured-question and selected-memory assembly;
* ``rubric_clbench.py``: strict binary rubric LLM judge and result schema.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api_config import (ANSWER_API_BASE, ANSWER_API_KEY, ANSWER_MODEL, JUDGE_API_BASE, JUDGE_API_KEY, JUDGE_MODEL, JUDGE_VERSION)


_CLBENCH_ANSWER_PROMPT_TEMPLATE = """\\
{{system_prompt}}

<context>
The following memories from previous conversations may provide additional context:

{{memories}}
</context>

<task>
Answer the question below thoroughly and completely. The question includes a reference document — read it carefully and base your answer on its content.

Requirements:
- Cover every aspect asked in the question.
- Follow all formatting and style rules defined above (e.g. use or avoid bullet points as instructed, use bold headers if required, etc.).
- Do not truncate your response. A complete, detailed answer is expected.
</task>

{{question}}"""


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_id(item: dict[str, Any], index: int) -> str:
    for key in ("idx", "id", "question_id", "task_id"):
        if item.get(key) is not None:
            return str(item[key])
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        for key in ("task_id", "id"):
            if metadata.get(key) is not None:
                return str(metadata[key])
    return str(index)


def render_answer_prompt_clbench(*, system_prompt: str, memories: str, question: str) -> str:
    """Exact rendering behaviour from the supplied ``memory_search.py``."""
    return (
        _CLBENCH_ANSWER_PROMPT_TEMPLATE.replace("{{system_prompt}}", system_prompt or "")
        .replace("{{memories}}", memories or "(no memories)")
        .replace("{{question}}", question)
    ).strip()


def format_structured_question(*, question: str, qa_type: str, options: list[str]) -> str:
    """Exact structured-QA formatting from the supplied ``memory_search.py``."""
    question_text = str(question or "").strip()
    normalized_type = str(qa_type or "").strip().lower()
    if normalized_type not in {"single_choice", "multi_select", "ordering"} or not options:
        return question_text
    option_lines: list[str] = []
    for index, raw_option in enumerate(options):
        letter = chr(ord("A") + index)
        option_text = re.sub(r"^\s*(?:\([A-Za-z]\)|[A-Za-z][\.:])\s*", "", str(raw_option or "")).strip()
        if option_text:
            option_lines.append(f"{letter}. {option_text}")
    if not option_lines:
        return question_text
    if normalized_type == "single_choice":
        output_contract = "Required answer format: exactly one uppercase option letter, such as A."
    elif normalized_type == "multi_select":
        output_contract = "Required answer format: all supported uppercase option letters, comma-separated."
    else:
        output_contract = "Required answer format: all supported uppercase option letters in order, comma-separated."
    return "\n\n".join((question_text, "Options:\n" + "\n".join(option_lines), output_contract))


def _selected_memory_items(payload: Any) -> list[dict[str, Any]]:
    return [item for item in (payload or {}).get("selected", []) if isinstance(item, dict)] if isinstance(payload, dict) else []


def format_selected_memories(selected: list[dict[str, Any]]) -> str:
    """The timestamped prompt block used by the supplied retrieval implementation."""
    lines: list[str] = []
    for item in selected:
        timestamp = str(item.get("created_at") or "").strip()
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- [{timestamp}] {text}" if timestamp else f"- {text}")
    return "\n".join(lines)


def collected_memories(item: dict[str, Any]) -> str:
    """Reproduce CL-Bench's MSP/dual-path retrieval memory concatenation."""
    if isinstance(item.get("retrieval"), dict):
        return format_selected_memories(_selected_memory_items(item["retrieval"]))
    if isinstance(item.get("msp_retrieval"), dict):
        return format_selected_memories(_selected_memory_items(item["msp_retrieval"]))
    blocks = [
        format_selected_memories(_selected_memory_items(item.get("speaker_a_retrieval"))),
        format_selected_memories(_selected_memory_items(item.get("speaker_b_retrieval"))),
    ]
    return "\n\n".join(block for block in blocks if block.strip())


def build_answer_prompt(item: dict[str, Any]) -> str:
    return render_answer_prompt_clbench(
        system_prompt=str(item.get("system_prompt") or ""),
        memories=collected_memories(item),
        question=format_structured_question(
            question=str(item.get("question") or ""),
            qa_type=str(item.get("qa_type") or ""),
            options=[str(option) for option in item.get("options") or []],
        ),
    )


def _normalize_model_output(text: str) -> str:
    cleaned = str(text or "").strip()
    if "Final Answer:" in cleaned:
        cleaned = cleaned.split("Final Answer:", 1)[1].strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1].strip()
    return cleaned


def _normalize_rubrics(rubrics: Sequence[str] | Sequence[dict[str, Any]] | None) -> list[str]:
    normalized: list[str] = []
    for item in rubrics or []:
        text = str(item.get("rubric_criteria", "") if isinstance(item, dict) else item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _build_rubrics_text(rubrics: Sequence[str]) -> str:
    if not rubrics:
        return "No specific rubrics provided."
    return "\n".join(f"{index}. {rubric}" for index, rubric in enumerate(rubrics, start=1))


def _coerce_score(payload: dict[str, Any]) -> int:
    try:
        return 1 if int(payload.get("Overall Score", "")) == 1 else 0
    except (TypeError, ValueError):
        return 0


def _coerce_status_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _compute_requirement_ratio(status_list: Sequence[str]) -> float:
    if not status_list:
        return 0.0
    return sum(str(item).strip().lower() in {"yes", "y", "true", "1"} for item in status_list) / len(status_list)


def _failed_payload(reason: str) -> dict[str, Any]:
    return {
        "rubric_clbench_score": 0.0,
        "rubric_clbench_rationale": reason,
        "rubric_clbench_requirement_status": [],
        "rubric_clbench_requirement_ratio": 0.0,
    }


def official_rubrics(item: dict[str, Any]) -> list[str] | list[dict[str, Any]] | None:
    rubrics = item.get("rubrics")
    if rubrics is None and isinstance(item.get("metadata"), dict):
        rubrics = item["metadata"].get("rubrics")
    return rubrics if isinstance(rubrics, list) else None


async def post_chat(client: httpx.AsyncClient, *, base_url: str, api_key: str, model: str, messages: list[dict[str, str]], temperature: float | None) -> str:
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
    response = await client.post(base_url.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    response.raise_for_status()
    return str(response.json()["choices"][0]["message"].get("content") or "").strip()


def rubric_judge_prompt(*, rubrics_text: str, model_output: str) -> str:
    """The supplied ``rubric_clbench.py`` judge prompt, including its protocol."""
    return f'''Starting now, you are a rigorous instruction-following grading teacher. Your task is to accurately grade and score student answers based on the 【Rubrics】.

Grading Criteria
This is a strict, all-or-nothing grading system. The final score is binary.
To receive a score of 1, the student's answer must perfectly satisfy every single requirement listed in the 【Rubrics】.
If even one requirement is not fully met, the final score will be 0.
Grading Process
Please strictly follow the steps below for analysis—no steps may be skipped:
Step 1: Analyze the Standard Answer
List all explicit requirements in the 【Rubrics】 item by item (including format, content, quantity, order, etc.).
Identify implicit requirements in the 【Rubrics】 (e.g., language style, logical structure).
Define specific evaluation criteria for each requirement (e.g., "must include X," "must not exceed Y").
Step 2: Check Each Requirement Against the Student's Answer
For every requirement in the 【Rubrics】, verify one by one whether the student's answer fully satisfies it.
Step 3: Self-Reflection
Before giving the final score, you must conduct the following checks:
  Completeness Check: Whether all requirements in the standard answer have been reviewed with no omissions.
  Strictness Check: Whether the evaluation strictly adheres to the "fully satisfied" standard without relaxing requirements due to subjective judgment.
  Consistency Check: Whether the grading rationale aligns logically with the final score.
  Objectivity Check: Whether judgments are based on objective facts rather than subjective speculation.
Output Format Requirements
【Grading Rationale】: xxx
【List of Requirement Satisfaction Status】: [x₁, x₂, …, xᵢ, …, xₙ] (where n is the total number of requirements in the 【Rubrics】, and xᵢ indicates whether the student's answer meets the i-th requirement, with values "yes"/"no")
【Overall Score】: x points (x is an integer, either 0 or 1.)

Content to Be Graded
【Rubrics】:
{rubrics_text}
【Student Response】:
{model_output}

Please strictly output ONLY the following JSON format (do not output any other content):
{{
  "Grading Rationale": "Your detailed grading rationale",
  "List of Requirement Satisfaction Status": ["yes", "no", ...],
  "Overall Score": 0 or 1
}}
'''


async def call_judge_api(client: httpx.AsyncClient, *, base_url: str, api_key: str, model: str, rubrics_text: str, model_output: str, max_retries: int) -> str | None:
    for attempt in range(max_retries):
        try:
            text = await post_chat(client, base_url=base_url, api_key=api_key, model=model, messages=[{"role": "user", "content": rubric_judge_prompt(rubrics_text=rubrics_text, model_output=model_output)}], temperature=0.0)
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            return text.strip()
        except Exception:
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
    return None


async def evaluate_rubric_clbench(client: httpx.AsyncClient, *, base_url: str, api_key: str, model: str, rubrics: Sequence[str] | Sequence[dict[str, Any]] | None, predicted_answer: str, max_retries: int = 3) -> dict[str, Any]:
    normalized_answer = _normalize_model_output(predicted_answer)
    normalized_rubrics = _normalize_rubrics(rubrics)
    if not normalized_answer:
        return _failed_payload("No model output (counted as score 0)")
    if not normalized_rubrics:
        return _failed_payload("No rubrics provided")
    rubrics_text = _build_rubrics_text(normalized_rubrics)
    for parse_attempt in range(max_retries):
        grading_result = await call_judge_api(client, base_url=base_url, api_key=api_key, model=model, rubrics_text=rubrics_text, model_output=normalized_answer, max_retries=max_retries)
        if not grading_result:
            if parse_attempt < max_retries - 1:
                await asyncio.sleep(2)
                continue
            return _failed_payload("API call failed (counted as score 0)")
        try:
            payload = json.loads(grading_result)
            if "Overall Score" not in payload:
                raise ValueError("Missing 'Overall Score' field")
            statuses = _coerce_status_list(payload.get("List of Requirement Satisfaction Status", []))
            return {
                "rubric_clbench_score": float(_coerce_score(payload)),
                "rubric_clbench_rationale": str(payload.get("Grading Rationale", "") or ""),
                "rubric_clbench_requirement_status": statuses,
                "rubric_clbench_requirement_ratio": _compute_requirement_ratio(statuses),
            }
        except (json.JSONDecodeError, ValueError):
            if parse_attempt < max_retries - 1:
                await asyncio.sleep(2)
                continue
            return _failed_payload(f"JSON parse failed ({max_retries} attempts): {grading_result[:500]}")
    return _failed_payload("Unknown error (counted as score 0)")


async def answer(args: argparse.Namespace) -> None:
    args.api_key = ANSWER_API_KEY
    args.base_url = ANSWER_API_BASE
    args.model = ANSWER_MODEL
    records = read_jsonl(args.input)
    existing = {str(row.get("idx", row.get("id"))) for row in read_jsonl(args.output)}
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
            prompt = build_answer_prompt(item)
            model_output = await post_chat(client, base_url=base_url, api_key=api_key, model=model, messages=[{"role": "user", "content": prompt}], temperature=args.temperature)
            out.write(json.dumps({"idx": ident, "model_output": model_output, "question": item.get("question", ""), "rubrics": official_rubrics(item), "prompt": prompt, "prompt_source": "prompts.py _CLBENCH_ANSWER_PROMPT_TEMPLATE + memory_search.py CL-Bench retrieval path", "model": model}, ensure_ascii=False) + "\n")
            out.flush()


async def evaluate(args: argparse.Namespace) -> None:
    args.api_key = JUDGE_API_KEY
    args.base_url = JUDGE_API_BASE
    args.model = JUDGE_MODEL
    records = read_jsonl(args.input)
    answers = {row_id(row, index): row for index, row in enumerate(read_jsonl(args.answers))}
    api_key = args.api_key
    base_url = args.base_url or os.environ.get("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1")
    model = args.model or os.environ.get("SILICONFLOW_MODEL", "Qwen/Qwen3-14B")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=args.timeout) as client, output_path.open("w", encoding="utf-8") as out:
        for index, item in enumerate(records):
            ident = row_id(item, index)
            answer_row = answers.get(ident, {})
            result = await evaluate_rubric_clbench(client, base_url=base_url, api_key=api_key, model=model, rubrics=official_rubrics(item), predicted_answer=str(answer_row.get("model_output", answer_row.get("generated_answer", ""))), max_retries=args.max_retries)
            out.write(json.dumps({"idx": ident, "question": item.get("question", ""), "judge_model": model, "prompt_source": "rubric_clbench.py evaluate_rubric_clbench", **result}, ensure_ascii=False) + "\n")
            out.flush()


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
    answer_parser.add_argument("--timeout", type=float, default=180.0)
    answer_parser.set_defaults(func=answer)
    eval_parser = sub.add_parser("evaluate")
    eval_parser.add_argument("--input", required=True)
    eval_parser.add_argument("--answers", required=True)
    eval_parser.add_argument("--output", required=True)
    eval_parser.add_argument("--model")
    eval_parser.add_argument("--base-url")
    eval_parser.add_argument("--api-key-env", default="SILICONFLOW_API_KEY")
    eval_parser.add_argument("--max-retries", type=int, default=3)
    eval_parser.add_argument("--timeout", type=float, default=180.0)
    eval_parser.set_defaults(func=evaluate)
    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
