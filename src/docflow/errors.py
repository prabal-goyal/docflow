"""Domain errors and their HTTP mapping, so service code never imports FastAPI."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from docflow.fieldpaths import InvalidFieldPathError
from docflow.workflow import InvalidTransitionError, NotPermittedError


class NotFoundError(Exception):
    """The resource does not exist, or this user may not know it exists (HTTP 404)."""


class InvalidRequestError(Exception):
    """The request is well-formed but breaks a business rule (HTTP 422)."""


STATUS_FOR: dict[type[Exception], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    NotPermittedError: status.HTTP_403_FORBIDDEN,
    InvalidTransitionError: status.HTTP_409_CONFLICT,
    InvalidRequestError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidFieldPathError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def register_error_handlers(app: FastAPI) -> None:
    for error_class, status_code in STATUS_FOR.items():

        def handler(_request: Request, error: Exception, code: int = status_code) -> JSONResponse:
            return JSONResponse(status_code=code, content={"detail": str(error)})

        app.add_exception_handler(error_class, handler)
