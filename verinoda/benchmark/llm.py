"""Optional model-in-the-loop measurement (``--llm anthropic``).

Runs only when all of these hold; otherwise every model field is reported as
"not measured" with the reason - numbers are never estimated or invented:

* ``llm == "anthropic"``;
* ``ANTHROPIC_API_KEY`` is set;
* the official ``anthropic`` SDK is importable (``pip install anthropic``).

Token counts come from the API response ``usage``; cost is computed from
:data:`PRICES` (USD per million tokens).
"""

from __future__ import annotations

import os
import time

DEFAULT_MODEL = "claude-sonnet-5"

# USD per 1M tokens, standard (non-batch, non-cached) first-party API rates.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
PRICE_SOURCE = ("Anthropic model pricing table (https://platform.claude.com/docs/en/about-claude/pricing), "
                "as cached in the Claude API reference on 2026-06-24; recorded for this benchmark on 2026-09-22")

SYSTEM_PROMPT = (
    "You answer questions about a code repository using only the context provided. "
    "Cite file:line locations for every statement. If the context does not contain the answer, "
    "say 'unknown' and name the next thing to check."
)
MAX_TOKENS = 8000


def status(llm: str) -> tuple[bool, str]:
    """(enabled, reason)."""
    if llm != "anthropic":
        return False, f"not measured: llm={llm!r} (use --llm anthropic with ANTHROPIC_API_KEY set)"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "not measured: ANTHROPIC_API_KEY is not set"
    try:
        import anthropic  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        return False, "not measured: the `anthropic` package is not installed"
    return True, "measured"


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    p = PRICES.get(model)
    if p is None:
        return None
    return round(input_tokens / 1e6 * p["input"] + output_tokens / 1e6 * p["output"], 6)


def ask_cmd(context: str, question: str, cmd: str, *, timeout: float = 600) -> dict:
    """The final answer from any command (``--answer-cmd``): the prompt goes to its standard input.

    The prompt is the one the API call sends (system text, context, question), so a local model or a
    command-line client (``claude -p``, ``ollama run ...``) answers what an API model would. Tokens
    are estimated (chars/4) and cost is not known."""
    import shlex
    import subprocess

    prompt = f"{SYSTEM_PROMPT}\n\n<context>\n{context}\n</context>\n\nQuestion: {question}\n"
    t0 = time.perf_counter()
    try:
        argv = cmd if os.name == "nt" else shlex.split(cmd)  # Windows takes the command line as written
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300], "seconds": round(time.perf_counter() - t0, 3)}
    if proc.returncode != 0:
        return {"status": "error", "error": f"exit {proc.returncode}: {(proc.stderr or '')[:300]}",
                "seconds": round(time.perf_counter() - t0, 3)}
    answer = proc.stdout or ""
    return {"status": "measured", "model": f"cmd: {cmd}"[:200], "seconds": round(time.perf_counter() - t0, 3),
            "input_tokens": len(prompt) // 4, "output_tokens": len(answer) // 4, "token_count_method": "chars/4 estimate",
            "cost_usd": None, "answer": answer}


def ask(context: str, question: str, *, model: str = DEFAULT_MODEL) -> dict:
    """One Messages API call; returns usage, cost and the answer text (or the error)."""
    import anthropic  # type: ignore[import-not-found]

    client = anthropic.Anthropic()
    t0 = time.perf_counter()
    try:
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"<context>\n{context}\n</context>\n\nQuestion: {question}"}],
        )
    except anthropic.APIStatusError as exc:
        return {"status": "error", "error": f"HTTP {exc.status_code}: {str(exc)[:300]}",
                "seconds": round(time.perf_counter() - t0, 3)}
    except anthropic.APIConnectionError as exc:
        return {"status": "error", "error": f"connection: {str(exc)[:300]}", "seconds": round(time.perf_counter() - t0, 3)}
    u = resp.usage
    inp, outp = int(u.input_tokens or 0), int(u.output_tokens or 0)
    answer = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return {
        "status": "measured", "model": model, "seconds": round(time.perf_counter() - t0, 3),
        "stop_reason": resp.stop_reason, "input_tokens": inp, "output_tokens": outp,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
        "cost_usd": cost_usd(model, inp, outp), "answer": answer,
    }
