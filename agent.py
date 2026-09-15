#!/usr/bin/env python3
"""
Zero-Bloat Autonomous Systems Engineering Agent
- Brain: Gemini 2.0 Flash (Fast, reliable free-tier)
- Sandbox: Root path confinement & dangerous command interception
- Safety: Ephemeral Git Checkpointing with 1-key rollback on abort/failure
- Pruning: Sliding-window memory management (drops stale compiler dumps)
- Worker: Local Ollama (Qwen2.5-Coder:0.5B) for log compression
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from google import genai
from google.genai import types
import ollama

PROJECT_ROOT = Path.cwd().resolve()
SESSION_FILE = PROJECT_ROOT / ".agent_session.json"

SYSTEM_INSTRUCTION = """You are an autonomous systems engineering agent.
Operate strictly inside the current workspace using ONLY the OPCODES below.
NEVER emit markdown commentary, conversational filler, or text outside opcode tags.

OPCODE LOOKUP TABLE:
1. READ <filepath>
   - Reads content of <filepath> directly. Use this instead of shell cat.

2. WRIT <filepath>
<content>
ENDFILE
   - Creates or completely overwrites <filepath>.

3. DIFF <filepath>
<<<< SEARCH
<exact existing lines to match>
====
<replacement lines>
>>>>
   - Surgically patches <filepath>. Keep search blocks compact (3-8 lines).

4. EXEC <command>
   - Executes a bash command (gcc, make, pytest, python3).

5. DONE <summary>
   - Emitted ONLY when the code compiles, tests pass, and work is complete.

