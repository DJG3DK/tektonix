# Middleware: what each one forbids, and where it is attached

Every model call in this system passes through a middleware chain. Each entry
in that chain exists because something went wrong once, and each one is a
*code-level* rule rather than a line in a prompt — the difference between
"the model was told not to" and "the model cannot".

The modules under `agent/middleware/` carry the full incident that produced
them, in comments, at length. That is the right place for the story and the
wrong place to look when the question is "which agent has the budget guard?"
This page is the index; the file is the story.

## The chains

Seven agents run in this system, and each builds its own `middleware=[...]`
list. A subagent does **not** inherit the coordinator's chain: deepagents
merges a subagent spec's middleware by name and appends the rest, so anything
missing from a subagent's own list is simply absent there. That is how a
general-purpose subagent once ran with no budget ceiling at all.

| Middleware | What it forbids | coordinator | investigator | test-writer | general-purpose | verifier | planner | consolidation |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `BudgetGuardMiddleware` | Spending past the task's dollar ceiling. Raises after each model call. | ● | ● | ● | ● | ● | ● | ● |
| `ModelCallLimitMiddleware` | More model calls in one run than the configured limit; an error on the coordinator, the end of the run on a subagent. | ● | ● | ● | ● | ● | ● | ● |
| `ToolCallLimitMiddleware` | More tool calls in one run than the configured limit (30 on the verifier, 60 on the test-writer). | ● | ● | ● | ● | ● | ● | ● |
| `SanitizeToolCallsMiddleware` | A malformed tool call in history reaching a provider. | ● | ● | ● | ● | ● | ● | |
| `EmptyReplyRetryMiddleware` | A reply that is nothing but exhausted reasoning reaching the conversation; retried once on the fallback seat, at most 30 times per invocation. | ● | ● | ● | ● | ● | | |
| `HiddenToolsMiddleware` | Tools the factory adds unasked (`task`, `glob`, `grep`, `execute`, `delete`). | ● | ● | ● | ● | ● | ● | |
| `RepeatCallGuardMiddleware` | Running the same call with the same result a third time, or the same call an eighth time whatever the result. | ● | ● | ● | ● | ● | ● | |
| `SummarizationMiddleware` | Unbounded context growth (library). | ● | | | | | ● | |
| `TodoListMiddleware` | — supplies `write_todos` (library). | ● | | | | | | |
| `StaleTodoMiddleware` | Letting a written plan go stale while work continues. | ● | | | | | | | |
| `StepBackMiddleware` | Circling past a third or two-thirds of the budget, or 45/90 minutes into a pass, without restating the goal and the evidence. | ● | | | | | | |
| `WrapUpMiddleware` | A bounded subagent hitting its tool-call cap with its findings unsent. | | | ● | | ● | | |
| `PlanCodeModelMiddleware` | One model doing both planning and coding. | ● | | | | | | |
| `BriefFirstMiddleware` | Reading the repo before the request is written down. | | | | | | ● | |
| `PinnedBriefMiddleware` | Compaction dropping the operator's own request. | | | | | | ● | |

`HumanInTheLoopMiddleware` is not in any of those lists: deepagents builds it
from the `interrupt_on` mapping that the coordinator and all four subagents
pass. That mapping is what gates a destructive command — see
`approval_gates` and `interrupt_on_for` in `agent/deep_agent.py`. A
benchmark project passes an empty mapping: nobody is there to answer.

`tests/test_middleware_inventory.py` compares this table against the real
chains, so a middleware added to an agent and not to this page fails the
suite. If you are here because that test failed, add the row; do not delete
the test.

## What each one actually does

**`BudgetGuardMiddleware`** (`budget_guard.py`) — the hard dollar ceiling, and
one of the two guards this system enforces in code rather than in a prompt. It
checks after every individual model call, inside the call, so the failure is a
Python exception rather than an instruction the model can decline. Attach it
to every new subagent by hand; a spec without it spends invisibly.
`BudgetMeterCallback` is the same accounting for models invoked outside any
middleware, which is how `SummarizationMiddleware`'s own model is metered.

