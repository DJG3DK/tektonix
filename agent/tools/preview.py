"""Looking at the app it just changed.

A frontend task used to be done blind: edit the CSS, run the tests, and
neither the agent nor the reviewer ever saw the page. `preview_app` closes
that -- it starts the project the way the project starts, waits for it to
serve, and renders it in a real browser so the agent can see what it did.

Two things it is careful about.

The container is torn down in a finally, always. A dev server left running on
a published port outlives the task that started it, and the next one gets a
port conflict from a process nobody remembers.

And the browser's SSRF guard stays on. The model chooses the command and the
path; this module chooses the host and the port. So "preview my app" cannot
become a way to fetch 127.0.0.1:4101 or a cloud metadata endpoint -- exactly
one loopback origin is allowed, the one this process just allocated.
"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

from agent.tools.sandbox import (
    preview_logs,
    start_preview_container,
    stop_preview_container,
    wait_for_preview,
)

logger = logging.getLogger("tektonix")

# Long enough for a cold `next dev` or a Vite first build on a busy box;
# short enough that a server which will never come up does not eat a task.
_READY_TIMEOUT_S = 150


def make_preview_tool(cwd_for_repo):
    """`preview_app`, bound to one project's workspace.

    `cwd_for_repo` is a callable so the tool cannot be pointed at another
    project by anything the model says -- same shape as the other per-project
    tools.
    """
    from agent.tools.tool_errors import tool_errors_to_text  # noqa: PLC0415

    @tool
    @tool_errors_to_text
    async def preview_app(command: str, port: int, path: str = "/", question: str = "") -> str:
        """Run this project and LOOK at it in a real browser.

        Use it when you have changed anything a person sees -- layout, colour,
        spacing, a component, a template -- because the tests do not check how
        it looks and neither does the reviewer.

        `command` starts the app in the foreground the way the project starts
        it (for example "npm run dev -- --host 0.0.0.0 --port 5173"). It must
        listen on 0.0.0.0, not localhost, or nothing outside the container can
        reach it. `port` is the port that command listens on. `path` is the
        page to open. `question` narrows what you are told about the render --
        ask about the thing you changed.

        Returns the page's visible text plus a description of how it actually
        looks. The app is stopped again before this returns.
        """
        if not command.strip():
            return "ERROR: give the command that starts the app"
        started = await start_preview_container(command, cwd_for_repo(), port)
        if not started.get("ok"):
            return f"ERROR: {started.get('error', 'could not start the app')}"

        container = started["container"]
        try:
            ready = await wait_for_preview(started["port"], container, timeout=_READY_TIMEOUT_S)
            if not ready.get("ok"):
                logs = ready.get("logs") or ""
                return (f"ERROR: {ready.get('error')}\n\n"
                        f"The app's own output:\n{logs[-2000:]}")

            from agent.tools.planning_tools import run_browse_page_on_origin  # noqa: PLC0415

            url = started["url"] + (path if path.startswith("/") else f"/{path}")
            rendered = await run_browse_page_on_origin(url, started["url"], question)
            logs = await preview_logs(container, tail=25)
            if logs.strip():
                rendered += f"\n\n--- the app's own output while that loaded ---\n{logs[-1500:]}"
            return rendered
        finally:
            await stop_preview_container(container)

    return preview_app