CRITICAL OPERATIONAL RULES:
- If the workspace is empty/fresh, DO NOT run discovery commands (no ls, find, pwd, env). Emit WRIT immediately.
- Never run more than ONE inspection command (READ/ls) consecutively without writing code.
- Always run a build/test command immediately after modifying files.
"""

OPCODE_PATTERN = re.compile(
    r'(?:READ\s+(?P<read_path>[^\n]+))|'
    r'(?:WRIT\s+(?P<writ_path>\S+)\n(?P<writ_body>[\s\S]*?)\nENDFILE)|'
    r'(?:DIFF\s+(?P<diff_path>\S+)\n<<<< SEARCH\n(?P<search>[\s\S]*?)\n====\n(?P<replace>[\s\S]*?)\n>>>>)|'
    r'(?:EXEC\s+(?P<exec_cmd>[^\n]+))|'
    r'(?:DONE\s+(?P<done_msg>[\s\S]*?)$)',
    re.MULTILINE
)

DANGEROUS_PATTERNS = [
    r'\brm\s+-[rfRF]{1,2}\b',
    r'\brmdir\b',
    r'\bmkfs\b',
    r'\bdd\s+if=',
    r'>\s*/dev/sd[a-z]',
    r'>\s*/dev/nvme',
    r'\bchmod\s+-[rR]\s+777\b',
    r'\bchown\s+-[rR]\b',
    r':\(\)\s*\{\s*:\|:&\s*\};:',
    r'\bgit\s+reset\s+--hard\b',
    r'\bgit\s+clean\s+-[xXdf]+\b',
    r'\b(curl|wget)\b.*\|\s*(ba)?sh\b',
    r'\b(shutdown|reboot|poweroff)\b',
]
DANGEROUS_REGEX = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)

# ----------------- 1. GIT CHECKPOINTING -----------------
class GitCheckpoint:
    def __init__(self):
        self.is_git = (PROJECT_ROOT / ".git").exists()
        self.stash_commit = None

    def create(self):
        if not self.is_git:
            return
        # Silently record untracked and modified files
        res = subprocess.run(
            "git stash create --include-untracked",
            shell=True, capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        commit = res.stdout.strip()
        if commit:
            self.stash_commit = commit
            print(f"\033[90m[GIT CHECKPOINT]\033[0m Snapshot: {commit[:8]}\033[0m")

    def rollback(self):
        if not self.is_git:
            return
        print("\n\033[91m[ROLLBACK DETECTED]\033[0m")
        choice = input("Restore workspace to clean pre-run state? [y/N]: ").strip().lower()
        if choice == "y":
            subprocess.run("git clean -fd", shell=True, cwd=PROJECT_ROOT, capture_output=True)
            if self.stash_commit:
                subprocess.run(f"git stash apply {self.stash_commit} --quiet", shell=True, cwd=PROJECT_ROOT, capture_output=True)
            else:
                subprocess.run("git reset --hard HEAD --quiet", shell=True, cwd=PROJECT_ROOT, capture_output=True)
            print("\033[92m[RESTORED]\033[0m Workspace reverted to snapshot.")

# ----------------- 2. MEMORY & WORKSPACE -----------------
def load_project_memory() -> dict:
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"milestones": []}

def save_project_memory(summary_text: str, task: str):
    mem = load_project_memory()
    prompt = f"Task: {task}\nResult: {summary_text}\nWrite a 1-sentence technical record of what was implemented or modified."
    try:
        res = ollama.chat(
            model="qwen2.5-coder:0.5b",
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0}
        )
        record = res["message"]["content"].strip()
    except Exception:
        record = summary_text.strip().splitlines()[0]

    mem["milestones"].append(record)
    mem["milestones"] = mem["milestones"][-6:]
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2)

C_SIG_PATTERN = re.compile(
    r'(?:(?:typedef\s+)?struct\s+\w+\s*\{[^}]*\}\s*\w+;)|'
    r'(?:(?:typedef\s+)?enum\s+\w+\s*\{[^}]*\}\s*\w+;)|'
    r'(?:[\w\*]+\s+[\w\*]+\s*\([^;{)]*\)\s*;)|'
    r'(?:[\w\*]+\s+[\w\*]+\s*\([^;{)]*\)\s*\{)',
    re.MULTILINE
)
PY_SIG_PATTERN = re.compile(r'^(?:def|class)\s+[^\n:]+\:', re.MULTILINE)
IGNORED_DIRS = {".git", "build", "dist", ".venv", "venv", "__pycache__", "node_modules"}

def scan_workspace_symbols(root: Path) -> str:
    summary = []
    file_count = 0
    for path in sorted(root.rglob("*")):
        if any(ig in path.parts for ig in IGNORED_DIRS):
            continue
        if path.is_file() and path.suffix in [".h", ".c", ".py"]:
            file_count += 1
            if file_count > 25:
                summary.append("  [...remaining files omitted...]")
                break
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                sigs = []
                if path.suffix in [".h", ".c"]:
                    for m in C_SIG_PATTERN.findall(content)[:10]:
                        clean = " ".join(m.split()).rstrip("{").strip()
                        sigs.append(clean if clean.endswith(";") else clean + ";")
                elif path.suffix == ".py":
                    sigs = [m.strip() for m in PY_SIG_PATTERN.findall(content)[:10]]
                rel = path.relative_to(root)
                summary.append(f"- {rel}:\n    " + "\n    ".join(sigs) if sigs else f"- {rel}")
            except Exception:
                continue
    return "\n".join(summary) if summary else "[Empty workspace or no C/Python files found]"

# ----------------- 3. SECURITY & DIFF -----------------
def is_path_inside_root(target_str: str) -> tuple[bool, Path]:
    try:
        t = (PROJECT_ROOT / target_str).resolve()
        t.relative_to(PROJECT_ROOT)
        return True, t
    except (ValueError, Exception):
        return False, Path(target_str)

def scan_command_for_escapes(cmd: str) -> tuple[bool, str]:
    if re.search(r'(^|\s|\/)\.\.(\/|\s|$)', cmd):
        return False, "Directory traversal ('..') detected."
    for p in [r'(^|\s)(/(etc|var|usr|bin|sbin|boot|dev|home|root|opt|tmp))\b', r'(^|\s)~(/|\s|$)']:
        m = re.search(p, cmd)
        if m:
            return False, f"Attempted access to system path: '{m.group(0).strip()}'"
    return True, ""

def apply_diff(filepath: Path, search: str, replace: str) -> tuple[bool, str]:
    if not filepath.exists():
        return False, f"File {filepath.name} does not exist."
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if search in content:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content.replace(search, replace, 1))
        return True, f"Patched {filepath.name}."

    file_lines = content.splitlines(keepends=True)
    search_lines = search.splitlines(keepends=True)
    clean_file = [l.strip() for l in file_lines]
    clean_search = [l.strip() for l in search_lines if l.strip()]

    for i in range(len(clean_file) - len(clean_search) + 1):
        if [l for l in clean_file[i:i + len(clean_search)] if l] == clean_search:
            end_idx = i + len(search_lines)
            rep = replace if replace.endswith("\n") else replace + "\n"
            file_lines[i:end_idx] = [rep]
            with open(filepath, "w", encoding="utf-8") as f:
                f.writelines(file_lines)
            return True, f"Patched {filepath.name} (fuzzy whitespace match)."

    return False, f"Search block mismatch in {filepath.name}."

def filter_diagnostics(cmd: str, exit_code: int, raw_output: str) -> str:
    lines = raw_output.strip().splitlines()
    if exit_code == 0 or len(lines) <= 8:
        return raw_output.strip()

    prompt = f"""Command `{cmd}` failed with exit {exit_code}.
