from __future__ import annotations

import array
import asyncio
import logging
import re

from enum import IntEnum
import bleak_retry_connector

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_EFFECT, ColorMode, LightEntity,
                                            LightEntityFeature, ATTR_COLOR_TEMP_KELVIN)

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util

from .const import DOMAIN
from pathlib import Path
import json
from .govee_utils import prepareMultiplePacketsData
import base64
from . import Hub
from datetime import timedelta

SCAN_INTERVAL = timedelta(seconds=30)


_LOGGER = logging.getLogger(__name__)

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
EFFECT_PARSE = re.compile("\[(\d+)/(\d+)/(\d+)/(\d+)]")
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199']

# Models whose solid color uses the 0x0D sub-command with a segment bitmask
# (33 05 0D <mask> RR GG BB), verified against a btsnoop capture of the Govee
# app. Each entry maps to the independently addressable segments exposed as
# separate HA entities: (name, segment mask, unique_id suffix).
BAR_SEGMENT_MODELS = {
    'H6053': [
        ("Both Bars", 0x11, "both"),
        ("Bar A", 0x01, "a"),
        ("Bar B", 0x10, "b"),
    ],
}

class LedCommand(IntEnum):
    """ A control command packet's type. """
    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05


class LedMode(IntEnum):
    """
    The mode in which a color change happens in.

    Currently only manual is supported.
    """
    MANUAL = 0x02
    SCENE = 0x04
    MICROPHONE = 0x06
    BAR_SEGMENTS = 0x0D
    SCENES = 0x05
    SEGMENTS = 0x15


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        model = config_entry.data["model"]
        if model in BAR_SEGMENT_MODELS:
            async_add_entities([
                GoveeBluetoothLight(hub, ble_device, config_entry, name=name, segment=segment, suffix=suffix)
                for name, segment, suffix in BAR_SEGMENT_MODELS[model]
            ])
        else:
            async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])


class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None
        self.update_scenes()

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        state = await self.hub.api.get_device_state(self.sku, self.device)
        for cap in state["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                self._state = cap['state']['value'] == 1
            if cap['instance'] == 'brightness':
                self._brightness = cap['state']['value']
            if cap['instance'] == 'colorTemperatureK':
                value = cap['state']['value']
                if value != 0:
                    self._attr_color_temp_kelvin = value
                    self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
            if cap['instance'] == 'colorRgb':
                num = cap['state']['value']
                self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)

    async def update_scenes(self):
        if LightEntityFeature.EFFECT in self.supported_features:
            if self._attr_effect_list is None or len(self._attr_effect_list) == 0:
                _LOGGER.info("Updating device effects: %s", self.device_data)

                store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
                scenes = await self.hub.api.list_scenes(self.sku, self.device)

                await store.async_save(scenes)

                self._attr_effect_list = [scene['name'] for scene in scenes]

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            scenes = (
                scene for scene in await store.async_load()
                if scene['name'] == effect_name
            )
            scene = next(scenes)
            _LOGGER.info("Set scene: %s", scene)
            await self.hub.api.set_scene(self.sku, self.device, scene['value'])

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False


