"""Domain identities and remembered switches in the Home Assistant registry."""

import json

from homeassistant.helpers import entity_registry as er

from .const import CONF_DOMAIN_GROUPS, CONF_SWITCHES, DOMAIN
from .policy import PolicyError, domain_base


def unique_id(entry_id: str, connector: str, domain: str) -> str:
    return (
        f"{entry_id}:{json.dumps([connector, domain_base(domain)], ensure_ascii=False)}"
    )


def registered_pair(entry_id: str, unique: str) -> tuple[str, str] | None:
    prefix = f"{entry_id}:"
    if not unique.startswith(prefix):
        return None
    try:
        pair = json.loads(unique[len(prefix) :])
        if not isinstance(pair, list) or len(pair) != 2:
            return None
        connector, domain = pair
        if (
            not isinstance(connector, str)
            or not connector
            or not isinstance(domain, str)
        ):
            return None
        return connector, domain_base(domain)
    except (ValueError, TypeError):
        return None


def domain_entries(hass, entry):
    return [
        item
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if item.domain == "switch" and item.platform == DOMAIN
    ]


def legacy_pair(item):
    if not isinstance(item, dict):
        return None
    connector, domain = item.get("connector"), item.get("domain")
    if not isinstance(connector, str) or not connector or not isinstance(domain, str):
        return None
    try:
        return connector, domain_base(domain)
    except PolicyError:
        return None


def remembered_pairs(hass, entry):
    pairs = {
        pair
        for item in domain_entries(hass, entry)
        if (pair := registered_pair(entry.entry_id, item.unique_id)) is not None
    }
    legacy = entry.options.get(CONF_SWITCHES, [])
    if isinstance(legacy, list):
        pairs.update(pair for item in legacy if (pair := legacy_pair(item)) is not None)
    return pairs


def forget_domains(hass, entry, connector, domains):
    """Forget only explicitly deleted pairs, after the remote change succeeded."""
    removed = {(connector, domain_base(domain)) for domain in domains}
    registry = er.async_get(hass)
    for item in domain_entries(hass, entry):
        if registered_pair(entry.entry_id, item.unique_id) in removed:
            registry.async_remove(item.entity_id)
    options = dict(entry.options)
    legacy = options.get(CONF_SWITCHES, [])
    if CONF_SWITCHES in options and isinstance(legacy, list):
        options[CONF_SWITCHES] = [
            item for item in legacy if legacy_pair(item) not in removed
        ]
    groups = options.get(CONF_DOMAIN_GROUPS, {})
    if groups:
        options[CONF_DOMAIN_GROUPS] = {
            key: {
                **group,
                "members": [
                    member
                    for member in group["members"]
                    if legacy_pair(member) not in removed
                ],
            }
            for key, group in groups.items()
        }
    if options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=options)
