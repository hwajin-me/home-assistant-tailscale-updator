"""Tailnet parent device with separate domain and local group devices."""

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_DOMAIN_GROUPS, DOMAIN
from .registry import registered_pair


def _parent_link(entry):
    # Newer HA uses registry device IDs; older releases use identifiers.
    if "via_device_id" in DeviceInfo.__annotations__:
        return {"via_device_id": entry.runtime_data.tailnet_device_id}
    return {"via_device": (DOMAIN, entry.entry_id)}


def domain_device_info(entry):
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}:domains")},
        name="Tailscale Domain",
        manufacturer="Tailscale",
        **_parent_link(entry),
    )


def group_device_info(entry, group_id):
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}:group:{group_id}")},
        name=f"Group - {entry.options[CONF_DOMAIN_GROUPS][group_id]['name']}",
        manufacturer="Tailscale",
        **_parent_link(entry),
    )


def _detach_device(devices, device, entry_id):
    if hasattr(device, "config_entry_id"):
        # Current registries own a device by one config entry.
        if device.config_entry_id == entry_id:
            devices.async_remove_device(device.id)
    else:
        # Legacy registries allow shared ownership: detach only our entry.
        devices.async_update_device(device.id, remove_config_entry_id=entry_id)


@callback
def reconcile_devices(hass, entry):
    """Create the hierarchy and move existing entities without changing identity."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    parent = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Tailscale",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    entry.runtime_data.tailnet_device_id = parent.id
    domain_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, **domain_device_info(entry)
    )
    groups = {
        f"{entry.entry_id}:group:{group_id}": devices.async_get_or_create(
            config_entry_id=entry.entry_id, **group_device_info(entry, group_id)
        )
        for group_id in entry.options.get(CONF_DOMAIN_GROUPS, {})
    }
    # Include disabled entities: they must migrate even if HA skips loading them.
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform != DOMAIN or entity.domain != "switch":
            continue
        target = groups.get(entity.unique_id)
        if registered_pair(entry.entry_id, entity.unique_id) is not None:
            target = domain_device
        if target is not None and entity.device_id != target.id:
            entities.async_update_entity(entity.entity_id, device_id=target.id)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if any(
            platform == DOMAIN
            and identifier.startswith(f"{entry.entry_id}:group:")
            and identifier not in groups
            for platform, identifier in device.identifiers
        ):
            _detach_device(devices, device, entry.entry_id)


@callback
def remove_group_device(hass, entry, group_id):
    """Detach only this integration's group device after explicit deletion."""
    devices = dr.async_get(hass)
    identifier = (DOMAIN, f"{entry.entry_id}:group:{group_id}")
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if identifier in device.identifiers:
            _detach_device(devices, device, entry.entry_id)
