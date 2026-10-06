"""Policy editing must never touch unrelated grants or comments."""

import json

import pytest

from custom_components.tailscale_updator.policy import (
    Policy,
    PolicyError,
    domain_bases,
    normalize_domain,
)

POLICY = """{
 // global comment: https://example.com
 "grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"],}],
 "nodeAttrs": [{"target": ["*"], "app": {
  "tailscale.com/app-connectors": [
   {"name": "Streaming", "connectors": ["tag:exit"],
    "domains": [/* local comment */ "example.com", "*.example.com",]},
   {"name": "Other", "domains": ["other.com"]},
  ],
 }}],
 "note": "escaped \\" and // not a comment",
}"""


def test_narrow_edit():
    source = POLICY
    policy = Policy(source)
    node = policy.connectors()["Streaming"].children["domains"]
    result = policy.set_domains("Streaming", ["new.example.com"], True)
    assert result.startswith(source[: node.start])
    assert result.endswith(source[node.end :])
    assert Policy(result).domains("Streaming") == [
        "example.com",
        "*.example.com",
        "new.example.com",
        "*.new.example.com",
    ]
    assert Policy(result).domains("Other") == ["other.com"]
    assert Policy(result).root.value["grants"] == policy.root.value["grants"]


def test_idempotent_and_pair_removal():
    assert Policy(POLICY).set_domains("Streaming", ["EXAMPLE.COM."], True) == POLICY
    result = Policy(POLICY).set_domains("Streaming", ["example.com"], False)
    assert Policy(result).domains("Streaming") == []
    assert Policy(result).set_domains("Streaming", ["*.example.com"], False) == result
    restored = Policy(result).set_domains("Streaming", [".example.com"], True)
    assert Policy(restored).domains("Streaming") == [
        "example.com",
        "*.example.com",
    ]


def test_partial_pair_and_atomic_rename():
    source = POLICY.replace('"example.com", "*.example.com",', '"*.example.com",')
    updated = Policy(source).set_domains("Streaming", ["example.com"], True)
    assert Policy(updated).domains("Streaming") == ["*.example.com", "example.com"]
    renamed = Policy(updated).change_domain_pairs(
        "Streaming", ["new.com"], ["example.com"]
    )
    assert Policy(renamed).domains("Streaming") == ["new.com", "*.new.com"]
    assert Policy(renamed).domains("Other") == ["other.com"]
    assert domain_bases(["*.new.com", "new.com"]) == {"new.com"}


@pytest.mark.parametrize(
    "source",
    [
        '{"a":1,"a":2}',
        "{",
        "{/*",
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1} extra',
        '{"a":1 "b":2}',
        '{"a": [1,,]}',
        "[]",
        '{"a": "bad\\x"}',
    ],
)
def test_reject_invalid_policy(source):
    with pytest.raises(PolicyError):
        Policy(source)


def test_duplicate_names_and_presets_are_rejected():
    data = Policy(POLICY).root.value
    apps = data["nodeAttrs"][0]["app"]["tailscale.com/app-connectors"]
    apps[1]["name"] = "Streaming"
    with pytest.raises(PolicyError):
        Policy(json.dumps(data)).domains("Streaming")
    apps.pop()
    apps[0]["presetAppID"] = "github"
    with pytest.raises(PolicyError):
        Policy(json.dumps(data)).set_domains("Streaming", ["a.com"], True)
    with pytest.raises(PolicyError):
        Policy(POLICY).domains("missing")


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.com",
        "a/b",
        "127.0.0.1",
        "*",
        "a..com",
        "-a.com",
        "a_.com",
        "a" * 64 + ".com",
        "",
    ],
)
def test_invalid_domains(domain):
    with pytest.raises(PolicyError):
        normalize_domain(domain)


def test_normalization():
    assert normalize_domain(" *.EXAMPLE.com. ") == "*.example.com"
    assert normalize_domain("한글.com").endswith(".com")
    assert normalize_domain(".EXAMPLE.com") == "example.com"
