"""SP0: service-layer DTO bases and the domain error hierarchy."""

import pytest
from pydantic import ValidationError

from durin.service.types import (
    Command,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    Query,
    Result,
    UnauthenticatedError,
    UnavailableError,
    ValidationFailedError,
)


class SampleCmd(Command):
    user_name: str


class SampleQuery(Query):
    page_size: int = 10


class SampleResult(Result):
    ok: bool


def test_command_accepts_snake_case():
    assert SampleCmd(user_name="a").user_name == "a"


def test_command_accepts_camel_case_alias():
    assert SampleCmd.model_validate({"userName": "a"}).user_name == "a"


def test_command_serializes_to_camel_by_alias():
    assert SampleCmd(user_name="a").model_dump(by_alias=True) == {"userName": "a"}


def test_command_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SampleCmd.model_validate({"userName": "a", "bogus": 1})


def test_query_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SampleQuery.model_validate({"pageSize": 5, "bogus": 1})


def test_result_ignores_extra_fields():
    # Results are built in code and stay forward-compatible: unknown keys are
    # ignored (pydantic default), not rejected.
    r = SampleResult.model_validate({"ok": True, "futureField": 99})
    assert r.ok is True


@pytest.mark.parametrize(
    "cls,code",
    [
        (UnauthenticatedError, "unauthenticated"),
        (ForbiddenError, "forbidden"),
        (NotFoundError, "not_found"),
        (ConflictError, "conflict"),
        (ValidationFailedError, "validation_failed"),
        (UnavailableError, "unavailable"),
    ],
)
def test_domain_error_codes(cls, code):
    err = cls("boom", details={"k": "v"})
    assert err.code == code
    assert err.message == "boom"
    assert err.details == {"k": "v"}
    assert str(err) == "boom"
    assert isinstance(err, DomainError)
    assert isinstance(err, Exception)


def test_domain_error_default_details_is_empty_dict():
    assert NotFoundError("x").details == {}


def test_base_domain_error_code():
    assert DomainError("x").code == "error"
