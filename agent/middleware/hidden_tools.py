"""HiddenToolsMiddleware — withhold a tool the agent factory adds for us.

`create_deep_agent` does not treat `subagents=None` as "no subagents". Its
graph assembly does an unconditional `inline_subagents.insert(0,
general_purpose_spec)` (deepagents/graph.py), where that spec carries the
COORDINATOR'S OWN tools and model, and any non-empty subagent list builds a
`task` tool. So every deep agent gets `task` whether or not it asked for one,
and the model finds it in the tool schema regardless of what the system
prompt says.

For agent/planning_chat.py that was a live, expensive bug. That module is
built as one flat research conversation -- it says so twice ("planning chat
has no subagents", "no subagents/todos to multiplex"), passes no `subagents=`,
and its system prompt enumerates every tool the model should use without ever
mentioning `task`. The model found `task` anyway and improvised with it
("Let me launch parallel subagents to do deep investigation"), which is how a
planning turn on 2026-08-27 ran 115 model calls and 4.7M input tokens against
a 1800s ceiling and timed out with only ~22 tool calls in its own transcript
-- the rest happened inside nested agent loops.

It was also a budget hole. BudgetGuardMiddleware has to be attached to each
subagent spec by hand (see that module's docstring); an auto-added subagent
nobody knew about has no such attachment, so its spend is invisible to the
tracker. That same turn recorded $0.44 against $3.98 of real router spend.

agent/deep_agent.py solves this the other way, correctly, for build tasks:
it declares its own general-purpose spec by the same name (which suppresses
the auto-add, since graph.py matches by name) and attaches the guard and the
call limits to it. That is the right answer when you WANT subagents. This
middleware is the right answer when you do not: it filters the tool out of
every model request, so the model never sees it and never calls it.

Filtering at request time rather than at construction is deliberate -- it is
the only supported seam. The auto-add is unconditional, so there is no
constructor argument that prevents it.
"""

from langchain_core.messages import ToolMessage

from agent.harness_voice import HARNESS
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse


class HiddenToolsMiddleware(AgentMiddleware):
    """Withholds the named tools from every model request.

    The tools remain registered on the graph's ToolNode -- this hides them
    from the model rather than unbinding them, because the factory owns that
    registration. A model that never sees a tool in its schema does not call
    it, which is the property that matters.
    """

    def __init__(self, *names: str):
        super().__init__()
        if not names:
            msg = "HiddenToolsMiddleware needs at least one tool name to hide"
            raise ValueError(msg)
        self.hidden = frozenset(names)

    def _visible(self, request: ModelRequest) -> list:
        def name_of(t) -> str:
            # Tools reach a request either as BaseTool instances or as raw
            # OpenAI-style dicts, depending on what bound them.
            if isinstance(t, dict):
                return t.get("name") or (t.get("function") or {}).get("name") or ""
            return getattr(t, "name", "")

        return [t for t in request.tools if name_of(t) not in self.hidden]

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return handler(request.override(tools=self._visible(request)))

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return await handler(request.override(tools=self._visible(request)))

    def _refusal(self, request) -> ToolMessage:
        """Hidden-tool EXECUTION is intercepted too, not just the schema.

        Hiding a tool from the request stops a model choosing it fresh -- but
        a model resumed over a history FULL of its own calls to that tool can
        pattern-complete another one without the schema offering it (observed
        risk on 2026-08-27: a build resumed over ~100 historical grep calls).
        The ToolNode still has the tool registered (the factory owns that),
        so an emitted call would EXECUTE and return the same misleading
        result the hiding exists to prevent. This turns it into an
        instructive refusal instead.
        """
        name = request.tool_call.get("name", "")
        return ToolMessage(
            content=(
                f"{HARNESS} ERROR: the `{name}` tool is not available to you (it searches your own "
                f"memory/skills space, never the repo, and has been withdrawn). To search or "
                f"explore the REAL repo, use `bash` with rg/grep inside /workspace, or "
                f"read_project_file/list_project_dir if you have them. Do not call `{name}` again."
            ),
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        if request.tool_call.get("name") in self.hidden:
            return self._refusal(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        if request.tool_call.get("name") in self.hidden:
            return self._refusal(request)
        return await handler(request)
