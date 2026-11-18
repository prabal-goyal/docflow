"""The review state machine. Pure: given a state, an action and who is acting, it returns the next
state or raises. Every rule about who may do what, and when, lives here."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from docflow.models import SubmissionStatus
from docflow.roles import Role


class Action(StrEnum):
    EDIT_FIELDS = "edit_fields"
    RAISE_QUERY = "raise_query"
    ANSWER_QUERIES = "answer_queries"
    APPROVE = "approve"
    REJECT = "reject"
    RETRY_EXTRACTION = "retry_extraction"


class InvalidTransitionError(Exception):
    """The action is not possible in the submission's current state (HTTP 409)."""


class NotPermittedError(Exception):
    """The action is possible, but not by this user (HTTP 403)."""


@dataclass(frozen=True)
class State:
    status: SubmissionStatus
    stage: int | None


@dataclass(frozen=True)
class Actor:
    role: Role
    is_owner: bool  # submitted this invoice


# Which statuses each action may start from.
ALLOWED_FROM: dict[Action, frozenset[SubmissionStatus]] = {
    Action.EDIT_FIELDS: frozenset({SubmissionStatus.IN_REVIEW}),
    Action.RAISE_QUERY: frozenset({SubmissionStatus.IN_REVIEW}),
    Action.ANSWER_QUERIES: frozenset({SubmissionStatus.QUERY_RAISED}),
    Action.APPROVE: frozenset({SubmissionStatus.IN_REVIEW}),
    Action.REJECT: frozenset({SubmissionStatus.IN_REVIEW, SubmissionStatus.QUERY_RAISED}),
    Action.RETRY_EXTRACTION: frozenset(
        {SubmissionStatus.EXTRACTION_FAILED, SubmissionStatus.UPLOADED}
    ),
}

STAGE_ACTIONS = frozenset({Action.EDIT_FIELDS, Action.RAISE_QUERY, Action.APPROVE, Action.REJECT})
# Roles that can upload, and so can own a submission and answer queries on it.
UPLOADER_ROLES = frozenset({Role.SUBMITTER, Role.ADMIN})


def stage_role(stage: int, stages: Sequence[Role]) -> Role:
    """Stages are numbered from 1."""
    return stages[stage - 1]


def transition(state: State, action: Action, actor: Actor, stages: Sequence[Role]) -> State:
    if state.status not in ALLOWED_FROM[action]:
        raise InvalidTransitionError(f"Cannot {action.value} a submission that is {state.status}")
    check_permission(state, action, actor, stages)

    match action:
        case Action.EDIT_FIELDS:
            return state
        case Action.RAISE_QUERY:
            return State(SubmissionStatus.QUERY_RAISED, state.stage)
        case Action.ANSWER_QUERIES:
            return State(SubmissionStatus.IN_REVIEW, state.stage)
        case Action.APPROVE:
            if state.stage is None:  # unreachable: check_permission rejects a missing stage
                raise InvalidTransitionError("Submission in review has no stage")
            if state.stage < len(stages):
                return State(SubmissionStatus.IN_REVIEW, state.stage + 1)
            return State(SubmissionStatus.APPROVED, None)
        case Action.REJECT:
            return State(SubmissionStatus.REJECTED, None)
        case Action.RETRY_EXTRACTION:
            return State(SubmissionStatus.UPLOADED, None)


def check_permission(state: State, action: Action, actor: Actor, stages: Sequence[Role]) -> None:
    if action in STAGE_ACTIONS:
        if state.stage is None or actor.role != stage_role(state.stage, stages):
            raise NotPermittedError(f"Only the stage {state.stage} role may {action.value}")
        # Defence in depth: today reviewers cannot upload, but nobody may review their own invoice
        # even if that changes.
        if actor.is_owner:
            raise NotPermittedError("You cannot review your own submission")
    elif action == Action.ANSWER_QUERIES:
        if not actor.is_owner or actor.role not in UPLOADER_ROLES:
            raise NotPermittedError("Only the submitter can answer queries on this submission")
    elif action == Action.RETRY_EXTRACTION and actor.role != Role.ADMIN:
        raise NotPermittedError("Only an admin can retry extraction")
