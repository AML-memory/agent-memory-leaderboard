"""PersonaMem v1 answer and evaluation pipeline.

Official source:
https://github.com/bowen-upenn/PersonaMem/blob/main/inference_standalone_openai.py

Expected input JSONL fields:
- id: stable record id
- context_messages: already-sliced PersonaMem conversation history
- question: question string
- all_options: original official options string, not a reconstructed list
- correct_answer: official gold option marker, e.g. "(a)" or "a"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api_config import (ANSWER_API_BASE, ANSWER_API_KEY, ANSWER_MODEL, JUDGE_API_BASE, JUDGE_API_KEY, JUDGE_MODEL, JUDGE_VERSION)


OFFICIAL_INSTRUCTION = (
    "Find the most appropriate model response and give your final answer "
    "(a), (b), (c), or (d) after the special token <final_answer>."
)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def row_id(item: dict[str, Any], index: int) -> str:
    for key in ("id", "question_id", "qid"):
        if item.get(key) is not None:
            return str(item[key])
    return str(index)


def official_user_prompt(question: str, all_options: str) -> str:
    if not isinstance(all_options, str):
        raise TypeError("PersonaMem v1 strict mode requires original `all_options` as a string.")
    return f"{question}\n\n{OFFICIAL_INSTRUCTION}\n\n{all_options}"


def convert_role_system_to_user(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    converted = []
    system_buffer = ""
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            system_buffer += f"[System]: {content}\n"
            continue
        if system_buffer:
            content = system_buffer + content
            system_buffer = ""
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"] += "\n" + content
        else:
            converted.append({"role": role, "content": content})
    return converted


def official_messages(item: dict[str, Any], model: str) -> list[dict[str, str]]:
    context = item.get("context_messages", item.get("context"))
    if not isinstance(context, list):
        raise TypeError("PersonaMem v1 input must include `context_messages` as a list of chat messages.")

    messages = [dict(message) for message in context]
    messages.append(
        {
            "role": "user",
            "content": official_user_prompt(str(item["question"]), item["all_options"]),
        }
    )

    if "o" in model:
        messages = convert_role_system_to_user(messages)
    return messages


def extract_option_set(answer: Any) -> set[str]:
    text = str(answer).strip().lower()
    in_parens = re.findall(r"\(([a-d])\)", text)
    if in_parens:
        return set(in_parens)
    return set(re.findall(r"\b([a-d])\b", text))


def extract_gold_option(correct_answer: Any) -> str:
    return str(correct_answer).lower().strip("() ")


def official_extract_answer(predicted_answer: Any, correct_answer: Any) -> tuple[bool, str]:
    full_response = str(predicted_answer)
    predicted = full_response.strip()
    correct = extract_gold_option(correct_answer)

    if "<final_answer>" in predicted:
        predicted = predicted.split("<final_answer>")[-1].strip()
    if predicted.endswith("</final_answer>"):
        predicted = predicted[: -len("</final_answer>")].strip()

    pred_options = extract_option_set(predicted)
    if pred_options == {correct}:
        return True, predicted

    response_options = extract_option_set(full_response)
    if response_options == {correct}:
        return True, predicted

    return False, predicted


async def post_chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float | None,
) -> str:
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
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
                messages=official_messages(item, model),
                temperature=args.temperature,
            )
            out.write(
                json.dumps(
                    {
                        "id": ident,
                        "generated_answer": generated,
                        "prompt_source": "PersonaMem inference_standalone_openai.py",
                        "model": model,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()


def evaluate(args: argparse.Namespace) -> None:
    input_rows = {row_id(item, i): item for i, item in enumerate(read_jsonl(args.input))}
    answer_rows = read_jsonl(args.answers)
    result = []
    for answer_row in answer_rows:
        ident = str(answer_row["id"])
        item = input_rows[ident]
        prediction = answer_row.get("generated_answer", answer_row.get("prediction", ""))
        is_correct, predicted_answer = official_extract_answer(prediction, item["correct_answer"])
        result.append(
            {
                "id": ident,
                "predicted_answer": predicted_answer,
                "predicted_options": sorted(extract_option_set(predicted_answer)),
                "gold_option": extract_gold_option(item["correct_answer"]),
                "is_correct": is_correct,
            }
        )
    write_jsonl(args.output, result)


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
    eval_parser.add_argument("--input", required=True)
    eval_parser.add_argument("--answers", required=True)
    eval_parser.add_argument("--output", required=True)
    eval_parser.set_defaults(func=evaluate)

    args = parser.parse_args()
    if args.cmd == "answer":
        asyncio.run(args.func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
