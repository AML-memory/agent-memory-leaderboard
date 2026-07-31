"""PersonaMem v2 answer and evaluation pipeline.

Official sources:
- https://github.com/bowen-upenn/PersonaMem-v2/blob/main/inference.py
- https://github.com/bowen-upenn/PersonaMem-v2/blob/main/inference_utils.py

The official v2 code has two materially different paths:
- MCQ mode: deterministic option construction, a system MCQ instruction, and
  exact mapped-answer scoring.
- Generative mode: append the official recall sentence to the user query and
  evaluate with the narrow judge prompt from inference_utils.py.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api_config import (ANSWER_API_BASE, ANSWER_API_KEY, ANSWER_MODEL, JUDGE_API_BASE, JUDGE_API_KEY, JUDGE_MODEL, JUDGE_VERSION)


RECALL_SUFFIX = (
    " Please recall my related preferences from our conversation history "
    "to give personalized responses."
)

MCQ_PROMPT_TEMPLATE = """Please choose the best answer from the following options:

{options}

Think step by step about which answer best fits the user's query and conversation context.
Provide your reasoning first, then give your final answer as 'Final Answer: [Letter]'"""

JUDGE_PROMPT_NARROW_POSITIVE = """You are evaluating an AI assistant's personalization capabilities in open-ended conversations.

User's query:
{user_query}

Ground truth user preference: {preference}

Model's response to evaluate:
{model_response}

Task: Evaluate how well the model personalizes its response according to the user's ground-truth preference. We expect the model to demonstrate awareness of this preference and provide a response that is specifically aligned with or relevant to it, rather than giving a generic answer that could apply to anyone.

A well-personalized response should:
- Acknowledge or incorporate the specific preference mentioned
- Tailor recommendations, advice, or information to align with this preference
- Show that the model remembers and respects the user's stated preference

A poorly personalized response:
- Ignores the preference entirely and gives generic advice
- Explicitly states it doesn't remember or track preferences
- Provides suggestions that contradict the stated preference

Give a score from 0.0 (completely irrelevant to or contradicts the preference) to 1.0 (explicitly and thoughtfully personalized to the preference).

Provide your reasoning first, then give your final score as \\boxed{{score}} where score is a number between 0.0 and 1.0.
Your response:"""

JUDGE_PROMPT_NARROW_NEGATIVE = """You are evaluating an AI assistant's personalization capabilities in open-ended conversations.

User's query:
{user_query}

Ground truth user preference: {preference}

Model's response to evaluate:
{model_response}

In this case, the user's ground-truth preference is a "do not personalize" or "avoid" instruction about a certain topic or attribute. The user does not want the assistant to tailor its response based on that specific preference, and instead expects a more general or neutral answer that does not rely on or mention that preference.

Task: Evaluate how well the model respects this preference. The model should avoid using or referencing the sensitive preference in a way that personalizes the response, while still providing a helpful and relevant answer.

Give a score from 0.0 (clearly personalizes using the forbidden preference or ignores the instruction) to 1.0 (fully respects the instruction to not personalize while remaining helpful).