Terminal output:
{raw_output[:2500]}

Extract ONLY:
- Failing file path and line number
- Root error/syntax/linker issue
Do NOT provide code solutions. Output raw diagnostics only."""
    try:
        res = ollama.chat(
            model="qwen2.5-coder:0.5b",
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0}
        )
        return res["message"]["content"].strip()
    except Exception:
        return "\n".join(lines[-15:])

# ----------------- 4. LOCAL DISPATCHER -----------------
def execute_step(response_text: str, interactive: bool = False) -> tuple[str, bool, str]:
    feedback = []
    is_done = False
    done_summary = ""
    matches = list(OPCODE_PATTERN.finditer(response_text))

    if not matches:
        return "ERROR: No opcodes recognized. Emit READ, WRIT, DIFF, EXEC, or DONE.", False, ""

    for m in matches:
        g = m.groupdict()

        # READ (Fast in-memory read without shell overhead)
        if g["read_path"]:
            raw_path = g["read_path"].strip()
            ok, target = is_path_inside_root(raw_path)
            if not ok:
                feedback.append(f"SECURITY_BLOCKED: Cannot read '{raw_path}' outside project root.")
                continue
            if not target.exists():
                feedback.append(f"READ_FAIL: File '{target.name}' not found.")
                continue

            try:
                content = target.read_text(encoding="utf-8", errors="ignore")
                print(f"\033[94m[READ]\033[0m {target.relative_to(PROJECT_ROOT)} ({len(content)} bytes)")
                feedback.append(f"FILE CONTENT ({target.relative_to(PROJECT_ROOT)}):\n{content}")
            except Exception as e:
                feedback.append(f"READ_FAIL: {e}")

        # WRIT
        elif g["writ_path"]:
            raw_path = g["writ_path"].strip()
            ok, target = is_path_inside_root(raw_path)
            if not ok:
                feedback.append(f"SECURITY_BLOCKED: Cannot write to '{raw_path}' outside project root.")
                continue

            body = g["writ_body"]
            rel_path = target.relative_to(PROJECT_ROOT)

            if interactive:
                print(f"\n\033[93m[PROPOSED WRIT]\033[0m {rel_path} ({len(body)} bytes)")
                if input("Approve writing file? [Y/n]: ").strip().lower() == "n":
                    feedback.append(f"WRIT_BLOCKED {rel_path}: User rejected.")
                    continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(body)
            print(f"\033[94m[WRIT]\033[0m {rel_path} ({len(body)} bytes)")
            feedback.append(f"WRIT_OK {rel_path}")

        # DIFF
        elif g["diff_path"]:
            raw_path = g["diff_path"].strip()
            ok, target = is_path_inside_root(raw_path)
            if not ok:
                feedback.append(f"SECURITY_BLOCKED: Cannot patch '{raw_path}' outside project root.")
                continue

            rel_path = target.relative_to(PROJECT_ROOT)
            search_blk, replace_blk = g["search"], g["replace"]

            if interactive:
                print(f"\n\033[93m[PROPOSED DIFF]\033[0m {rel_path}")
                print("\033[91m--- SEARCH ---\033[0m\n" + search_blk)
                print("\033[92m+++ REPLACE +++\033[0m\n" + replace_blk)
                if input("Apply patch? [Y/n]: ").strip().lower() == "n":
                    feedback.append(f"DIFF_BLOCKED {rel_path}: User rejected.")
                    continue

            success, msg = apply_diff(target, search_blk, replace_blk)
            color = "\033[92m" if success else "\033[91m"
            print(f"{color}[DIFF]\033[0m {msg}")
            feedback.append(f"DIFF_OK {rel_path}" if success else f"DIFF_FAIL: {msg}")

        # EXEC
        elif g["exec_cmd"]:
            cmd = g["exec_cmd"].strip()
            safe_bounds, reason = scan_command_for_escapes(cmd)
            if not safe_bounds:
                feedback.append(f"EXEC_BLOCKED `{cmd}`: {reason}")
                continue

            is_dangerous = bool(DANGEROUS_REGEX.search(cmd))
            if interactive or is_dangerous:
                tag = "[HIGH-RISK COMMAND]" if is_dangerous else "[PROPOSED EXEC]"
                print(f"\n\033[91m{tag}\033[0m {cmd}")
                choice = input("Run command? [Y/n/edit]: ").strip().lower()
                if choice == "edit":
                    cmd = input("Enter replacement command: ").strip()
                elif choice == "n":
                    feedback.append(f"EXEC_BLOCKED `{cmd}`: User denied execution.")
                    continue

            print(f"\033[93m[EXEC]\033[0m {cmd}")
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=PROJECT_ROOT)
            raw_out = (res.stdout + res.stderr).strip()

            if res.returncode == 0:
                summary = raw_out if len(raw_out.splitlines()) < 6 else "\n".join(raw_out.splitlines()[:5]) + "\n[...passed]"
                feedback.append(f"EXEC_OK `{cmd}`:\n{summary}")
            else:
                filtered = filter_diagnostics(cmd, res.returncode, raw_out)
                print(f"\033[95m[0.5B FILTERED DIAGNOSTIC]\033[0m\n{filtered}")
                feedback.append(f"EXEC_FAIL `{cmd}` (exit {res.returncode}):\n{filtered}")

        # DONE
        elif g["done_msg"]:
            done_summary = g["done_msg"].strip()
            print(f"\n\033[92m[DONE]\033[0m {done_summary}")
            is_done = True

    return "\n\n".join(feedback), is_done, done_summary

# ----------------- 5. RESILIENT RUNNER & SLIDING WINDOW -----------------
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash-lite", "gemini-3.7-flash"]

def send_with_backoff(client, chat_session, message: str, current_model: str):
    delay = 2
    for attempt in range(1, 5):
        try:
            time.sleep(0.8)
            return chat_session.send_message(message), current_model, chat_session
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ["demand", "quota", "429", "503", "resource"]):
                print(f"\033[93m[BUSY]\033[0m {current_model} rate-limited. Waiting {delay}s...")
                time.sleep(delay)
                delay *= 2
            else:
                raise e

    for alt in FALLBACK_MODELS:
        if alt != current_model:
            try:
                print(f"\033[94m[SWITCHING]\033[0m Swapping brain to \033[1m{alt}\033[0m...")
                new_chat = client.chats.create(
                    model=alt,
                    config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
                )
                return new_chat.send_message(message), alt, new_chat
            except Exception:
                continue

    raise RuntimeError("All available models over capacity.")

def prune_chat_history(chat_session, keep_last_n_turns: int = 4):
    """
    Sliding Window Pruning: Keeps Turn 1 (Task + Symbols) and the most recent N turns.
    Drops intermediate turns containing resolved compiler error traces.
    """
    history = chat_session.get_history()
    # Each turn has a user part and a model part (2 messages per turn)
    total_messages = len(history)
    messages_to_keep = (keep_last_n_turns * 2) + 2  # Turn 1 (2) + Last N turns

    if total_messages > messages_to_keep:
        # Reconstruct: [Turn 1 User, Turn 1 Model] + [Last N User/Model pairs]
        pruned = [history[0], history[1]] + list(history[-(keep_last_n_turns * 2):])
        chat_session._history = pruned
        print(f"\033[90m[CONTEXT PRUNED]\033[0m Retained Turn 1 + last {keep_last_n_turns} turns.")

def run_task_loop(client, chat, current_model: str, task: str, interactive: bool):
    checkpoint = GitCheckpoint()
    checkpoint.create()

    memory = load_project_memory()
    mem_context = ""
    if memory["milestones"]:
        mem_context = "PROJECT MILESTONES (PREVIOUS RUNS):\n" + "\n".join(f"- {m}" for m in memory["milestones"]) + "\n\n"

    symbols = scan_workspace_symbols(PROJECT_ROOT)
    workspace_context = (
        "PROJECT STATUS: Fresh / Greenfield workspace (no existing C/Python files)."
        if symbols == "[Empty workspace or no C/Python files found]"
        else f"EXISTING WORKSPACE MAP & SIGNATURES:\n{symbols}"
    )

    current_input = f"{mem_context}{workspace_context}\n\nUSER TASK:\n{task}"
    print(f"\033[96m[TASK INITIATED]\033[0m {task}\n" + "="*50)

    consecutive_read_only = 0
    read_only_cmds = ("ls", "dir", "find", "pwd", "env", "printenv", "whoami")
    is_success = False

    try:
        for step in range(1, 16):
            print(f"\n--- Turn {step} (Model: {current_model}) ---")
            
            # Context window pruning to nmaintain sub-second latency
            if step > 5:
                prune_chat_history(chat, keep_last_n_turns=3)

            response, current_model, chat = send_with_backoff(client, chat, current_input, current_model)
            raw_text = response.text.strip()

            # Anti-paralysis check
            has_writes = "WRIT " in raw_text or "DIFF " in raw_text
            if has_writes:
                consecutive_read_only = 0
            elif any(raw_text.strip().startswith(f"EXEC {cmd}") for cmd in read_only_cmds) or "READ " in raw_text:
                consecutive_read_only += 1

            feedback, done, done_summary = execute_step(raw_text, interactive=interactive)

            if consecutive_read_only >= 2:
                feedback += "\n\nSYSTEM ALERT: Stop running discovery/read commands. Workspace state is known. Proceed immediately to WRIT or DIFF."

            if done:
                save_project_memory(done_summary, task)
                print("\033[92m[SAVED TO .agent_session.json]\033[0m")
                is_success = True
                break

            current_input = f"EXECUTION FEEDBACK:\n{feedback}"
        else:
            print("\033[91m[ABORT]\033[0m Reached maximum turn budget.")

    except KeyboardInterrupt:
        print("\n\033[93m[INTERRUPTED BY USER]\033[0m")
    finally:
        if not is_success:
            checkpoint.rollback()

    return chat, current_model

# ----------------- ENTRYPOINT -----------------
def main():
    parser = argparse.ArgumentParser(description="Zero-Bloat Autonomous Engineering Agent")
    parser.add_argument("task", nargs="*", help="Task to execute (leave empty for chat mode)")
    parser.add_argument("-i", "--interactive", action="store_true", help="Prompt before every WRIT, DIFF, and EXEC")
    args = parser.parse_args()

    client = genai.Client()
    current_model = "gemini-3.8-flash"
    chat = client.chats.create(
        model=current_model,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
    )

    print(f"\033[96m[WORKSPACE]\033[0m {PROJECT_ROOT}")

    if args.task:
        task_str = " ".join(args.task)
        chat, current_model = run_task_loop(client, chat, current_model, task_str, args.interactive)
    else:
        print("\033[92m[INTERACTIVE CHAT MODE]\033[0m Type your request or 'exit' to quit.")

    while True:
        try:
            followup = input("\n\033[1;34magent>\033[0m ").strip()
            if not followup:
                continue
            if followup.lower() in ["exit", "quit", "q"]:
                break
            chat, current_model = run_task_loop(client, chat, current_model, followup, args.interactive)
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

if __name__ == "__main__":
    main()
