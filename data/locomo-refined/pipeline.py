"""LoCoMo/LoCoMo-Refined answer generation and binary evaluation."""
from __future__ import annotations

# LoCoMo uses exactly the same answer and evaluation contracts as the aligned
# LongMemEval pipeline. Keep local copies so each benchmark directory remains
# runnable and auditable on its own.
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api_config import (ANSWER_API_BASE, ANSWER_API_KEY, ANSWER_MODEL, JUDGE_API_BASE, JUDGE_API_KEY, JUDGE_MODEL, JUDGE_VERSION)


OPEN_ENDED_ANSWER_TEMPLATE = """You are asked to answer a question based on your memories of a conversation.

<instructions>
1. Use only the provided memories. Prefer the memory that answers the question most directly.
2. Your memories are episodic raw observations. Reason about what they imply. Do not refuse just because the answer is not stated verbatim.
3. The question may contain typos. Match it to the most relevant memory even if the wording differs.
4. When multiple answers are possible, list all supported answers, not just the first.
5. For counts or time intervals, enumerate carefully before answering.
6. Preserve specific names, titles, places, and labels from the memories. Use "Rob" not "a colleague", "Sweden" not "home country".
7. Convert relative times like "yesterday", "last month", and "last year" into dates, months, or years when the memory timestamp makes it clear. Keep week-based expressions relative.
8. If memories conflict, prefer the most recent supported memory.
9. For list questions, include all required items and no extras.
10. Keep the final answer minimal. Do not add explanation, background, or extra dates unless needed for correctness.
</instructions>

<memories>
Memories for user {{speaker_1_name}}:

{{speaker_1_memories}}

Memories for user {{speaker_2_name}}:

{{speaker_2_memories}}
</memories>

Question: {{question}}
Answer with the shortest correct phrase or sentence. No preamble, no fluff:"""


ACCURACY_PROMPT = """Your task is to label an answer as ’CORRECT’ or ’WRONG’ given:
(1) a question,
(2) a gold (ground truth) answer,
(3) a generated answer.

Core principle — Inclusion + Non-contradiction
- Be GENEROUS: if the generated answer clearly includes the gold’s key content (or a clear paraphrase of the same content) and does not contradict it, mark CORRECT — even if extra details are added.
- Mark WRONG only when the generated answer does not include the gold’s content, changes it, or contradicts it.

TIME (strict granularity; relative form equivalence; no calendar math)
- Granularity must match exactly: HOUR↔HOUR, DAY↔DAY, MONTH↔MONTH, YEAR↔YEAR.
  Do not answer a gold at a different time unit — even if the numeric value overlaps. Do not answer a month-level gold with a specific day, nor a year with a specific month/day/hour, etc.
  (e.g., gold = "July 26, 2019" [DAY]; generated = "2019-07-26 08:09:17" [includes Second] → WRONG)
- Do NOT convert relative ↔ absolute. If the gold uses a relative time expression, the generated answer must also use a relative form (or a clear paraphrase of that same form), not a computed date/range.
- Treat harmless modifiers in relative forms (e.g., “the/last/previous/just prior”) as equivalent when both the anchor date and the time unit are the same.

- Lists of DISTINCT facts:
- If the gold answer lists multiple distinct facts (joined by "and", commas, or slashes), the generated answer must cover **all** of them.
- Extra non-contradictory items **generally count as WRONG**.
    - Example: gold = A, B, C ; gen = A, B, C → CORRECT
    - Example: gold = A, B, C ; gen = A, B, C, D → WRONG
- Exception: If a gold element is elaborated or split into finer details in the generated answer (e.g., C → C, C′), it is still considered CORRECT.

Preference/Benefit Questions (e.g., "what X likes/values most")
- If gold lists multiple reasons/aspects, the generated answer only needs to include **any one** of them without contradiction to be CORRECT.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG.
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label":

```json
{{
    "label": "CORRECT" or "WRONG"
}}
```"""


