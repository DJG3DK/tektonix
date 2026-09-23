"""build_deep_agent -- per-project, per-task factory for the deepagents-based
work engine. Returns a CompiledStateGraph, invoked manually (not nested as a
native subgraph -- the outer AgentState and this graph's own state share no
keys) by the outer "work" node.
"""

import json
import logging
import subprocess
import warnings
from dataclasses import dataclass, field

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.store.base import BaseStore

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

from agent.config import Config, PROJECTS
from agent.frontend_route import CODER_ROLE
from agent.tools.github_tools import make_github_inbox_tool, make_github_tools, token_source
from agent.memory_freshness import memory_with_freshness
from agent import episode_recall
from agent import memory_sections
from agent import new_files

# langchain-openai cannot attach response headers on the structured-output
# stream path (with_structured_output sets response_format) and warns on every
# such call. Those calls are metered at their estimate, which is the documented
# fallback -- the warning adds nothing per call.
warnings.filterwarnings(
    "ignore", message="Cannot currently include response headers when response_format is specified"
)
from agent import runtime_settings as _rs
from agent.middleware.hidden_tools import HiddenToolsMiddleware
from agent.middleware.repeat_guard import RepeatCallGuardMiddleware
from agent.middleware.sanitize_tool_calls import SanitizeToolCallsMiddleware
from agent.middleware.budget_guard import BudgetMeterCallback, BudgetGuardMiddleware, BudgetTracker
from agent.middleware.model_pin import PlanCodeModelMiddleware
from agent.middleware.todo_nag import StaleTodoMiddleware
from agent.store_paging import all_items
from agent.tools.agent_tools import make_agent_tools
from agent.tools.project_db import make_project_db_tool
from agent.tools.checks import run_all_checks

# Three-tier memory:
#
#   /memories/AGENTS.md   -- semantic, per-project, agent-writable during
#                            normal work (durable facts about this repo).
#   /org-memory/AGENTS.md -- semantic, cross-project, read-only to every task
#                            agent (enforced via `permissions`, not just
#                            prompting -- this system doesn't trust
#                            instruction-following alone for anything that
#                            matters). Populated by application code only:
#                            one task's agent should never be able to alter
#                            what every other project's agent believes.
#   /episodes/{repo}/...  -- structured records of past task outcomes
#                           (goal, result, cost, escalation reason if any),
#                           written by verify_and_ship at terminal state, not
#                           auto-loaded into every task's context (that would
#                           defeat the point of keeping the hot path small).
#                           Read only by the consolidation agent
#                           (agent/consolidation.py), which distills them
#                           into /memories/AGENTS.md updates on a schedule --
#                           background consolidation, not the agent editing
#                           its own memory ad hoc as the only mechanism.
MEMORY_PATH = "/memories/AGENTS.md"
ORG_MEMORY_PATH = "/org-memory/AGENTS.md"
ORG_NAMESPACE = ("org",)
EPISODES_ROUTE = "/episodes/"

# Skills -- deepagents' progressive-disclosure mechanism, distinct from
# memory: memory is small, durable, always-relevant facts (loaded in full,
# every task); skills are large, situational domain knowledge (a subsystem's
# real architecture, a non-obvious integration's rules) that would bloat
# every task's context if treated as memory, so only a one-line
# name+description loads by default -- the agent reads a skill's full
# SKILL.md via its own read_file tool only when a task actually touches that
# area. Per-project namespace (skills are repo-specific domain knowledge,
# not cross-project policy like org-memory). Read-only to the agent, same
# reasoning as org-memory: skills are curated reference material, updated
# deliberately (seed_skill), not something that should drift from an
# agent's own mid-task edits.
SKILLS_ROUTE = "/skills/"
SKILLS_MANIFEST_PATH = "/skills/_manifest.json"

# Memory, once it is too big to carry whole: the same progressive disclosure,
# one level down. /memories/sections/_index.json + /memories/sections/<slug>.md
# hold what /memories/AGENTS.md used to hold alone, and agent/memory_sections.py
# decides which of them stay resident. Nothing here exists until a project's
# memory has actually been split -- with no index present every read below
# falls back to the whole file, which is what this system did before and what
# it keeps doing for a project small enough not to need any of this.
SECTIONS_INDEX_PATH = memory_sections.SECTIONS_INDEX_PATH
SECTIONS_CORE_PATH = memory_sections.SECTIONS_CORE_PATH

logger = logging.getLogger("tektonix")

# Real per-call cost via BudgetGuardMiddleware makes SummarizationMiddleware's
# own trigger about context quality, not cost -- the budget ceiling already
# owns cost. Trigger well before a model's real context limit so summarization
# is a normal, unremarkable event, not a last-resort save. `keep` mirrors the
# library default (last 20 messages kept verbatim after summarizing).
# Tuned alongside READ_INLINE_CAP_CHARS (agent_tools.py): a whole large source
# file can arrive inline, so the trigger needs enough headroom to admit one
# such read without churning the summarizer immediately afterward. Every
# pinned model has >=262K context, so trading some headroom for stability is
# cheap -- context quality owns this trigger, cost never did (BudgetGuard
# owns cost).
# Tokens ONLY. There used to be an OR'd ("messages", 120) clause, and it is the
# exact degenerate case described below: `keep` is 30k TOKENS, and 30k tokens
# of short tool calls is easily more than 120 messages. Observed 2026-09-09 on
# task 828ca1d9 (webapp): every compaction preserved 139 messages, so the
# message clause was true again on the very next call, and summarization fired
# before EVERY model call for over an hour -- a 3.5k-token summary regenerated
# each step, plus a failed primary-summarizer attempt each step, while the
# coordinator's own context never dropped below ~50k. Progress continued, at
# roughly twice the cost and latency per step. A message-count clause can only
# be safe against a message-count keep; with a token keep it must not exist.
# Operator-tunable since 2026-09-14 (runtime_settings' "Context" group). The
# fixed 80_000 below was 7.6% of the 1,048,576-token window the coder model
# actually has, and a real task rode it for an hour -- climb to 80k, compact to
# ~55k, climb again, 15 summarizer calls and zero lines written, because each
# compaction discarded the files it had just read. These two remain the
# FLOOR-level defaults the knobs start from; everything below about why the
# units must match and why keep must stay well under trigger still applies.
def summarization_trigger() -> list[tuple[str, int]]:
    return [("tokens", _rs.as_int("summarization_trigger_tokens"))]


def summarization_keep() -> tuple[str, int]:
    """Clamped to 60% of the trigger.

    If the kept window ever approaches the trigger, summarization fires before
    EVERY model call and can never get back under it -- the task keeps paying
    for a summarizer each turn while making no progress. That is a degenerate
    state a pair of independently-set knobs can reach by accident, so it is
    forbidden here rather than documented.
    """
    trigger = _rs.as_int("summarization_trigger_tokens")
    keep = _rs.as_int("summarization_keep_tokens")
    return ("tokens", min(keep, int(trigger * 0.6)))


SUMMARIZATION_TRIGGER = [("tokens", 80_000)]
# Retention is expressed in TOKENS, deliberately matching the unit the trigger
# above uses. It was ("messages", 20), and a message-count keep against a
# token-count trigger is a unit mismatch with no relationship between the two:
# nothing bounds how much a summarization actually reclaims, because 20
# messages can be 8k tokens or 70k depending purely on how big the recent tool
# results happen to be.
#
# That is not theoretical. Measured on a real planning session (2026-08-27) at
# the exact state that fired summarization: 28 messages / 73_068 tokens, of
# which the six largest were ~10k tokens each (inline file reads). Keeping 20
# messages preserved 61_695 tokens -- it reclaimed 11k of 73k, 15%, and left
# 18k of headroom under an 80k trigger. Roughly two more file reads and it
# fired again. The session's observed cycle was: summarize, 2-3 tool calls,
# summarize, 2-3 tool calls. Two summarizations inside twelve minutes, both
# doing real work and neither buying meaningful room.
#
# The degenerate end of that mismatch is worse than churn: if the kept window
# ever exceeds the trigger on its own, summarization fires before EVERY model
# call and can never get back under, so the conversation stops making progress
# while still paying for a summarizer call each turn.
#
# 30k against an 80k trigger guarantees >=50k reclaimed every time (measured on
# the same state: 12 messages / 26_163 tokens kept, 53_837 of headroom -- about
# eight large reads instead of two). It also lowers the average context a
# planning call carries, which is where the real money goes: kimi-k3 input is
# $3/M, so every 10k tokens of avoided context is ~$0.03 off every single call
# in the conversation.
#
# Caveat worth knowing: the library keeps at least one message, so a SINGLE
# message larger than this budget still lands above it (the binary search in
# _find_token_based_cutoff cannot cut inside a message). Planning reads are
# capped at 40k chars (~12k tokens) so they cannot breach it; agent_tools.read
# offloads above READ_INLINE_CAP_CHARS for the same reason.
SUMMARIZATION_KEEP = ("tokens", 30_000)

# Planning gets a much larger window than a build task, because it is a
# READ-heavy investigation loop rather than an edit loop, and the 80k/30k pair
# above turned out to be actively harmful there.
#
# Measured on session 0616917 (2026-08-31), a HARD planning turn that ran
# 1h52m and died on its $8 ceiling having produced NO plan at all:
#
#   123 model calls, 183 tool calls, 184 summarizations
#   1,080 file reads across 99 DISTINCT files
#   src/core/bot.js read 129 times, config/pairs.json 103, gridBot.js 69
#
# That is one summarization per ~1.5 model calls. The mechanism is the gap
# between the two numbers, not either one alone: 80k trigger minus 30k keep is
# 50k of headroom, and a planning read is capped at 40k chars (~12k tokens),
# so THREE big reads refill it. The summarizer then drops the file contents,
# the model no longer has what it just read, and it reads it again -- forever,
# at ~$0.12 a round, until the budget guard stops it.
#
# The question that turn was asked spanned a strategy module, an API route and
# two frontend components. It could never hold all four at once, so it could
# never answer. 170k/70k gives 100k of headroom (~8 large reads) and leaves
# ~90k of slack under the >=262K context every pinned model has -- enough for
# the triggering call and its response. Cost per call rises with context; that
# is the intended trade, and it is far cheaper than reading bot.js 129 times.
PLANNING_SUMMARIZATION_TRIGGER = [("tokens", 170_000)]  # tokens only -- see SUMMARIZATION_TRIGGER
PLANNING_SUMMARIZATION_KEEP = ("tokens", 70_000)
# The library default (4000) is tuned for ordinary back-and-forth chat, where a
# HumanMessage recurs often. Our conversations are tool-call-heavy: one
# HumanMessage with the goal, then dozens of AIMessage/ToolMessage pairs before
# the next one. SummarizationMiddleware's own trim step (trim_messages with
# strategy="last", start_on="human") requires a HumanMessage to anchor the
# trimmed batch -- with a small token budget, a batch that's mostly tool pairs
# can have no HumanMessage in that window, so trim_messages returns an empty
# list. The library's own fallback for that case is to silently return the
# literal string "Previous conversation was too long to summarize." as the
# entire prior context, with no retry and no error -- confirmed to wipe the
# goal and prior findings from context on a real task.
#
# A bigger trim budget doesn't fix this: a regression test
# (tests/test_summarization_trim.py) confirms that raising it still produces
# an empty trim on the same failure shape, because the sole HumanMessage can
# sit further back than any finite budget reaches if the tool-heavy stretch
# after it is long enough. The trim step exists to bound the summarizer
# call's own input size -- but the trigger already bounds the untrimmed batch
# to roughly its own threshold (it is tokens-only, see summarization_trigger,
# and fires the moment the token count crosses it, so total tokens at fire
# time can't run away past that), comfortably inside any modern model's
# context window. That bound depends on the trigger staying tokens-only: the
# message-count clause removed after task 828ca1d9 must not return. So the
# trim step is redundant for our shape and only adds a failure mode --
# disable it outright (None skips trimming entirely) rather than trying to
# out-guess a budget that has no safe value for an unbounded-distance-to-
# last-HumanMessage conversation shape.
SUMMARIZATION_TRIM_TOKENS = None

