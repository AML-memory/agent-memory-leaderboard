"""BEAM answer generation and rubric evaluation aligned to the upstream protocol."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api_config import (ANSWER_API_BASE, ANSWER_API_KEY, ANSWER_MODEL, JUDGE_API_BASE, JUDGE_API_KEY, JUDGE_MODEL, JUDGE_VERSION)


UPSTREAM_REPOSITORY = "mohammadtavakoli78/BEAM"
UPSTREAM_COMMIT = "3e12035532eb85768f1a7cd779832b650c4b2ef9"
DEFAULT_MODEL = "Qwen/Qwen3-14B"

# Verbatim src/prompts.py:answer_generation_for_rag at UPSTREAM_COMMIT.
ANSWER_GENERATION_FOR_RAG = """
You are an assistant that MUST answer questions using ONLY the information provided in the context below. 

STRICT INSTRUCTIONS:
1. Answer ONLY based on the provided context
2. Do NOT use your internal knowledge

CONTEXT:
<context>

QUESTION:
<question>

ANSWER REQUIREMENTS:
- Be direct and concise
- Only output the answer to the question without any explanation 

RESPONSE:
"""

# Verbatim src/prompts.py:unified_llm_judge_base_prompt at UPSTREAM_COMMIT.
UNIFIED_LLM_JUDGE_BASE_PROMPT = """
You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- QUESTION (what the user asked): <question>
- RUBRIC CRITERION (what to check): <rubric_item>
- RESPONSE TO EVALUATE: <llm_response>

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate. 

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT (anchored to the QUESTION)
A compliant response must be **on-topic with respect to the QUESTION** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms (e.g., “$68,000”, “68k”, “68,000 USD”, or “sixty-eight thousand dollars”). Treat them as equal when numerically equivalent.
- If the rubric expects a number or duration, prefer **normalized comparison** (extract and compare values) over string matching.

## STYLE NEUTRALITY (prevents style contamination):
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure (e.g., “itemized list”, “no citations”, “one sentence”).
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
  - Positive: required element present, accurate, properly executed (allowing semantic equivalents).
  - Negative: prohibited element **absent** AND response is **responsive**.
  
- **0.5 (Partial Compliance)**: Partially complies.
  - Positive: element present but minor inaccuracies/incomplete execution.
  - Negative: generally responsive and mostly avoids the prohibited element but with minor/edge violations.
  
- **0.0 (No Compliance)**: Fails to comply.
  - Positive: required element missing or incorrect.
  - Negative: prohibited element present **or** response is non-responsive/evasive even if the element is absent.

## EVALUATION INSTRUCTIONS:
1. **Understand the Requirement**: Determine if the rubric is asking for something to be present (positive) or absent (negative/constraint).

2. **Parse Compound Statements**: If the rubric contains multiple elements connected by "and" or commas, evaluate whether:
   - **All elements** must be present for full compliance (1.0)
   - **Some elements** present indicates partial compliance (0.5)
   - **No elements** present indicates no compliance (0.0)
   
3. **Check Compliance**: 
   - For positive requirements: Look for the presence and quality of the required element
   - For negative constraints: Look for the absence of the prohibited element

4. **Assign Score**: Based on compliance with the specific rubric criterion according to the scoring scale above.

5. **Provide Reasoning**: Explain whether the rubric criterion was satisfied and justify the score.

## OUTPUT FORMAT:
Return your evaluation in JSON format with two fields:

{
   "score": [your score: 1.0, 0.5, or 0.0],
   "reason": "[detailed explanation of whether the rubric criterion was satisfied and why this justified the assigned score]"
}

NOTE: ONLY output the json object, without any explanation before or after that
"""


BATCH_OUTPUT_FORMAT = """## OUTPUT FORMAT:
Return one independent evaluation for every indexed rubric criterion in JSON:

{
  "scores": [
    {"index": 0, "score": 1.0, "reason": "detailed justification"}
  ]
}

