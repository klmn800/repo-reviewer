#!/usr/bin/env python3
"""Repo Reviewer — Evaluate a GitHub repository's code against its README.

Built for non-technical reviewers (recruiters, hiring managers) who want to
sanity-check a candidate's portfolio without reading the code themselves.

Clones a public GitHub repository (shallow), reads the README and source files,
and asks Claude to assess: does the code do what the README claims? Are there
obvious bugs or security issues? Is it real work or AI-generated boilerplate?

Outputs a plain-English verdict, scores across four axes, and specific
strengths and concerns.
"""

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SUPPORTED_MODELS = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "opus-4-6": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5",
}

# Per-million-token pricing for cost estimation (USD).
MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-7": {"input": 5.00, "output": 25.00},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TOTAL_CHAR_BUDGET = 600_000   # ~150K tokens; ~$0.45 on Sonnet 4.6
DEFAULT_MAX_FILES = 200
DEFAULT_MAX_CHARS_PER_FILE = 200_000  # safety net only; real source files are much smaller

# File extensions worth reading.
SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".scala",
    ".sh", ".bash", ".ps1", ".bat",
    ".html", ".css", ".scss", ".sass", ".vue", ".svelte",
    ".sql", ".graphql",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".rst", ".txt",
}

# Manifest files we always want to see if present.
MANIFEST_FILES = {
    "package.json", "requirements.txt", "pyproject.toml", "setup.py",
    "Cargo.toml", "go.mod", "Gemfile", "composer.json", "pom.xml",
    "build.gradle", "Makefile", "Dockerfile", "docker-compose.yml",
}

# Directories to skip wholesale.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", "out", "bin", "obj",
    ".next", ".nuxt", ".cache", ".pytest_cache", ".mypy_cache",
    "vendor", "third_party", "coverage", ".coverage",
    ".idea", ".vscode", "site-packages",
}

# File patterns to skip even if extension matches.
SKIP_FILE_PATTERNS = (
    ".min.js", ".min.css", ".lock", "-lock.json",
    ".map", ".bundle.js",
)

# README candidates, in priority order.
README_CANDIDATES = ["README.md", "README.rst", "README.txt", "README", "readme.md"]


# ----------------------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------------------

@dataclass
class RepoSnapshot:
    """A captured view of a cloned repository."""
    url: str
    repo_name: str
    readme: str
    file_listing: list[str]            # every file in the repo
    file_contents: dict[str, str]      # source files actually read
    source_eligible_count: int         # source files that matched our filters
    total_chars: int
    truncated: bool


@dataclass
class ReviewLimits:
    """Caps controlling how much code we read into context."""
    total_char_budget: int = DEFAULT_TOTAL_CHAR_BUDGET
    max_files: int = DEFAULT_MAX_FILES
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE


# ----------------------------------------------------------------------------
# Git operations
# ----------------------------------------------------------------------------

