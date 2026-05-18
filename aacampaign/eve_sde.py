"""Helpers for working with django-eveonline-sde models."""

# Standard Library
from functools import lru_cache

# Django
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

REGION_MODEL = getattr(settings, "AACAMPAIGN_SDE_REGION_MODEL", "eve_sde.Region")
CONSTELLATION_MODEL = getattr(
    settings, "AACAMPAIGN_SDE_CONSTELLATION_MODEL", "eve_sde.Constellation"
)
SOLAR_SYSTEM_MODEL = getattr(
    settings, "AACAMPAIGN_SDE_SOLAR_SYSTEM_MODEL", "eve_sde.SolarSystem"
)
ITEM_TYPE_MODEL = getattr(settings, "AACAMPAIGN_SDE_ITEM_TYPE_MODEL", "eve_sde.ItemType")


def _split_model_label(label):
    try:
        app_label, model_name = label.split(".")
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Invalid model label '{label}'. Expected 'app_label.ModelName'."
        ) from exc
    return app_label, model_name


@lru_cache(maxsize=None)
def get_model(label):
    app_label, model_name = _split_model_label(label)
    try:
        return apps.get_model(app_label, model_name)
    except LookupError as exc:
        raise ImproperlyConfigured(
            f"Model '{label}' not found. Check your "
            "AACAMPAIGN_SDE_*_MODEL settings."
        ) from exc


def get_region_model():
    return get_model(REGION_MODEL)


def get_constellation_model():
    return get_model(CONSTELLATION_MODEL)


def get_solar_system_model():
    return get_model(SOLAR_SYSTEM_MODEL)


def get_item_type_model():
    return get_model(ITEM_TYPE_MODEL)


def get_related_field_name(model, candidates):
    field_names = {field.name for field in model._meta.get_fields()}
    for name in candidates:
        if name in field_names:
            return name
    return None


@lru_cache(maxsize=None)
def get_system_constellation_field():
    return get_related_field_name(get_solar_system_model(), ("constellation", "eve_constellation"))


@lru_cache(maxsize=None)
def get_constellation_region_field():
    return get_related_field_name(get_constellation_model(), ("region", "eve_region"))


def get_system_constellation(system):
    if not system:
        return None
    field_name = get_system_constellation_field()
    if not field_name:
        return None
    return getattr(system, field_name, None)


def get_system_region(system):
    constellation = get_system_constellation(system)
    if not constellation:
        return None
    region_field = get_constellation_region_field()
    if not region_field:
        return None
    return getattr(constellation, region_field, None)


def get_solar_system_select_related_fields(prefix="solar_system"):
    constellation_field = get_system_constellation_field()
    region_field = get_constellation_region_field()
    fields = [prefix]
    if constellation_field:
        fields.append(f"{prefix}__{constellation_field}")
        if region_field:
            fields.append(f"{prefix}__{constellation_field}__{region_field}")
    return fields
