#!/usr/bin/env python
"""Ω Shadow RCA — LLM-powered root cause analysis agent (Iron Law #12 Phase 3).

Triggered asynchronously on Sev1/Sev2 alerts or process crashes.
Collects git context + traceback/alert data, feeds to LLM for classification,
validates against hallucination, and dispatches results to alert channels
and the data loss register.

Usage:
    python scripts/shadow_rca.py --alert-json '<json>' [--data-dir data_btc]
    python scripts/shadow_rca.py --traceback '<tb>' --file core/exec.py --line 847

Input via stdin (preferred for large payloads):
    echo '{"alert": {...}, "traceback": "..."}' | python scripts/shadow_rca.py

LLM backends (auto-detected):
    - ANTHROPIC_API_KEY set → Anthropic Messages API
    - OPENAI_API_KEY set     → OpenAI Chat Completions API
    - Neither                → local template mode (human fills in)

Output: JSON to stdout + shadow log written to data dir.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

logger = logging.getLogger("shadow_rca")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ── Constants ──────────────────────────────────────────────────────────────
TRACEBACK_HEAD_CHARS = 1000  # Keep first N chars (entry point)
TRACEBACK_TAIL_CHARS = 2000  # Keep last N chars (final exception)
GIT_LOG_DEPTH = 5  # Recent FIX commits to include in context
LLM_TIMEOUT = 30  # Seconds before LLM call is abandoned

# Known RC categories from FIX_REGISTRY.md (for validation)
VALID_RC_CATEGORIES = {f"RC-{i:02d}" for i in range(1, 13)}  # RC-01 through RC-12

# ── Context collection ─────────────────────────────────────────────────────


def _truncate_traceback(tb: str) -> str:
    """Keep traceback head + tail, drop the middle to protect LLM context window."""
    if len(tb) <= TRACEBACK_HEAD_CHARS + TRACEBACK_TAIL_CHARS:
        return tb
    head = tb[:TRACEBACK_HEAD_CHARS]
    tail = tb[-TRACEBACK_TAIL_CHARS:]
    omitted = len(tb) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


def _git_blame(file_path: str, line: int) -> str:
    """Get git blame for a specific line."""
    try:
        result = subprocess.run(
            ["git", "blame", "-L", f"{line},{line}", "--", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _git_recent_fixes(file_path: str, n: int = GIT_LOG_DEPTH) -> list[dict]:
    """Get recent FIX commits touching *file_path*."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"-{n}", "--follow", "--", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    commits: list[dict] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        hsh = parts[0]
        try:
            msg_r = subprocess.run(
                ["git", "log", "-1", "--format=%B", hsh],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        msg = msg_r.stdout.strip() if msg_r.returncode == 0 else parts[1]
        # Only keep FIX-tagged commits
        if re.search(r"FIX-\d{8}-\d{3}", msg):
            commits.append({"hash": hsh, "message": msg[:500]})
    return commits


def _fix_registry_context(fix_ids: list[str]) -> str:
    """Extract relevant sections from FIX_REGISTRY.md for given FIX IDs."""
    registry_path = ROOT / "blueprints" / "system" / "FIX_REGISTRY.md"
    if not registry_path.exists():
        return ""
    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    snippets: list[str] = []
    for fid in fix_ids:
        # Find the FIX ID and grab surrounding lines
        idx = text.find(fid)
        if idx >= 0:
            start = max(0, idx - 50)
            end = min(len(text), idx + 400)
            snippets.append(text[start:end].strip())
    return "\n---\n".join(snippets[:3])  # limit to 3 entries


def collect_context(
    alert_json: str = "",
    traceback: str = "",
    file_path: str = "",
    line: int = 0,
) -> dict[str, str]:
    """Assemble the context package for LLM RCA analysis."""
    ctx: dict[str, str] = {}

    if traceback:
        ctx["traceback"] = _truncate_traceback(traceback)

    if alert_json:
        try:
            alert_obj = json.loads(alert_json)
            ctx["alert"] = json.dumps(alert_obj, indent=2, ensure_ascii=False)[:2000]
        except json.JSONDecodeError:
            ctx["alert"] = alert_json[:2000]

    if file_path:
        ctx["file"] = file_path
        if line:
            blame = _git_blame(file_path, line)
            if blame:
                ctx["git_blame"] = blame

        recent = _git_recent_fixes(file_path)
        if recent:
            fix_ids: list[str] = []
            for c in recent:
                found = re.findall(r"FIX-\d{8}-\d{3}", c["message"])
                fix_ids.extend(found)
            ctx["recent_fix_commits"] = json.dumps(recent, indent=2)[:2000]
            if fix_ids:
                registry = _fix_registry_context(list(dict.fromkeys(fix_ids)))
                if registry:
                    ctx["fix_registry_entries"] = registry[:2000]

    return ctx


# ── LLM invocation ─────────────────────────────────────────────────────────


def _build_prompt(ctx: dict[str, str]) -> str:
    """Build the structured prompt for LLM RCA analysis."""
    sections: list[str] = []
    for key, label in [
        ("traceback", "Error Traceback"),
        ("alert", "Alert/Event JSON"),
        ("file", "File"),
        ("git_blame", "Git Blame (last modifier)"),
        ("recent_fix_commits", "Recent FIX History (last 5 commits to this file)"),
        ("fix_registry_entries", "Relevant FIX_REGISTRY entries"),
    ]:
        if key in ctx:
            sections.append(f"- {label}:\n{ctx[key]}")

    context_block = "\n\n".join(sections) if sections else "(no context available)"

    return f"""You are an institutional root cause analyst for a quantitative trading system.

CONTEXT:
{context_block}

TASK: Determine whether this is:
A) Recurrence of a known RC pattern (reference the FIX-ID)
B) New root cause requiring a new FIX registration

OUTPUT (strict JSON, no other text):
{{
  "l_level": "L1" | "L2" | "L3",
  "rc_category": "RC-01" through "RC-12" or "RC-NEW",
  "is_recurrence": true | false,
  "related_fix_ids": ["FIX-YYYYMMDD-NNN"],
  "root_cause": "concise causal statement (<100 words)",
  "suggested_action": "immediate fix + systemic prevention",
  "confidence": 0.0-1.0
}}"""


def _call_anthropic(prompt: str) -> dict | None:
    """Call Anthropic Messages API. Returns parsed JSON or None on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import urllib.request

        body = json.dumps(
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data.get("content", [{}])[0].get("text", "")
            return _parse_llm_json(content)
    except Exception:
        logger.exception("Anthropic API call failed")
        return None


def _call_openai(prompt: str) -> dict | None:
    """Call OpenAI Chat Completions API. Returns parsed JSON or None."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        import urllib.request

        body = json.dumps(
            {
                "model": "gpt-4o",
                "max_tokens": 1024,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return _parse_llm_json(content)
    except Exception:
        logger.exception("OpenAI API call failed")
        return None


def _parse_llm_json(text: str) -> dict | None:
    """Extract JSON object from LLM output (may be wrapped in markdown fences)."""
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Try extracting from ```json ... ``` block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


# ── Hallucination guard ────────────────────────────────────────────────────


def _validate_llm_output(parsed: dict, valid_fix_ids: set[str]) -> dict:
    """Validate LLM output fields and flag hallucinations.

    Modifies *parsed* in-place and returns it.
    """
    parsed.setdefault("hallucination_suspected", False)

    # Validate l_level
    if parsed.get("l_level") not in ("L1", "L2", "L3"):
        parsed["l_level"] = "L2"  # safe default
        parsed["hallucination_suspected"] = True

    # Validate rc_category
    rc = parsed.get("rc_category", "")
    if rc not in VALID_RC_CATEGORIES and rc != "RC-NEW":
        parsed["rc_category"] = "RC-NEW"
        parsed["hallucination_suspected"] = True

    # Validate related_fix_ids — LLMs love to hallucinate plausible FIX IDs
    related = parsed.get("related_fix_ids", [])
    if isinstance(related, list) and valid_fix_ids:
        for fid in related:
            if not isinstance(fid, str):
                continue
            if fid not in valid_fix_ids:
                logger.warning("LLM hallucinated FIX-ID: %s (not in context)", fid)
                parsed["hallucination_suspected"] = True
                # Don't remove — just flag. Humans can cross-check.

    # Clamp confidence
    conf = parsed.get("confidence", 0.5)
    if not isinstance(conf, int | float) or conf < 0 or conf > 1:
        parsed["confidence"] = 0.5

    return parsed


# ── Result dispatch ────────────────────────────────────────────────────────


def _dispatch_result(parsed: dict, data_dir: str) -> None:
    """Write RCA result to shadow log and optionally to data loss register."""
    # Shadow log
    state_dir = ROOT / data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source": "shadow_rca",
        **parsed,
    }

    shadow_log = state_dir / "rca_shadow_log.jsonl"
    try:
        with open(shadow_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("RCA shadow log written: %s", shadow_log)
    except OSError:
        logger.exception("Failed to write shadow log")

    # Low-confidence → log warning, don't auto-fill data_loss
    if parsed.get("confidence", 0) < 0.5:
        logger.warning(
            "LOW CONFIDENCE RCA (%.2f) — human review required. " "See %s for details.",
            parsed["confidence"],
            shadow_log,
        )


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ω Shadow RCA — LLM-powered root cause analysis")
    parser.add_argument("--alert-json", default="", help="Alert JSON string")
    parser.add_argument("--traceback", default="", help="Error traceback string")
    parser.add_argument("--file", default="", help="Source file path")
    parser.add_argument("--line", type=int, default=0, help="Source file line number")
    parser.add_argument(
        "--data-dir", default="data_btc", help="Data directory for shadow log output"
    )
    parser.add_argument("--stdin", action="store_true", help="Read input JSON from stdin")
    args = parser.parse_args()

    # ── Parse input ──
    alert_json = args.alert_json
    traceback = args.traceback
    file_path = args.file
    line = args.line

    if args.stdin or (not alert_json and not traceback and sys.stdin.isatty() is False):
        try:
            raw = sys.stdin.read()
            if raw.strip():
                data = json.loads(raw)
                alert_json = json.dumps(data.get("alert", data.get("alert_json", "")))
                traceback = data.get("traceback", traceback)
                file_path = data.get("file", file_path)
                line = data.get("line", line)
        except (json.JSONDecodeError, OSError):
            pass

    if not alert_json and not traceback:
        print(
            json.dumps(
                {
                    "error": "No input provided. Use --alert-json, --traceback, or stdin.",
                    "l_level": "L2",
                    "rc_category": "RC-NEW",
                    "is_recurrence": False,
                    "related_fix_ids": [],
                    "root_cause": "Insufficient context for RCA.",
                    "suggested_action": "Provide alert JSON or traceback for analysis.",
                    "confidence": 0.0,
                }
            )
        )
        return 1

    # ── Collect context ──
    ctx = collect_context(
        alert_json=alert_json,
        traceback=traceback,
        file_path=file_path,
        line=line,
    )

    # Build set of valid FIX IDs from context (for hallucination guard)
    valid_fix_ids: set[str] = set()
    if "recent_fix_commits" in ctx:
        valid_fix_ids.update(re.findall(r"FIX-\d{8}-\d{3}", ctx["recent_fix_commits"]))
    if "fix_registry_entries" in ctx:
        valid_fix_ids.update(re.findall(r"FIX-\d{8}-\d{3}", ctx["fix_registry_entries"]))

    # ── Invoke LLM ──
    prompt = _build_prompt(ctx)
    parsed: dict | None = None

    # Try Anthropic first, then OpenAI, then fallback
    parsed = _call_anthropic(prompt)
    if parsed is None:
        parsed = _call_openai(prompt)

    if parsed is None:
        # Local fallback — output structured template for human
        parsed = {
            "l_level": "L2",
            "rc_category": "RC-NEW",
            "is_recurrence": False,
            "related_fix_ids": list(valid_fix_ids)[:3],
            "root_cause": "LLM UNAVAILABLE — manual RCA required. "
            f"Review traceback/alert and recent FIX history "
            f"({len(valid_fix_ids)} FIX IDs found in context).",
            "suggested_action": "LLM unavailable. Manually classify root cause "
            "using Iron Law #12 protocol (STOP→LOOKUP→DIG→MAP→PLAN).",
            "confidence": 0.0,
            "llm_unavailable": True,
        }

    # ── Validate ──
    parsed = _validate_llm_output(parsed, valid_fix_ids)

    # ── Dispatch ──
    _dispatch_result(parsed, args.data_dir)

    # ── Output to stdout (for caller to attach to alert notifications) ──
    print(json.dumps(parsed, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        print(
            json.dumps(
                {
                    "error": "shadow_rca internal failure",
                    "l_level": "L2",
                    "rc_category": "RC-NEW",
                    "is_recurrence": False,
                    "related_fix_ids": [],
                    "root_cause": "shadow_rca crashed — see logs.",
                    "suggested_action": "Diagnose shadow_rca failure, then manually classify.",
                    "confidence": 0.0,
                }
            )
        )
        sys.exit(0)  # Fail-open: don't block caller
