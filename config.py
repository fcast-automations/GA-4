import os
from dataclasses import dataclass


SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


def required_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or str(value).strip() == "":
        raise ValueError(f"Missing required environment variable: {name}")

    return str(value).strip()


def optional_env(name: str, default: str) -> str:
    value = os.getenv(name)

    if value is None or str(value).strip() == "":
        return default

    return str(value).strip()


def optional_int_env(name: str, default: int) -> int:
    value = optional_env(name, str(default))

    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be a number. Current value: {value}")


def optional_bool_env(name: str, default: bool) -> bool:
    value = optional_env(name, "true" if default else "false").strip().lower()

    if value in {"1", "true", "yes", "y", "on"}:
        return True

    if value in {"0", "false", "no", "n", "off"}:
        return False

    raise ValueError(f"{name} must be true or false. Current value: {value}")


@dataclass(frozen=True)
class Config:
    spreadsheet_id: str
    service_account_json: str

    apps_config_sheet: str
    merged_sheet: str

    start_date: str
    end_date: str
    timezone: str

    default_home_screen_name: str
    default_screen_field: str
    retention_days: int

    notification_event_names: str
    key_event_names: str

    fetch_package_name: bool
    ga4_admin_api_base: str
    cleanup_old_tabs: bool


def load_config() -> Config:
    return Config(
        spreadsheet_id=required_env("SPREADSHEET_ID"),
        service_account_json=required_env("GA4_SERVICE_ACCOUNT_JSON"),

        apps_config_sheet=optional_env("APPS_CONFIG_SHEET", "Apps Config"),
        merged_sheet=optional_env("MERGED_SHEET", "GA4 Merged Data"),

        start_date=optional_env("START_DATE", "7daysAgo"),
        end_date=optional_env("END_DATE", "today"),
        timezone=optional_env("TIMEZONE", "Asia/Karachi"),

        default_home_screen_name=optional_env("DEFAULT_HOME_SCREEN_NAME", "MainActivity"),
        default_screen_field=optional_env("DEFAULT_SCREEN_FIELD", "unifiedPagePathScreen"),
        retention_days=optional_int_env("RETENTION_DAYS", 7),

        notification_event_names=optional_env(
            "NOTIFICATION_EVENT_NAMES",
            "notification_receive,notification_foreground,notification_open,notification_dismiss",
        ),
        key_event_names=optional_env(
            "KEY_EVENT_NAMES",
            "ad_impression,in_app_purchase,purchase,begin_checkout,subscribe,trial_start",
        ),

        fetch_package_name=optional_bool_env("FETCH_PACKAGE_NAME", True),
        ga4_admin_api_base=optional_env(
            "GA4_ADMIN_API_BASE",
            "https://analyticsadmin.googleapis.com/v1beta",
        ),
        cleanup_old_tabs=optional_bool_env("CLEANUP_OLD_TABS", True),
    )
