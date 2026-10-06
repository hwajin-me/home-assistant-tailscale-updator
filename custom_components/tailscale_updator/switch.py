"""One switch for each app connector domain and its wildcard partner."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .policy import PolicyError, domain_base, domain_bases, normalize_domain
from .registry import domain_entries, registered_pair, remembered_pairs, unique_id


async def async_setup_entry(hass, entry, async_add_entities):
    manager = DomainSwitchManager(hass, entry, async_add_entities)
    manager.reconcile()
    entry.async_on_unload(entry.runtime_data.async_add_listener(manager.reconcile))


class DomainSwitchManager:
    """Discover remote domains on setup and on every policy refresh."""

    def __init__(self, hass, entry, async_add_entities):
        self.hass = hass
        self.entry = entry
        self.coordinator = entry.runtime_data
        self.async_add_entities = async_add_entities
        self.known: set[str] = set()

    @callback
    def reconcile(self):
        pairs: set[tuple[str, str]] = set()
        try:
            for connector in self.coordinator.data.policy.connectors():
                try:
                    pairs.update(
                        (connector, base)
                        for base in domain_bases(
                            self.coordinator.data.policy.domains(connector)
                        )
                    )
                except PolicyError:
                    # Preset apps have no editable domains array.
                    continue
        except PolicyError:
            return
        # Previously seen domains survive an Off toggle and a HA restart.
        # Migrate legacy single-wildcard entity IDs to their parent ID.
        registry = er.async_get(self.hass)
        entries = domain_entries(self.hass, self.entry)
        registered = {item.unique_id for item in entries}
        for item in entries:
            parsed = registered_pair(self.entry.entry_id, item.unique_id)
            if parsed is None:
                continue
            pairs.add(parsed)
            canonical = unique_id(self.entry.entry_id, *parsed)
            if item.unique_id != canonical:
                if canonical in registered:
                    registry.async_remove(item.entity_id)
                else:
                    registry.async_update_entity(
                        item.entity_id, new_unique_id=canonical
                    )
                    registered.add(canonical)
        pairs.update(remembered_pairs(self.hass, self.entry))
        new = [
            (connector, base)
            for connector, base in sorted(pairs)
            if unique_id(self.entry.entry_id, connector, base) not in self.known
        ]
        self.known.update(
            unique_id(self.entry.entry_id, connector, base) for connector, base in new
        )
        if new:
            entities = []
            for connector, base in new:
                entity = DomainSwitch(self.entry, connector, base)
                entity.async_on_remove(
                    lambda uid=entity.unique_id: self.known.discard(uid)
                )
                entities.append(entity)
            self.async_add_entities(entities)


class DomainSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:lan-connect"

    def __init__(self, entry, connector: str, domain: str):
        super().__init__(entry.runtime_data)
        self.connector = connector
        self.domain = domain_base(domain)
        self._attr_unique_id = unique_id(entry.entry_id, connector, self.domain)
        self._attr_name = f"{connector}: {self.domain}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Tailscale {entry.title}",
            manufacturer="Tailscale",
        )

    def _domains(self) -> set[str]:
        return {
            normalize_domain(d)
            for d in self.coordinator.data.policy.domains(self.connector)
        }

    @property
    def available(self):
        if not super().available:
            return False
        try:
            self._domains()
        except PolicyError:
            return False
        return True

    @property
    def is_on(self):
        if not self.available:
            return None
        domains = self._domains()
        return self.domain in domains and f"*.{self.domain}" in domains

    @property
    def extra_state_attributes(self):
        if not self.available:
            return None
        domains = self._domains()
        return {
            "parent_present": self.domain in domains,
            "wildcard_present": f"*.{self.domain}" in domains,
        }

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_write(
            self.coordinator.client.set_domains, self.connector, [self.domain], True
        )

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_write(
            self.coordinator.client.set_domains, self.connector, [self.domain], False
        )
