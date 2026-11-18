"""The state machine, exhaustively: every status x action x role. The allowed transitions are
listed explicitly; everything not listed must raise."""

import itertools

import pytest

from docflow.models import SubmissionStatus as S
from docflow.roles import Role
from docflow.workflow import (
    Action,
    Actor,
    InvalidTransitionError,
    NotPermittedError,
    State,
    transition,
)

STAGES = [Role.REVIEWER, Role.APPROVER]

# (status, stage, action, role, is_owner) -> resulting (status, stage)
ALLOWED: dict[tuple[S, int | None, Action, Role, bool], tuple[S, int | None]] = {
    # stage 1: the reviewer
    (S.IN_REVIEW, 1, Action.EDIT_FIELDS, Role.REVIEWER, False): (S.IN_REVIEW, 1),
    (S.IN_REVIEW, 1, Action.RAISE_QUERY, Role.REVIEWER, False): (S.QUERY_RAISED, 1),
    (S.IN_REVIEW, 1, Action.APPROVE, Role.REVIEWER, False): (S.IN_REVIEW, 2),
    (S.IN_REVIEW, 1, Action.REJECT, Role.REVIEWER, False): (S.REJECTED, None),
    (S.QUERY_RAISED, 1, Action.REJECT, Role.REVIEWER, False): (S.REJECTED, None),
    # stage 2: the approver
    (S.IN_REVIEW, 2, Action.EDIT_FIELDS, Role.APPROVER, False): (S.IN_REVIEW, 2),
    (S.IN_REVIEW, 2, Action.RAISE_QUERY, Role.APPROVER, False): (S.QUERY_RAISED, 2),
    (S.IN_REVIEW, 2, Action.APPROVE, Role.APPROVER, False): (S.APPROVED, None),
    (S.IN_REVIEW, 2, Action.REJECT, Role.APPROVER, False): (S.REJECTED, None),
    (S.QUERY_RAISED, 2, Action.REJECT, Role.APPROVER, False): (S.REJECTED, None),
    # answering returns to the SAME stage
    (S.QUERY_RAISED, 1, Action.ANSWER_QUERIES, Role.SUBMITTER, True): (S.IN_REVIEW, 1),
    (S.QUERY_RAISED, 2, Action.ANSWER_QUERIES, Role.SUBMITTER, True): (S.IN_REVIEW, 2),
    # an admin who uploaded an invoice is its owner too
    (S.QUERY_RAISED, 1, Action.ANSWER_QUERIES, Role.ADMIN, True): (S.IN_REVIEW, 1),
    (S.QUERY_RAISED, 2, Action.ANSWER_QUERIES, Role.ADMIN, True): (S.IN_REVIEW, 2),
    # admin recovery
    (S.EXTRACTION_FAILED, None, Action.RETRY_EXTRACTION, Role.ADMIN, False): (S.UPLOADED, None),
    (S.EXTRACTION_FAILED, None, Action.RETRY_EXTRACTION, Role.ADMIN, True): (S.UPLOADED, None),
    (S.UPLOADED, None, Action.RETRY_EXTRACTION, Role.ADMIN, False): (S.UPLOADED, None),
    (S.UPLOADED, None, Action.RETRY_EXTRACTION, Role.ADMIN, True): (S.UPLOADED, None),
}


def stage_for(status: S) -> list[int | None]:
    """The stages a submission can actually have in each status."""
    return [1, 2] if status in (S.IN_REVIEW, S.QUERY_RAISED) else [None]


ALL_CASES = [
    (status, stage, action, role, owner)
    for status, action, role, owner in itertools.product(S, Action, Role, [False, True])
    for stage in stage_for(status)
]


@pytest.mark.parametrize(("status", "stage", "action", "role", "owner"), ALL_CASES)
def test_transition_table(
    status: S, stage: int | None, action: Action, role: Role, owner: bool
) -> None:
    state, actor = State(status, stage), Actor(role, owner)
    expected = ALLOWED.get((status, stage, action, role, owner))

    if expected is not None:
        assert transition(state, action, actor, STAGES) == State(*expected)
    else:
        with pytest.raises((InvalidTransitionError, NotPermittedError)):
            transition(state, action, actor, STAGES)


def test_wrong_state_is_a_conflict_not_a_permission_error() -> None:
    with pytest.raises(InvalidTransitionError):
        transition(State(S.APPROVED, None), Action.APPROVE, Actor(Role.APPROVER, False), STAGES)


def test_wrong_role_in_the_right_state_is_a_permission_error() -> None:
    with pytest.raises(NotPermittedError):
        transition(State(S.IN_REVIEW, 2), Action.APPROVE, Actor(Role.REVIEWER, False), STAGES)


def test_more_stages_come_from_configuration() -> None:
    """ONGC-style four-step chain: only the configuration changes."""
    stages = [Role.REVIEWER, Role.APPROVER]
    state = State(S.IN_REVIEW, 1)
    state = transition(state, Action.APPROVE, Actor(Role.REVIEWER, False), stages)
    assert state == State(S.IN_REVIEW, 2)
    single = [Role.APPROVER]
    assert transition(
        State(S.IN_REVIEW, 1), Action.APPROVE, Actor(Role.APPROVER, False), single
    ) == State(S.APPROVED, None)