def validate_github_url(url: str) -> tuple[str, str]:
    """Return (repo_owner, repo_name) for a github.com URL, or raise ValueError."""
    parsed = urlparse(url.strip().rstrip("/"))
    if parsed.netloc not in ("github.com", "www.github.com"):
        raise ValueError(f"Not a github.com URL: {url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"URL doesn't look like a repository: {url}")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return owner, repo


def clone_shallow(url: str, target_dir: str, verbose: bool = False, timeout: int = 120) -> None:
    """Clone a repository at depth 1 into target_dir.

    The `--` separator before the URL prevents git from interpreting a
    maliciously-crafted URL (e.g. starting with `--upload-pack=`) as a flag.
    """
    cmd = ["git", "clone", "--depth", "1", "--quiet", "--", url, target_dir]
    if verbose:
        print(f"  Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git clone timed out after {timeout}s")
    if result.returncode != 0:
        raise RuntimeError(
            f"git clone failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )


# ----------------------------------------------------------------------------
# Repository scanning
# ----------------------------------------------------------------------------

def is_skippable_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def is_source_file(path: Path) -> bool:
    name = path.name
    if any(name.endswith(p) for p in SKIP_FILE_PATTERNS):
        return False
    if name in MANIFEST_FILES:
        return True
    return path.suffix.lower() in SOURCE_EXTENSIONS


def read_file_safely(path: Path, max_chars: int) -> Optional[str]:
    """Read a file as text, truncating to max_chars. Returns None on failure."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n... [truncated]"
        return content
    except (OSError, UnicodeDecodeError):
        return None


def find_readme(repo_dir: Path) -> str:
    """Locate and return the README contents, or a placeholder."""
    for name in README_CANDIDATES:
        candidate = repo_dir / name
        if candidate.is_file():
            content = read_file_safely(candidate, max_chars=20_000)
            if content:
                return content
    return "[No README found in repository root.]"


def collect_files(repo_dir: Path, limits: ReviewLimits) -> tuple[list[str], dict[str, str], int, int, bool]:
    """Walk the repo, read source files.

    Returns (listing, contents_by_path, source_eligible_count, total_chars, truncated).
    Files are read in a deterministic order with manifests first, then by depth then by name.
    """
    all_paths: list[Path] = []
    listing: list[str] = []

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if not is_skippable_dir(d)]
        root_path = Path(root)
        rel_root = root_path.relative_to(repo_dir)
        for fname in sorted(files):
            full = root_path / fname
            rel = (rel_root / fname).as_posix() if str(rel_root) != "." else fname
            listing.append(rel)
            if is_source_file(full):
                all_paths.append(full)

    # Sort: manifests first, then by depth (shallow first), then alphabetical.
    def sort_key(p: Path) -> tuple:
        rel = p.relative_to(repo_dir)
        is_manifest = 0 if p.name in MANIFEST_FILES else 1
        depth = len(rel.parts)
        return (is_manifest, depth, str(rel).lower())

    all_paths.sort(key=sort_key)

    source_eligible_count = len(all_paths)
    contents: dict[str, str] = {}
    total_chars = 0
    truncated = False

    for full in all_paths:
        if len(contents) >= limits.max_files:
            truncated = True
            break
        if total_chars >= limits.total_char_budget:
            truncated = True
            break

        remaining = limits.total_char_budget - total_chars
        cap = min(limits.max_chars_per_file, remaining)
        text = read_file_safely(full, max_chars=cap)
        if text is None:
            continue

        rel = full.relative_to(repo_dir).as_posix()
        contents[rel] = text
        total_chars += len(text)

    return listing, contents, source_eligible_count, total_chars, truncated


def snapshot_repo(url: str, clone_dir: Path, limits: ReviewLimits) -> RepoSnapshot:
    _, repo_name = validate_github_url(url)
    readme = find_readme(clone_dir)
    listing, contents, source_count, total_chars, truncated = collect_files(clone_dir, limits)
    return RepoSnapshot(
        url=url,
        repo_name=repo_name,
        readme=readme,
        file_listing=listing,
        file_contents=contents,
        source_eligible_count=source_count,
        total_chars=total_chars,
        truncated=truncated,
    )


# ----------------------------------------------------------------------------
# Claude review
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a code reviewer evaluating a GitHub repository on behalf of a non-technical reviewer (often a recruiter or hiring manager). Your job is to assess whether the code in the repository actually does what its README claims, and to flag obvious bugs, security issues, and signs of low effort or non-functional AI-generated code.

Be fair and grounded:
- A small, new, or in-development portfolio is not bad. Distinguish between "labeled as in-progress" (honest) and "claims work that doesn't exist" (oversold).
- Reward honest READMEs that accurately describe status (including "in development" or "experimental").
- Don't penalize unconventional code that works. Don't reward "looks impressive" code that doesn't.
- Consider what kind of project this is. A learning exercise, a personal utility, and a production library should each be judged on their own terms.

Focus your evaluation on four areas:

1. **Substance** — Does the code actually implement what the README advertises? Are advertised features real, or are they empty stubs / TODOs / `pass` statements? If the README lists 5 features and 4 are real, say so.

2. **Correctness** — Are there obvious bugs, broken logic, things that wouldn't run or crash on the first call? Is error handling reasonable for the type of project?

3. **Security** — Hardcoded API keys or secrets, SQL injection risks, unsafe shell calls (shell=True with user input), exposed credentials, missing input validation on external surfaces. Be specific — give file/function names where possible.

4. **Polish** — Organization, structure, signs of real effort. Watch for AI-slop tells: dead imports, placeholder TODOs that were never filled, suspiciously generic boilerplate that doesn't match the rest, README sophistication wildly mismatched with code sophistication.

Write the output for someone who does not read code. Avoid jargon where possible; when you must use a technical term, explain it briefly. Keep the summary tight (3-5 sentences) — concrete observations, not adjectives.

Scores are 0-100. Use the full range:
- 90-100: excellent for the type of project
- 70-89: solid
- 50-69: acceptable, with notable gaps
- 30-49: weak; significant problems
- 0-29: broken, oversold, or non-functional

Suggest 5-8 GitHub topic tags that characterize the repository. These must be:
- Lowercase, hyphenated, no spaces, max 35 characters each (GitHub topic format).
- Established/popular tags where they fit — e.g. prefer `claude-api` over `claude-anthropic-api`, `cli` over `command-line`, `react` over `reactjs`.
- A reasonable mix: language/framework, domain or use-case, project type. Don't waste slots on uninformative tags like `code` or `software`.
- Accurate to what the code actually does, not what the README aspires to.

Tag every concern and security flag with a severity, and use the labels honestly:
- HIGH:   Real bug, broken feature, or security issue that would matter to a user or maintainer.
- MEDIUM: Real issue that's not blocking but should be addressed (UX bug, fragile pattern, missing validation that bites in realistic scenarios).
- LOW:    Worth knowing — code smell, minor inconsistency, maintenance landmine that hasn't bitten yet.
- NIT:    Cosmetic or stylistic. Notable while reading, not actually a problem.

Don't suppress observations to keep lists short. Showing low/nit findings is part of the value — it tells the reader you actually engaged with the code. But don't inflate severity to fill space either: if something is genuinely cosmetic, tag it NIT and move on. The severity tag is what stops a non-technical reader from over-weighting a small finding."""


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["legit", "mostly_legit", "concerning", "bs"],
            "description": "Overall judgment. 'legit' = code matches claims and is solid; 'mostly_legit' = matches claims with minor issues; 'concerning' = real gaps between claims and code, or notable issues; 'bs' = oversold, broken, or substantively non-functional."
        },
        "summary": {
            "type": "string",
            "description": "3-5 sentence plain-English summary for a non-technical reader. Concrete observations only."
        },
        "claims_vs_reality": {
            "type": "string",
            "description": "Specifically address: what does the README say this does, and does the code actually do it? List gaps."
        },
        "scores": {
            "type": "object",
            "properties": {
                "substance": {"type": "integer", "minimum": 0, "maximum": 100},
                "correctness": {"type": "integer", "minimum": 0, "maximum": 100},
                "security": {"type": "integer", "minimum": 0, "maximum": 100},
                "polish": {"type": "integer", "minimum": 0, "maximum": 100}
            },
            "required": ["substance", "correctness", "security", "polish"],
            "additionalProperties": False
        },
        "suggested_topics": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 8,
            "description": "5-8 GitHub topic tags characterizing the repo. Lowercase, hyphenated, max 35 chars each. Use established tags where possible."
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific things this repo does well. Empty list if nothing notable."
        },
        "concerns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "NIT"]},
                    "text": {"type": "string"}
                },
                "required": ["severity", "text"],
                "additionalProperties": False
            },
            "description": "Specific issues — bugs, gaps, design problems. Reference file names where useful. Each item must have an honest severity tag."
        },
        "security_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "NIT"]},
                    "text": {"type": "string"}
                },
                "required": ["severity", "text"],
                "additionalProperties": False
            },
            "description": "Specific security observations. Reference file/function where useful. Each item must have an honest severity tag — non-issues should be tagged NIT or omitted."
        }
    },
    "required": ["verdict", "summary", "claims_vs_reality", "scores", "suggested_topics", "strengths", "concerns", "security_flags"],
    "additionalProperties": False
}