# Defense-in-depth against a runaway loop that BudgetGuardMiddleware alone
# wouldn't catch -- a stuck loop making many near-zero-cost calls (routed to
# a cheap model tier) could burn enormous wall-clock/API-call-count before
# ever crossing the dollar ceiling. Deliberately generous (well above any
# normal task's real call count) so this never fires on legitimate work,
# only genuine runaway pathology. `run_limit` (per single astream_events()
# invocation, i.e. per outer "work" pass), not `thread_limit` -- a
# thread_limit would persist across every resume of a long task's whole
# lifetime, which doesn't map onto anything meaningful here the way it would
# for a genuinely single-shot agent. exit_behavior="error" (not "end"/
# "continue") so this surfaces as a real exception routed through
# work_node's existing generic `except Exception` handler -> a clear
# escalation, not a silently-truncated response that could get misread as a
# normal completion.
# The numbers themselves now live in runtime_settings (Settings -> Runtime
# limits) so they can be retuned without an edit and a restart; the reasoning
# above is why they are shaped this way, and still applies. Read via
# _rs.as_int("model_call_run_limit") / ("tool_call_run_limit") at the point of
# use -- deliberately not re-declared here as constants, because a constant
# that no longer drives anything is a trap for the next person to edit it.

# Human-in-the-loop pre-execution approval gate. This system's only other
# safety layers are the budget ceiling and the post-hoc review-service gate
# (after code already ran). deepagents' own HumanInTheLoopMiddleware (wired
# in below via create_deep_agent's `interrupt_on=` param, backed by
# LangGraph's native interrupt()/Command(resume=...)) pauses before a
# specific tool call executes, when it matches a `when` predicate here.
#
# Deliberately narrow, not "approve every tool call" (that would make this
# system unusable -- a real task makes dozens of tool calls). Scoped to
# what the sandboxing fix (agent/tools/sandbox.py) does not already cover:
# Docker isolation bounds `bash`'s blast radius to the repo checkout, but
# genuinely sensitive files (a project's own auth/config, .env, .git
# internals, CI/deploy config) live inside that same checkout -- the sandbox
# boundary doesn't protect a repo from itself. Also gates recognizably
# destructive git/shell patterns (force-push, rm -rf, sudo) regardless of
# path, since those are dangerous even scoped to one repo.
#
# Applied to the coordinator and both subagents (below) -- not just the
# coordinator -- because investigator, despite its own system prompt's
# "read-only" framing, is given the full `bash` tool (needed for real
# find/grep-across-the-tree exploration; there's no separate read-only-
# shell primitive in this system yet) and could otherwise run something
# destructive with zero gate at all. Two decisions only (approve/reject),
# not edit/respond -- keeps the operator-facing payload and the dashboard
# UI simple; a rejected call gets a clear message back to the model instead
# of silently vanishing.
_SENSITIVE_PATH_MARKERS = (
    "config/", "auth.json", ".env", ".git/", "secret", "credential",
    "deploy", ".github/workflows", ".ssh",
    # audit C-2: the check runner executes `npm run <script>` and the reviewer
    # runs install lifecycle scripts, all defined in these files -- which the
    # agent can rewrite with its own `write`/`edit` tool. Until now that write
    # was ungated (matched no marker), so a rewritten test/build/install script
    # was an unreviewed path to host code execution. Editing any of them now
    # requires operator approval in strict mode, the same as touching a .env.
    # Lowercase (both predicates lowercase before matching).
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pnpm-workspace.yaml", "vitest.config", "jest.config", "playwright.config",
    "tsconfig", ".npmrc", "makefile", "dockerfile",
)
# MUST be lowercase: both predicates below lowercase the command before
# matching, so an uppercase character in a marker can never match anything.
# "chmod -R"/"chown -R" were written with a capital R and therefore silently
# never gated a single recursive chmod/chown from the day they were added --
# found 2026-08-24 by the auto-approve gate's own tests. Keep this list
# lowercase, and let tests/test_auto_approve_gate.py catch it if that slips.
_DANGEROUS_COMMAND_MARKERS = (
    # "> /dev/" removed from this substring list -- the regex below owns that
    # case now, because a substring cannot express the needed exception.
    "rm -rf", "git push", "sudo ", "chmod -r", "chown -r", ":(){ ",
)


# audit M-7: the substring matchers below were trivially evadable -- `rm  -rf`
# (two spaces), `rm -fr` (flag order), a tab instead of a space all slipped past.
# The list can't be made sound by extension, and it no longer needs to be: C-2
# made the Docker sandbox the real boundary (bash runs in an isolated container
# that cannot see the host, other repos, or any secret, and whose only mutable
# state is a throwaway worktree copy). This gate is now a footgun safety-net --
# it catches the obvious destructive command so an operator gets a confirm
# prompt -- not the security boundary. Normalizing whitespace and matching a few
# regexes for flag-order variants makes the net catch the demonstrated evasions.
_WS_RUN = __import__("re").compile(r"\s+")


def _normalize_command(command: str) -> str:
    return _WS_RUN.sub(" ", command.lower()).strip()


import re as _re
_DANGEROUS_COMMAND_PATTERNS = (
    _re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"),  # rm -rf / -fr / -Rf ...
    _re.compile(r"\bgit\b.*\bpush\b.*(--force|-f\b|\+)"),  # force push (any -c ... prefix)
    _re.compile(r"\bsudo\b"),
    _re.compile(r"\bchmod\s+-[a-z]*r|\bchown\s+-[a-z]*r"),  # recursive chmod/chown
    # Writing to a raw DEVICE (> /dev/sda) is destructive. Redirecting to the
    # null/stream devices is not -- and `2>/dev/null` is the single most
    # common idiom in shell, so matching it made EVERY quiet command prompt
    # for approval, auto-approve mode included (live 2026-08-27: a build's
    # plain `ls ... 2>/dev/null` and `find ... 2>/dev/null` both gated as
    # "destructive"; the operator asked why auto-approve wasn't working).
    _re.compile(r">\s*/dev/(?!null\b|zero\b|stdout\b|stderr\b|tty\b|fd/)"),
    # dd writing to a raw device -- was never caught (no `>` involved), found
    # while fixing the redirect pattern above.
    _re.compile(r"\bof=/dev/(?!null\b|zero\b)"),
    _re.compile(r":\(\)\s*\{"),  # fork bomb
    _re.compile(r"\bfind\b.*-delete"),
)

# `ln -s`, `ln --symbolic`, and the same via env/xargs. Word-boundary anchored
# so "align -symbolic" or a filename containing "ln -s" does not trip it.
_SYMLINK_RE = _re.compile(r"(^|[;&|]\s*|\s)ln\s+(-[a-zA-Z]*s[a-zA-Z]*|--symbolic)\b")


def _matches_dangerous(command: str) -> bool:
    norm = _normalize_command(command)
    if any(marker in norm for marker in _DANGEROUS_COMMAND_MARKERS):
        return True
    return any(p.search(norm) for p in _DANGEROUS_COMMAND_PATTERNS)


def _bash_needs_approval(req) -> bool:
    raw = str(req.tool_call["args"].get("command", ""))
    norm = _normalize_command(raw)
    if any(marker in norm for marker in _SENSITIVE_PATH_MARKERS):
        return True
    if _bash_creates_a_symlink(norm):
        return True
    return _matches_dangerous(raw)


def _bash_creates_a_symlink(command: str) -> bool:
    """audit C2/H2: `ln -s <anything> node_modules` inside the worktree decides
    what the HOST bind-mounts into the next container. sandbox.py now refuses
    targets outside the project's own paths, so this is defence in depth --
    but a symlink whose target the agent chose is worth surfacing, and it
    matched no marker before."""
    return bool(_SYMLINK_RE.search(command))


# ---------------------------------------------------------------------------
# What auto mode still stops for: a deletion that loses real work.
#
# 2026-09-11, operator report: auto mode interrupted about every 30 seconds.
# The predicate it used (_bash_is_destructive) fires on any `rm -rf`, and the
# call that kept firing was `cd /tmp && rm -rf u && mkdir u && ...` -- scratch
# space inside a container, thrown away when the command ends. A prompt for
# that is noise, and noise is what makes an operator stop reading prompts.
#
# bash NEVER runs on the host: agent_tools.py sends every command through
# run_shell_sandboxed -- a per-command Docker container with all Linux
# capabilities dropped, no-new-privileges, a pid cap, no credentials in its
# environment (no SSH key, no router key, no GitHub token), and exactly one
# writable mount: the task's throwaway worktree at /workspace. sudo cannot
# escalate there, a push cannot authenticate, a fork bomb hits the pid cap and
# a raw-device write has no device to reach. Those markers still gate in
# strict mode, where they read as "look at this"; in auto mode the container
# has already answered them.
#
# What the container does not answer is the loss of work: the worktree holds
# the task's own edits, and a delete that reaches the repo -- or anything
# outside scratch -- throws away state no revert brings back. That is the one
# class the operator asked to keep, and it is the only one auto mode stops for.
_SCRATCH_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm")

# Names whose contents a build regenerates. Deleting one is a clean step, not
# a loss: `rm -rf dist && npm run build` is ordinary work between check runs.
_REGENERABLE_NAMES = frozenset({
    "node_modules", "dist", "build", "out", ".next", ".nuxt", ".svelte-kit",
    ".cache", ".turbo", ".parcel-cache", ".vite", "coverage", ".nyc_output",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".eggs", ".gradle", ".venv", "venv", "tmp", ".tmp", ".output",
    "tsconfig.tsbuildinfo", ".ds_store",
})

# sandbox.py runs every command with `-w /workspace` (the worktree mount), so
# a relative target resolves against that unless the command cd's first.
_SANDBOX_CWD = "/workspace"

_SEGMENT_SPLIT = _re.compile(r"\|\||&&|[;|\n()]")
_DELETE_COMMANDS = ("rm", "rmdir", "shred", "unlink")
# Marks a loss entry that is not a path to look up: a shell variable, an
# unparseable segment, `rm` with only flags, a wholesale `git clean -f`.
# Those always ask, whatever git says about the worktree.
_ALWAYS = "\x00"
_UNPARSEABLE = _ALWAYS + "unparseable"


def _segments(command: str) -> list[list[str]]:
    """The command split into simple segments, each tokenised. A segment that
    will not tokenise comes back as one unparseable token, so a caller fails
    closed on it instead of skipping past it."""
    import shlex
    out: list[list[str]] = []
    for raw in _SEGMENT_SPLIT.split(command):
        raw = raw.strip()
        if not raw:
            continue
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = [_UNPARSEABLE]
        if tokens:
            out.append(tokens)
    return out


def _resolve(target: str, cwd: str) -> str:
    import posixpath
    if target.startswith("~"):
        return posixpath.normpath("/root" + target[1:])      # $HOME in the image
    if not posixpath.isabs(target):
        target = posixpath.join(cwd, target)
    return posixpath.normpath(target)


def _target_is_scratch(target: str, cwd: str) -> bool:
    """True when deleting this path loses nothing -- container scratch space,
    or something a build regenerates. Anything uncertain is not scratch."""
    if not target or any(ch in target for ch in "$`"):
        return False                                         # a variable: cannot judge it
    resolved = _resolve(target, cwd)
    segments = [seg.lower() for seg in resolved.split("/") if seg not in ("", ".")]
    if not segments or ".." in segments:
        return False                                         # "/" itself, or a path climbing out
    if ".git" in segments:
        return False                                         # history is work
    for root in _SCRATCH_ROOTS:
        root_segments = [seg for seg in root.split("/") if seg]
        if segments[:len(root_segments)] == root_segments and len(segments) > len(root_segments):
            return True                                      # inside scratch, never the root itself
    return any(seg in _REGENERABLE_NAMES for seg in segments)


