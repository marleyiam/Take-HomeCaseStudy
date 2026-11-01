"""Runs the evaluation set against the agent and writes a report.

Usage:
    python -m eval.run_eval                 # all cases
    python -m eval.run_eval --only decide   # cases whose id contains "decide"
    python -m eval.run_eval --rejudge eval/results/run-<stamp>.jsonl   # re-grade saved transcripts only

A case passes when every deterministic check passes AND the LLM judge rates the final answer
both grounded (every claim backed by tool outputs) and meeting the case's expectation.
"""

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import yaml
from google import genai
from google.genai import types
from pydantic import BaseModel

# Keep eval tickets out of the real escalation log. Must be set before importing the agent config.
os.environ.setdefault("NOVAMART_ESCALATIONS_PATH", str(Path(tempfile.gettempdir()) / "novamart_eval_escalations.jsonl"))

from google.adk.runners import InMemoryRunner  # noqa: E402

from novamart_assistant import config  # noqa: E402
from novamart_assistant.agent import root_agent  # noqa: E402
from novamart_assistant.prompts import DECLINE_PHRASE  # noqa: E402

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"


# ---------------------------------------------------------------- running the agent

async def run_conversation(runner: InMemoryRunner, turns: list[str]) -> dict:
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="eval")
    tool_calls, tool_outputs, replies = [], [], []
    for turn in turns:
        message = types.Content(role="user", parts=[types.Part(text=turn)])
        reply = ""
        async for event in runner.run_async(user_id="eval", session_id=session.id, new_message=message):
            for call in event.get_function_calls():
                tool_calls.append({"name": call.name, "args": dict(call.args or {})})
            for resp in event.get_function_responses():
                tool_outputs.append({"name": resp.name, "response": resp.response})
            if event.is_final_response() and event.content and event.content.parts:
                reply += "".join(p.text or "" for p in event.content.parts if not getattr(p, "thought", False))
        replies.append(reply.strip())
    return {"replies": replies, "tool_calls": tool_calls, "tool_outputs": tool_outputs}


# ---------------------------------------------------------------- deterministic checks

def check_case(case: dict, run: dict) -> list[str]:
    """Returns the list of failed checks (empty list = all passed)."""
    failures = []
    reply = run["replies"][-1]
    called = {c["name"] for c in run["tool_calls"]}

    for tool in case.get("tools_called", []):
        if tool not in called:
            failures.append(f"tool not called: {tool}")
    for tool in case.get("tools_not_called", []):
        if tool in called:
            failures.append(f"tool should not be called: {tool}")
    if "decision" in case:
        decisions = [o["response"].get("decision") for o in run["tool_outputs"]
                     if o["name"] == "check_return_eligibility" and o["response"].get("decision")]
        if case["decision"] not in decisions:
            failures.append(f"decision {decisions or 'none'} != {case['decision']}")
    for doc in case.get("cites", []):
        if doc not in reply:
            failures.append(f"missing citation: {doc}")
    for pattern in case.get("contains", []):
        if not re.search(pattern, reply, re.IGNORECASE):
            failures.append(f"missing /{pattern}/")
    for pattern in case.get("not_contains", []):
        if re.search(pattern, reply, re.IGNORECASE):
            failures.append(f"forbidden /{pattern}/")
    if "declines" in case:
        declined = DECLINE_PHRASE.lower() in reply.lower()
        if declined != case["declines"]:
            failures.append("should decline" if case["declines"] else "declined unexpectedly")
    if case.get("asks") and "?" not in reply:
        failures.append("did not ask a question")
    return failures


# ---------------------------------------------------------------- LLM judge

class Verdict(BaseModel):
    grounded: bool
    meets_expectation: bool
    explanation: str


JUDGE_PROMPT = """You are grading an internal retail assistant used by store employees.

Conversation (employee turns and assistant replies):
{conversation}

Tool outputs the assistant received (the ONLY allowed source of facts, besides today's date):
{evidence}

The assistant is told today's date in its instructions: {today} ({weekday}).
Its tools are: search_policies, get_order_status, check_return_eligibility, create_escalation_ticket
(opens a ticket for the store manager or the operations team). Offering to use them is not a factual claim.

Expected behaviour for the FINAL reply:
{expect}

Grade the FINAL assistant reply:
- grounded: true if every factual claim about policies, orders, amounts, dates or decisions is supported by the
  tool outputs above. Asking questions, declining, or offering help are not factual claims. If the reply states
  any fact that is not in the tool outputs, grounded is false.
- meets_expectation: true if the reply does what the expected behaviour describes (wording can differ).
- explanation: one or two sentences.
"""


def judge(client: genai.Client, case: dict, run: dict) -> Verdict:
    conversation = "\n".join(f"EMPLOYEE: {t}\nASSISTANT: {r}" for t, r in zip(case["turns"], run["replies"]))
    evidence = json.dumps(run["tool_outputs"], indent=1, default=str)[:20000] or "(no tools called)"
    response = client.models.generate_content(
        model=config.JUDGE_MODEL,
        contents=JUDGE_PROMPT.format(conversation=conversation, evidence=evidence, expect=case["expect"],
                                     today=date.today().isoformat(), weekday=date.today().strftime("%A")),
        config=types.GenerateContentConfig(
            temperature=0, response_mime_type="application/json", response_schema=Verdict
        ),
    )
    return response.parsed


# ---------------------------------------------------------------- orchestration

class EvalError(Exception):
    pass