def build_user_message(snap: RepoSnapshot) -> str:
    """Assemble README + file listing + source contents into a single user message."""
    parts: list[str] = []
    parts.append(f"Repository: {snap.url}")
    parts.append(f"Name: {snap.repo_name}")
    parts.append("")
    parts.append("=" * 70)
    parts.append("README")
    parts.append("=" * 70)
    parts.append(snap.readme)
    parts.append("")
    parts.append("=" * 70)
    parts.append(f"FILE LISTING ({len(snap.file_listing)} files total)")
    parts.append("=" * 70)
    # Cap the listing too — long monorepos shouldn't blow it out.
    listing_to_show = snap.file_listing[:300]
    parts.extend(listing_to_show)
    if len(snap.file_listing) > 300:
        parts.append(f"... and {len(snap.file_listing) - 300} more")
    parts.append("")
    parts.append("=" * 70)
    parts.append(f"SOURCE FILES ({len(snap.file_contents)} files, {snap.total_chars:,} chars)")
    if snap.truncated:
        parts.append("Note: truncated — not every file in the repo is shown.")
    parts.append("=" * 70)
    for path, content in snap.file_contents.items():
        parts.append("")
        parts.append(f"--- {path} ---")
        parts.append(content)

    return "\n".join(parts)


def review_repo(snap: RepoSnapshot, model: str, api_key: str, verbose: bool = False) -> tuple[dict, dict]:
    """Send the snapshot to Claude and return (parsed_review, usage_dict)."""
    client = anthropic.Anthropic(api_key=api_key)
    user_message = build_user_message(snap)

    if verbose:
        print(f"  Prompt size: ~{len(user_message):,} chars")
        print(f"  Calling {model}...")

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
        tools=[
            {
                "name": "submit_review",
                "description": "Submit your structured review of the repository.",
                "input_schema": REVIEW_SCHEMA,
            }
        ],
        tool_choice={"type": "tool", "name": "submit_review"},
    )

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("Model returned no tool call; cannot extract review.")
    parsed = tool_use.input
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
    }
    return parsed, usage