def _deletions_that_lose_work(command: str) -> list[str]:
    """Targets of this command's deletions that are not scratch. Empty means
    nothing of value is being deleted. A target that cannot be read (a shell
    variable, an unparseable segment, `rm` with only flags) is reported, so
    the caller asks about it rather than assuming."""
    cwd = _SANDBOX_CWD
    losses: list[str] = []

    def _record(targets: list[str]) -> None:
        for t in targets:
            if any(ch in t for ch in "$`") or t.startswith(_ALWAYS):
                losses.append(_ALWAYS + t)                    # unreadable: ask, never resolve
                continue
            if _target_is_scratch(t, cwd):
                continue
            resolved = _resolve(t, cwd)
            if ".git" in resolved.lower().split("/"):
                # git cannot report on its own directory -- ls-files lists
                # nothing under .git, so the tracked-content check would wave
                # a `rm -rf .git` straight through. Always ask.
                losses.append(_ALWAYS + resolved)
            else:
                losses.append(resolved)

    for tokens in _segments(command):
        head = tokens[0].rsplit("/", 1)[-1]
        if head == "cd" and len(tokens) > 1 and not any(ch in tokens[1] for ch in "$`"):
            cwd = _resolve(tokens[1], cwd)
            continue
        if head == "git" and "clean" in tokens[1:4] and any(
                t.startswith("-") and not t.startswith("--") and "f" in t for t in tokens):
            losses.append(_ALWAYS + "git clean -f")           # removes untracked work wholesale
            continue
        if head == "find" and "-delete" in tokens:
            paths = []
            for tok in tokens[1:]:
                if tok.startswith("-"):
                    break                                     # find's paths come before its tests
                paths.append(tok)
            _record(paths or ["."])
            continue
        if head in _DELETE_COMMANDS:
            targets = [t for t in tokens[1:] if not t.startswith("-")]
            if not targets:
                losses.append(_ALWAYS + " ".join(tokens))     # flags only: unreadable, so ask
                continue
            _record(targets)
    return losses


def _covers_tracked_files(repo_root: str, target: str) -> bool:
    """Does this delete target cover anything git tracks in the worktree?

    The second half of "loses work", and the half a command string cannot
    answer on its own (2026-09-11, second report: the coder writes a probe
    script, runs it, and deletes it -- `cat > scripts/probe.mjs <<EOF ... EOF;
    node scripts/probe.mjs; rm -f scripts/probe.mjs` -- and every cleanup
    asked for approval). A file git never heard of is the agent's own
    scratch: deleting it changes nothing in the diff the operator reviews.
    A tracked path is repo content, and removing it is exactly the deletion
    worth stopping for.

    `git ls-files -- <path>` lists tracked paths under a directory as well as
    a file, so a bare `src` or `.` answers correctly. Anything that fails --
    no repo, a timeout, a path git refuses -- returns True: unknown means ask.
    """
    rel = target[len(_SANDBOX_CWD) + 1:] if target.startswith(_SANDBOX_CWD + "/") else target
    if not rel or rel == _SANDBOX_CWD:
        return True                                          # the worktree root itself
    try:
        r = subprocess.run(["git", "-C", repo_root, "ls-files", "--", rel],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 -- unknown means ask
        return True
    if r.returncode != 0:
        return True
    return bool(r.stdout.strip())


def _bash_deletes_real_work(req, repo_root: str | None = None) -> bool:
    """Auto mode's only bash gate -- see the block comment above.

    With `repo_root`, a delete inside the worktree is judged against git:
    tracked content asks, the agent's own untracked scratch does not. Without
    it (the module-level default, and what the tests pin) every non-scratch
    delete asks, which is the safe direction to be wrong in.
    """
    targets = _deletions_that_lose_work(str(req.tool_call["args"].get("command", "")))
    if not targets or repo_root is None:
        return bool(targets)
    for target in targets:
        if target.startswith(_ALWAYS):
            return True                                      # unreadable, or a wholesale clean
        if not (target == _SANDBOX_CWD or target.startswith(_SANDBOX_CWD + "/")):
            return True                                      # outside the worktree: always ask
        if _covers_tracked_files(repo_root, target):
            return True
    return False


def _bash_is_destructive(req) -> bool:
    """The recognisably destructive shape: rm -rf, a force push, sudo, a fork
    bomb, a raw-device write.

    Deliberately NOT the same predicate as _bash_needs_approval: the two
    marker lists guard genuinely different risks. A sensitive PATH match
    (reading .env, editing .github/workflows) is about touching something
    that deserves a second look, and it's the case that fires constantly on
    ordinary work -- that's the noise auto-approve exists to remove.

    Auto mode used this as its gate until 2026-09-11 and it was too wide:
    it cannot tell `rm -rf /tmp/scratch` from `rm -rf src`, and the sandbox
    already neutralises everything here except the loss of work. Auto mode
    asks _bash_deletes_real_work instead; this stays as strict mode's notion
    of "destructive", which is also what the tests pin.
    """
    return _matches_dangerous(str(req.tool_call["args"].get("command", "")))


def _writes_a_git_dir(path: str) -> bool:
    """A write that lands the worktree's `.git` pointer, or anything that looks
    like a git dir or a hook.

    audit H2: the sensitive-path list matches ".git/" WITH the slash, so it
    caught `.git/hooks/pre-commit` but not the bare pointer FILE `.git` --
    and rewriting that pointer is what re-aims every host-side git command at
    an agent-controlled git dir (see agent/tools/git.py). Hooks are disabled
    unconditionally there now, so this is the second lock rather than the
    only one, but the write itself still deserves a prompt.
    """
    norm = path.replace("\\", "/").lower().rstrip("/")
    base = norm.rsplit("/", 1)[-1]
    if base == ".git":                      # the pointer file itself
        return True
    if "/hooks/" in norm or base == "hooks":  # any hooks directory
        return True
    # a bare git dir the agent is assembling under another name
    return base in ("head", "config") and "/objects/" not in norm and (
        norm.endswith("/head") and "/.git/" not in norm)


def _file_op_needs_approval(req) -> bool:
    args = req.tool_call["args"]
    path = str(args.get("path", "")).lower()
    if any(marker in path for marker in _SENSITIVE_PATH_MARKERS):
        return True
    return _writes_a_git_dir(path)


def _describe_bash(tool_call, state, runtime) -> str:
    return f"Approval needed: run this shell command?\n\n{tool_call['args'].get('command', '')}"


def _describe_file_op(tool_call, state, runtime) -> str:
    name = tool_call["name"]
    path = tool_call["args"].get("path", "")
    return f"Approval needed: {name} a sensitive-looking path?\n\npath: {path}"


def _describe_ask_user(tool_call, state, runtime) -> str:
    # Same (tool_call, state, runtime) signature as _describe_bash -- a bare
    # single-arg lambda here crashes the work node the moment ask_user fires.
    args = tool_call.get("args", {}) or {}
    text = "QUESTION FROM THE AGENT:\n" + str(args.get("question", ""))
    if args.get("options"):
        text += "\n\nOptions:\n" + str(args["options"])
    return text


def _make_ask_user_tool():
    """ask_user -- the agent's clarification channel to the operator.

    Exists so an ambiguous goal (two reasonable readings that lead to
    materially different work) gets resolved by asking rather than guessed
    silently. The tool never executes: the HITL middleware interrupts it and
    the operator's typed answer becomes the ToolMessage via the native
    `respond` decision -- the library's own documented "ask user"-style-tool
    pattern.
    """

    @tool
    def ask_user(question: str, options: str = "") -> str:
        """Ask the OPERATOR one focused clarifying question and wait for
        their answer. Use BEFORE starting large work when the goal has a
        consequential ambiguity -- two reasonable readings that lead to
        materially different implementations. Give concrete options when
        they exist (e.g. "A) paginate the list  B) optimize image caching").
        Do NOT use for anything you can verify yourself in the repo, and do
        not ask more than one question at a time. The tool result is the
        operator's own reply -- treat it as authoritative direction."""
        return (
            "(no operator response captured -- proceed with your best "
            "judgment and state the assumption you are making)"
        )

    return ask_user


INTERRUPT_ON = {
    "ask_user": {
        "allowed_decisions": ["respond"],
        # No `when` predicate: every call interrupts -- the whole point is
        # a human answer; the tool body is only a fallback if something
        # ever bypasses the interrupt.
        "description": _describe_ask_user,
    },
    "bash": {
        "allowed_decisions": ["approve", "reject"],
        "when": _bash_needs_approval,
        "description": _describe_bash,
    },
    "write": {
        "allowed_decisions": ["approve", "reject"],
        "when": _file_op_needs_approval,
        "description": _describe_file_op,
    },
    "edit": {
        "allowed_decisions": ["approve", "reject"],
        "when": _file_op_needs_approval,
        "description": _describe_file_op,
    },
}

# Auto-approve mode (per-user opt-in, User.auto_approve_commands, set from
# the dashboard's Settings tab). Removes the approval prompt for the
# sensitive-PATH class -- reading a config file, editing a workflow -- which
# is the case that fires constantly during ordinary work and is what makes
# a long task need babysitting.
#
# What it deliberately does NOT remove:
#   * a deletion that loses real work (_bash_deletes_real_work) stays gated,
#     always: the repo worktree, a path outside the container's scratch
#     dirs, anything under .git, or a target too unreadable to judge. Those
#     are the actions a revert can't undo, and a preference toggle is the
#     wrong instrument for switching them off. Scratch and build-output
#     deletes go through, because losing them costs nothing.
#   * ask_user stays interrupting. It's the agent's question channel, not a
#     safety gate -- auto-approving it would just feed every clarifying
#     question the generic "use your best judgment" fallback instead of the
#     operator's real answer, which makes the agent worse, not faster.
#
# This grants no capability the operator doesn't already have: they could
# approve each of these by hand today. It only removes the clicking.
INTERRUPT_ON_AUTO_APPROVE = {
    "ask_user": INTERRUPT_ON["ask_user"],
    "bash": {
        "allowed_decisions": ["approve", "reject"],
        # Deletions that lose real work, NOT every destructive marker: the
        # sandbox already answers sudo, a push, a fork bomb and a device
        # write, and gating scratch cleanup made auto mode interrupt every
        # half minute. See _bash_deletes_real_work's own block comment.
        "when": _bash_deletes_real_work,
        "description": _describe_bash,
    },
}


def interrupt_on_for(auto_approve_commands: bool, repo_root: str | None = None) -> dict:
    """The gate map for this task. In auto mode, `repo_root` lets the bash
    predicate ask git whether a delete target is repo content or the agent's
    own scratch; without it every non-scratch delete asks."""
    if not auto_approve_commands:
        return INTERRUPT_ON
    if repo_root is None:
        return INTERRUPT_ON_AUTO_APPROVE
    return {
        **INTERRUPT_ON_AUTO_APPROVE,
        "bash": {
            **INTERRUPT_ON_AUTO_APPROVE["bash"],
            "when": lambda req: _bash_deletes_real_work(req, repo_root),
        },
    }


def llm_for_role(config: Config, model_name: str, reasoning_effort: str | None = None,
                 timeout: int | None = None, callbacks: list | None = None,
                 task_id: str | None = None, session_id: str | None = None) -> ChatOpenAI:
    # model_name is a bare router alias, resolved entirely by the
    # proxy, not by anything in this process.
    #
    # stream_usage=True is not optional: ChatOpenAI only auto-enables it when
    # talking to the default OpenAI base_url/client, which this custom
    # router_base_url never matches. Every model call here goes through
    # agent.astream_events(..., version="v3"), so it's invoked as a real
    # token stream rather than a single ainvoke -- and without stream_usage,
    # a streamed OpenAI-compatible response never includes
    # `stream_options: {"include_usage": true}`, so
    # response_metadata["token_usage"] comes back empty on every call.
    # BudgetGuardMiddleware's cost read falls back to 0.0 in that case (by
    # design, to fail loud-ish rather than guess), which would mean the one
    # non-negotiable hard dollar ceiling in this whole system silently never
    # trips.
    #
    # reasoning_effort (None by default -- opt-in per call site, not a
    # blanket default): confirmed live that OpenRouter's Gemini models
    # accept this directly on ChatOpenAI and genuinely spend extra "thinking"
    # tokens on it (response.usage_metadata.output_token_details.reasoning
    # comes back > 0, and it's billed/counted like any other output token --
    # BudgetGuardMiddleware sees it same as always). Not every pinned role's
    # model necessarily supports this OpenRouter parameter; only pass it for
    # a role/model combination confirmed to actually use it.
    #
    # timeout (180 by default, overridable per call site): confirmed live
    # 2026-08-23 that agent-planning-chat (gemini-3.7-flash) called with
    # reasoning_effort="high" routinely blows past 180s -- OpenRouter itself
    # aborts the still-in-flight call ("OpenrouterException - The operation
    # was aborted"), which the router then surfaces to this client as an HTTP
    # 400, which openai's SDK in turn raises as BadRequestError. A "high"
    # reasoning budget on a planning turn isn't on the same latency budget as
    # an interactive coordinator call, so a role that opts into high
    # reasoning_effort should also opt into a longer timeout at its own call
    # site rather than eating spurious aborts on real, in-progress work.
    return ChatOpenAI(
        model=model_name,
        base_url=config.router_base_url,
        api_key=config.router_api_key,
        temperature=0,
        timeout=timeout if timeout is not None else _rs.as_int("model_call_timeout_s"),
        # ONE retry, not the openai SDK's silent default of two.
        #
        # The default was inherited rather than chosen, and it turns a slow
        # call into a very long one without saying so: a tool-calling call
        # (non-streaming, see disable_streaming below) that hits the timeout
        # is retried twice before this code ever sees an error, so one logical
        # model call can occupy 3x the timeout -- fifteen minutes at the
        # current 300s -- while the agent log stays silent, because the SDK
        # swallows the first two failures.
        #
        # Measured 2026-09-14: a coder call logged 1802s upstream at the
        # router for 280 output tokens, err=False, with no corresponding error
        # on this side. The router keeps an abandoned upstream running after
        # the client has given up, so each retry adds a concurrent upstream
        # rather than replacing one.
        #
        # One retry still covers the case retries exist for -- a transient
        # blip -- at half the worst case. Zero would make every hiccup an
        # escalation.
        max_retries=1,
        stream_usage=True,
        # Do not stream a response that carries tool-call arguments.
        #
        # Measured 2026-09-12: one 734-token tool call cost ~25 seconds at
        # 100% of a core, and the whole of it was langchain-core merging the
        # stream back together. Every chunk re-runs AIMessageChunk's
        # init_tool_calls validator, which re-parses the WHOLE accumulated
        # argument JSON, so chunk N pays for chunks 1..N:
        #
        #     25 chunks  0.02s      100 chunks  0.47s
        #     50 chunks  0.09s      200 chunks  2.73s
        #
        # ...while 800 text-only chunks cost 0.009s. The cost is entirely in
        # the tool-call half.
        #
        # Nothing in this system reads those chunks. work.py and
        # planning_chat.py both consume `run.values` -- a full state snapshot
        # per superstep -- so the dashboard shows messages as they complete,
        # never token by token. We were paying tens of CPU-seconds per turn to
        # assemble something no reader ever saw.
        #
        # "tool_calling", not True: a call with no tools bound (the
        # summarizer) still streams, because that path is cheap and harmless.
        # stream_usage above stays for exactly those calls.
        disable_streaming="tool_calling",
        # include_response_headers: the proxy's x-router-call-id lands in
        # response_metadata["headers"], which is how BudgetGuardMiddleware
        # matches a call to the router's own billed cost for it
        # (agent/tools/router_ledger.py). Streaming included: langchain-openai
        # attaches the headers to the first chunk's generation_info, and
        # langchain-core merges generation_info into the final message's
        # response_metadata.
        include_response_headers=True,
        reasoning_effort=reasoning_effort,
        # callbacks: how a model invoked OUTSIDE the graph's model node still
        # gets metered -- SummarizationMiddleware ainvoke()s its summary model
        # directly, where no agent middleware wraps the call, so the summarizer
        # role attaches a BudgetMeterCallback here (see budget_guard.py).
        callbacks=callbacks,
        # Who this call is for, carried to the router so its own ledger can be
        # read back per task.
        #
        # The proxy merges a request body's `metadata` into what its logging
        # callback sees -- the same channel the router's routing_decision
        # already travels on -- so one line of routing.jsonl can say which
        # task spent the money. Without it the ledger can price a CALL (by
        # x-router-call-id) but cannot total a TASK, which is why a restart
        # reset the displayed spend to the last checkpoint and lost everything
        # the killed pass had spent: real money, invisible.
        extra_body=_call_metadata(task_id, session_id),
    )


def _call_metadata(task_id: str | None, session_id: str | None) -> dict | None:
    tags = {k: v for k, v in (("agent_task_id", task_id), ("agent_session_id", session_id)) if v}
    return {"metadata": tags} if tags else None


def project_namespace(repo: str):
    # One shared, cross-task memory file per project -- every task/thread for
    # this repo reads and writes the same store-backed AGENTS.md, not a
    # per-conversation copy. `rt` (Runtime) is unused here since the
    # namespace is fully determined by which project this factory call is
    # for, not by anything request-time.
    def namespace(rt):
        return (repo,)

    return namespace


def org_namespace(rt):
    return ORG_NAMESPACE


def episodes_namespace(repo: str):
    def namespace(rt):
        return ("episodes", repo)

    return namespace


def skills_namespace(repo: str):
    def namespace(rt):
        return ("skills", repo)

    return namespace


def build_memory_backend(repo: str, store: BaseStore) -> CompositeBackend:
    return CompositeBackend(
        default=StateBackend(),  # ephemeral, thread-scoped scratch for everything else
        routes={
            "/memories/": StoreBackend(namespace=project_namespace(repo), store=store),
            "/org-memory/": StoreBackend(namespace=org_namespace, store=store),
            EPISODES_ROUTE: StoreBackend(namespace=episodes_namespace(repo), store=store),
            SKILLS_ROUTE: StoreBackend(namespace=skills_namespace(repo), store=store),
        },
    )


def route_local_path(route: str, path: str) -> str:
    """The key a CompositeBackend route's StoreBackend actually stores `path`
    under: the route prefix is stripped down to a leading slash before the
    path reaches the route's backend (a composite aread of
    "/memories/AGENTS.md" errors with "File '/AGENTS.md' not found").
    Every piece of app code that touches an agent-visible file via a bare
    StoreBackend (seeding, the system-prompt reads, consolidation) must use
    this stripped form, or it reads/writes a key the agent's own file tools
    can never see.
    """
    assert path.startswith(route), f"{path!r} is not under route {route!r}"
    return "/" + path[len(route):]


async def _seed_if_absent(backend: StoreBackend, path: str, content: str) -> None:
    existing = await backend.aread(path)
    if existing.error is None:
        return
    await backend.awrite(path, content)


async def seed_memory(repo: str, store: BaseStore, content: str) -> None:
    """Writes the initial AGENTS.md content for a project if nothing is
    there yet -- idempotent, safe to call on every server startup. Does not
    overwrite existing content, since the agent is expected to extend this
    file over time and a redeploy shouldn't clobber what it's learned since
    the last seed.
    """
    backend = StoreBackend(namespace=project_namespace(repo), store=store)
    # route_local_path, not MEMORY_PATH -- a bare StoreBackend must use the
    # same stripped key the composite's /memories/ route produces, or the
    # agent's own file tools can never see what was seeded.
    await _seed_if_absent(backend, route_local_path("/memories/", MEMORY_PATH), content)


async def seed_org_memory(store: BaseStore, content: str) -> None:
    """Same idempotent-seed contract as seed_memory, but for the single
    cross-project org-memory file. Application-code-only -- the agent has no
    write access to this path (see the `permissions` rule in
    build_deep_agent), so this is the only way this file is ever updated,
    by design.
    """
    backend = StoreBackend(namespace=org_namespace, store=store)
    await _seed_if_absent(backend, route_local_path("/org-memory/", ORG_MEMORY_PATH), content)


async def seed_skill(repo: str, store: BaseStore, name: str, description: str, content: str) -> None:
    """Writes (or overwrites) one skill's SKILL.md and registers it in the
    project's manifest. Unlike seed_memory/seed_org_memory this is not
    idempotent-skip -- a skill is curated reference material authored
    deliberately (application code, not the agent), so re-running this with
    updated content is the intended way to revise a skill.
    """
    backend = StoreBackend(namespace=skills_namespace(repo), store=store)
    await backend.awrite(route_local_path(SKILLS_ROUTE, f"{SKILLS_ROUTE}{name}/SKILL.md"), content)

    manifest_key = route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH)
    manifest_result = await backend.aread(manifest_key)
    manifest = {}
    if manifest_result.error is None and manifest_result.file_data:
        try:
            manifest = json.loads(file_data_to_string(manifest_result.file_data))
        except (json.JSONDecodeError, TypeError):
            manifest = {}
    manifest[name] = description
    await backend.awrite(manifest_key, json.dumps(manifest, indent=2))