async def with_retries(make_call):
    """Retries transient API errors (rate limits, 5xx). `make_call` returns a fresh awaitable each attempt."""
    for attempt in range(3):
        try:
            return await make_call()
        except Exception as exc:  # noqa: BLE001
            daily_quota = "PerDay" in str(exc)  # free-tier daily cap: retrying won't help
            if attempt == 2 or daily_quota:
                raise EvalError("daily API quota exhausted" if daily_quota else f"error: {str(exc)[:200]}") from exc
            await asyncio.sleep(10 * (attempt + 1))


def error_record(case: dict, error: EvalError, run: dict | None = None, latency: float | None = None) -> dict:
    return {"id": case["id"], "category": case["category"], "passed": False, "failures": [str(error)],
            "latency_s": latency, "verdict": None, "run": run}


async def grade(case: dict, run: dict, latency: float | None, client: genai.Client) -> dict:
    failures = check_case(case, run)
    verdict = None
    # A case that failed a deterministic check can't be rescued by the judge, so don't pay for the call.
    if not failures:
        verdict = await with_retries(lambda: asyncio.to_thread(judge, client, case, run))
        if not verdict.grounded:
            failures.append("judge: not grounded")
        if not verdict.meets_expectation:
            failures.append("judge: expectation not met")
    return {"id": case["id"], "category": case["category"], "passed": not failures, "failures": failures,
            "latency_s": latency, "verdict": verdict.model_dump() if verdict else None, "run": run}


async def evaluate_case(case: dict, runner: InMemoryRunner, client: genai.Client, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            started = time.perf_counter()
            run = await with_retries(lambda: run_conversation(runner, case["turns"]))
            latency = round(time.perf_counter() - started, 1)
        except EvalError as exc:
            return error_record(case, exc)
        try:
            return await grade(case, run, latency, client)
        except EvalError as exc:
            return error_record(case, exc, run, latency)


async def rejudge_case(record: dict, case: dict, client: genai.Client, sem: asyncio.Semaphore) -> dict:
    """Re-grades a saved transcript with the current checks and judge, without re-running the agent."""
    async with sem:
        try:
            return await grade(case, record["run"], record["latency_s"], client)
        except EvalError as exc:
            return error_record(case, exc, record["run"], record["latency_s"])


def write_report(results: list[dict], source: str) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with (RESULTS_DIR / f"run-{stamp}.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")

    by_cat = Counter(r["category"] for r in results)
    passed_cat = Counter(r["category"] for r in results if r["passed"])
    judged = [r for r in results if r["verdict"]]
    grounded = sum(1 for r in judged if r["verdict"]["grounded"])
    latencies = sorted(r["latency_s"] for r in results if r["latency_s"] is not None)
    lines = [
        f"# Evaluation report — {stamp}",
        "",
        f"Model: `{config.MODEL}` · Judge: `{config.JUDGE_MODEL}` · Cases: {len(results)} · {source}",
        "",
        f"**Passed: {sum(r['passed'] for r in results)}/{len(results)}** · "
        f"Grounded (judge): {grounded}/{len(judged)} judged "
        f"(cases failing a deterministic check are not sent to the judge) · "
        f"Median latency: {latencies[len(latencies) // 2] if latencies else '-'} s",
        "",
        "| Category | Passed |",
        "|---|---|",
        *[f"| {c} | {passed_cat[c]}/{n} |" for c, n in by_cat.items()],
        "",
        "| Case | Result | Failures | Judge notes |",
        "|---|---|---|---|",
    ]
    for r in results:
        note = (r["verdict"] or {}).get("explanation", "").replace("|", "/").replace("\n", " ")
        lines.append(f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {'; '.join(r['failures']).replace('|', '/')} | {note} |")
    report = RESULTS_DIR / "latest.md"
    report.write_text("\n".join(lines) + "\n")
    return report


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="run only cases whose id contains this text")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--rejudge", type=Path, metavar="RUN_JSONL",
                        help="re-grade the transcripts saved in a previous run (no agent calls), "
                             "e.g. after changing the judge prompt or a case's checks")
    args = parser.parse_args()

    cases = yaml.safe_load((EVAL_DIR / "cases.yaml").read_text())
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]

    client = genai.Client()
    sem = asyncio.Semaphore(args.concurrency)
    if args.rejudge:
        # Turns are replayed as saved, so a case whose `turns` changed since that run must be re-run instead.
        saved = {r["id"]: r for r in map(json.loads, args.rejudge.read_text().splitlines()) if r["run"]}
        skipped = [c["id"] for c in cases if c["id"] not in saved]
        if skipped:
            print(f"No saved transcript for {len(skipped)} case(s), skipping: {', '.join(skipped)}")
        cases = [c for c in cases if c["id"] in saved]
        results = await asyncio.gather(*(rejudge_case(saved[c["id"]], c, client, sem) for c in cases))
        source = f"re-graded from {args.rejudge.name}"
    else:
        runner = InMemoryRunner(agent=root_agent, app_name="novamart_eval")
        results = await asyncio.gather(*(evaluate_case(c, runner, client, sem) for c in cases))
        source = "fresh agent run"

    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<40} {'; '.join(r['failures'])}")
    judge_calls = sum(1 for r in results if r["verdict"])
    print(f"\nJudge calls: {judge_calls}/{len(results)}")
    report = write_report(results, source)
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed — report: {report.relative_to(EVAL_DIR.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