class GoveeBluetoothLight(LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry,
                 name: str | None = None, segment: int | None = None, suffix: str | None = None) -> None:
        """Initialize an bluetooth light.

        For BAR_SEGMENT_MODELS, one instance is created per addressable segment:
        the master ("Both Bars", mask 0x11) plus one per bar. `segment` is the
        0x0D color bitmask. Only color is per-segment on this hardware; power
        (33 01) and brightness (33 04) are device-global, so every entity
        sends the real global command and all entities share that state.
        """
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in SEGMENTED_MODELS
        self._ble_device = ble_device
        self._segment = segment
        self._suffix = suffix
        self._name = name or "GOVEE Light"
        # Master = the whole-device entity (also any non bar-segment model).
        self._is_master = segment is None or segment == 0x11
        # On/off and brightness are global: shared across this device's
        # entities so all of them always display the same values.
        if not hasattr(hub, "global_light_state"):
            hub.global_light_state = {"on": None, "brightness": None, "entities": []}
        self._shared = hub.global_light_state
        if self._is_master:
            self._attr_supported_features = LightEntityFeature(
                LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)
        else:
            self._attr_supported_features = LightEntityFeature(
                LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)

    @property
    def effect_list(self) -> list[str] | None:
        if not self._is_master:
            return None
        effect_list = []
        json_data = json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())
        for categoryIdx, category in enumerate(json_data['data']['categories']):
            for sceneIdx, scene in enumerate(category['scenes']):
                for leffectIdx, lightEffect in enumerate(scene['lightEffects']):
                    label = category['categoryName'] + " - " + scene['sceneName']
                    if lightEffect.get('scenceName'):
                        label += ' - ' + lightEffect['scenceName']
                    specialEffects = lightEffect.get('specialEffect') or []
                    if specialEffects:
                        for seffectIxd, specialEffect in enumerate(specialEffects):
                            # if 'supportSku' not in specialEffect or self._model in specialEffect['supportSku']:
                            # Workaround cause we need to store some metadata in effect (effect names not unique)
                            indexes = str(categoryIdx) + "/" + str(sceneIdx) + "/" + str(leffectIdx) + "/" + str(
                                seffectIxd)
                            effect_list.append(label + " [" + indexes + "]")
                    elif lightEffect.get('sceneCode'):
                        # Scene with no per-LED payload: fired as 33 05 04 <sceneCode>.
                        indexes = str(categoryIdx) + "/" + str(sceneIdx) + "/" + str(leffectIdx) + "/0"
                        effect_list.append(label + " [" + indexes + "]")

        return effect_list

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        mac = self._mac.replace(":", "")
        if self._suffix:
            return mac + "_" + self._suffix
        return mac

    @property
    def brightness(self):
        return self._shared["brightness"]

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._shared["on"]

    async def async_added_to_hass(self) -> None:
        self._shared["entities"].append(self)

    async def async_will_remove_from_hass(self) -> None:
        if self in self._shared["entities"]:
            self._shared["entities"].remove(self)

    def _notify_peers(self) -> None:
        """Refresh sibling entities so shared on/brightness stays in sync."""
        for entity in self._shared["entities"]:
            if entity is not self and getattr(entity, "hass", None) is not None:
                entity.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        # Power (33 01) is device-global — there is no per-segment power on
        # this hardware (the controller ignores 000000 segment writes), so
        # every entity turns the whole device on.
        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]

        self._shared["on"] = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            # Brightness (33 04) is device-global as well: real level, sent
            # unmodified from any entity.
            self._shared["brightness"] = brightness
            commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            self._attr_rgb_color = (red, green, blue)

            if self._segment is not None:
                # H6053-style bar models: 33 05 0D <segment mask> R G B —
                # the only per-segment control the hardware offers.
                commands.append(self._prepareSinglePacketData(
                    LedCommand.COLOR, [LedMode.BAR_SEGMENTS, self._segment, red, green, blue]))
            elif self._is_segmented:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00,
                                                               0x00, 0x00, 0x00, 0xFF, 0x7F]))
            else:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR, [LedMode.MANUAL, red, green, blue]))

        if ATTR_EFFECT in kwargs and self._is_master:
            effect = kwargs.get(ATTR_EFFECT)
            if len(effect) > 0:
                search = EFFECT_PARSE.search(effect)

                # Parse effect indexes
                categoryIndex = int(search.group(1))
                sceneIndex = int(search.group(2))
                lightEffectIndex = int(search.group(3))
                specialEffectIndex = int(search.group(4))

                json_data = json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())
                category = json_data['data']['categories'][categoryIndex]
                scene = category['scenes'][sceneIndex]
                lightEffect = scene['lightEffects'][lightEffectIndex]
                specialEffects = lightEffect.get('specialEffect') or []

                if specialEffects and specialEffects[specialEffectIndex].get('scenceParam'):
                    specialEffect = specialEffects[specialEffectIndex]
                    # Prepare packets to send big payload in separated chunks
                    for command in prepareMultiplePacketsData(0xa3,
                                                              array.array('B', [0x02]),
                                                              array.array('B',
                                                                          base64.b64decode(specialEffect['scenceParam'])
                                                                          )):
                        commands.append(command)
                else:
                    # No per-LED payload: select the scene by its code,
                    # 33 05 04 <sceneCode> (as captured from the Govee app).
                    scene_code = int(lightEffect.get('sceneCode') or scene.get('sceneCode') or 0)
                    commands.append(self._prepareSinglePacketData(
                        LedCommand.COLOR, [LedMode.SCENE, scene_code & 0xFF]))

        client = await self._connectBluetooth()
        for command in commands:
            _LOGGER.warning("BYTES seg=%s %s", self._segment, command.hex())
            await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)
            await asyncio.sleep(0.2)
        self._notify_peers()

    async def async_turn_off(self, **kwargs) -> None:
        client = await self._connectBluetooth()
        # Global power off — the only off the hardware supports.
        command = self._prepareSinglePacketData(LedCommand.POWER, [0x0])
        _LOGGER.warning("BYTES seg=%s %s", self._segment, command.hex())
        await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)
        self._shared["on"] = False
        self._notify_peers()

    async def _connectBluetooth(self) -> BleakClient:
        for i in range(3):
            try:
                client = await bleak_retry_connector.establish_connection(BleakClient, self._ble_device, self._mac.replace(":", ""))
                return client
            except:
                continue

    def _prepareSinglePacketData(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)

        frame = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))

        # The checksum is calculated by XORing all data bytes
        checksum = 0
        for b in frame:
            checksum ^= b

        frame += bytes([checksum & 0xFF])
        return frame