async def load_skills_manifest(repo: str, store: BaseStore) -> dict[str, str]:
    """{skill name: description} for every skill registered to `repo`; empty
    when nothing is registered or the manifest is unreadable."""
    backend = StoreBackend(namespace=skills_namespace(repo), store=store)
    manifest_result = await backend.aread(route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH))
    if manifest_result.error is not None or not manifest_result.file_data:
        return {}
    try:
        manifest = json.loads(file_data_to_string(manifest_result.file_data))
    except (json.JSONDecodeError, TypeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


async def unregister_skill(repo: str, store: BaseStore, name: str) -> int:
    """Removes one skill from `repo`: its manifest entry and every file under
    /skills/<name>/. Returns the number of files deleted. The inverse of
    seed_skill, for a skill the operator has decided the agents must not see.

    Goes to the store directly rather than through StoreBackend: the backend's
    `ls` is synchronous, which AsyncPostgresStore refuses inside a running
    event loop, and the async search is private. The namespace tuple and the
    key shape ("/<name>/<relpath>") are the ones skills_namespace/route_local_path
    produce, so this sees exactly what the agent's own read_file sees.
    """
    namespace = skills_namespace(repo)(None)
    manifest = await load_skills_manifest(repo, store)
    if name in manifest:
        del manifest[name]
        backend = StoreBackend(namespace=skills_namespace(repo), store=store)
        await backend.awrite(route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH), json.dumps(manifest, indent=2))
    prefix = f"/{name}/"
    deleted = 0
    # Read the whole namespace first, then delete: deleting while paging
    # moves every later row up under the offset, so half of a multi-page
    # skill would survive the sweep that was meant to remove it.
    for item in await all_items(store, namespace):
        if item.key.startswith(prefix):
            await store.adelete(namespace, item.key)
            deleted += 1
    return deleted


async def load_skills_summary(repo: str, store: BaseStore) -> str:
    """The level-1 progressive-disclosure load: name+description only, for
    every registered skill, formatted for the system prompt. Manual
    workaround for the same AsyncPostgresStore incompatibility documented on
    build_deep_agent (SkillsMiddleware's own startup load calls the same
    broken download_files/adownload_files path) -- deepagents' `skills=`
    parameter is not used here for the same reason `memory=` isn't.
    """
    manifest = await load_skills_manifest(repo, store)
    if not manifest:
        return "(no skills registered for this project)"
    lines = [
        f"- {name}: {description} (full instructions: read {SKILLS_ROUTE}{name}/SKILL.md)"
        for name, description in manifest.items()
    ]
    return "\n".join(lines)


def _make_run_checks_tool(repo_root: str, repo: str):
    @tool
    async def run_checks() -> str:
        """Run this project's REAL typecheck, lint, and test suite (the
        exact same commands the outer verification gate will run after you
        report done). Call this yourself before claiming a step or the
        whole task is complete -- it's much cheaper and faster than finding
        out from the outer gate that something you thought was fixed
        actually isn't. The outer gate re-runs these checks itself
        regardless of what you report; this tool exists purely so you get
        the feedback sooner, not as a substitute for that gate.
        """
        result = await run_all_checks(repo_root, repo)
        return result["summary"]

    return run_checks


