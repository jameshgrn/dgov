"""Task routing, slug generation, and prompt structuring."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time

logger = logging.getLogger(__name__)

# -- Qwen 4B helper (tunnel-aware) --

_QWEN_4B_URL = "http://localhost:8082/v1/chat/completions"
_QWEN_4B_TIMEOUT = 5


def _qwen_4b_request(messages: list[dict], max_tokens: int = 20, temperature: float = 0) -> dict:
    """Send a request to Qwen 4B, trying localhost first then SSH tunnel.

    Returns the parsed JSON response dict.
    Raises on failure (caller should catch).
    """
    import urllib.request

    body = json.dumps(
        {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode()

    # Try 1: direct localhost (tunnel is up locally)
    try:
        req = urllib.request.Request(
            _QWEN_4B_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_QWEN_4B_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception:
        logger.debug("Qwen 4B direct request failed, trying SSH fallback")

    # Try 2: SSH to river and curl from there
    json_str = json.dumps(
        {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    )
    curl_cmd = (
        f"curl -s --max-time {_QWEN_4B_TIMEOUT} -X POST "
        f"-H 'Content-Type: application/json' "
        f"-d @- 'http://localhost:8082/v1/chat/completions' <<'__JSON__'\n{json_str}\n__JSON__"
    )
    script = f"ssh river 'bash -l' <<'HEREDOC'\n{curl_cmd}\nHEREDOC"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=_QWEN_4B_TIMEOUT + 30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH curl to river failed (exit {result.returncode})")
    return json.loads(result.stdout)


# -- Task routing --


def classify_task(prompt: str) -> str:
    """Classify a task prompt and recommend an agent via Qwen 4B.

    Returns "pi" for mechanical tasks (run commands, edit specific lines,
    format files) and "claude" for analytical tasks (debug flaky tests,
    refactor architecture, multi-step reasoning).

    Falls back to "claude" if the model is unreachable.
    """
    import dgov.panes as _p  # access through panes so test mocks propagate

    try:
        result = _p._qwen_4b_request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify this task as either 'pi' or 'claude'.\n"
                        "pi = mechanical: run a command, edit a specific line, "
                        "add a comment, format files, simple find-and-replace.\n"
                        "claude = analytical: debug why something fails, read and "
                        "understand complex code, refactor architecture, fix flaky "
                        "tests, multi-file reasoning, rework/redesign a system, "
                        "add a new feature with multiple moving parts, "
                        "anything involving scheduler.py or panes.py.\n"
                        "Reply with ONLY 'pi' or 'claude', nothing else."
                    ),
                },
                {"role": "user", "content": prompt[:300]},
            ],
            max_tokens=5,
            temperature=0,
        )
        answer = result["choices"][0]["message"]["content"].strip().lower()
        if "pi" in answer:
            return "pi"
        return "claude"
    except Exception:
        return "claude"


def _structure_pi_prompt(raw_prompt: str, files: list[str] | None = None) -> str:
    """Wrap a raw task description into pi's numbered-step format.

    Takes a freeform prompt and returns a structured prompt with:
    1. Read instructions for mentioned files
    2. The original task description
    3. Lint step
    4. Explicit git add + git commit steps

    If files are provided, they're used for the read/add steps.
    If not, extract file paths from the prompt text.
    """
    if files is None:
        # Extract file paths from prompt
        # Patterns: src/..., tests/..., or anything with an extension
        matches = re.findall(r"\b(?:src/|tests/)[\w\-\./]+|[\w\-\./]+\.\w+", raw_prompt)
        files = []
        seen = set()
        for f in matches:
            f = f.strip("./")
            if f and f not in seen and ("/" in f or "." in f):
                # Avoid matching things like "3.5" or "1.2.3"
                if not re.match(r"^\d+(\.\d+)+$", f):
                    files.append(f)
                    seen.add(f)

    steps = []
    step_num = 1

    # 1. Read steps
    if files:
        for f in files:
            steps.append(f"{step_num}. Read {f}")
            step_num += 1

    # 2. Original task
    steps.append(f"{step_num}. {raw_prompt.strip()}")
    step_num += 1

    # 3. Lint step
    if files:
        py_files = [f for f in files if f.endswith(".py")]
        if py_files:
            steps.append(f"{step_num}. Run: uv run ruff check {' '.join(py_files)}")
            step_num += 1

    # 4. git add
    if files:
        steps.append(f"{step_num}. git add {' '.join(files)}")
        step_num += 1

    # 5. git commit
    # Infer commit message from first line of prompt
    first_line = raw_prompt.strip().split("\n")[0]
    commit_msg = first_line[:50].strip().rstrip(".")
    if not commit_msg:
        commit_msg = "Worker changes"

    # Use double quotes for the commit message in the step text
    steps.append(f'{step_num}. git commit -m "{commit_msg}"')

    return "\n".join(steps)


# -- Slug validation --

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")


def _validate_slug(slug: str) -> str:
    """Validate slug for safe use in file paths and shell commands."""
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid slug: {slug!r}. "
            "Must be 1-50 chars, lowercase alphanumeric and hyphens, "
            "starting with alphanumeric."
        )
    return slug


# -- Slug generation --


def _generate_slug(prompt: str, max_words: int = 4) -> str:
    """Generate a descriptive kebab-case slug using Qwen 4B, with local fallback."""
    import dgov.panes as _p  # access through panes so test mocks propagate

    try:
        result = _p._qwen_4b_request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate a short kebab-case slug (2-4 words, lowercase, "
                        "hyphens only) that describes this task. "
                        "Reply with ONLY the slug, nothing else."
                    ),
                },
                {"role": "user", "content": prompt[:200]},
            ],
            max_tokens=20,
            temperature=0.3,
        )
        raw = result["choices"][0]["message"]["content"].strip().lower()
        slug = re.sub(r"[^a-z0-9-]", "", raw).strip("-")
        if slug and _SLUG_RE.match(slug):
            return slug
    except Exception:
        logger.debug("LLM-based slug generation failed, using word extraction fallback")
    # Fallback: local word extraction
    # Strip absolute path segments (e.g. /Users/jake/...) and keep only the tail
    prompt_tail = re.sub(r"/\S+/", " ", prompt)
    words = re.sub(r"[^a-z0-9\s]", " ", prompt_tail.lower()).split()
    noise = {
        "the",
        "a",
        "an",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "is",
        "it",
        "read",
        "run",
        "git",
        "add",
        "commit",
        "pytest",
        "ruff",
        "uv",
        "file",
        "files",
        "path",
        "paths",
    }
    content = []
    for w in words:
        if w in noise:
            continue
        if w.isdigit():
            continue
        content.append(w)
        if len(content) >= max_words:
            break
    slug = "-".join(content) if content else f"task-{int(time.time())}"
    slug = slug[:50]
    # Ensure generated slug passes validation (strip leading/trailing hyphens)
    slug = slug.strip("-") or f"task-{int(time.time())}"
    return slug
