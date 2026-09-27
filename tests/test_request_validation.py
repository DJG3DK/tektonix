"""audit M-33: POST /api/tasks request-model validation."""
import pytest
from pydantic import ValidationError

from agent.server import AttachmentEntry, CreateTaskRequest


def test_empty_goal_is_rejected():
    with pytest.raises(ValidationError):
        CreateTaskRequest(goal="", repo="x")


def test_negative_or_zero_budget_is_rejected():
    with pytest.raises(ValidationError):
        CreateTaskRequest(goal="do a thing", repo="x", budget_usd=0)
    with pytest.raises(ValidationError):
        CreateTaskRequest(goal="do a thing", repo="x", budget_usd=-5)


def test_absurd_budget_is_rejected():
    with pytest.raises(ValidationError):
        CreateTaskRequest(goal="do a thing", repo="x", budget_usd=10_000)


def test_a_valid_request_passes():
    req = CreateTaskRequest(goal="do a thing", repo="x", budget_usd=2.5)
    assert req.budget_usd == 2.5


def test_attachment_without_path_is_rejected():
    # {"kind": "image"} used to reach _attachments_note and KeyError -> 500.
    with pytest.raises(ValidationError):
        AttachmentEntry(kind="image")


def test_valid_attachment_parses():
    a = AttachmentEntry(kind="pdf", path=".uploads/b/x.pdf", pages=3)
    assert a.path.endswith("x.pdf") and a.pages == 3


def test_a_planning_message_validates_its_attachments_the_same_way():
    """The planning route took `list[dict]` and handed it to the same
    attachments note the task route validates for."""
    from agent.routers.planning import PlanningMessageRequest

    with pytest.raises(ValidationError):
        PlanningMessageRequest(text="look", attachments=[{"kind": "image"}])
    with pytest.raises(ValidationError):
        PlanningMessageRequest(text="look", attachments=[{"kind": "image", "path": {"x": 1}}])
    ok = PlanningMessageRequest(text="look", attachments=[{"kind": "image", "path": ".uploads/b/a.png"}])
    assert ok.attachments[0].path == ".uploads/b/a.png"
