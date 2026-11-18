from enum import StrEnum


class Role(StrEnum):
    SUBMITTER = "submitter"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    ADMIN = "admin"
