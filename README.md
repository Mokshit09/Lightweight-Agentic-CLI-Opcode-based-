# Lightweight State Handlers for Persistent Coding Sessions

> A deterministic, token-efficient, zero-framework autonomous coding agent designed for fast compilation-fix loops in C and Python projects.

---

## 📌 Overview

Standard agentic frameworks incur significant token overhead (500–1500 tokens of JSON-RPC boilerplate per tool call) and execution latency [1, 17]. This project implements a lightweight, single-file autonomous engineering agent (`agent.py`, ~380 LOC) that eliminates framework dependencies by delegating strategic reasoning, local execution, and context compression across specialized components [17, 19, 20].

---

## 🏗️ Architecture & Core Components

```text
+-------------------------------------------------+
|          Gemini 2.0 Flash (Cloud Brain)         |
+-------------------------------------------------+
          ^ Filtered               | Plaintext
          | Diagnostics            | Opcodes
          | (~45 tokens)           | (READ, WRIT, DIFF, EXEC)
          v                        v
+-------------------------------------------------+
|       Python Regex Dispatcher & Sandbox         |
|        (Root Confinement & Security)            |
+-------------------------------------------------+
          ^ Subprocess             | Native Toolchain
          | Raw Output             | Execution
          | (150+ lines)           v
+-------------------------------------------------+
|              gcc / make / pytest                |
+-------------------------------------------------+
          | Failure
          v
+-------------------------------------------------+
|      Qwen2.5-Coder:0.5B (Local SLM Filter)      |
+-------------------------------------------------+
```

### 1. Plaintext Opcode Protocol
* Bypasses JSON-RPC schemas using a 75-token system lookup table (`READ`, `WRIT`, `DIFF`, `EXEC`, `DONE`) [2].
* Parses model responses in $<1\text{ ms}$ using a native compiled regular expression [2, 17].
* Features resilient API loop handling with exponential backoff, model hot-swapping (`gemini-2.0-flash` → `gemini-2.0-flash-lite` → `gemini-1.5-flash`), and turn pacing [3].

### 2. Local Sandbox & Command Security
* **Filesystem Traversal Confinement:** Anchors execution strictly within `PROJECT_ROOT`, blocking parent directory access (`../../etc/shadow`) [5, 6].
* **High-Risk Command Interception:** Intercepts destructive shell operations (`rm -rf`, `mkfs`, raw partition writes) via regex pattern matching with a Human-in-the-Loop interactive gate [6, 7].

### 3. Diagnostic Compression with Local 0.5B SLM
* Integrates `qwen2.5-coder:0.5b` via Ollama as an inline error filter [9].
* Distills 150+ lines (~2,500 tokens) of noisy compiler stderr into ~3 lines (~45 tokens) of targeted diagnostics [9].
* Operates with $<800\text{ MB}$ VRAM/RAM footprint at ~250–400 ms latency [10].

### 4. Memory, Context Pruning & Git Safety
* **Symbol Regex Scanner:** Performs a $<50\text{ ms}$ regex pass on `.h`, `.c`, and `.py` files on Turn 1 to extract top-level prototypes (~150–350 tokens) for instant surgical patching [12, 13].
* **Sliding Window Pruner:** Retains Turn 1 task/symbols alongside the latest $N$ turns after Turn 5, discarding resolved error outputs [13].
* **Persistent Project Memory:** Appends compressed milestone records to `.agent_session.json` (capped at 6 items) without vector database complexity [14].
* **Ephemeral Git Snapshots:** Stashes workspace state (`git stash create --include-untracked`) before execution, enabling automated rollback on failure or abort [14, 15].

---

## 📊 Performance Benchmarks

| Metric / Dimension | Basic Calculator | Web Scraper | Multi-File C HTTP Server |
| --- | --- | --- | --- |
| **Language** | Python | Python | C (C99, POSIX Sockets) |
| **Files Managed** | 2 | 2 | 14 (.c, .h, Makefile) |
| **Lines of Code** | ~80 | ~120 | ~800 |
| **Turns / Tokens** | 1 / 3.0K tokens | 1 / 5.1K tokens | 6–8 / 36.9K tokens |
| **API Cost (Flash Tier)** | $0.0001 | $0.0002 | $0.0123 (or $0 on free tier) |
| **Terminal Outcome** | ✅ Passed tests | ✅ Passed tests | ✅ Compiled -Wall, tests passed |

---

## 💻 Architectural Comparison

| Feature | Hybrid Agent (`agent.py`) | LangChain / AutoGen | OpenCode / Odysseus |
| --- | --- | --- | --- |
| **Turn Overhead** | ~75 tokens (Opcodes) [17] | 500–1,500 tokens [1] | 1,000–3,000 tokens [17] |
| **Execution Latency** | $<1\text{ ms}$ (Native regex) [17] | 150–300 ms [17] | Variable (Docker overhead) [17] |
| **Compiler Error Dumps** | Compacted via 0.5B model [9, 17] | Full raw dump [17] | Full raw dump [17] |
| **Rollback Mechanism** | Ephemeral git snapshot [15, 17] | None (Manual) [17] | Container restart [17] |
| **Footprint** | Single file (~380 LOC) [17] | 100+ dependencies [17] | Multi-container Docker [17] |

---

## ⚙️ Requirements & Setup

1. **Python Runtime:** Python 3.10+
2. **Local SLM:** [Ollama](https://ollama.ai) running `qwen2.5-coder:0.5b` [9]
3. **Build Toolchain:** `gcc`, `clang`, `make`, `pytest`, `git` [4, 14]