# ----------------------------------------------------------------------------
# Output formatting
# ----------------------------------------------------------------------------

VERDICT_LABELS = {
    "legit": "LEGIT — Code matches claims, solid work",
    "mostly_legit": "MOSTLY LEGIT — Minor gaps, generally honest",
    "concerning": "CONCERNING — Real gaps between claims and reality",
    "bs": "BS — Oversold, broken, or non-functional",
}


def estimate_cost(model: str, usage: dict) -> float:
    """Rough USD cost estimate for the call."""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return 0.0
    # Treat cache reads at ~10% of input price; cache writes at ~125%.
    in_tokens = usage["input_tokens"]
    cache_read = usage["cache_read_input_tokens"]
    cache_create = usage["cache_creation_input_tokens"]
    out_tokens = usage["output_tokens"]
    cost = (
        in_tokens * pricing["input"] / 1_000_000
        + cache_read * pricing["input"] * 0.10 / 1_000_000
        + cache_create * pricing["input"] * 1.25 / 1_000_000
        + out_tokens * pricing["output"] / 1_000_000
    )
    return cost


def render_text_report(snap: RepoSnapshot, review: dict, model: str, usage: dict) -> str:
    lines: list[str] = []
    bar = "=" * 70

    lines.append("")
    lines.append(bar)
    lines.append(f"  REPO REVIEW: {snap.repo_name}")
    lines.append(f"  {snap.url}")
    lines.append(bar)
    lines.append("")

    verdict = review["verdict"]
    lines.append(f"VERDICT:  {VERDICT_LABELS.get(verdict, verdict.upper())}")
    lines.append("")

    scores = review["scores"]
    lines.append("SCORES (0-100)")
    lines.append("-" * 70)
    for key in ("substance", "correctness", "security", "polish"):
        bar_width = 40
        filled = int(scores[key] * bar_width / 100)
        bar_str = "#" * filled + "-" * (bar_width - filled)
        lines.append(f"  {key.title():14s} {scores[key]:3d}  [{bar_str}]")
    lines.append("")

    lines.append("SUMMARY")
    lines.append("-" * 70)
    lines.append(_wrap(review["summary"], 70))
    lines.append("")

    topics = review.get("suggested_topics") or []
    if topics:
        lines.append("TOPICS")
        lines.append("-" * 70)
        # Sanitize to GitHub's topic rules just in case the model strays.
        clean = [_clean_topic(t) for t in topics]
        clean = [t for t in clean if t]
        lines.append("  " + "  ".join(clean))
        lines.append("")

    lines.append("CLAIMS vs. REALITY")
    lines.append("-" * 70)
    lines.append(_wrap(review["claims_vs_reality"], 70))
    lines.append("")

    if review["strengths"]:
        lines.append("STRENGTHS")
        lines.append("-" * 70)
        for s in review["strengths"]:
            lines.append(f"  + {_wrap(s, 66, indent=4)}")
        lines.append("")

    if review["concerns"]:
        lines.append("CONCERNS")
        lines.append("-" * 70)
        for item in _sort_by_severity(review["concerns"]):
            tag = f"[{item['severity']}]"
            lines.append(f"  {tag:8s} {_wrap(item['text'], 60, indent=11)}")
        lines.append("")

    if review["security_flags"]:
        lines.append("SECURITY FLAGS")
        lines.append("-" * 70)
        for item in _sort_by_severity(review["security_flags"]):
            tag = f"[{item['severity']}]"
            lines.append(f"  {tag:8s} {_wrap(item['text'], 60, indent=11)}")
        lines.append("")
    else:
        lines.append("SECURITY FLAGS:  None identified")
        lines.append("")

    cost = estimate_cost(model, usage)
    lines.append(bar)
    lines.append(
        f"  Model: {model}   Tokens in/out: {usage['input_tokens']:,}/{usage['output_tokens']:,}"
        f"   Est. cost: ${cost:.4f}"
    )
    lines.append(
        f"  Files: read {len(snap.file_contents)} of {snap.source_eligible_count} source files "
        f"({len(snap.file_listing)} total in repo)"
    )
    if snap.truncated:
        lines.append("  Note: budget limits applied — not every source file was read.")
    lines.append("")
    lines.append("  AI-generated review. Treat findings as a starting point, not a verdict —")
    lines.append("  verify them against the code before acting. AI reviewers can miss real")
    lines.append("  issues and confidently flag things that aren't actually problems.")
    lines.append(bar)
    lines.append("")
    return "\n".join(lines)