# Shared across all three agents (coordinator + both subagents). This system
# exposes two entirely separate filesystems with no way to tell them apart
# from tool names alone:
#   - deepagents' own native tools (ls/read_file/write_file/edit_file/glob/
#     grep, auto-provided by FilesystemMiddleware, required, can't be
#     removed) -- these only see the virtual CompositeBackend routes
#     (/memories/, /org-memory/, /skills/, /episodes/) and never the real
#     repo, no matter what path is given.
#   - This system's own custom tools (bash/read/write/edit, agent_tools.py)
#     -- these are the only way to reach the real repo. `bash` runs inside a
#     Docker sandbox (agent/tools/sandbox.py) with the repo mounted at
#     /workspace; `read`/`write`/`edit` take paths relative to that same
#     repo root directly (no /workspace/ prefix -- e.g. "frontend/src/App.tsx").
# Without this guidance, a model can burn real iterations discovering the
# distinction the hard way: `ls`/`read_file`/`grep` fail with "No files
# found"/"not found" against real repo paths (they structurally cannot see
# the repo) before it stumbles onto /workspace via bash trial-and-error.
_LOGO_GUIDANCE = """MAKING A LOGO OR A BRAND KIT:

If the plan you were given carries an agreed SVG, THAT is the logo: the \
design was chosen in planning, by the operator, by looking at it. Export it \
(steps 3-5 below); do not redesign it. And if the project already has a logo \
and nobody asked for a new one, start from it with `logo_trace_image`.

You write the SVG yourself -- nothing here designs one for you. The logo \
tools do the parts after that, and the order matters:

1. Write two or three concepts as plain SVG. Simple shapes, a real viewBox, \
   no filters or gradients you cannot justify at 16 pixels.
2. `logo_render` each one and READ what comes back. You are writing \
   coordinates, and this is the only way to find out whether they add up to \
   a mark rather than to overlapping shapes or clipped strokes. Render on a \
   dark background too if it has to work on both.
3. `logo_text_to_path` on the one you keep. A <text> element renders in \
   whatever font the viewer has, so a wordmark that looks right here looks \
   wrong everywhere else, including in every PNG exported from it.
4. `logo_optimize_svg`.
5. `logo_export_brand_kit` LAST, once the mark is right. It writes about two \
   dozen files -- PNGs, favicon, social images, BRAND.md -- and they are all \
   the same logo, so exporting a bad one just makes two dozen copies of the \
   problem.

`logo_trace_image` turns an existing PNG or JPG logo into paths, for a \
rebrand where the mark already exists. A trace is a starting point, not a \
finished logo.

`logo_render` is a check for you and shows the operator nothing. \
`show_images` puts images in the task's log for them to see -- use it for \
the finished mark, and whenever what you made is something they judge by \
looking.

The exported files are binary assets in the repo: commit them with the \
change that uses them, and say in your final summary that they are there.
"""

_VISUAL_GUIDANCE = """LOOKING AT WHAT YOU CHANGED:

If you change anything a person SEES -- layout, colour, spacing, a component, \
a template, a stylesheet -- the checks will not tell you whether it looks \
right, and neither will the reviewer. Both only know whether it runs.

`preview_app` starts this project the way the project starts and renders it in \
a real browser: `preview_app("npm run dev -- --host 0.0.0.0 --port 5173", 5173, \
"/")`. The command must bind 0.0.0.0 rather than localhost, or nothing outside \
the container can reach it. Ask a `question` about the specific thing you \
changed rather than "how does it look".

Use it to check your own work before you say you are done, and again if the \
answer surprises you. `browse_page` is the same idea for a page that is \
already served somewhere -- a staging deploy, a design you are matching, a \
project that runs on another host.

Do not use either for a change nobody looks at.
"""

_FILESYSTEM_GUIDANCE = """IMPORTANT -- two separate filesystems, not one:
- Your built-in `ls`/`read_file`/`write_file`/`edit_file` tools ONLY see this \
agent's own memory/skills paths (/memories/, /org-memory/, /skills/, /episodes/) -- NEVER the \
actual repo code, regardless of what path you give them. Use them only for those specific paths. \
There is NO built-in glob/grep: to SEARCH the real repo, use `bash` with rg/grep inside \
/workspace (e.g. `rg -n "someSymbol" src frontend/src`); to find a skills/memory file, read the \
skills manifest or ls the route and read_file the file directly.
- And symmetrically: `bash` CANNOT see /memories/, /org-memory/, /skills/ or /episodes/ -- they \
are not mounted in its container, so `grep /memories/AGENTS.md` returns "No such file" and \
`cat >> /memories/AGENTS.md` writes into a sandbox that is discarded while reporting success. \
Your own memory is reachable through read_file/write_file/edit_file and nothing else.
- EVERY `bash` CALL IS ITS OWN CONTAINER, so nothing outside /workspace survives to the next \
one. A file you write to /tmp, a package you `pip install`, a variable you export, a server you \
start -- all gone when that call returns. Observed 2026-09-22: a task curled a file to /tmp and \
the next call answered `sed: can't read express.qll: No such file or directory`, so it downloaded \
it again, and again. If you need something in a LATER call, write it under /workspace (that is \
the bind mount, and it persists); if you need it in THIS call, chain it with `&&` in the same \
command. /tmp is fine as scratch WITHIN one call and worthless between them.
- `gh` IS installed in the sandbox and is NOT logged in, on purpose. No GitHub token is passed \
into the container -- a token in there is one a prompt-injected instruction could push with -- so \
`gh` works for PUBLIC things only (`gh api` on public endpoints, reading a public repo, fetching a \
rule or doc). For anything in THIS deployment's own repositories, which are private, use the \
github tools (github_pull_request / github_pull_requests / github_inbox_items): they hold the \
token server-side, outside the sandbox. Do not run `gh auth login` or hunt for a token in the \
environment -- there isn't one, and that is the design rather than a gap to work around.
- Your `bash`/`read`/`write`/`edit` tools are the ONLY way to reach the real repo. `bash` runs \
inside a sandbox with the repo mounted at /workspace (so `pwd` there shows /workspace, and \
`/workspace` IS the repo root). `read`/`write`/`edit` take paths RELATIVE to that same repo root \
-- e.g. "frontend/src/App.tsx", never "/workspace/frontend/src/App.tsx" and never any other \
absolute host path.
- Concretely: calling `read_file` with `file_path: "/workspace/src/core/app.js"` returns "File not \
found" -- NOT because the file is missing, but because `read_file` can never see the real repo at \
all, so every real-repo path looks "not found" to it. If you see that error on a path you know \
exists, the fix is never to search harder for the file -- it's to switch tools: use `read` (with \
the path made relative, "src/core/app.js") instead of `read_file`. The two tools take different \
parameter names -- `read_file` wants `file_path`, `read`/`write`/`edit` want `path` -- but the repo \
tools accept `file_path` too, so that particular slip costs you nothing. Reaching for the wrong \
TOOL still does.
- REACH FOR `read`/`write`/`edit` FIRST; bash is the last resort for anything touching a file you \
can already name. Those three run in-process -- 0.1ms, measured -- while every bash call starts a \
container, measured at 389ms before the command itself does anything.
- READING SEVERAL FILES IS STILL `read`. Issue one `read` call per file in the SAME TURN; they run \
together, and five of them measured 0.3ms in total against 389ms for a single `cat a b c`. For part \
of a big file use `read` with offset/limit rather than sed/head/awk. There is no batch-read tool and \
you do not need one -- parallel calls already are the batch.
- Bash is for what only bash can do here: SEARCHING the repo (rg, grep, find), git log/diff/status, \
tests, builds, a script you wrote. Your built-in glob/grep cannot see the repo at all, so bash \
genuinely is the only way to search it -- that is not a fallback, it is the right tool. If you find \
yourself writing a heredoc to patch a file, or cat-ing a path you already know, that is the signal \
to use `edit`/`read` instead.
- NEVER run `git commit` (or amend/rebase) yourself via bash. The verify/ship gate commits your \
work for you after its own checks pass -- a self-made commit bypasses that bookkeeping and gets \
absorbed anyway, so it only adds confusion. Just edit files and let the gate handle git."""
_FINDING_GUIDANCE = """ACTING ON A REPORTED FINDING (a scanner alert, a failing check, a stack trace):

A finding that names a file and a line has already done the hard part. Open THAT file at THAT \
line, first, before anything else. The message plus the code it points at is usually the whole \
story: "this query object depends on a user-provided value" sitting beside a `User.findOne` \
whose filter is an `email` taken straight out of `req.body` is not a puzzle to be researched, it \
IS the answer -- send a `$ne` operator where the string was expected and the query matches every \
row.

(No braces appear anywhere in this section on purpose: it is concatenated into prompts that are \
`.format()`-ed and into prompts that are not, so a literal brace either raises KeyError in one or \
renders as a doubled brace in the other.)

DO NOT go and read the tool that produced the finding. Its rule definitions, its query source, \
its extension packs, its documentation past a one-line description -- that is studying the \
detector instead of the defect, and it is the most reliable way there is to spend an hour and \
change no files. Observed 2026-09-22: a task handed a CodeQL alert with sixteen exact file:line \
locations spent twenty-five minutes downloading SqlInjection.qll and express.qll, then delegated \
a subagent to research the query further, and never once opened the controller it was pointed at. \
The two fixes it was asked for were three lines each and visible on sight.

What IS worth investigating is the CODE: how user input reaches that line, what the callers \
assume, what else in the repo shares the pattern, what a fix would break. Those are real \
questions and the `investigator` subagent is the right place for them. "How does this analyser \
decide?" is not one of them -- and if you are handed that question as a delegation, answer it \
from the finding itself in a sentence and spend your effort on the code instead.

If the message is still opaque after you have read the code it points at, look the rule up ONCE \
by its short description and move on. If it is opaque even then, fix what you can see is wrong \
and say plainly in your conclusion what you could not interpret. An honest partial fix beats a \
complete understanding of a linter.
"""


INVESTIGATOR_SYSTEM_PROMPT = """You are a read-only investigation subagent. You research and \
report -- you never modify anything. Your tools do not include write/edit (restricted at the code \
level, not just instruction), so don't waste turns trying to change files; focus entirely on \
reading, searching, and reporting back a clear, complete answer to whatever you were asked to \
investigate. `read` is how you OPEN a file -- one call per file, and several `read` calls \
in the SAME TURN run together (five measured at 0.3ms total). Use offset/limit for part of a big \
one instead of sed/head. `bash` is for what only bash can do here: SEARCHING the repo (rg, grep, \
find), git log/diff/status, and running things -- the built-in glob/grep tools cannot see the repo \
at all, so bash really is the only way to search it. What bash is NOT for is opening files you \
already know the path of: every bash call starts a container (389ms measured, against 0.1ms for \
`read`), so `cat a.js b.js` is roughly a thousand times the cost of two `read` calls that return \
the same bytes. Never use bash to modify anything. A genuinely destructive command from you (or anyone) now requires operator approval \
before it runs at all -- that gate exists as a real backstop, not as license to test what you can \
get away with. You also have `describe_image` for any attached screenshot/photo -- use it instead \
of `read` or your built-in read_file for image files, since those return raw bytes or fail, not a \
description. Stay strictly within what you were actually asked to investigate: if the delegation \
prompt doesn't ask you to examine an image, don't go analyze one on your own initiative -- a single \
`describe_image` call is the only appropriate way to look at one at all, never manual pixel/byte \
inspection via bash. If the prompt already states a fact (a product name, a file path, a value), \
treat it as given and move on to the actual investigation instead of re-deriving it yourself.

""" + _FILESYSTEM_GUIDANCE + _VISUAL_GUIDANCE + _FINDING_GUIDANCE + """

IMPORTANT: your final report is what returns to the coordinator -- everything else you did (every \
file you read, every command you ran) stays isolated in your own context and is NOT automatically \
visible to it. Return only the essential answer: the specific finding, file paths and line numbers \
that matter, and a concise summary. Do NOT paste raw file contents, full command output, or a blow \
by blow of your own process -- a long, unfiltered report defeats the entire reason you were \
delegated to in the first place (keeping the coordinator's own context small)."""


