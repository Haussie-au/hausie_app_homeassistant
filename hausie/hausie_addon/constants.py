"""
Shared constants for labels, entity types, and input/helper types.
Keep these in sync with registry and templates.
"""


class Labels:
    BLIND = "blind" # actionable
    BUTTON = "button"
    COOLING = "cooling" # actionable
    DEVICES = "devices"
    HEATING = "heating" # actionable
    MOTION = "motion"
    PLANT = "plant"
    PRIMARY_LIGHT = "primary_light" # actionable
    SECONDARY_LIGHT = "secondary_light" # actionable
    SYSTEM = "system"
    TEMPERATURE = "temperature"
    UTILITIES = "utilities"

    ALL = {
        BLIND,
        BUTTON,
        COOLING,
        DEVICES,
        HEATING,
        MOTION,
        PLANT,
        PRIMARY_LIGHT,
        SECONDARY_LIGHT,
        SYSTEM,
        TEMPERATURE,
        UTILITIES,
    }


class EntityType:
    BATTERY = "battery"
    BUTTON = "button"
    CLIMATE = "climate"
    COOLING = "cooling" # actionable
    COVER = "cover"
    HEATING = "heating" # actionable
    HUMIDITY = "humidity"
    LIGHT = "light" # actionable
    LUX = "lux"
    MAIN = "main"
    MOTION = "motion"
    PLANT = "plant"
    TEMPERATURE = "temperature"

    ALL = {
        BATTERY,
        BUTTON,
        CLIMATE,
        COOLING,
        COVER,
        HEATING,
        HUMIDITY,
        LIGHT,
        LUX,
        MAIN,
        MOTION,
        PLANT,
        TEMPERATURE,
    }


class InputType:
    INPUT_BOOLEAN = "input_boolean"
    INPUT_NUMBER = "input_number"
    INPUT_DATETIME = "input_datetime"
    INPUT_SELECT = "input_select"
    CLIMATE = "climate"
    INPUT_BUTTON = "input_button"
    INPUT_TEXT = "input_text"

    ALL = {
        INPUT_BOOLEAN,
        INPUT_NUMBER,
        INPUT_DATETIME,
        INPUT_SELECT,
        CLIMATE,
        INPUT_BUTTON,
        INPUT_TEXT,
    }


# Ordered tuples for stable iteration.
LABELS = (
    Labels.BLIND,
    Labels.BUTTON,
    Labels.COOLING,
    Labels.DEVICES,
    Labels.HEATING,
    Labels.MOTION,
    Labels.PLANT,
    Labels.PRIMARY_LIGHT,
    Labels.SECONDARY_LIGHT,
    Labels.SYSTEM,
    Labels.TEMPERATURE,
    Labels.UTILITIES,
)

ENTITY_TYPES = (
    EntityType.BATTERY,
    EntityType.BUTTON,
    EntityType.CLIMATE,
    EntityType.COOLING,
    EntityType.COVER,
    EntityType.HEATING,
    EntityType.HUMIDITY,
    EntityType.LIGHT,
    EntityType.LUX,
    EntityType.MAIN,
    EntityType.MOTION,
    EntityType.PLANT,
    EntityType.TEMPERATURE,
)

INPUT_TYPES = (
    InputType.INPUT_BOOLEAN,
    InputType.INPUT_NUMBER,
    InputType.INPUT_DATETIME,
    InputType.INPUT_SELECT,
    InputType.CLIMATE,
    InputType.INPUT_BUTTON,
    InputType.INPUT_TEXT,
)

# Convenience sets for quick membership checks.
LABEL_SET = set(LABELS)
ENTITY_TYPE_SET = set(ENTITY_TYPES)
INPUT_TYPE_SET = set(INPUT_TYPES)