Provide your reasoning first, then give your final score as \\boxed{{score}} where score is a number between 0.0 and 1.0.
Your response:"""


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
    for key in ("id", "question_id", "qid", "sample_id"):
        if item.get(key) is not None:
            return str(item[key])
    return str(index)


def user_query_text(item: dict[str, Any]) -> str:
    query = item.get("user_query", item.get("question", item.get("query")))
    if isinstance(query, str) and query.strip().startswith("{"):
        try:
            query = ast.literal_eval(query)
        except (ValueError, SyntaxError):
            pass
    if isinstance(query, dict):
        return str(query.get("content", query.get("text", "")))
    return str(query)


def base_chat_history(item: dict[str, Any]) -> list[dict[str, str]]:
    history = item.get("chat_history", item.get("messages", item.get("context_messages")))
    if not isinstance(history, list):
        raise TypeError("PersonaMem v2 input must include `chat_history` or `messages`.")
    return [dict(message) for message in history]


def official_generative_messages(item: dict[str, Any]) -> list[dict[str, str]]:
    messages = base_chat_history(item)
    messages.append({"role": "user", "content": user_query_text(item) + RECALL_SUFFIX})
    return messages


def official_mcq_options(item: dict[str, Any]) -> tuple[list[str], dict[str, str], str]:
    correct_answer = str(item["correct_answer"])
    incorrect_answers = item.get("incorrect_answers", [])
    if isinstance(incorrect_answers, str):
        try:
            incorrect_answers = json.loads(incorrect_answers) if incorrect_answers else []
        except json.JSONDecodeError:
            incorrect_answers = []
    incorrect_answers = list(incorrect_answers)
    if not incorrect_answers:
        raise TypeError("PersonaMem v2 MCQ mode requires `incorrect_answers`.")

    options = [correct_answer] + [str(answer) for answer in incorrect_answers]
    query_for_seed = user_query_text(item) + RECALL_SUFFIX
    seed = hash(f"{item.get('persona_id', '')}_{query_for_seed}") % (2**32)
    random.seed(seed)
    random.shuffle(options)

    letters = [chr(65 + i) for i in range(len(options))]
    mapping = dict(zip(letters, options))
    correct_letter = next(letter for letter, option in mapping.items() if option == correct_answer)
    return options, mapping, correct_letter


def official_mcq_messages(item: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, str], str]:
    options, mapping, correct_letter = official_mcq_options(item)
    option_text = "\n".join(f"{chr(65 + i)}. {option}" for i, option in enumerate(options))
    messages = official_generative_messages(item)
    messages.append({"role": "system", "content": MCQ_PROMPT_TEMPLATE.format(options=option_text)})
    return messages, mapping, correct_letter


def extract_final_letter(text: Any) -> str:
    raw = str(text)
    if not raw:
        return ""
    patterns = [
        r"\$\\boxed\{([A-Z])\}\$",
        r"\\boxed\{([A-Z])\}",
        r"Final Answer:\s*([A-Z])",
        r"final answer:\s*([A-Z])",
        r"Answer:\s*([A-Z])",
        r"answer:\s*([A-Z])",
        r"final answer is\s*\$?\\boxed\{([A-Z])\}\$?",
        r"final answer is\s*([A-Z])",
        r"the answer is\s*\$?\\boxed\{([A-Z])\}\$?",
        r"the answer is\s*([A-Z])",
        r"\b([A-Z])\.\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).upper()
    return ""


def extract_boxed_score(text: Any) -> float:
    response = str(text)
    if not response:
        return 0.0

    boxed_patterns = [
        r"\\boxed\{([0-9]*\.?[0-9]+)\}",
        r"\$\\boxed\{([0-9]*\.?[0-9]+)\}\$",
        r"\\boxed\s*\{([0-9]*\.?[0-9]+)\}",
    ]
    for pattern in boxed_patterns:
        match = re.search(pattern, response)
        if match:
            try:
                score = float(match.group(1))
                return max(0.0, min(1.0, score))
            except ValueError:
                continue

    score_patterns = [
        r"score[:\s]+([0-9]*\.?[0-9]+)",
        r"rating[:\s]+([0-9]*\.?[0-9]+)",
        r"([0-9]*\.[0-9]+)\s*/\s*1\.?0?",
    ]
    for pattern in score_patterns:
        match = re.search(pattern, response, flags=re.IGNORECASE)
        if match:
            try:
                score = float(match.group(1))
                if 0.0 <= score <= 1.0:
                    return score
            except ValueError:
                continue
    return 0.0


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
            if args.mode == "mcq":
                messages, option_mapping, correct_letter = official_mcq_messages(item)
            else:
                messages = official_generative_messages(item)
                option_mapping = None
                correct_letter = None
            generated = await post_chat(
                client,
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=args.temperature,
            )
            payload: dict[str, Any] = {
                "id": ident,
                "mode": args.mode,
                "generated_answer": generated,
                "prompt_source": "PersonaMem-v2 inference.py",
                "model": model,
            }
            if option_mapping is not None:
                payload["option_mapping"] = option_mapping
                payload["correct_letter"] = correct_letter
            out.write(json.dumps(payload, ensure_ascii=False) + "\n")
            out.flush()


def evaluate_mcq(args: argparse.Namespace) -> None:
    answer_rows = read_jsonl(args.answers)
    result = []
    for row in answer_rows:
        predicted_letter = extract_final_letter(row.get("generated_answer", ""))
        mapping = row["option_mapping"]
        predicted_answer = mapping.get(predicted_letter)
        gold_answer = mapping[row["correct_letter"]]
        result.append(
            {
                "id": row["id"],
                "predicted_letter": predicted_letter,
                "gold_letter": row["correct_letter"],
                "predicted_answer": predicted_answer,
                "gold_answer": gold_answer,
                "is_correct": predicted_answer == gold_answer,
            }
        )
    write_jsonl(args.output, result)


async def evaluate_narrow(args: argparse.Namespace) -> None:
    args.api_key = JUDGE_API_KEY
    args.base_url = JUDGE_API_BASE
    args.model = JUDGE_MODEL
    input_rows = {row_id(item, i): item for i, item in enumerate(read_jsonl(args.input))}
    answer_rows = read_jsonl(args.answers)
    api_key = args.api_key
    base_url = args.base_url or os.environ.get("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1")
    model = args.model or os.environ.get("SILICONFLOW_MODEL", "Qwen/Qwen3-14B")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=args.timeout) as client, output_path.open("w", encoding="utf-8") as out:
        for row in answer_rows:
            item = input_rows[str(row["id"])]
            preference = str(item.get("preference", item.get("target_preference", "")))
            prompt_template = JUDGE_PROMPT_NARROW_NEGATIVE if preference.lower().startswith("do not") else JUDGE_PROMPT_NARROW_POSITIVE
            prompt = prompt_template.format(
                user_query=user_query_text(item),
                preference=preference,
                model_response=row.get("generated_answer", ""),
            )
            judge_text = await post_chat(
                client,
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=args.temperature,
            )
            out.write(
                json.dumps(
                    {
                        "id": row["id"],
                        "judge_model": model,
                        "prompt_source": "PersonaMem-v2 inference_utils.py narrow judge",
                        "score": extract_boxed_score(judge_text),
                        "judge_text": judge_text,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    answer_parser = sub.add_parser("answer")
    answer_parser.add_argument("--input", required=True)
    answer_parser.add_argument("--output", required=True)
    answer_parser.add_argument("--mode", choices=["mcq", "generative"], required=True)
    answer_parser.add_argument("--model")
    answer_parser.add_argument("--base-url")
    answer_parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    answer_parser.add_argument("--temperature", type=float, default=None)
    answer_parser.add_argument("--timeout", type=float, default=120.0)
    answer_parser.set_defaults(func=answer)

    mcq_parser = sub.add_parser("evaluate-mcq")
    mcq_parser.add_argument("--answers", required=True)
    mcq_parser.add_argument("--output", required=True)
    mcq_parser.set_defaults(func=evaluate_mcq)

    narrow_parser = sub.add_parser("evaluate-narrow")
    narrow_parser.add_argument("--input", required=True)
    narrow_parser.add_argument("--answers", required=True)
    narrow_parser.add_argument("--output", required=True)
    narrow_parser.add_argument("--model")
    narrow_parser.add_argument("--base-url")
    narrow_parser.add_argument("--api-key-env", default="SILICONFLOW_API_KEY")
    narrow_parser.add_argument("--temperature", type=float, default=None)
    narrow_parser.add_argument("--timeout", type=float, default=120.0)
    narrow_parser.set_defaults(func=evaluate_narrow)

    args = parser.parse_args()
    if args.cmd in {"answer", "evaluate-narrow"}:
        asyncio.run(args.func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
