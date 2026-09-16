"""Classifies a task's goal at creation time -- purely for the Analytics
dashboard's own cost/count-by-category breakdown, and for injecting an
explicit test-coverage reminder into the goal when warranted. Never read by
the coordinator or anything that actually executes the task otherwise, so a
wrong or missing classification never affects real work, only how it's
later summarized (and, for needs_tests, how strongly the goal nudges toward
test-writer).
"""

import logging

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from agent.config import Config

logger = logging.getLogger("tektonix")

TASK_CATEGORIES = [
    "bug-fix",
    "feature",
    "ui-styling",
    "performance",
    "investigation",
    "other",
]


class TaskClassification(BaseModel):
    category: str = Field(description=f"Exactly one of: {', '.join(TASK_CATEGORIES)}")
    needs_tests: bool = Field(
        description=(
            "True if this task involves new or changed backend/business logic that should have "
            "real, verified test coverage. False for purely visual/styling changes, read-only "
            "investigation, or a trivial mechanical fix (e.g. renaming a value, updating a string)."
        )
    )


_CLASSIFY_PROMPT = """Classify the following task goal.

category -- exactly one of: {categories}.
- bug-fix: something is broken, behaving incorrectly, or needs correcting
- feature: new functionality or capability that doesn't exist yet
- ui-styling: a visual/layout/styling adjustment with no behavior change
- performance: speed, loading time, or efficiency improvement
- investigation: research or a read-only question, no code change expected
- other: doesn't clearly fit any of the above

needs_tests -- true only if the task involves new or changed backend/business logic worth real
test coverage (a calculation, a data transformation, an API endpoint, state that can go wrong).
False for styling, investigation, or a trivial one-line fix.

Goal:
{goal}"""

# Fallback used whenever classification itself fails (timeout, malformed
# reply, the alias unreachable) -- conservative on both fields: "other" is
# the default bucket, and needs_tests defaults to False so a classification
# failure never itself forces an unwanted test-writer delegation.
_FALLBACK = TaskClassification(category="other", needs_tests=False)


async def classify_task(goal: str, config: Config) -> TaskClassification:
    """Best-effort: any failure falls back to `_FALLBACK` rather than
    blocking task creation on a classification call that isn't essential to
    the task actually running.
    """
    model = ChatOpenAI(
        model="agent-classifier",
        base_url=config.router_base_url,
        api_key=config.router_api_key,
        temperature=0,
        timeout=15,
    ).with_structured_output(TaskClassification)
    try:
        result = await model.ainvoke([
            HumanMessage(content=_CLASSIFY_PROMPT.format(categories=", ".join(TASK_CATEGORIES), goal=goal[:2000])),
        ])
    except Exception as e:  # noqa: BLE001 -- classification is a nice-to-have, never worth blocking task creation
        logger.warning("task classification failed: %s", e)
        return _FALLBACK
    if not isinstance(result, TaskClassification) or result.category not in TASK_CATEGORIES:
        return _FALLBACK
    return result


TEST_REMINDER_NOTE = (
    "\n\n--- TEST COVERAGE ---\n"
    "This task looks like it involves new or changed logic worth real test coverage. If you write "
    "or change any testable logic, delegate that test-writing to the test-writer subagent via your "
    "task() tool -- don't skip this for anything beyond a trivial mechanical fix."
)