def _clean_topic(t: str) -> str:
    """Coerce a string into a valid GitHub topic: lowercase, hyphenated, max 35 chars."""
    s = t.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)        # spaces and underscores -> hyphens
    s = re.sub(r"[^a-z0-9-]", "", s)     # drop anything else
    s = re.sub(r"-+", "-", s).strip("-") # collapse and trim hyphens
    return s[:35]


_SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NIT": 3}


def _sort_by_severity(items: list[dict]) -> list[dict]:
    """Sort findings by severity, HIGH first."""
    return sorted(items, key=lambda x: _SEVERITY_RANK.get(x.get("severity", "NIT"), 99))


def _wrap(text: str, width: int, indent: int = 0) -> str:
    """Simple word-wrap that preserves paragraph breaks."""
    out_lines = []
    for para in text.split("\n"):
        if not para.strip():
            out_lines.append("")
            continue
        wrapped = textwrap.wrap(para, width=width)
        if not wrapped:
            out_lines.append("")
            continue
        out_lines.append(wrapped[0])
        for line in wrapped[1:]:
            out_lines.append(" " * indent + line)
    return "\n".join(out_lines)


# ----------------------------------------------------------------------------
# Configuration loading
# ----------------------------------------------------------------------------

USER_CONFIG_PATH = Path.home() / ".repo_reviewer" / "config.ini"


