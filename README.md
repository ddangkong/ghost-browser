# Ghost Browser by Gemma

> **Free, local browser automation agent** powered by Gemma4 27B + Ollama —  
> ranked **3rd in our 79-task Stealth Bench V1 run**, competitive with several paid cloud browser providers.

> **Experimental local browser automation runner evaluated on Browser-Use Stealth Bench V1.**  
> Results are environment-dependent and not a guarantee of bypassing any specific protection.

---

## Stealth Bench V1 Results

| Rank | Agent | Tasks | Passed | Accuracy | Type |
|------|-------|------:|-------:|--------:|------|
| 1 | browser-use-cloud | 80 | 58 | **73%** | ☁️ Paid cloud |
| 2 | anchor | 80 | 55 | **69%** | ☁️ Paid cloud |
| **3** | **Ghost Browser (Gemma4)** | **79** | **53** | **67.1%** | **💻 Local / Free** |
| 4 | onkernel | 80 | 53 | **66%** | ☁️ Paid cloud |
| 5 | browserless | 80 | 42 | **52%** | ☁️ Paid cloud |
| 6 | local_headful (bu-2-0) | 80 | 40 | **50%** | 💻 Local |
| 7 | steel | 80 | 39 | **49%** | ☁️ Paid cloud |
| 8 | browserbase | 80 | 32 | **40%** | ☁️ Paid cloud |
| 9 | hyperbrowser | 80 | 28 | **35%** | ☁️ Paid cloud |
| 10 | local_headless | 80 | 2 | **3%** | 💻 Local |

**67.1% with a free local model — no API key, no subscription, no cloud dependency.**

> Note: other agents ran 80 tasks; our run completed 79. Results are from the same benchmark suite under the same judge LLM, but are not perfectly matched conditions.

### Reproducibility

| Field | Value |
|-------|-------|
| Model | gemma4:latest (27B) via Ollama |
| Browser | local_headful (Chromium, persistent profile) |
| OS | Windows 11 |
| Date | 2026-05-10 |
| Tasks run | 79 / 80 |
| Judge LLM | gemini-2.5-flash (Google AI Studio) |
| Per-task timeout | 1800s |
| Ollama context | 262144 tokens |
| Parallel tasks | 3 |

---

## Failure Analysis (26 failed tasks)

Being transparent about what failed and why:

| Category | Count | Sites | Fixable? |
|----------|------:|-------|---------|
| Press & Hold antibot | 13 | walmart, wayfair, yeti, fiverr, samsclub… | Custom JS mouse-hold action needed |
| LLM timeout | 5 | x.com, reddit, zillow, bloomberg, lianjia | Retry logic / faster hardware |
| Cloudflare / Access Denied | 3 | gamestop, homedepot, crocs | Profile warmup may help |
| Geo-block (Korean IP) | 2 | sephora, williams-sonoma | US proxy/VPN |
| Site down during test | 2 | belk, davidjones | Not our fault |
| Login popup blocking content | 1 | douyin | Task-specific handling |

**Rough upper bound** (if each failure category were independently resolved): timeouts → ~73%, Press & Hold → ~90%+. These are estimates, not guarantees.  
Full per-task breakdown in [`benchmark_results.json`](benchmark_results.json).

---

## What is Ghost Browser?

Ghost Browser is a customized [browser-use](https://github.com/browser-use/browser-use) agent harness that runs entirely on your local machine:

- **Gemma4 27B** via Ollama — no API key, no cost
- **Reflexion-style retry loop** — learns from failed attempts, avoids repeating mistakes
- **Guard layer** — intercepts bad actions before execution, blocks repeated failures
- **Auto-recovery** — detects internal browser pages (edge://, chrome://) and navigates back
- **Persistent browser profile** — reduces bot detection fingerprint over time
- **Partial result saving** — saves progress even if the agent doesn't fully complete a task

This is a **research and benchmarking tool**, not a commercial scraping or bypass service.

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- [Ollama](https://ollama.com) with `gemma4` model pulled

---

## Setup

```bash
# 1. Install Ollama and pull Gemma4
ollama pull gemma4

# 2. Clone and install
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
generate_plan()           # LLM drafts step-by-step plan
    ↓
[Attempt Loop]
    ↓
generate_recovery_plan()  # Reflexion: learns from previous failures
    ↓
Agent.run()               # browser-use agent executes
    ↓
guard_model_actions()     # intercepts each action before execution
  ├─ forbidden_actions    # blocks repeated failed signatures
  ├─ internal URL check   # recovers from edge:// / chrome:// pages
  └─ recovery injection   # forces alternative action on loop detection
    ↓
save_partial_results()    # saves progress if agent doesn't complete
    ↓
[Next Attempt or Done]
```

---

## Join the Development

We're sharing this as an open experiment in local LLM browser automation.  
**Gemma4 is getting better fast** — and we want to grow this together.

If you're interested in:
- Improving challenge interaction handling (Press & Hold, browser compatibility)
- Trying with other local models (Mistral, Llama, Qwen…)
- Building a UI layer (chat interface + live browser view)
- Running benchmarks on your own hardware and comparing results
- Packaging as an MCP server for Claude / Cursor integration

Feel free to open an issue, submit a PR, or just share your benchmark results.  
Every run on different hardware/models adds to the picture.

---

## Known Limitations

1. **SEC EDGAR / DART XBRL viewer** → empty DOM (iframe structure, no visible content)
2. **Press & Hold challenges** → browser-use doesn't support mouse-hold natively
3. **Repeated extraction loops** — harness-injected `extract` not always blocked by forbidden_actions
4. **Performance is hardware-dependent** — Gemma4 27B needs a capable GPU or fast CPU

---

## Based on

- [browser-use](https://github.com/browser-use/browser-use) — MIT License, Copyright (c) 2024 Gregor Zunic
- [Ollama](https://ollama.com)
- [Gemma4](https://huggingface.co/google/gemma-4-27b-it) by Google
