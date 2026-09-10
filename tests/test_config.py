"""The environment layer, which is where a working repo meets a cold machine.

None of this touches the gate. It covers the two things that made a correct
system look broken: a ``.env`` the README told people to write and nothing read,
and a Bedrock refusal reported as the wrong cause.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pantryrelay import config  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    """No AWS or PantryRelay settings inherited from the developer's shell."""
    for var in (
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "PANTRYRELAY_MODEL_ID",
        "PANTRYRELAY_TEST_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_dotenv_sets_values(tmp_path, clean_env):
    path = write_env(tmp_path, "AWS_REGION=us-east-1\nPANTRYRELAY_MODEL_ID=some.model\n")
    applied = config.load_dotenv(path)

    assert applied == {"AWS_REGION": "us-east-1", "PANTRYRELAY_MODEL_ID": "some.model"}
    assert config.region() == "us-east-1"
    assert config.model_id() == "some.model"


def test_dotenv_skips_comments_and_blank_lines(tmp_path, clean_env):
    path = write_env(
        tmp_path,
        "# a comment\n\n   \nAWS_REGION=eu-west-1\nnot-a-pair\n",
    )
    assert config.load_dotenv(path) == {"AWS_REGION": "eu-west-1"}


def test_dotenv_strips_quotes_and_export(tmp_path, clean_env):
    path = write_env(tmp_path, "export PANTRYRELAY_TEST_KEY='quoted value'\n")
    assert config.load_dotenv(path) == {"PANTRYRELAY_TEST_KEY": "quoted value"}


def test_real_environment_wins_over_the_file(tmp_path, clean_env):
    """Exporting a variable for one run must not require editing .env."""
    clean_env.setenv("AWS_REGION", "ap-south-1")
    path = write_env(tmp_path, "AWS_REGION=us-east-1\n")

    assert config.load_dotenv(path) == {}
    assert os.environ["AWS_REGION"] == "ap-south-1"

    assert config.load_dotenv(path, override=True) == {"AWS_REGION": "us-east-1"}
    assert os.environ["AWS_REGION"] == "us-east-1"


def test_missing_dotenv_is_not_an_error(tmp_path, clean_env):
    assert config.load_dotenv(tmp_path / "nothing-here") == {}


class FakeClientError(Exception):
    """Stands in for botocore's ClientError, which carries the code in a dict."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def test_account_verification_is_not_reported_as_a_model_problem():
    """The failure that cost the most time to diagnose.

    A new AWS account has Bedrock refused wholesale, and the message says the
    account is being verified. Reading that as "the model is not enabled" sends
    someone to the model-access console, where there is nothing to fix.
    """
    exc = FakeClientError(
        "AccessDeniedException",
        "Your account is currently being verified. Verification normally takes "
        "less than 2 hours.",
    )
    problem, remedy = config.explain_client_error(exc)

    assert "activating" in problem
    assert "model" not in problem.lower()
    assert "verification" in remedy.lower()


def test_operation_not_allowed_is_not_a_waiting_problem():
    """The correction to the test that used to live here.

    "Operation not allowed" was read as the same thing as an unverified
    account, so the advice was to wait a few hours. It never clears: the
    control plane keeps answering while every model in every region is refused
    at invocation. Telling someone to wait costs them the day, so the remedy
    has to name something to do.
    """
    exc = FakeClientError("ValidationException", "Operation not allowed")
    problem, remedy = config.explain_client_error(exc)

    assert "activating" not in problem
    assert "verification" not in remedy.lower()
    assert "IAM user" in remedy
    assert "Support" in remedy


def test_operation_not_allowed_names_root_when_root_is_calling():
    """Root is the cause that is free to rule out, so it is named first."""
    problem, remedy = config._classify(
        "ValidationException",
        "Operation not allowed",
        model="global.anthropic.claude-opus-5",
        reg="us-east-1",
        arn="arn:aws:iam::905609278350:root",
    )

    assert "root" in problem
    assert "AmazonBedrockFullAccess" in remedy


def test_operation_not_allowed_stops_blaming_the_identity_once_iam_calls():
    """Once an IAM user is refused too, the identity is ruled out.

    Repeating "use an IAM user" to someone already signing as one sends them
    to redo the fix that just failed. The account is the only cause left, so
    the remedy has to name the case to open instead of listing both causes.
    """
    problem, remedy = config._classify(
        "ValidationException",
        "Operation not allowed",
        model="global.anthropic.claude-opus-5",
        reg="us-east-1",
        arn="arn:aws:iam::905609278350:user/pantryrelay-agent",
    )

    assert "account" in problem
    assert "root" not in remedy
    assert "AmazonBedrockFullAccess" not in remedy
    assert "Support" in remedy
    assert "Operation not allowed" in remedy


def test_denied_model_points_at_model_access(clean_env):
    clean_env.setenv("PANTRYRELAY_MODEL_ID", "global.anthropic.claude-opus-5")
    clean_env.setenv("AWS_REGION", "us-east-1")

    exc = FakeClientError("AccessDeniedException", "User is not authorized")
    problem, remedy = config.explain_client_error(exc)

    assert "global.anthropic.claude-opus-5" in problem
    assert "us-east-1" in remedy
    assert "bedrock:InvokeModel" in remedy


def test_unknown_model_points_at_the_region(clean_env):
    clean_env.setenv("PANTRYRELAY_MODEL_ID", "made.up.model")
    clean_env.setenv("AWS_REGION", "us-east-1")

    problem, remedy = config.explain_client_error(
        FakeClientError("ResourceNotFoundException", "not found")
    )
    assert "made.up.model" in problem and "us-east-1" in problem
    assert "PANTRYRELAY_MODEL_ID" in remedy


def test_bad_keys_are_named_as_bad_keys():
    problem, remedy = config.explain_client_error(
        FakeClientError("UnrecognizedClientException", "invalid token")
    )
    assert "credentials were rejected" in problem
    assert "aws configure" in remedy


def test_throttling_says_the_wiring_is_fine():
    problem, remedy = config.explain_client_error(
        FakeClientError("ThrottlingException", "slow down")
    )
    assert "throttling" in problem.lower()
    assert "wiring is fine" in remedy


def test_unknown_code_falls_through_with_its_message():
    problem, remedy = config.explain_client_error(
        FakeClientError("SomethingNew", "a message we do not recognise")
    )
    assert "SomethingNew" in problem
    assert "a message we do not recognise" in remedy


def test_preflight_result_is_falsey_when_not_ok():
    """`if preflight():` has to read the way it looks."""
    bad = config.Preflight(False, "p", "r", "us-east-1", "m")
    good = config.Preflight(True, "", "", "us-east-1", "m")
    assert not bad
    assert good