TEST_WRITER_SYSTEM_PROMPT = """You are a test-writing subagent for a live production codebase. \
High-consequence logic (anything that moves money, mutates external state, or touches a \
third-party API) must have REAL behavioral test coverage -- tests that actually invoke the \
function against a mocked dependency and assert on real side effects.

A cautionary example of what NOT to do: a test for a state-mutating function that only asserted \
`someFunction.toString().includes('expectedCall')` -- i.e. it checked the FUNCTION'S SOURCE CODE \
as a string, never actually called the function. It would pass even if the logic were completely \
broken (wrong lock key, called with the wrong argument, a race condition mishandled). Do not write \
this kind of test. Ever.

If the task refers to the GitHub inbox or to Dependabot alerts, `github_inbox_items(repo)` is the exact list. \
If the task names a GitHub pull request, read it with `github_pull_request(repo, number)` BEFORE planning \
the work: the review comments (file:line) are the findings to address, the diff is the code they refer to, \
and the checks say what is failing. Treat each review comment as a todo. (The tool exists only when this \
deployment has a GitHub token; if it is missing, say so instead of guessing.)

Before reporting a test as done, call the `run_checks` tool yourself to confirm it actually runs \
and actually passes -- and read what it's asserting one more time: would this test fail if the \
underlying logic were subtly wrong? If you're not sure, it isn't a real test yet.

For frontend/UI work, LOOK at what you built before reporting done: the `webapp-testing` skill \
(read /skills/webapp-testing/SKILL.md) shows how to render the app headlessly in your bash \
sandbox, screenshot it into the workspace, and read the screenshot with `describe_image`. A \
component that compiles is not a component that renders.

Also make sure any new test file is actually registered as an npm script and included in the \
project's aggregate `test` script in package.json -- a test that exists on disk but was never wired \
in silently never runs as part of any check.

""" + _FILESYSTEM_GUIDANCE + _VISUAL_GUIDANCE + _FINDING_GUIDANCE + """

Your final report returns to the coordinator; the rest of your own work stays isolated in your own \
context. Report which file(s) you wrote/changed and a short summary of what the tests actually \
cover -- not a full reprint of the test file contents (the coordinator can read the file itself if \
it needs to) and not a turn-by-turn narration of your own process."""

COORDINATOR_SYSTEM_PROMPT_TEMPLATE = """You are working on exactly one task in the repo at /workspace \
(project: {repo}). Use `write_todos` to plan and track your own work as you go -- adapt the plan as \
you learn more, rather than treating an initial plan as fixed.

ASK BEFORE GUESSING: if the goal has a consequential ambiguity -- two \
reasonable interpretations that lead to materially different work (which \
items to move, which of two approaches the operator named, destructive vs \
additive changes) -- use the `ask_user` tool with ONE focused question and \
concrete options BEFORE starting the large work, then follow the answer as \
authoritative. Never ask about things the repo itself can answer (read the \
code instead), and never ask more than one question at a time. A wrong \
guess costs a full build-review-rework cycle; a question costs one minute.

DELEGATE TEST WORK: whenever the task calls for writing NEW tests or making non-trivial changes to \
existing test files, delegate that piece to the `test-writer` subagent via your task() tool instead \
of writing the tests yourself -- it runs a different model precisely to get an independent set of \
eyes on test quality, and a test you author yourself to validate your own implementation is exactly \
the blind spot it exists to remove. (Trivial mechanical fixes -- updating an expectation string, \
renaming an import -- are fine to do directly.)

""" + _FILESYSTEM_GUIDANCE + _VISUAL_GUIDANCE + _FINDING_GUIDANCE + """

DELEGATE RESEARCH: before you can change something you usually have to find out how it works. \
The moment that costs more than a couple of looks -- you are about to open a third file, or run a \
second round of `rg` because the first did not settle it -- stop and hand the question to the \
`investigator` subagent via task(). Give it the specific question, then act on what it reports. \
Do not keep exploring inline past that point.

This is not a cost optimisation you may decline. Exploration output is bulky and almost entirely \
irrelevant once the question is answered, and it accumulates in YOUR context, which is what pushes \
this conversation into summarization -- and what summarization compacts is the earlier material: \
your plan, your findings, and the reasons behind them. The investigator spends its own context on \
the search and returns you the answer. Running one `rg` whose output you already know how to read \
is fine to do yourself; a hunt is not.

Delegate writing or hardening tests to the `test-writer` subagent, especially for anything that \
moves money or touches an external API boundary.

Call `run_checks` yourself before considering any todo done. A deterministic check failing is \
never something to argue around or reinterpret as unrelated -- if it fails, the work isn't done, \
full stop. Investigate a failure rather than asserting it's a pre-existing environment issue.

If you learn something durable and non-obvious about THIS repo specifically that would help on a \
future task (a convention, a gotcha, a concurrency primitive's real purpose, a test-wiring rule), \
write it to {memory_path} via your file-edit tool so it's there next time -- don't rediscover the \
same thing from scratch on a future task.

<org_memory path="{org_memory_path}">
Cross-project conventions that apply everywhere this agent works, not just this repo. READ-ONLY to \
you -- writes to this path are blocked at the code level, not just discouraged. Treat it as settled \
policy, not something to revise mid-task.

{org_memory_content}
</org_memory>

<project_memory path="{memory_path}">
Durable facts about THIS repo specifically, written by past runs of this same agent against this \
same project. Agent-writable -- extend it via your file-edit tool when you learn something durable \
and non-obvious (see above).

{project_memory_content}
</project_memory>

<available_skills>
Deeper, subsystem-specific reference material for this repo -- too large to keep loaded by default, \
so only names and one-line descriptions are shown here. If a listed skill sounds relevant to this \
task, read its full instructions with your read_file tool (path shown below) BEFORE making changes \
in that area -- it exists specifically because that subsystem has real, non-obvious rules that are \
easy to get wrong without it. Skills are read-only reference material, not something to edit.

{skills_summary}
</available_skills>"""


async def read_memory_or_empty(backend: StoreBackend, path: str) -> str:
    result = await backend.aread(path)
    if result.error is not None or not result.file_data:
        return "(nothing recorded yet)"
    return file_data_to_string(result.file_data)


@dataclass(frozen=True)
class ProjectMemory:
    """One project's memory as a seat's prompt carries it.

    `entries` is empty for a project that has never been split -- the content
    is then the whole file, exactly as it always was. When it is not empty,
    the content is the preamble plus the pinned sections plus the index, and
    `entries` is what the seat's read_memory_section tool is allowed to fetch.
    """

    content: str
    entries: list[memory_sections.IndexEntry] = field(default_factory=list)


async def _read_text(backend: StoreBackend, key: str) -> str | None:
    """The file's text, or None when it is not there. Distinct from
    read_memory_or_empty, which answers with a sentence for a system prompt --
    here the difference between "empty" and "absent" decides whether a whole
    code path applies."""
    result = await backend.aread(key)
    if result.error is not None or not result.file_data:
        return None
    return file_data_to_string(result.file_data)


async def load_memory_index(backend: StoreBackend) -> memory_sections.MemoryIndex:
    """This project's section index, with no entries for a project whose
    memory has never been split -- which is every project until the migration
    runs, and permanently for one small enough that splitting it would cost
    more than it saves (memory_sections.SPLIT_FLOOR_CHARS)."""
    raw = await _read_text(backend, route_local_path("/memories/", SECTIONS_INDEX_PATH))
    return memory_sections.parse_index_document(raw) if raw is not None else memory_sections.MemoryIndex(entries=[])


async def resplit_memory_sections(
    backend: StoreBackend, text: str, *, budget_tokens: int | None = None,
) -> list[str]:
    """Rewrite the section layer from `text`, for a project that already has one.

    The nightly consolidator (agent/consolidation.py) reads the whole memory,
    asks a model for an updated whole memory, and writes it back. Once a
    project is split, that write lands on a key the prompt no longer reads --
    so without this the consolidator would go on working, report success
    every night, and quietly stop reaching any agent. That is the exact
    failure this subsystem exists to prevent, arriving through the back door.

    Deterministic, and no model in the path: the same split the migration
    performs, against the text it is handed. Stale section files from a
    heading the consolidator removed are deleted, and the index is written
    LAST so an interruption leaves an orphaned body rather than an index
    entry pointing at nothing.

    Returns the slugs written. Does nothing, and returns [], for a project
    that has never been split -- whether because it is under
    SPLIT_FLOOR_CHARS or because the migration has not run.
    """
    index = await load_memory_index(backend)
    if not index.entries:
        return []

    budget = budget_tokens if budget_tokens is not None else int(
        _rs.value("memory_inline_token_budget"))
    preamble, sections = memory_sections.split_sections(text)
    entries = memory_sections.build_index(sections, preamble=preamble, budget_tokens=budget)

    await backend.awrite(route_local_path("/memories/", SECTIONS_CORE_PATH), preamble)
    for section in sections:
        await backend.awrite(
            route_local_path("/memories/", memory_sections.section_path(section.slug)), section.body)

    # Anything the consolidator dropped. Left behind it is unreachable rather
    # than harmful -- nothing indexes it -- but it would be read back by a
    # later "all" and reappear as memory nobody wrote.
    fresh = {s.slug for s in sections}
    for old in index.entries:
        if old.slug not in fresh:
            await backend.adelete(route_local_path("/memories/", memory_sections.section_path(old.slug)))

    await backend.awrite(
        route_local_path("/memories/", SECTIONS_INDEX_PATH),
        # source_sha256, NOT source_digest: the reader (_sections_match_source)
        # looks for that exact key, and a mismatch does not fail -- an index
        # with no recorded digest is taken at its word, so the drift check
        # that protects against an agent writing the whole file just stops
        # running. Silently. This wrote the wrong key once and disabled that
        # protection on live data until someone compared the two spellings.
        memory_sections.index_to_json(
            entries,
            source_sha256=memory_sections.source_digest(text),
        ))
    return [s.slug for s in sections]


async def gather_memory_sections(
    backend: StoreBackend, entries: list[memory_sections.IndexEntry],
) -> tuple[list[str], list[str]]:
    """(the core and every section body in index order, the slugs whose file
    was not there).

    With nothing missing, joining the parts is the original file byte for
    byte: a section body is a slice of it and the index preserves the order
    the cuts were made in. Shared by the rollback path here and by
    read_memory_section("all"), because a second copy of this loop is a second
    place for "in index order" to stop being true.
    """
    parts: list[str] = []
    missing: list[str] = []
    core = await _read_text(backend, route_local_path("/memories/", SECTIONS_CORE_PATH))
    if core is None:
        missing.append(SECTIONS_CORE_PATH)
    else:
        parts.append(core)
    for entry in entries:
        body = await _read_text(backend, route_local_path("/memories/", memory_sections.section_path(entry.slug)))
        if body is None:
            missing.append(entry.slug)
        else:
            parts.append(body)
    return parts, missing


async def _render_sectioned_memory(backend: StoreBackend, entries: list[memory_sections.IndexEntry]) -> str | None:
    """The prompt block for a split memory, or None if the index turns out to
    describe files that are not there.

    None matters more than it looks. An index without its sections is the one
    way this subsystem could silently amputate a project's memory -- the
    prompt would carry a confident list of sections and nothing behind it --
    so it degrades to the whole-file read instead, which is the behaviour of
    every version of this system before today.

    Which is why "all or nothing" is the rule here rather than "as much as we
    can find". A PARTIALLY present split is worse than an absent one, and the
    worst case of all is the one that reads as healthy: a pinned section whose
    file is gone renders as a prompt with the body missing and an index line
    next to it reading "already above, do not re-read" -- the content removed
    AND the model told not to go looking. The sections that get pinned are the
    ones the operator's policy pins BECAUSE they fail silently (test wiring,
    sandbox constraints, collision traps), so that is a silent failure about
    silent failures, and the only evidence would be work that quietly breaks a
    rule nobody can see any more. A whole-file read costs tokens; this costs a
    rule.

    Reachable without a bad migration, which writes the index last precisely
    so that an interruption leaves no index at all: a restore that replays
    some of a namespace, an operator deleting a section by hand to force a
    re-split, a later consolidator edit that writes an index entry whose
    section write did not land.

    An EMPTY /sections/_core.md is a real state -- a memory file that opens
    straight into `## ` has no preamble -- and is not the same as a missing
    one. The store distinguishes them (an empty file reads back as file_data
    with content ""), so _read_text's None genuinely means absent.
    """
    core = await _read_text(backend, route_local_path("/memories/", SECTIONS_CORE_PATH))
    if core is None:
        logger.warning("memory index exists but /sections/_core.md does not; reading the whole file")
        return None
    bodies: dict[str, str] = {}
    for entry in entries:
        if not entry.always:
            continue
        body = await _read_text(backend, route_local_path("/memories/", memory_sections.section_path(entry.slug)))
        if body is None:
            logger.warning("pinned memory section %s is indexed but missing; reading the whole file", entry.slug)
            return None
        bodies[entry.slug] = body
    # The budget is spent here and not only in the migration that set the
    # `always` flags, so an operator who lowers it sees the next task's prompt
    # shrink instead of having to re-run a migration to find out whether the
    # dial does anything. A section that no longer fits is demoted to an
    # ordinary index line, not dropped.
    return memory_sections.render_prompt_block(
        core, entries, bodies, budget_tokens=_rs.as_int("memory_inline_token_budget"),
    )


