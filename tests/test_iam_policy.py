"""The documented policy and the calls the code makes must be the same set.

README.md publishes the exact IAM policy a user should grant, with the words
"Grant exactly this and no more". :data:`REQUIRED_IAM_ACTIONS`
(``dora_roi.collectors.aws``) is built from reading every ``readonly()`` call
site plus the declared ``sts:AssumeRole``/``sts:GetCallerIdentity``
exceptions. This test is what keeps the two from drifting apart again: one
permission too many is a tool asking for access it never uses; one too few is
a sweep that fails in the field.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from dora_roi.collectors.aws import REQUIRED_IAM_ACTIONS

README = Path(__file__).parent.parent / "README.md"

#: Anchors on the section header so a stray ```json``` block added anywhere
#: else in the document can never be mistaken for the IAM policy — a test
#: that just grabbed "the first ```json``` block" would pass vacuously the
#: day a second one is added above it.
_SECTION = re.compile(
    r"## What dora-roi is allowed to do to your AWS account.*?```json\n(?P<block>\{.*?\})\n```",
    re.DOTALL,
)


def _documented_policy() -> dict[str, object]:
    text = README.read_text(encoding="utf-8")
    match = _SECTION.search(text)
    assert match, (
        "README.md no longer has a ```json``` policy block under "
        "'## What dora-roi is allowed to do to your AWS account' — "
        "did the section get renamed or the block removed?"
    )
    try:
        policy = json.loads(match.group("block"))
    except json.JSONDecodeError as e:
        raise AssertionError(f"the README's IAM policy block is not valid JSON: {e}") from e
    assert isinstance(policy, dict) and "Statement" in policy, "the README's policy block has no 'Statement'"
    return policy


def _documented_actions() -> list[str]:
    policy = _documented_policy()
    statement = policy["Statement"]
    assert isinstance(statement, list) and statement, "the README's policy has no statements"
    actions = statement[0].get("Action")
    assert isinstance(actions, list) and actions, "the README's policy statement has no 'Action' list"
    return actions


class TestReadmePolicyMatchesTheCode:
    def test_required_iam_actions_is_not_empty(self) -> None:
        """A vacuous ``REQUIRED_IAM_ACTIONS == set()`` would make every other
        assertion here pass for the wrong reason: an empty README block would
        equal an empty constant. Guard the constant itself first."""
        assert len(REQUIRED_IAM_ACTIONS) > 0

    def test_the_readme_declares_a_real_policy_block(self) -> None:
        actions = _documented_actions()
        assert len(actions) > 0

    def test_the_readme_has_no_duplicate_actions(self) -> None:
        """A copy-pasted duplicate would silently collapse into the same set
        as a missing action elsewhere — catch the duplicate itself, not just
        the set it happens to still add up to."""
        actions = _documented_actions()
        assert len(actions) == len(set(actions)), f"the README lists a duplicate action: {actions}"

    def test_the_readme_grants_exactly_what_the_code_calls(self) -> None:
        """One permission too many is a tool asking for access it never uses;
        one too few is a sweep that fails in the field.

        Plain ``set`` equality on ``str`` is case-sensitive by construction —
        no ``casefold()`` anywhere in this comparison — so a policy that got
        the case of a service or verb wrong (``S3:Getobject`` is not
        ``s3:GetObject``) fails here rather than sliding through as a match.
        """
        documented = set(_documented_actions())
        extra = documented - REQUIRED_IAM_ACTIONS
        missing = REQUIRED_IAM_ACTIONS - documented
        assert not extra, f"README grants actions no code calls: {sorted(extra)}"
        assert not missing, f"code calls actions the README never grants: {sorted(missing)}"
        assert documented == REQUIRED_IAM_ACTIONS