**`ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`** (library) — generous
backstops against a runaway loop, not normal-operation caps. The limits are
runtime knobs (`model_call_run_limit`, `tool_call_run_limit`). Reaching one
is an error on the coordinator (the pass escalates) and the end of the run
on a subagent (the coordinator gets what it has). The verifier and the
test-writer cap tool calls tighter, at 30 and 60 (`VERIFIER_TOOL_CALLS`,
`TEST_WRITER_TOOL_CALLS`), and LangChain refuses two instances of one
middleware class on an agent, so each seat carries one limit at the tighter
of the two numbers.

**`SanitizeToolCallsMiddleware`** (`sanitize_tool_calls.py`) — strips malformed
tool calls out of the message history before it is serialized. A truncated
call stays in the thread forever, and strict providers reject the whole
request because of it: one such call answered 37 consecutive turns with a 400.

**`HiddenToolsMiddleware`** (`hidden_tools.py`) — withholds tools the agent
factory adds whether or not they were asked for. `subagents=None` does not
mean no subagents, so every deep agent gets a `task` tool; the built-in
`glob`/`grep` search the agent's own file space rather than the repo, and
answer "no matches found" for strings that plainly exist.

**`RepeatCallGuardMiddleware`** (`repeat_guard.py`) — the same tool call with
the same result is served from cache the third time instead of run again,
refused from the fourth, and past `BREAK_AT` refusals the run ends. Results
are compared with addresses, durations, timestamps and temp paths stripped,
and from the eighth identical call (`HARD_REPEAT_AT`) the result is not
consulted at all; different calls returning the same long output eight times
get a note too. A coder once issued one identical `bash` command fourteen
times in a row, each at 50k tokens of context, and later one 352 times whose
output carried an object address. On a subagent (`contain=True`) a stuck run
ends with a report to the coordinator; on the coordinator it raises
`RepeatLoopError` and the pass moves to the fallback seat. Counts reset per
invocation.

**`StaleTodoMiddleware`** (`todo_nag.py`) — reminds the coordinator to update
the plan it wrote. Without it a twelve-item task sat at 0/12 for two hours and
snapped to 12/12 at the end; nothing was broken except that the list was never
touched.

**`StepBackMiddleware`** (`step_back.py`) — at a third and two-thirds of the
task's budget, and 45 and 90 minutes into a pass, the coordinator's next call
asks it to restate what should happen, what its change does and what evidence
shows it works, and to finish if that evidence has stopped moving. The
benchmark tasks that failed were the ones that circled for two hours.

**`WrapUpMiddleware`** (`wrap_up.py`) — counts down the verifier's and the
test-writer's tool calls in their own results before the cap, and hands the
last model call no tools at all, so a report comes back instead of "Tool call
limit reached" and nothing.

**`EmptyReplyRetryMiddleware`** (`empty_reply.py`) — a reply with no content
and no tool calls (reasoning that ran to the output cap) is retried once on
the fallback seat before the conversation sees it, at most 30 times per
invocation. On the coordinator it sits after `PlanCodeModelMiddleware`, which
sets the model on every call and would otherwise replace the retry's model.

**`PlanCodeModelMiddleware`** (`model_pin.py`) — the coordinator's turn that
answers fresh outer input (the goal, a loopback, an operator message) runs on
the planner model, every turn after on the coder model. Deterministic, not
classified: a turn is a planning turn exactly when a `HumanMessage` arrived
after the model's last own message.

**`BriefFirstMiddleware` / `PinnedBriefMiddleware`** (`pinned_brief.py`) — the
planner writes the brief before it may read anything, and that brief is pinned
into the system message so compaction cannot drop it. The operator's request
is the oldest content in the window and therefore the first thing a summarizer
throws away.

## Adding one

Attaching middleware to a new subagent is the usual reason to read this page.
The order in the list is the order it wraps, so put the sanitizer before
anything that inspects messages, the budget guard before anything that
calls a model, and anything that overrides the model after
`PlanCodeModelMiddleware`. One instance per class per agent, and one
instance serves every invocation of a subagent, so per-run state is reset in
`before_agent`. Then add the row above, run the inventory test, and put the
incident that motivated it in the module — not here.