async def _sections_match_source(backend: StoreBackend, index: memory_sections.MemoryIndex) -> bool:
    """Whether the sections are still a faithful copy of /memories/AGENTS.md.

    They are cut from it, and it stays the authoritative copy until the
    pointer-stub commit -- while the nightly consolidator rewrites the whole
    file and the coordinator's prompt still tells the agent to record facts
    there. Both of those writes land somewhere no prompt reads the moment an
    index exists, and nothing about that is visible: the prompt still renders,
    the sections are still valid, they are just a snapshot of a file that has
    moved on. That is the memory subsystem losing memory, which is the one
    failure it cannot be allowed to have.

    So the index records what it was cut from, and a source that no longer
    matches turns this back into a whole-file read until the next migration
    run re-splits it -- self-healing rather than silent. An index with no
    recorded digest (hand-written, or from before this check) makes no claim
    about the source and is taken at its word.

    A note for whoever writes the pointer stub: rewriting /AGENTS.md must
    rewrite the index's source_sha256 with it, or every project falls back
    here -- loudly, to a prompt containing the stub, which is the failure
    being visible rather than quiet.
    """
    if not index.source_sha256:
        return True
    whole = await _read_text(backend, route_local_path("/memories/", MEMORY_PATH))
    if whole is None:
        return True
    if memory_sections.source_digest(whole) == index.source_sha256:
        return True
    logger.warning("/memories/AGENTS.md has changed since the split; reading it whole instead of the sections")
    return False


async def _whole_memory(backend: StoreBackend, entries: list[memory_sections.IndexEntry]) -> str:
    """The whole memory, for every path that is not a rendered split: the
    disclosure toggle turned off, an index that does not resolve, a project
    that was never split at all.

    Reassembled from the sections when there are any, because /AGENTS.md
    becomes a pointer stub one commit after the migration and the documented
    rollback ("the toggle restores the whole-file behaviour, without touching
    data") would otherwise hand the agent the stub. Reassembly IS the whole
    file -- byte for byte, asserted in tests -- so this is the same answer by
    a route that survives the stub. With no sections, or with one of them
    missing, it is the read this function has always been.
    """
    if entries:
        parts, missing = await gather_memory_sections(backend, entries)
        if not missing:
            return "".join(parts)
        logger.warning("cannot reassemble memory from sections (missing %s); reading /AGENTS.md", ", ".join(missing))
    return await read_memory_or_empty(backend, route_local_path("/memories/", MEMORY_PATH))


async def load_project_memory(repo: str, store: BaseStore, *, task_id: str | None = None) -> ProjectMemory:
    """The project memory block, for any seat that has one.

    ONE function, called by both the build coordinator and the planning chat.
    They had two copies of the same four lines, and the two seats drifting is
    not a hypothetical: the planner is where a fact gets recorded and the
    coordinator is where it has to be obeyed, so a section pinned in one seat
    and indexed in the other is a plan written against rules the build cannot
    see.

    With no index present this is byte for byte what it always did -- read
    /memories/AGENTS.md whole, attach the stale-flags block -- which is what
    makes deploying the reader before the data migration a no-op rather than a
    change to be verified in production.
    """
    backend = StoreBackend(namespace=project_namespace(repo), store=store)
    index = await load_memory_index(backend)
    entries = index.entries
    current = await _sections_match_source(backend, index)
    content: str | None = None
    # A dial rather than a redeploy, because the thing being rolled back is a
    # prompt: if progressive disclosure turns out to lose work, the operator
    # needs the old prompt on the next task, not after a deploy.
    if entries and current and _rs.value("memory_progressive_disclosure") >= 1:
        content = await _render_sectioned_memory(backend, entries)
    if content is None:
        # Reassembled from the sections when they are the good copy, and only
        # then -- if /AGENTS.md has moved on since the split it holds facts
        # the sections do not, and serving a stale reassembly instead would be
        # this subsystem losing exactly what it exists to keep.
        content = await _whole_memory(backend, entries if current else [])
        entries = []
    else:
        episode_recall.record_sections_offered(
            repo, [e.slug for e in entries],
            always=[e.slug for e in entries if e.always], task_id=task_id,
        )
    return ProjectMemory(content=await memory_with_freshness(backend, content), entries=entries)


