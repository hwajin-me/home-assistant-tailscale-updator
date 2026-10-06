"""Strict JSONC parsing with source spans for narrowly scoped policy edits."""

import ipaddress
import json
import re
from dataclasses import dataclass

CAPABILITY = "tailscale.com/app-connectors"


class PolicyError(ValueError):
    """Invalid policy or ambiguous connector selection."""


@dataclass
class Node:
    value: object
    start: int
    end: int
    children: dict | list | None = None


class Policy:
    """Parse JSON plus comments/trailing commas, retaining original source."""

    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        try:
            self.root = self._value()
            self._space()
            if self.pos != len(source) or not isinstance(self.root.value, dict):
                raise PolicyError("Policy must be a single JSONC object")
        except (ValueError, IndexError, RecursionError) as err:
            raise PolicyError("Invalid JSONC policy") from err

    def _space(self):
        while self.pos < len(self.source):
            if self.source[self.pos] in " \t\r\n":
                self.pos += 1
            elif self.source.startswith("//", self.pos):
                end = self.source.find("\n", self.pos)
                self.pos = len(self.source) if end < 0 else end + 1
            elif self.source.startswith("/*", self.pos):
                end = self.source.find("*/", self.pos + 2)
                if end < 0:
                    raise PolicyError("Unterminated comment")
                self.pos = end + 2
            else:
                break

    def _value(self):
        self._space()
        start = self.pos
        char = self.source[self.pos]
        if char not in "[{":
            value, length = json.JSONDecoder().raw_decode(self.source[self.pos :])
            if isinstance(value, float) and not float("-inf") < value < float("inf"):
                raise PolicyError("Non-finite number")
            self.pos += length
            return Node(value, start, self.pos)
        self.pos += 1
        is_object = char == "{"
        close = "}" if is_object else "]"
        children = {} if is_object else []
        self._space()
        while self.source[self.pos] != close:
            if is_object:
                key = self._value().value
                self._space()
                if (
                    not isinstance(key, str)
                    or key in children
                    or self.source[self.pos] != ":"
                ):
                    raise PolicyError("Invalid or duplicate object key")
                self.pos += 1
                children[key] = self._value()
            else:
                children.append(self._value())
            self._space()
            if self.source[self.pos] == close:
                break
            if self.source[self.pos] != ",":
                raise PolicyError("Missing comma")
            self.pos += 1
            self._space()
        self.pos += 1
        value = (
            {k: v.value for k, v in children.items()}
            if is_object
            else [v.value for v in children]
        )
        return Node(value, start, self.pos, children)

    def connectors(self) -> dict[str, Node]:
        """Return explicit-domain apps; reject duplicate names and malformed paths."""
        result = {}
        attrs = self.root.children.get("nodeAttrs")
        if attrs is None:
            return result
        if not isinstance(attrs.value, list):
            raise PolicyError("nodeAttrs must be an array")
        for attr in attrs.children:
            if not isinstance(attr.value, dict):
                raise PolicyError("Invalid nodeAttrs entry")
            app = attr.children.get("app")
            if app is None:
                continue
            if not isinstance(app.value, dict):
                raise PolicyError("Invalid app entry")
            apps = app.children.get(CAPABILITY)
            if apps is None:
                continue
            if not isinstance(apps.value, list):
                raise PolicyError("Invalid app connectors")
            for connector in apps.children:
                if not isinstance(connector.value, dict):
                    raise PolicyError("Invalid connector")
                name = connector.value.get("name")
                if not isinstance(name, str) or not name or name in result:
                    raise PolicyError("Missing or duplicate connector name")
                result[name] = connector
        return result

    def domains(self, name: str) -> list[str]:
        connector = self.connectors().get(name)
        if connector is None:
            raise PolicyError("App connector no longer exists")
        domains = connector.value.get("domains")
        if (
            "presetAppID" in connector.value
            or not isinstance(domains, list)
            or any(not isinstance(v, str) for v in domains)
        ):
            raise PolicyError(
                "Choose an app with an explicit domains array, not a preset"
            )
        return domains

    def set_domains(self, name: str, domains: list[str], enabled: bool) -> str:
        """Change complete parent/wildcard pairs for the requested domains."""
        return self.change_domain_pairs(
            name, domains if enabled else [], domains if not enabled else []
        )

    def change_domain_pairs(self, name: str, add: list[str], remove: list[str]) -> str:
        """Apply a batch of pair edits without touching other policy sections."""
        current = self.domains(name)
        add_bases = {domain_base(domain) for domain in add}
        remove_bases = {domain_base(domain) for domain in remove}
        if add_bases & remove_bases:
            raise PolicyError("A domain cannot be added and removed together")
        updated = [d for d in current if domain_base(d) not in remove_bases]
        existing = {normalize_domain(d) for d in updated}
        for base in sorted(add_bases):
            for member in (base, f"*.{base}"):
                if member not in existing:
                    updated.append(member)
                    existing.add(member)
        if current == updated:
            return self.source
        node = self.connectors()[name].children["domains"]
        # Only this array is serialized; all unrelated text is byte-for-byte intact.
        return (
            self.source[: node.start]
            + json.dumps(updated, ensure_ascii=False)
            + self.source[node.end :]
        )


def normalize_domain(value: str) -> str:
    """Accept DNS names and a leading wildcard; reject URLs, IPs and paths."""
    value = value.strip().lower().rstrip(".")
    # Accept leading-dot input as shorthand, but emit Tailscale's bare parent.
    if value.startswith("."):
        value = value[1:]
    wildcard = value.startswith("*.")
    host = value[2:] if wildcard else value
    try:
        host = host.encode("idna").decode("ascii")
        ipaddress.ip_address(host)
    except UnicodeError as err:
        raise PolicyError("Invalid domain") from err
    except ValueError:
        pass
    else:
        raise PolicyError("Use a DNS name, not an IP address")
    if (
        len(host) > 253
        or not host
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        )
    ):
        raise PolicyError("Invalid domain")
    return ("*." if wildcard else "") + host


def domain_base(value: str) -> str:
    """Map a parent or wildcard member to one switch identity."""
    normalized = normalize_domain(value)
    return normalized[2:] if normalized.startswith("*.") else normalized


def domain_bases(domains: list[str]) -> set[str]:
    return {domain_base(domain) for domain in domains}
