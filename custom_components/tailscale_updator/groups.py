"""Home Assistant-only domain groups; never written to Tailscale policy metadata."""

import uuid

from homeassistant.helpers import entity_registry as er

from .const import CONF_DOMAIN_GROUPS, DOMAIN
from .devices import remove_group_device
from .policy import PolicyError, domain_base, domain_bases
from .registry import remembered_pairs


def available_pairs(hass, entry):
    pairs = remembered_pairs(hass, entry)
    policy = entry.runtime_data.data.policy
    for connector in policy.connectors():
        try:
            pairs.update(
                (connector, domain)
                for domain in domain_bases(policy.domains(connector))
            )
        except PolicyError:
            continue
    return pairs


def group_unique_id(entry_id, group_id):
    return f"{entry_id}:group:{group_id}"


def save_group(
    hass, entry, name, members, group_id=None, *, require_existing=False, expected=None
):
    """Create or update local membership without any remote policy write."""
    name = name.strip()
    if not name or not isinstance(members, list) or not members:
        raise PolicyError("A group needs a name and at least one domain")
    pairs = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {"connector", "domain"}:
            raise PolicyError("Invalid group member")
        if not isinstance(member["connector"], str) or not isinstance(
            member["domain"], str
        ):
            raise PolicyError("Invalid group member")
        pairs.add((member["connector"], domain_base(member["domain"])))
    if not pairs <= available_pairs(hass, entry):
        raise PolicyError("Select existing or remembered domains")
    groups = dict(entry.options.get(CONF_DOMAIN_GROUPS, {}))
    if require_existing and group_id not in groups:
        raise PolicyError("Group no longer exists; reopen the editor")
    if expected is not None and groups.get(group_id) != expected:
        raise PolicyError("Group changed; reopen the editor")
    group_id = group_id or uuid.uuid4().hex
    if any(
        key != group_id and value["name"].casefold() == name.casefold()
        for key, value in groups.items()
    ):
        raise PolicyError("A group with this name already exists")
    groups[group_id] = {
        "name": name,
        "members": [
            {"connector": connector, "domain": domain}
            for connector, domain in sorted(pairs)
        ],
    }
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_DOMAIN_GROUPS: groups}
    )
    return group_id


def delete_group(hass, entry, group_id):
    groups = dict(entry.options.get(CONF_DOMAIN_GROUPS, {}))
    if group_id not in groups:
        raise PolicyError("Group no longer exists")
    groups.pop(group_id)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, group_unique_id(entry.entry_id, group_id)
    )
    if entity_id:
        registry.async_remove(entity_id)
    remove_group_device(hass, entry, group_id)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_DOMAIN_GROUPS: groups}
    )


def rename_group_member(hass, entry, connector, old_domain, new_domain):
    """Keep group membership aligned with an explicit domain rename."""
    old_pair = (connector, domain_base(old_domain))
    new_pair = (connector, domain_base(new_domain))
    groups = entry.options.get(CONF_DOMAIN_GROUPS, {})
    updated = {}
    for group_id, group in groups.items():
        pairs = {(member["connector"], member["domain"]) for member in group["members"]}
        if old_pair in pairs:
            pairs.remove(old_pair)
            pairs.add(new_pair)
        updated[group_id] = {
            **group,
            "members": [
                {"connector": name, "domain": domain} for name, domain in sorted(pairs)
            ],
        }
    if updated != groups:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DOMAIN_GROUPS: updated}
        )