async def build_deep_agent(
    config: Config,
    repo: str,
    budget_usd: float,
    checkpointer,
    store: BaseStore,
    starting_cost: float = 0.0,
    starting_last_failed_edit: str | None = None,
    auto_approve_commands: bool = False,
    route: str = "general",
    task_id: str | None = None,
    goal: str = "",
    reference_repos: list[str] | None = None,
):
    """NOTE: async, unlike a typical factory -- it needs to `await` reading
    both memory files before constructing the agent. This is a deliberate
    workaround, not the deepagents-native path: `create_deep_agent`'s own
    `memory=[...]` parameter (backed by MemoryMiddleware) is broken for this
    system specifically -- confirmed empirically that MemoryMiddleware's
    `download_files`/`adownload_files` call the store synchronously
    (`store.get(...)`), and AsyncPostgresStore explicitly raises
    `InvalidStateError` on synchronous calls from within a running event loop
    ("Synchronous calls to AsyncPostgresStore detected in the main event
    loop... replace `store.get(...)` with `await store.aget(...)`"). The
    result was a silent `(No memory loaded)` in the real system prompt -- no
    exception surfaced. Reading memory ourselves via the same async-safe
    `aread()` path the rest of this module already uses sidesteps the bug
    entirely and produces the same end result (memory content in the system
    prompt) via a confirmed-working path instead of a confirmed-broken one.
    """
    # The task's own workspace (agent/workspaces.py); the project's when there
    # is no task -- or, for a task whose workspace does not exist yet, which
    # the work node creates before it gets here.
    from agent.workspaces import own_or_template  # noqa: PLC0415
    repo_root = own_or_template(PROJECTS[repo]["sandbox"], task_id)
    # Which of the paths this task names are not on disk. Appended to the
    # coordinator's prompt AND to every subagent's, because the subagents are
    # what go looking: on task 3ee0d030 seven delegations each searched for a
    # file the task existed in order to create, and the coordinator read the
    # resulting "it does not exist" as a blocker. See agent/new_files.py.
    absent_files = new_files.guidance(repo_root, goal)
    # One gate, shared by the coordinator and every subagent -- investigator
    # carries the full bash tool too, so a laxer gate there would be a hole.
    # repo_root so auto mode can tell repo content from the agent's own
    # scratch when it judges a delete (see _bash_deletes_real_work).
    interrupt_on = interrupt_on_for(auto_approve_commands, repo_root)
    tracker = BudgetTracker(budget_usd=budget_usd, starting_cost=starting_cost)
    backend = build_memory_backend(repo, store)

    project_tools, last_failed_edit_ref = make_agent_tools(
        repo_root, backend=backend, initial_last_failed_edit=starting_last_failed_edit,
    )
    # Read-only SQL against this project's own application database, when it
    # has one. The codebase describes the schema; only this shows what's
    # actually IN the database -- see project_db.py for why it lives here
    # rather than inside the sandbox container.
    db_tool = make_project_db_tool(repo)
    if db_tool is not None:
        project_tools = [*project_tools, db_tool]
    tool_by_name = {t.name: t for t in project_tools}
    # No write/edit for investigator -- read/bash/describe_image are all
    # read-only against the real repo. describe_image is required, not
    # optional: without it, an investigator asked to look at an uploaded
    # image has no way to actually see one -- `read` returns raw bytes as
    # text (not a description), and the built-in read_file tool can't reach
    # the real repo filesystem at all (see _FILESYSTEM_GUIDANCE).
    # GitHub PR tools (read-only, host-side, only when a token is configured):
    # the coordinator, the investigator and the general-purpose seat all read
    # PRs; the test-writer has no use for them.
    github_tools = make_github_tools(token_source(config))
    github_tools = [*github_tools, make_github_inbox_tool(store)]
    project_tools = [*project_tools, *github_tools]

    # Seeing the work. A frontend task used to be done blind -- edit the CSS,
    # run the tests, and neither the agent nor the reviewer ever looked at the
    # page. browse_page renders any public URL (a design reference, a staging
    # deploy, a project that runs somewhere else); preview_app starts THIS
    # project in the sandbox and renders that. Both come back as visible text
    # plus a description of how it actually looks.
    from agent.tools.planning_tools import make_browse_page_tool  # noqa: PLC0415
    from agent.tools.preview import make_preview_tool  # noqa: PLC0415

    visual_tools = [make_browse_page_tool(), make_preview_tool(lambda: repo_root)]
    project_tools = [*project_tools, *visual_tools]

    # Reading the operator's OTHER projects, for "use the one in X as a
    # template". Host-side and read-only -- no mount, no write path, no bash
    # -- so the sandbox boundary is exactly where it was; what changes is
    # that a pattern from another project can reach a build task as something
    # other than prose in the plan. Empty when there is nothing else this
    # task may read, so the seat is not offered tools that can only refuse.
    from agent.tools.reference_tools import make_reference_tools  # noqa: PLC0415

    reference_tools = make_reference_tools(repo, reference_repos)
    project_tools = [*project_tools, *reference_tools]

    # Designing a mark and exporting a brand kit (agent/tools/logo_tools.py).
    # The coordinator only: it is the seat that writes files, and an export
    # puts two dozen binary assets in the repo.
    from agent.tools.logo_tools import make_logo_tools, new_show_state  # noqa: PLC0415

    show_state = new_show_state()
    logo_tools = make_logo_tools(lambda: repo_root, show_state=show_state)
    # Showing the operator an image -- a render, a screenshot, the exported
    # logo -- in the task's log (agent/tools/show_tools.py).
    from agent.tools.show_tools import make_show_images_tool  # noqa: PLC0415
    project_tools = [*project_tools, *logo_tools,
                     make_show_images_tool(repo, lambda: repo_root, show_state=show_state)]

    # What past tasks ran into (agent/tools/history_tools.py). Empty when the
    # installation has no history index, so no seat carries a pair of tools
    # that could only report that search is unavailable. The gate is the
    # task's own reference_repos -- the same list the reference tools use,
    # through the same check -- except that this task's OWN project is always
    # in it and is the default.
    from agent.tools.history_tools import guidance as history_guidance  # noqa: PLC0415
    from agent.tools.history_tools import make_history_tools  # noqa: PLC0415

    history_tools = make_history_tools(repo, reference_repos, store, task_id=task_id)
    project_tools = [*project_tools, *history_tools]
    history_note = history_guidance(repo, reference_repos)

    # What the test-writer seat does NOT get, named once. A list rather than
    # a set because a langchain tool is a pydantic model and unhashable;
    # membership here is the same `==` the comprehension below always used.
    # The point of collecting it is that the next seat-specific tool is one
    # name added here, not another clause in a comprehension three
    # subsystems are all editing.
    # history_tools is here for the same reason the other two are: the
    # test-writer writes tests for this repo against this repo's suite, and
    # what some other task escalated over in June changes nothing about that.
    test_writer_excluded = [*reference_tools, *logo_tools, *history_tools]
    # Named in the prompt, not just discoverable in the tool list: a model
    # asked to "do it like the other project does" will otherwise say it has
    # no way to see that project, which is what it used to have to say.
    reference_note = ""
    if reference_tools:
        others = sorted({r for r in (reference_repos or []) if r != repo and r in PROJECTS})
        reference_note = (
            "\n\nANOTHER PROJECT AS A REFERENCE:\n\n"
            f"You can READ the operator's other projects: {', '.join(others)}. "
            "`search_project(repo, pattern)`, `read_project_file(repo, path)`, "
            "`list_project_dir(repo, path)` and `find_files(repo, glob)` take the project "
            "name as their first argument.\n\n"
            "Use them when the task points at one -- \"like the one in X\", \"the same "
            "approach as X\", \"match X's look\". Search for the thing first, then read "
            "only what the hit points at. They are READ-ONLY and they are for the OTHER "
            f"projects: {repo} itself is this task's repo, and you reach it with `read`, "
            "`bash` and `edit` as usual.\n\n"
            "Borrow the approach, not the file. Another project's code was written against "
            "its own conventions, its own dependencies and its own data -- copying it across "
            "wholesale is how you get an import that does not resolve and a pattern nobody "
            f"else in {repo} follows."
        )
    # The investigator gets both. It was given browse_page alone on the
    # grounds that starting the project is not read-only -- but it already has
    # `bash` in the same sandbox, so it could always start a server; what it
    # could not do was SEE one, and it is the seat that gets sent to find out
    # what a page currently does. preview_app is also the tidier way to do it:
    # bash leaves a dev server running, this tears the container down in a
    # finally.
    # history_tools reads a table and writes nothing, and the investigator is
    # the seat that gets sent to find things out -- history is another place
    # to look.
    read_only_tools = [tool_by_name["read"], tool_by_name["bash"], tool_by_name["describe_image"],
                       *github_tools, *visual_tools, *reference_tools, *history_tools]
    if db_tool is not None:
        read_only_tools.append(db_tool)
    run_checks_tool = _make_run_checks_tool(repo_root, repo)

    # Pinned per-role models: one fixed model per role rather than a
    # smart-router pool/classifier, so cost and accuracy are directly
    # attributable per model. The aliases live in the LLM router's own
    # config: to swap a role's model, edit the router config, not this file.
    # Coordinator gets two models -- planner for the first turn of a thread
    # (the one that writes the todo plan), coder for every turn after -- via
    # PlanCodeModelMiddleware below.
    # Frontend route (agent/frontend_route.py): the coordinator that writes
    # the edits AND the investigator that reads for it move to the frontend
    # coder alias; the test-writer stays general by the operator's call
    # (2026-09-09), and the todo planner is shape-neutral either way.
    coder_role = CODER_ROLE.get(route, CODER_ROLE["general"])
    # Every seat carries the task id, so the router's ledger can total a task
    # rather than only price a call -- subagents included, since their spend is
    # the task's spend.
    coordinator_model = llm_for_role(config, coder_role, task_id=task_id)
    planner_model = llm_for_role(config, "agent-planner", task_id=task_id)
    investigator_model = llm_for_role(config, coder_role if route == "frontend" else "agent-investigator",
                                     task_id=task_id)
    test_writer_model = llm_for_role(config, "agent-test-writer", task_id=task_id)

    project_memory = await load_project_memory(repo, store, task_id=task_id)
    project_memory_content = project_memory.content
    # Stripped keys (route_local_path), not the full agent-visible paths --
    # this read must land on the same key the agent's own file tools write
    # to (via the composite's route stripping), or agent-written memory
    # updates are invisible to every future task's prompt.
    org_memory_backend = StoreBackend(namespace=org_namespace, store=store)
    org_memory_content = await read_memory_or_empty(org_memory_backend, route_local_path("/org-memory/", ORG_MEMORY_PATH))
    skills_summary = await load_skills_summary(repo, store)

    # Bound to this seat, and to the planning chat, and to nothing else: they
    # are the two whose prompt carries the memory index, and a tool no prompt
    # mentions is how the investigator came to describe a preview_app it did
    # not have. Empty for a project whose memory was never split.
    from agent.tools.memory_tools import make_memory_tools  # noqa: PLC0415

    memory_tools = make_memory_tools(repo, store, project_memory.entries, task_id=task_id)

    # skills=[SKILLS_ROUTE] on each subagent: per deepagents' own docs, only
    # the general-purpose subagent automatically inherits main-agent skills
    # -- custom subagents require an explicit skills parameter. Without this,
    # investigator (the subagent doing exactly the deep multi-file
    # exploration a domain skill matters most for) never saw the skills
    # manifest at all. Uses the native deepagents SkillsMiddleware here
    # (unlike the coordinator's own manual-injection workaround below) --
    # confirmed to correctly await backend.als()/adownload_files() and load
    # real data against our AsyncPostgresStore-backed CompositeBackend with
    # no error. MemoryMiddleware (the `memory=` param) is still broken the
    # same way it always was, so this fix is scoped to skills specifically,
    # not a signal to also switch the coordinator's own proven-working
    # memory/skills prompt injection over to native params.
    investigator = {
        "name": "investigator",
        "description": (
            "Delegate read-only research/exploration here: mapping out how something works across "
            "multiple files, finding every call site of something, or answering a question that "
            "needs digging before any change can be made. Cannot write or edit files."
        ),
        "system_prompt": INVESTIGATOR_SYSTEM_PROMPT + absent_files + reference_note + history_note,
        "tools": read_only_tools,
        "model": investigator_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead. delete likewise: it
            # sees the agent's own file space, never the repo, so a coder
            # cleaning up dead modules gets "not found" four times before it
            # thinks of `rm` (observed 2026-09-08). bash rm is the real one.
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            RepeatCallGuardMiddleware(),  # the same call with the same result is not run a third time (2026-09-09)
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        # investigator has the full `bash` tool despite its own "read-only"
        # framing (needed for real find/grep-across-the-tree exploration --
        # see INTERRUPT_ON's own comment) -- this is what actually gates a
        # destructive command from it, since nothing else does at the code
        # level.
        "interrupt_on": interrupt_on,
    }

    test_writer = {
        "name": "test-writer",
        "description": (
            "Delegate here to write or harden tests, especially for anything that moves money, "
            "mutates external state, or touches a third-party API. Must produce real behavioral "
            "coverage, never source-inspection-only tests."
        ),
        "system_prompt": TEST_WRITER_SYSTEM_PROMPT + absent_files,
        # test_writer_excluded, not a clause per subsystem: it writes tests
        # for THIS repo against this repo's suite, and nothing in its prompt
        # tells it another project exists. Tools a seat was never told about
        # are how the investigator ended up with a prompt describing
        # preview_app it did not have.
        "tools": [*[t for t in project_tools if t not in test_writer_excluded], run_checks_tool],
        "model": test_writer_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead.
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            RepeatCallGuardMiddleware(),  # the same call with the same result is not run a third time (2026-09-09)
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        "interrupt_on": interrupt_on,
    }

    # Explicit general-purpose subagent: unless a spec with this exact name
    # exists, create_deep_agent auto-adds its own general-purpose subagent
    # carrying the coordinator's full tools and model but not its custom
    # middleware (deepagents' graph.py deliberately inherits only middleware
    # that overrides a default GP slot, without carrying over middleware
    # that's specific to the main agent). That means any work the
    # coordinator delegated to general-purpose would run with no
    # BudgetGuardMiddleware (its LLM spend invisible to the hard dollar
    # ceiling -- one of this system's two non-negotiable code-enforced
    # guards) and no call-limit backstops. Defining it inline with the same
    # name suppresses the auto-add (graph.py checks by name) while the
    # subagent factory still attaches its default slots (filesystem/
    # summarization/skills), same as investigator/test-writer get -- so
    # this keeps the capability and closes the enforcement hole.
    general_purpose = {
        **GENERAL_PURPOSE_SUBAGENT,  # canonical name/description/system_prompt from the lib
        # history_note as well, because this seat is handed search_history
        # and read_history by *project_tools below. Given but not described
        # is the half of the bug the feature's own comment argues against: a
        # model not told a capability exists says it cannot do the thing and
        # works around it. (reference_note is deliberately NOT here -- that
        # tool set refuses this task's own repo, and a delegated sub-task
        # reaching for another project is a scope decision the coordinator
        # makes, not this seat.)
        "system_prompt": (GENERAL_PURPOSE_SUBAGENT["system_prompt"] + "\n\n"
                          + _FILESYSTEM_GUIDANCE + absent_files + history_note),
        "tools": [*project_tools, run_checks_tool],
        "model": coordinator_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead.
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            RepeatCallGuardMiddleware(),  # the same call with the same result is not run a third time (2026-09-09)
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        "interrupt_on": interrupt_on,
    }

    agent = create_deep_agent(
        model=coordinator_model,
        tools=[*project_tools, run_checks_tool, _make_ask_user_tool(), *memory_tools],
        system_prompt=COORDINATOR_SYSTEM_PROMPT_TEMPLATE.format(
            # No repo_root here -- the prompt says "/workspace" literally
            # (see _FILESYSTEM_GUIDANCE): the real host repo_root path is
            # meaningless to the model now that `bash` runs sandboxed --
            # that path doesn't exist inside the container at all.
            repo=repo,
            memory_path=MEMORY_PATH,
            org_memory_path=ORG_MEMORY_PATH,
            project_memory_content=project_memory_content,
            org_memory_content=org_memory_content,
            skills_summary=skills_summary,
        ) + absent_files + reference_note + history_note + ("\n\n" + _LOGO_GUIDANCE if logo_tools else ""),
        middleware=[
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            RepeatCallGuardMiddleware(),  # the same call with the same result is not run a third time (2026-09-09)  # see subagent specs' comment
            BudgetGuardMiddleware(tracker),
            # Planner on the thread's first turn, coder after -- see model_pin.py.
            PlanCodeModelMiddleware(planner_model, coordinator_model),
            SummarizationMiddleware(
                # Meter callback, not middleware: this model is ainvoke()d
                # directly by SummarizationMiddleware, a path no agent
                # middleware wraps -- see BudgetMeterCallback.
                model=llm_for_role(config, "agent-summarizer", callbacks=[BudgetMeterCallback(tracker)],
                                   task_id=task_id),
                trigger=summarization_trigger(),
                keep=summarization_keep(),
                trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS,
            ),
            # Not included by default for a custom model like ours --
            # TodoListMiddleware is only auto-added for specific built-in
            # harness profiles, not universally, so it's added explicitly
            # here.
            TodoListMiddleware(),
            # ...and a reminder when the model stops maintaining the list it
            # just wrote -- the 0/12-until-done plan strip of 2026-09-11.
            StaleTodoMiddleware(),
            # Defense-in-depth backstop against a runaway loop -- see this
            # module's own comment on MODEL_CALL_RUN_LIMIT/TOOL_CALL_RUN_LIMIT
            # for why these are generous limits, not a normal-operation cap.
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        subagents=[general_purpose, investigator, test_writer],
        interrupt_on=interrupt_on,
        # No `memory=[...]` here -- see build_deep_agent's own docstring for
        # why: MemoryMiddleware's automatic loading is broken against
        # AsyncPostgresStore specifically. Content is already embedded
        # directly in system_prompt above via the confirmed-working async
        # read path instead.
        backend=backend,
        # Code-level enforcement, not a prompt instruction: org-memory is
        # populated by application code only (seed_org_memory) -- one
        # task's agent must never be able to alter what every other
        # project's agent believes is settled policy. Skills get the same
        # treatment -- curated reference material (seed_skill), not
        # something to drift via ad hoc mid-task edits the way /memories/
        # is deliberately allowed to.
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/org-memory/*"], mode="deny"),
            FilesystemPermission(operations=["write"], paths=["/skills/*"], mode="deny"),
        ],
        checkpointer=checkpointer,
        store=store,
    )

    return agent, tracker, last_failed_edit_ref
