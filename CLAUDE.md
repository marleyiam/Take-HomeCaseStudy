# NovaMart Assist

Take-home case study: an internal assistant for NovaMart store employees on Google ADK + Gemini.
README.md explains the design and decisions; docs/PRODUCTION_NOTE.md must stay at one page (~640 words now); longer material goes in docs/PRODUCTION_DEEP_DIVE.md.

## Commands
- Unit tests (no API calls, run after every change): `.venv/bin/pytest -q`
- One eval case (paid API): `.venv/bin/python -m eval.run_eval --only <case-id-substring>`
- Re-grade saved transcripts without re-running the agent: `.venv/bin/python -m eval.run_eval --rejudge eval/results/run-<stamp>.jsonl`
- Full eval (~100 paid calls per run): `.venv/bin/python -m eval.run_eval`
- Chat: `.venv/bin/adk run novamart_assistant` or `.venv/bin/adk web`
- Ticket desk: `.venv/bin/python -m ticket_desk` (http://localhost:8080); feed it with `cp data/tickets/samples/*.json data/tickets/inbox/` (~4 paid calls per ticket)

## Eval cost policy
- Iterate with `--only` on the failing cases. If only the judge prompt or a case's checks changed, use `--rejudge`.
- Run the full suite only for numbers that go in the README, and say how many runs you're about to do.
- Before changing the agent because of a failure, read the transcript: about half the failures so far were judge or test errors. The `eval-triage` agent does this classification.

## Invariants
- Return decisions are computed in `novamart_assistant/return_rules.py`, never by the LLM. A rule change means changing the code, the matching `data/policies/*.md` text and `tests/test_return_rules.py` together.
- Every policy fact the agent states must come from a tool output and be cited as `[doc-id › Section]`. Declines use the exact `DECLINE_PHRASE` in `prompts.py`, because the eval matches it.
- Mock orders store day offsets (`delivered_days_ago`), not dates. Keep it that way, or the scenarios expire.
- The instruction is a callable (`build_instruction`), which also bypasses ADK's `{state}` templating, so literal braces in the prompt are safe.

## Gotchas
- `gemini-2.5-flash` is retired for new API keys. The agent uses `gemini-3.5-flash` at temperature 0.1 (tested against 1.0, see the README) and the judge `gemini-3.1-pro-preview`; all can be overridden in `.env`.
- Never read or print `.env` (it holds the API key).
