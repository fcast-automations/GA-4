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


@dataclass(frozen=True)
class Config:
    app_name: str
    property_id: str
    spreadsheet_id: str
    service_account_json: str

    sheet_name: str
    start_date: str
    end_date: str

    home_screen_name: str
    screen_field: str
    timezone: str


def load_config() -> Config:
    return Config(
        app_name=required_env("APP_NAME"),
        property_id=required_env("GA4_PROPERTY_ID"),
        spreadsheet_id=required_env("SPREADSHEET_ID"),
        service_account_json=required_env("GA4_SERVICE_ACCOUNT_JSON"),

        sheet_name=optional_env("SHEET_NAME", "GA4 Basic Funnel"),
        start_date=optional_env("START_DATE", "30daysAgo"),
        end_date=optional_env("END_DATE", "today"),

        home_screen_name=required_env("HOME_SCREEN_NAME"),

        # Your GA4 report showed: Page path and screen class = MainActivity
        # API field for this is unifiedPagePathScreen
        screen_field=optional_env("SCREEN_FIELD", "unifiedPagePathScreen"),

        timezone=optional_env("TIMEZONE", "Asia/Karachi"),
    )
