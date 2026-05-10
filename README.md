# Ghost Browser by Gemma

> Local, free browser automation agent powered by Gemma4 + Ollama — ranked **3rd** on the Browser-Use Stealth Benchmark V1.

---

## Benchmark Results

Tested against the [Browser-Use Stealth Benchmark V1](https://github.com/browser-use/benchmark) (80 tasks, anti-bot detection + task accuracy):

| Rank | Agent | Score | Type |
|------|-------|-------|------|
| 1 | browser-use-cloud | 73% | ☁️ Paid cloud |
| 2 | anchor | 69% | ☁️ Paid cloud |
| **3** | **Ghost Browser (Gemma4)** | **67%** | **💻 Local / Free** |
| 4 | onkernel | 66% | ☁️ Paid cloud |
| 5 | browserless | 52% | ☁️ Paid cloud |
| 6 | local_headful (bu-2-0) | 50% | 💻 Local |
| 7 | steel | 49% | ☁️ Paid cloud |
| 8 | browserbase | 40% | ☁️ Paid cloud |
| 9 | hyperbrowser | 35% | ☁️ Paid cloud |
| 10 | local_headless | 3% | 💻 Local |

**67% accuracy with a free local model — outperforming most paid cloud services.**

---

## What is this?

Ghost Browser is a customized [browser-use](https://github.com/browser-use/browser-use) agent harness that runs entirely on your machine using:

- **Gemma4 27B** via Ollama (no API key needed)
- **Reflexion-style retry loop** — learns from failed attempts and avoids repeating the same mistakes
- **Guard layer** — intercepts bad actions before execution, blocks repeated failures
- **Auto-recovery** — detects when the browser lands on internal pages and navigates back
- **Persistent browser profile** — reduces bot detection over time

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- [Ollama](https://ollama.com) with `gemma4` model

---

## Setup

```bash
# 1. Install Ollama and pull Gemma4
ollama pull gemma4

# 2. Clone and install dependencies
git clone https://github.com/ddangkong/ghost-browser
cd ghost-browser
uv venv --python 3.11
uv sync

# 3. Run a task
uv run python run.py
```

---

## Architecture

```
Task Input
    ↓
generate_plan()          # LLM drafts step-by-step plan
    ↓
[Attempt Loop]
    ↓
generate_recovery_plan() # Reflexion: learns from previous failures
    ↓
Agent.run()              # browser-use agent executes
    ↓
guard_model_actions()    # intercepts each action before execution
  ├─ forbidden_actions   # blocks repeated failed signatures
  ├─ internal URL check  # recovers from edge:// / chrome:// pages
  └─ recovery injection  # forces alternative action on loop detection
    ↓
save_partial_results()   # saves progress if agent doesn't complete
    ↓
[Next Attempt or Done]
```

---

## Based on

- [browser-use](https://github.com/browser-use/browser-use) — MIT License, Copyright (c) 2024 Gregor Zunic
- [Ollama](https://ollama.com)
- [Gemma4](https://huggingface.co/google/gemma-4-27b-it) by Google
