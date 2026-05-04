# Repo Reviewer

**Does the code do what the README says it does?**

A small CLI tool that points Claude at a public GitHub repository and asks it to evaluate whether the code actually delivers on the README's claims, whether there are obvious bugs or security flaws, and how much real effort went into it.

Built for non-technical reviewers — recruiters, hiring managers, friends-of-engineers — who want to sanity-check a candidate's portfolio without learning to read code.

## What it checks

- **Substance** — Does the README's feature list match what the code actually implements? Or are key pieces empty stubs and TODOs?
- **Correctness** — Are there obvious bugs or things that wouldn't run?
- **Security** — Hardcoded credentials, SQL injection, unsafe shell calls, exposed secrets.
- **Polish** — Real organization and effort vs. dead imports, generic boilerplate, AI-generated filler that doesn't fit.

It produces a one-line verdict (`legit` / `mostly_legit` / `concerning` / `bs`), four scores (0-100), a plain-English summary, and specific strengths and concerns.

## What it does NOT check

- Profile-level patterns (account age, fork ratios, activity bursts) — those unfairly torch new portfolios.
- Cross-repo consistency or claimed expertise vs. evidence.
- Anything requiring the code to actually run.

This is a per-repo code review, not a vibe check on the candidate.

## Install

```bash
pip install -r requirements.txt
```

You'll also need `git` on your PATH (for the shallow clone).

## First-time setup

Run the setup command and paste your API key when prompted:

```bash
python repo_reviewer.py --setup
```

That's it. The setup will:
- Prompt for your Anthropic API key (input is hidden — won't appear in your terminal or shell history)
- Validate the key by making a free test call to the Anthropic API
- Save it to `~/.repo_reviewer/config.ini` so you only do this once

**Don't have a key?** Get one (free signup, no payment needed to start) at https://console.anthropic.com/settings/keys.

**Already configured?** Verify it works:

```bash
python repo_reviewer.py --check
```

### Other ways to provide the key

If you'd rather not save the key to disk, set an environment variable instead:

```bash
# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# macOS/Linux
export ANTHROPIC_API_KEY="sk-ant-..."
```

The tool checks for keys in this order: `--api-key` flag → `ANTHROPIC_API_KEY` env var → `config.ini`.

## Use

```bash
# Basic
python repo_reviewer.py https://github.com/user/project

# Cheaper model (Haiku)
python repo_reviewer.py https://github.com/user/project --model haiku

# Most capable model (Opus 4.7)
python repo_reviewer.py https://github.com/user/project --model opus

# JSON output to a file
python repo_reviewer.py https://github.com/user/project --format json --output report.json

# Verbose progress
python repo_reviewer.py https://github.com/user/project --verbose

# Interactive prompt for URL
python repo_reviewer.py --interactive
```

## Models

| Alias       | Model ID              | Notes                        |
|-------------|-----------------------|------------------------------|
| `sonnet`    | `claude-sonnet-4-6`   | Default — good balance       |
| `opus`      | `claude-opus-4-7`     | Most capable, more expensive |
| `opus-4-6`  | `claude-opus-4-6`     | Previous Opus                |
| `haiku`     | `claude-haiku-4-5`    | Cheapest, fastest            |

Cost per review is typically a few cents on Sonnet, even less on Haiku, more on Opus.

## How it works

1. Shallow-clones the repo to a temp directory.
2. Reads the README and walks the source tree, picking up code, configs, and manifests.
3. Skips `node_modules`, `.git`, build outputs, lockfiles, minified bundles, etc.
4. Caps total content sent to the model (default 150,000 chars; tunable in `config.ini`).
5. Asks Claude to evaluate against the four criteria above, returning structured JSON.
6. Renders a readable terminal report and cleans up the temp directory.

## Sample output

```
======================================================================
  REPO REVIEW: my-cool-tool
  https://github.com/me/my-cool-tool
======================================================================

VERDICT:  MOSTLY LEGIT — Minor gaps, generally honest

SCORES (0-100)
----------------------------------------------------------------------
  Substance       82  [################################--------]
  Correctness     78  [###############################---------]
  Security        90  [####################################----]
  Polish          74  [#############################-----------]

SUMMARY
----------------------------------------------------------------------
The README accurately describes a CLI utility for X. Core flows are
implemented and would run. Two of seven advertised subcommands are
unimplemented stubs but are flagged in the README.

CLAIMS vs. REALITY
----------------------------------------------------------------------
README claims subcommands A, B, C, D, E, F, G. A-E are real and
working. F and G are listed as "planned" — code present but stubbed.
...
```

## Tuning

If you're reviewing a larger repo and want more thorough coverage, raise `total_char_budget` in `config.ini` (or pass `--char-budget`). The default keeps cost bounded for typical portfolio repos.

## License

MIT