Include every index exactly once. Each score must be 1.0, 0.5, or 0.0.
NOTE: ONLY output the json object, without any explanation before or after that
"""


EQUIVALENCE_SYSTEM_PROMPT = """You are a binary classifier.
If the TWO snippets describe the SAME event/fact, reply **YES**
Otherwise reply **NO**. No extra words.
DO NOT provide any exaplanation."""


def rows(path: str | Path) -> list[dict]:
    parsed = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row.get("id") for row in parsed]
    if any(ident is None for ident in ids) or len(ids) != len(set(ids)):
        raise ValueError(f"{path} must contain unique, non-empty ids")
    return parsed


def text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def context_text(item: dict) -> str:
    for key in ("context", "retrieved_context", "memories"):
        if key in item:
            return text(item[key])
    speaker_sections = []
    for number in (1, 2):
        key = f"speaker_{number}_memories"
        if key in item:
            name = text(item.get(f"speaker_{number}_name", f"speaker {number}"))
            speaker_sections.append(f"Memories for user {name}:\n{text(item[key])}")
    if speaker_sections:
        return "\n\n".join(speaker_sections)
    raise ValueError(f"record {item.get('id', '<unknown>')} has no context or memories")


def render_answer_prompt(item: dict) -> str:
    values = {"context": context_text(item), "question": text(item["question"])}
    return re.sub(r"<(context|question)>", lambda match: values[match.group(1)], ANSWER_GENERATION_FOR_RAG)


def rubric_items(item: dict) -> list[str]:
    value = item.get("rubric_nuggets", item.get("rubrics", item.get("rubric")))
    if not isinstance(value, list) or not value:
        raise ValueError(f"record {item.get('id', '<unknown>')} has no rubric list")
    result = []
    for rubric in value:
        if isinstance(rubric, dict):
            rubric = rubric.get("rubric_criteria", rubric.get("criterion", rubric.get("text")))
        if not isinstance(rubric, str) or not rubric.strip():
            raise ValueError(f"record {item.get('id', '<unknown>')} has an invalid rubric")
        result.append(rubric.strip())
    return result


def render_batch_judge_prompt(question: str, response: str, rubrics: list[str]) -> str:
    criteria = "\n".join(f"[{index}] {rubric}" for index, rubric in enumerate(rubrics))
    prompt = (UNIFIED_LLM_JUDGE_BASE_PROMPT
              .replace("<question>", question)
              .replace("<rubric_item>", criteria)
              .replace("<llm_response>", response))
    prompt = prompt[:prompt.index("## OUTPUT FORMAT:")] + BATCH_OUTPUT_FORMAT
    return ("Evaluate every indexed RUBRIC CRITERION independently. Apply the complete protocol "
            "below separately to each criterion; do not let one criterion affect another.\n\n" + prompt)


def parse_json_object(response: str) -> dict:
    candidate = response.strip()
    if candidate.startswith("```"):
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            raise ValueError("model response does not contain a JSON object") from None
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("model response must be a JSON object")
    return payload


def parse_rubric_scores(response: str, count: int) -> list[dict]:
    payload = parse_json_object(response)
    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, list):
        raise ValueError("judge response must contain a scores list")
    scores = {}
    for item in raw_scores:
        if not isinstance(item, dict):
            raise ValueError("every score must be an object")
        try:
            index = int(item["index"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("every score needs a numeric index and score") from error
        if index in scores or not 0 <= index < count:
            raise ValueError("score indices must be unique and in range")
        if score not in {0.0, 0.5, 1.0}:
            raise ValueError("scores must be exactly 0, 0.5, or 1")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("every score needs a non-empty reason")
        scores[index] = {"index": index, "score": score, "reason": reason.strip()}
    if set(scores) != set(range(count)):
        raise ValueError("judge must return every rubric index exactly once")
    return [scores[index] for index in range(count)]


async def call_model(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    messages: list[dict],
    max_tokens: int,
    json_mode: bool = False,
) -> str:
    payload = {
        "model": args.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if (
        args.base_url.rstrip("/") == "https://api.siliconflow.cn/v1"
        and args.model == "Qwen/Qwen3-14B"
    ):
        payload["enable_thinking"] = False
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {args.api_key}", "Content-Type": "application/json"}
    for attempt in range(6):
        try:
            response = await client.post(endpoint, headers=headers, json=payload)
            if response.status_code == 429 and attempt < 5:
                await asyncio.sleep(min(90, 12 * (attempt + 1)))
                continue
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == 5:
                raise
            await asyncio.sleep(min(60, 3 * (2 ** attempt)))
    raise RuntimeError("unreachable")


async def answer(args: argparse.Namespace) -> None:
    args.api_key = ANSWER_API_KEY
    args.base_url = ANSWER_API_BASE
    args.model = ANSWER_MODEL
    items = rows(args.input)
    output = Path(args.output)
    done = {item["id"] for item in rows(output)} if output.exists() else set()
    async with httpx.AsyncClient(timeout=120) as client, output.open("a", encoding="utf-8") as handle:
        for item in (item for item in items if item["id"] not in done):
            generated = await call_model(
                client,
                args,
                [{"role": "user", "content": render_answer_prompt(item)}],
                args.max_tokens,
            )
            handle.write(json.dumps({"id": item["id"], "generated_answer": generated}, ensure_ascii=False) + "\n")
            handle.flush()


async def snippets_equivalent(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    reference: str,
    candidate: str,
) -> bool:
    response = await call_model(
        client,
        args,
        [
            {"role": "system", "content": EQUIVALENCE_SYSTEM_PROMPT},
            {"role": "user", "content": f"First snippet: {reference}\n\nSecond snippet: {candidate}"},
        ],
        8,
    )
    return "yes" in response.casefold()


async def align_with_llm(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    reference: list[str],
    system: list[str],
) -> tuple[list[str], list[str]]:
    used = set()
    system_output = []
    for candidate in system:
        matched_index = None
        for index, expected in enumerate(reference):
            if index in used:
                continue
            if await snippets_equivalent(client, args, expected, candidate):
                matched_index = index
                break
        if matched_index is None:
            system_output.append(candidate)
        else:
            system_output.append(reference[matched_index])
            used.add(matched_index)
    return reference, system_output


def kendall_tau_b(first: list[int], second: list[int]) -> float:
    concordant = discordant = first_ties = second_ties = 0
    for left in range(len(first)):
        for right in range(left + 1, len(first)):
            first_delta = first[left] - first[right]
            second_delta = second[left] - second[right]
            if first_delta == 0 and second_delta == 0:
                continue
            if first_delta == 0:
                first_ties += 1
            elif second_delta == 0:
                second_ties += 1
            elif first_delta * second_delta > 0:
                concordant += 1
            else:
                discordant += 1
    denominator = math.sqrt(
        (concordant + discordant + first_ties) *
        (concordant + discordant + second_ties)
    )
    return (concordant - discordant) / denominator if denominator else 0.0


def event_ordering_metrics(reference: list[str], system: list[str]) -> dict:
    true_positive = len(set(reference) & set(system))
    false_positive = len([item for item in system if item not in reference])
    false_negative = len([item for item in reference if item not in system])
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    union = list(dict.fromkeys(reference + system))
    tie_rank = len(union) + 1

    def ranks(sequence: list[str]) -> list[int]:
        positions = {item: index + 1 for index, item in enumerate(sequence)}
        return [positions.get(item, tie_rank) for item in union]

    tau_norm = (kendall_tau_b(ranks(reference), ranks(system)) + 1) / 2
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tau_norm": tau_norm,
        "final_score": tau_norm * f1,
    }


async def evaluate(args: argparse.Namespace) -> None:
    args.api_key = JUDGE_API_KEY
    args.base_url = JUDGE_API_BASE
    args.model = JUDGE_MODEL
    items = {item["id"]: item for item in rows(args.input)}
    answers = {item["id"]: item["generated_answer"] for item in rows(args.answers)}
    if set(items) != set(answers):
        raise SystemExit("input/answer ID mismatch")
    output = Path(args.output)
    done = {item["id"] for item in rows(output) if "llm_judge_score" in item} if output.exists() else set()
    async with httpx.AsyncClient(timeout=120) as client, output.open("a", encoding="utf-8") as handle:
        for ident, item in ((ident, item) for ident, item in items.items() if ident not in done):
            rubrics = rubric_items(item)
            judge_response = await call_model(
                client,
                args,
                [{"role": "user", "content": render_batch_judge_prompt(text(item["question"]), answers[ident], rubrics)}],
                args.judge_max_tokens,
                json_mode=True,
            )
            scores = parse_rubric_scores(judge_response, len(rubrics))
            result = {
                "id": ident,
                "question_type": item.get("question_type", item.get("category")),
                "judge_model": args.model,
                "rubric_scores": [
                    {"rubric": rubric, "score": score["score"], "reason": score["reason"]}
                    for rubric, score in zip(rubrics, scores)
                ],
                "llm_judge_score": sum(score["score"] for score in scores) / len(scores),
                "judge_response": judge_response,
            }
            if result["question_type"] == "event_ordering":
                reference, system = await align_with_llm(client, args, rubrics, answers[ident].split("\n"))
                result["event_ordering"] = event_ordering_metrics(reference, system)
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    answer_parser = commands.add_parser("answer")
    answer_parser.add_argument("--input", required=True)
    answer_parser.add_argument("--output", required=True)
    answer_parser.add_argument("--model", default=DEFAULT_MODEL)
    answer_parser.add_argument("--base-url", default="https://api.siliconflow.cn/v1")
    answer_parser.add_argument("--api-key-env", default="SILICONFLOW_API_KEY")
    answer_parser.add_argument("--max-tokens", type=int, default=512)
    answer_parser.set_defaults(run=answer)
    eval_parser = commands.add_parser("evaluate")
    eval_parser.add_argument("--input", required=True)
    eval_parser.add_argument("--answers", required=True)
    eval_parser.add_argument("--output", required=True)
    eval_parser.add_argument("--model", default=DEFAULT_MODEL)
    eval_parser.add_argument("--base-url", default="https://api.siliconflow.cn/v1")
    eval_parser.add_argument("--api-key-env", default="SILICONFLOW_API_KEY")
    eval_parser.add_argument("--judge-max-tokens", type=int, default=1024)
    eval_parser.set_defaults(run=evaluate)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    asyncio.run(arguments.run(arguments))
