"""Constants for Battery Maintenance."""

from datetime import time
from typing import Final

DOMAIN: Final = "battery_maintenance"

CONF_REPLACE_ENTITIES: Final = "replace_entities"
CONF_CHARGE_ENTITIES: Final = "charge_entities"
CONF_LOW_THRESHOLD: Final = "low_threshold"
CONF_RECOVERY_THRESHOLD: Final = "recovery_threshold"
CONF_SCAN_TIME: Final = "scan_time"
CONF_DONETICK_ENTRY_ID: Final = "donetick_entry_id"

DEFAULT_LOW_THRESHOLD: Final = 20
DEFAULT_RECOVERY_THRESHOLD: Final = 40
DEFAULT_SCAN_TIME: Final = time(8, 0)

ACTION_REPLACE: Final = "replace"
ACTION_CHARGE: Final = "charge"

SERVICE_SYNC: Final = "sync"

DONETICK_DOMAIN: Final = "donetick"
DONETICK_CREATE_SERVICE: Final = "create_task_form"
DONETICK_UPDATE_SERVICE: Final = "update_task_form"
DONETICK_ALL_TASKS_ENTITY: Final = "todo.all_tasks_internal"
DONETICK_AUTH_TYPE_KEY: Final = "auth_type"
DONETICK_JWT_AUTH_TYPE: Final = "jwt"

STORE_VERSION: Final = 1
STORE_MINOR_VERSION: Final = 1
STORE_SAVE_DELAY: Final = 1

TASK_DUE_HOUR: Final = 17
PENDING_RETRY_SECONDS: Final = 30 * 60
TASK_REFRESH_DELAY: Final = 1.0
TASK_REFRESH_TIMEOUT: Final = 15.0
REFERENCE_PREFIX: Final = "BATT-"