def load_config(config_path: Optional[str]) -> dict:
    """Load config from config.ini if present; values override defaults.

    Search order (first hit wins):
      1. --config CLI arg
      2. ./config.ini (current working directory)
      3. <script_dir>/config.ini
      4. ~/.repo_reviewer/config.ini  (where --setup writes by default)
    """
    cfg: dict = {}
    paths_to_try = []
    if config_path:
        paths_to_try.append(Path(config_path))
    paths_to_try.append(Path.cwd() / "config.ini")
    paths_to_try.append(Path(__file__).parent / "config.ini")
    paths_to_try.append(USER_CONFIG_PATH)

    for p in paths_to_try:
        if p.is_file():
            parser = configparser.ConfigParser()
            parser.read(p, encoding="utf-8")
            if parser.has_section("API"):
                if parser.has_option("API", "anthropic_api_key"):
                    cfg["api_key"] = parser.get("API", "anthropic_api_key").strip()
                if parser.has_option("API", "model"):
                    cfg["model"] = parser.get("API", "model").strip()
            if parser.has_section("Limits"):
                for opt in ("total_char_budget", "max_files", "max_chars_per_file"):
                    if parser.has_option("Limits", opt):
                        cfg[opt] = parser.getint("Limits", opt)
            return cfg
    return cfg


def resolve_model(name: str) -> str:
    """Accept a friendly name or full model ID; return the API model ID."""
    if name in SUPPORTED_MODELS:
        return SUPPORTED_MODELS[name]
    if name in MODEL_PRICING:
        return name
    raise ValueError(
        f"Unknown model '{name}'. Use a full model ID or one of: "
        + ", ".join(SUPPORTED_MODELS.keys())
    )


# ----------------------------------------------------------------------------
# Setup and validation
# ----------------------------------------------------------------------------

