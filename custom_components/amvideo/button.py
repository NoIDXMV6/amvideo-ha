"""Кнопки открытия шлагбаумов А.М.Видео."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL_BARRIER
from .coordinator import AmVideoCoordinator
from .entity_registry import register_open_entity, unregister_open_entity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создать по кнопке «Открыть» на каждый доступный КПУ."""
    coordinator: AmVideoCoordinator = entry.runtime_data
    entities: list[AmVideoOpenButton] = []
    for panel in coordinator.data.get("panels", []):
        entities.append(AmVideoOpenButton(coordinator, entry.entry_id, panel))
    async_add_entities(entities)


class AmVideoOpenButton(CoordinatorEntity, ButtonEntity):
    """Кнопка отправки команды открытия КПУ."""

    _attr_has_entity_name = True
    _attr_name = "Открыть"
    _attr_icon = "mdi:boom-gate-arrow-up-outline"

    def __init__(
        self,
        coordinator: AmVideoCoordinator,
        entry_id: str,
        panel: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._panel = panel
        id_shlag = str(panel["id_shlag"])
        self._attr_unique_id = f"{entry_id}_open_{id_shlag}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"shlag_{id_shlag}")},
            name=panel.get("adress") or f"КПУ {id_shlag}",
            manufacturer=MANUFACTURER,
            model=MODEL_BARRIER,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "id_shlag": self._panel.get("id_shlag"),
            "relay": self._panel.get("relay"),
            "adress": self._panel.get("adress"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        register_open_entity(self.entity_id, self)

    async def async_will_remove_from_hass(self) -> None:
        unregister_open_entity(self.entity_id)
        await super().async_will_remove_from_hass()

    async def async_press(self) -> None:
        """Отправить команду открытия."""
        await self.async_open_barrier()

    async def async_open_barrier(self) -> None:
        """Метод для сервиса amvideo.open_barrier."""
        try:
            message = await self.coordinator.async_open_barrier(self._panel)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info("%s: %s", self._panel.get("adress"), message)
