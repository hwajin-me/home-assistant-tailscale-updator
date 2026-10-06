"""Validate complete translations and load them through Home Assistant."""

import json
from pathlib import Path
from string import Formatter

import pytest
import yaml
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

ROOT = Path("custom_components/tailscale_updator")
LANGUAGES = ("en", "ko", "ja")


def flatten(data, prefix=""):
    result = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def placeholders(text):
    return {field for _, field, _, _ in Formatter().parse(text) if field is not None}


def test_translation_languages_and_service_coverage():
    assert {p.stem for p in (ROOT / "translations").glob("*.json")} == set(LANGUAGES)
    source = json.loads((ROOT / "strings.json").read_text())
    assert source == json.loads((ROOT / "translations/en.json").read_text())
    services = yaml.safe_load((ROOT / "services.yaml").read_text())
    assert source["services"].keys() == services.keys()
    for service, schema in services.items():
        assert source["services"][service]["fields"].keys() == schema["fields"].keys()


@pytest.mark.parametrize("language", LANGUAGES)
async def test_all_translations_load_in_home_assistant(tmp_path, language):
    source = json.loads((ROOT / "strings.json").read_text())
    translated = json.loads((ROOT / "translations" / f"{language}.json").read_text())
    expected = flatten(source)
    actual = flatten(translated)
    assert expected.keys() == actual.keys()
    for key, text in actual.items():
        assert isinstance(text, str) and text.strip(), key
        assert placeholders(text) == placeholders(expected[key]), key
    assert set(translated["config"]["step"]["user"]["data"]) == {
        "client_id",
        "client_secret",
    }
    (tmp_path / "custom_components").symlink_to(
        Path("custom_components").resolve(), target_is_directory=True
    )
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    try:
        for category in ("config", "options", "selector", "services"):
            loaded = await async_get_translations(
                hass, language, category, {"tailscale_updator"}
            )
            for key, text in flatten(translated[category]).items():
                assert loaded[f"component.tailscale_updator.{category}.{key}"] == text
    finally:
        await hass.async_stop(force=True)
