"""Environment resolution and a Bedrock preflight.

Two jobs, both about the gap between "the code is correct" and "this machine can
run it".

The first is loading ``.env``. The README tells a reader to copy ``.env.example``
to ``.env``; nothing read it, so the documented setup silently did nothing and
``--live`` reported the wrong region back. A short parser is cheaper than a
dependency and cheaper than the confusion.

The second is :func:`preflight`. Bedrock refuses a call for at least five
different reasons and the fix is different for each one. Guessing "the model is
probably not enabled" is wrong often enough to send someone to the model-access
console when their real problem is that the account has not finished activating.
So this asks AWS directly and reports what it was told.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The repository root, two levels up from src/pantryrelay/.
REPO_ROOT = Path(__file__).resolve().parents[2]

FALLBACK_MODEL_ID = "global.anthropic.claude-opus-5"
FALLBACK_REGION = "us-west-2"

# Tried in order by `--check` when the configured model is refused. All are
# Bedrock-hosted Claude models; the point is to name one that works rather than
# leave the reader guessing which to put in .env.
CANDIDATE_MODELS = (
    "global.anthropic.claude-opus-5",
    "global.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)

_QUOTES = ("\"", "'")


def load_dotenv(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    """Read a ``.env`` file into the environment and return what it set.

    A real environment variable wins over the file unless ``override`` is set,
    so exporting a key for one run does not mean editing the file. A missing
    file is not an error: the offline demo needs no configuration at all.
    """
    env_path = Path(path) if path is not None else REPO_ROOT / ".env"
    if not env_path.is_file():
        return {}

    applied: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in _QUOTES and value[-1] == value[0]:
            value = value[1:-1]
        if not key:
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


def model_id() -> str:
    """Bedrock model id both agents use."""
    return os.getenv("PANTRYRELAY_MODEL_ID", FALLBACK_MODEL_ID)


def region() -> str:
    """Region for the Bedrock call.

    Falls back to whatever the AWS config already says before guessing, so a
    machine set up with ``aws configure`` needs no PantryRelay-specific setting.
    """
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.getenv(var)
        if value:
            return value
    try:
        import boto3

        session_region = boto3.Session().region_name
        if session_region:
            return session_region
    except Exception:  # pragma: no cover - boto3 absent or unconfigured
        pass
    return FALLBACK_REGION


@dataclass(frozen=True)
class Preflight:
    """What one attempt to reach Bedrock actually found."""

    ok: bool
    problem: str
    remedy: str
    region: str
    model_id: str
    account: str | None = None
    code: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _classify(code: str, message: str, *, model: str, reg: str) -> tuple[str, str]:
    """Turn one Bedrock error into a problem and a remedy a person can act on."""
    lowered = message.lower()

    if "being verified" in lowered or "operation not allowed" in lowered:
        return (
            "this AWS account has not finished activating",
            "Bedrock is refusing every model in every region for this account, "
            "not only this one. AWS clears new-account verification on its own, "
            "usually within a few hours. Nothing in the repo needs changing. "
            "Re-run this check later.",
        )
    if code == "AccessDeniedException":
        return (
            "Bedrock will not let this identity call " + repr(model),
            "Request access to the model in the Bedrock console for " + reg
            + ", and check the identity has bedrock:InvokeModel.",
        )
    if code == "ResourceNotFoundException":
        return (
            repr(model) + " does not exist in " + reg,
            "Set PANTRYRELAY_MODEL_ID in .env to a model id this region serves.",
        )
    if code in {"UnrecognizedClientException", "InvalidSignatureException"}:
        return (
            "the AWS credentials were rejected",
            "The access key or secret is wrong, or the key has been deleted. "
            "Run `aws configure` again.",
        )
    if code == "ExpiredTokenException":
        return ("the AWS session token has expired", "Refresh the session and retry.")
    if code == "ThrottlingException":
        return (
            "Bedrock is throttling this account",
            "The wiring is fine. Retry in a minute, or use a smaller model.",
        )
    return ("Bedrock refused the call (" + code + ")", message.strip() or "No detail given.")


def explain_client_error(exc: object) -> tuple[str, str]:
    """Problem and remedy for a botocore ClientError raised by a live run.

    The same classifier the preflight uses, so the sentence someone reads after
    a failed `--live` matches the one `--check` would have given them first.
    """
    response = getattr(exc, "response", {}) or {}
    code = response.get("Error", {}).get("Code", "Unknown")
    return _classify(code, str(exc), model=model_id(), reg=region())


def preflight(*, check_model: bool = True) -> Preflight:
    """Ask AWS whether a live run would work, and say precisely why not.

    Cheap enough to run before a demo: one STS call, and one Bedrock call
    capped at a handful of tokens.
    """
    reg, model = region(), model_id()

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:  # pragma: no cover - boto3 is a declared dependency
        return Preflight(
            False,
            "boto3 is not installed",
            "pip install -r requirements.txt",
            reg,
            model,
        )

    session = boto3.Session(region_name=reg)
    if session.get_credentials() is None:
        return Preflight(
            False,
            "no AWS credentials were found",
            "Run `aws configure`, or set AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY in the environment. Keys never belong in a "
            "file inside the repo.",
            reg,
            model,
        )

    account: str | None = None
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        problem, remedy = _classify(code, str(exc), model=model, reg=reg)
        return Preflight(False, problem, remedy, reg, model, code=code)
    except (BotoCoreError, NoCredentialsError) as exc:
        return Preflight(False, "AWS could not be reached", str(exc), reg, model)

    if not check_model:
        return Preflight(True, "", "", reg, model, account=account)

    try:
        session.client("bedrock-runtime").converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 8},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        problem, remedy = _classify(code, str(exc), model=model, reg=reg)
        return Preflight(False, problem, remedy, reg, model, account=account, code=code)
    except (BotoCoreError, NoCredentialsError) as exc:
        return Preflight(
            False, "AWS could not be reached", str(exc), reg, model, account=account
        )

    return Preflight(True, "", "", reg, model, account=account)


def working_models(candidates: tuple[str, ...] = CANDIDATE_MODELS) -> list[str]:
    """Which candidate model ids this account can actually call, in order.

    Used by ``--check`` to name a fallback instead of telling someone to go and
    find one for themselves.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.Session(region_name=region()).client("bedrock-runtime")
    usable: list[str] = []
    for candidate in candidates:
        try:
            client.converse(
                modelId=candidate,
                messages=[{"role": "user", "content": [{"text": "ping"}]}],
                inferenceConfig={"maxTokens": 8},
            )
        except (ClientError, BotoCoreError):
            continue
        usable.append(candidate)
    return usable