def validate_api_key(api_key: str) -> tuple[bool, str]:
    """Make a free Models API call to confirm the key works. Returns (ok, message)."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        client.models.list(limit=1)
        return True, "OK"
    except anthropic.AuthenticationError:
        return False, "API key was rejected. Double-check that you copied the full key."
    except anthropic.PermissionDeniedError:
        return False, "Key authenticated but lacks permission. Check your account/workspace."
    except anthropic.APIConnectionError as e:
        return False, f"Could not reach the Anthropic API: {e}"
    except anthropic.APIError as e:
        return False, f"API error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def _mask_key(key: str) -> str:
    if len(key) <= 14:
        return "****"
    return f"{key[:8]}...{key[-4:]}"


def _invocation() -> str:
    """The path the user used to invoke this script — usable in copy-paste examples."""
    return f"python {sys.argv[0]}"


def run_setup() -> int:
    """Interactive: prompt for API key, validate it, save to ~/.repo_reviewer/config.ini."""
    import getpass

    print()
    print("Repo Reviewer setup")
    print("=" * 50)
    print()
    print("You need an Anthropic API key. Get one (free signup) at:")
    print("  https://console.anthropic.com/settings/keys")
    print()

    if USER_CONFIG_PATH.is_file():
        print(f"A config already exists at: {USER_CONFIG_PATH}")
        try:
            resp = input("Overwrite? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            return 1
        if resp != "y":
            print("Setup cancelled. Existing config left in place.")
            return 0
        print()

    api_key: Optional[str] = None
    while True:
        try:
            raw = getpass.getpass("Anthropic API key (input hidden): ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            return 1

        # Be forgiving about pasted whitespace and surrounding quotes.
        candidate = raw.strip().strip("'\"").strip()

        if not candidate:
            print("  No key entered. Try again, or Ctrl+C to cancel.")
            continue

        if not candidate.startswith("sk-ant-"):
            print(f"  Heads up — Anthropic keys usually start with 'sk-ant-'. Got '{candidate[:10]}...'")
            try:
                resp = input("  Try this anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.", file=sys.stderr)
                return 1
            if resp != "y":
                continue

        print("  Validating against Anthropic API...", end=" ", flush=True)
        ok, msg = validate_api_key(candidate)
        if ok:
            print("OK")
            api_key = candidate
            break

        print("FAIL")
        print(f"    {msg}")
        try:
            resp = input("  Try a different key? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            return 1
        if resp == "n":
            return 1

    # Write config.
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    parser["API"] = {
        "anthropic_api_key": api_key,
        "model": DEFAULT_MODEL,
    }
    parser["Limits"] = {
        "total_char_budget": str(DEFAULT_TOTAL_CHAR_BUDGET),
        "max_files": str(DEFAULT_MAX_FILES),
        "max_chars_per_file": str(DEFAULT_MAX_CHARS_PER_FILE),
    }
    with open(USER_CONFIG_PATH, "w", encoding="utf-8") as f:
        parser.write(f)

    # On POSIX, restrict permissions on the config file (best-effort; no-op on Windows).
    try:
        os.chmod(USER_CONFIG_PATH, 0o600)
    except OSError:
        pass

    print()
    print(f"Saved config to: {USER_CONFIG_PATH}")
    print()
    print("You're set. Try a review:")
    print(f"  {_invocation()} https://github.com/octocat/Hello-World")
    print()
    return 0


def run_check(args: argparse.Namespace, cfg: dict) -> int:
    """Diagnose: show where the key was found, validate it, check git availability."""
    print()
    print("Repo Reviewer setup check")
    print("=" * 50)

    if args.api_key:
        api_key, source = args.api_key, "--api-key flag"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        api_key, source = os.environ["ANTHROPIC_API_KEY"], "ANTHROPIC_API_KEY env var"
    elif cfg.get("api_key"):
        api_key, source = cfg["api_key"], "config.ini"
    else:
        api_key, source = None, None

    print(f"  API key source:  {source or 'NOT FOUND'}")
    if api_key:
        print(f"  API key:         {_mask_key(api_key)}")
    print(f"  Git on PATH:     {'yes' if shutil.which('git') else 'NO — install Git first'}")

    if not api_key or api_key.startswith("your_"):
        print()
        print(f"ERROR: No API key configured. Run:  {_invocation()} --setup")
        return 1

    print("  Validating key... ", end="", flush=True)
    ok, msg = validate_api_key(api_key)
    if ok:
        print("OK")
        print()
        print("All checks passed. You're ready to go.")
        return 0
    print("FAIL")
    print(f"    {msg}")
    return 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a GitHub repo: does the code do what the README claims?",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
First-time setup:
  python repo_reviewer.py --setup        Interactive: paste your API key once.
  python repo_reviewer.py --check        Verify your setup is working.

Examples:
  python repo_reviewer.py https://github.com/octocat/Hello-World
  python repo_reviewer.py https://github.com/user/proj --model haiku
  python repo_reviewer.py https://github.com/user/proj --format json --output report.json
  python repo_reviewer.py https://github.com/user/proj --verbose
""",
    )
    parser.add_argument("url", nargs="?", help="GitHub repository URL")
    parser.add_argument(
        "--setup", action="store_true",
        help="Run interactive setup (prompts for API key, validates, saves config)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Verify your current setup (shows where the key was found and tests it)",
    )
    parser.add_argument(
        "--model", "-m",
        help=f"Model alias or full ID (default: sonnet). Aliases: {', '.join(SUPPORTED_MODELS)}",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write output to file instead of stdout",
    )
    parser.add_argument("--config", "-c", help="Path to config.ini")
    parser.add_argument("--api-key", help="Anthropic API key (overrides env and config)")
    parser.add_argument(
        "--char-budget", type=int,
        help=f"Total characters of source to read (default: {DEFAULT_TOTAL_CHAR_BUDGET})",
    )
    parser.add_argument(
        "--max-files", type=int,
        help=f"Max files to read (default: {DEFAULT_MAX_FILES})",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose progress output")
    parser.add_argument("--interactive", "-i", action="store_true", help="Prompt for URL interactively")
    return parser.parse_args()


def get_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    if args.interactive:
        try:
            url = input("GitHub repository URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            sys.exit(1)
        if not url:
            print("ERROR: no URL provided.", file=sys.stderr)
            sys.exit(1)
        return url
    print(
        "ERROR: provide a GitHub URL as a positional argument, or use --interactive.\n"
        f"  Example: {_invocation()} https://github.com/owner/repo",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> int:
    # Make stdout/stderr UTF-8-tolerant so em-dashes and other Unicode
    # render correctly on Windows consoles (default cp1252 otherwise).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    args = parse_args()

    if args.setup:
        return run_setup()

    cfg = load_config(args.config)

    if args.check:
        return run_check(args, cfg)

    api_key = (
        args.api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or cfg.get("api_key")
    )
    if not api_key or api_key.startswith("your_"):
        print(
            "ERROR: Anthropic API key required.\n"
            "  Easiest fix — run setup once (it'll walk you through it):\n"
            f"      {_invocation()} --setup\n"
            "  Or set ANTHROPIC_API_KEY in your environment.\n"
            "  Get a key at: https://console.anthropic.com/settings/keys",
            file=sys.stderr,
        )
        return 1

    if shutil.which("git") is None:
        print("ERROR: 'git' command not found on PATH. Install Git first.", file=sys.stderr)
        return 1

    model_name = args.model or cfg.get("model") or DEFAULT_MODEL
    try:
        model = resolve_model(model_name)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    limits = ReviewLimits(
        total_char_budget=args.char_budget or cfg.get("total_char_budget", DEFAULT_TOTAL_CHAR_BUDGET),
        max_files=args.max_files or cfg.get("max_files", DEFAULT_MAX_FILES),
        max_chars_per_file=cfg.get("max_chars_per_file", DEFAULT_MAX_CHARS_PER_FILE),
    )

    url = get_url(args)
    try:
        validate_github_url(url)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"Reviewing: {url}")
        print(f"Model:     {model}")

    tmp_root = tempfile.mkdtemp(prefix="repo_reviewer_")
    clone_dir = Path(tmp_root) / "repo"
    try:
        if args.verbose:
            print("Cloning (shallow)...")
        clone_shallow(url, str(clone_dir), verbose=args.verbose)

        if args.verbose:
            print("Scanning files...")
        snap = snapshot_repo(url, clone_dir, limits)

        if args.verbose:
            print(
                f"  Read {len(snap.file_contents)} of {snap.source_eligible_count} source files "
                f"({len(snap.file_listing)} total in repo, {snap.total_chars:,} chars)"
            )
            if snap.truncated:
                print("  Note: hit budget limits — not every source file was read")

        review, usage = review_repo(snap, model=model, api_key=api_key, verbose=args.verbose)

    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except anthropic.APIError as e:
        print(f"ERROR: API call failed: {e}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if args.format == "json":
        output = json.dumps(
            {
                "repo": {"url": snap.url, "name": snap.repo_name},
                "model": model,
                "review": review,
                "usage": usage,
                "estimated_cost_usd": round(estimate_cost(model, usage), 6),
                "files_reviewed": len(snap.file_contents),
                "files_total": len(snap.file_listing),
                "truncated": snap.truncated,
            },
            indent=2,
        )
    else:
        output = render_text_report(snap, review, model, usage)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        if args.verbose:
            print(f"Wrote report to {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