def rows(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def memory_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(memory_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def render_answer_prompt(item: dict) -> str:
    fallback_memories = item.get("retrieved_context", item.get("memories", ""))
    values = {
        "speaker_1_name": item.get("speaker_1_name", "speaker 1"),
        "speaker_1_memories": item.get("speaker_1_memories", fallback_memories),
        "speaker_2_name": item.get("speaker_2_name", "speaker 2"),
        "speaker_2_memories": item.get("speaker_2_memories", ""),
        "question": item["question"],
    }
    return re.sub(
        r"\{\{(speaker_1_name|speaker_1_memories|speaker_2_name|speaker_2_memories|question)\}\}",
        lambda match: memory_text(values[match.group(1)]),
        OPEN_ENDED_ANSWER_TEMPLATE,
    )


def gold_answer(item: dict) -> str:
    for key in ("gold_answer", "golden_answer", "reference_answer", "correct_answer"):
        if key in item:
            return memory_text(item[key])
    raise ValueError(f"record {item.get('id', '<unknown>')} has no gold answer")


def render_accuracy_prompt(item: dict, generated_answer: str) -> str:
    values = {
        "question": memory_text(item["question"]),
        "gold_answer": gold_answer(item),
        "generated_answer": generated_answer,
    }
    return re.sub(
        r"\{(question|gold_answer|generated_answer)\}",
        lambda match: values[match.group(1)],
        ACCURACY_PROMPT,
    )


def parse_judge_label(response: str) -> str:
    match = re.search(r"\{.*?\}", response, re.DOTALL)
    if not match:
        raise ValueError("judge response does not contain a JSON object")
    payload = json.loads(match.group(0))
    label = str(payload.get("label", "")).upper()
    if label not in {"CORRECT", "WRONG"}:
        raise ValueError("judge label must be CORRECT or WRONG")
    return label


async def complete(client: httpx.AsyncClient, args: argparse.Namespace, prompt: str, max_tokens: int) -> str:
    response = await client.post(
        args.base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {args.api_key}"},
        json={
            "model": args.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


async def answer(args: argparse.Namespace) -> None:
    args.api_key = ANSWER_API_KEY
    args.base_url = ANSWER_API_BASE
    args.model = ANSWER_MODEL
    items = rows(args.input)
    output = Path(args.output)
    done = {item.get("id") for item in rows(output)} if output.exists() else set()
    async with httpx.AsyncClient(timeout=120) as client, output.open("a", encoding="utf-8") as handle:
        for item in (item for item in items if item["id"] not in done):
            generated = await complete(client, args, render_answer_prompt(item), args.max_tokens)
            handle.write(json.dumps({"id": item["id"], "generated_answer": generated}, ensure_ascii=False) + "\n")
            handle.flush()


async def evaluate(args: argparse.Namespace) -> None:
    args.api_key = JUDGE_API_KEY
    args.base_url = JUDGE_API_BASE
    args.model = JUDGE_MODEL
    items = {item["id"]: item for item in rows(args.input)}
    answers = {item["id"]: item["generated_answer"] for item in rows(args.answers)}
    if set(items) != set(answers):
        raise SystemExit("input/answer ID mismatch")
    output = Path(args.output)
    async with httpx.AsyncClient(timeout=120) as client, output.open("w", encoding="utf-8") as handle:
        for ident, item in items.items():
            response = await complete(client, args, render_accuracy_prompt(item, answers[ident]), args.max_tokens)
            label = parse_judge_label(response)
            result = {"id": ident, "label": label, "is_correct": label == "CORRECT", "judge_response": response}
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    answer_parser = commands.add_parser("answer")
    answer_parser.add_argument("--input", required=True)
    answer_parser.add_argument("--output", required=True)
    answer_parser.add_argument("--model", default=None)
    answer_parser.add_argument("--base-url", default=None)
    answer_parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    answer_parser.add_argument("--max-tokens", type=int, default=256)
    answer_parser.set_defaults(run=answer)
    eval_parser = commands.add_parser("evaluate")
    eval_parser.add_argument("--input", required=True)
    eval_parser.add_argument("--answers", required=True)
    eval_parser.add_argument("--output", required=True)
    eval_parser.add_argument("--model", default=None)
    eval_parser.add_argument("--base-url", default=None)
    eval_parser.add_argument("--api-key-env", default="SILICONFLOW_API_KEY")
    eval_parser.add_argument("--max-tokens", type=int, default=256)
    eval_parser.set_defaults(run=evaluate)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    asyncio.run(arguments.run(arguments))
