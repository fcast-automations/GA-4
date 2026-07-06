import json
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
    Filter,
    FilterExpression,
    FilterExpressionList,
)
from googleapiclient.discovery import build

from config import SCOPES, load_config


config = load_config()


def get_credentials():
    service_account_info = json.loads(config.service_account_json)

    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


credentials = get_credentials()


def exact_filter(field_name: str, value: str) -> FilterExpression:
    return FilterExpression(
        filter=Filter(
            field_name=field_name,
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.EXACT,
                value=value,
                case_sensitive=False,
            ),
        )
    )


def contains_filter(field_name: str, value: str) -> FilterExpression:
    return FilterExpression(
        filter=Filter(
            field_name=field_name,
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.CONTAINS,
                value=value,
                case_sensitive=False,
            ),
        )
    )


def get_ga4_step_data(event_name: str, screen_name: str | None = None) -> dict:
    client = BetaAnalyticsDataClient(credentials=credentials)

    filters = [
        exact_filter("eventName", event_name)
    ]

    if screen_name:
        filters.append(
            contains_filter(config.screen_field, screen_name)
        )

    request = RunReportRequest(
        property=f"properties/{config.property_id}",
        date_ranges=[
            DateRange(
                start_date=config.start_date,
                end_date=config.end_date,
            )
        ],
        dimensions=[
            Dimension(name="eventName"),
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="eventCount"),
        ],
        dimension_filter=FilterExpression(
            and_group=FilterExpressionList(
                expressions=filters
            )
        ),
    )

    response = client.run_report(request)

    if not response.rows:
        return {
            "active_users": 0,
            "event_count": 0,
        }

    row = response.rows[0]

    return {
        "active_users": int(row.metric_values[0].value or 0),
        "event_count": int(row.metric_values[1].value or 0),
    }


def get_sheets_service():
    return build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )


def ensure_sheet_exists(service):
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=config.spreadsheet_id
    ).execute()

    existing_sheets = [
        sheet["properties"]["title"]
        for sheet in spreadsheet.get("sheets", [])
    ]

    if config.sheet_name not in existing_sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=config.spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": config.sheet_name
                            }
                        }
                    }
                ]
            },
        ).execute()


def write_rows_to_sheet(rows: list[list]):
    service = get_sheets_service()
    ensure_sheet_exists(service)

    service.spreadsheets().values().clear(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.sheet_name}!A:Z",
        body={},
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        body={
            "values": rows
        },
    ).execute()


def calculate_rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0%"

    return f"{round((numerator / denominator) * 100, 2)}%"


def build_rows(first_open_data: dict, home_users_data: dict) -> list[list]:
    first_open_users = first_open_data["active_users"]
    home_users = home_users_data["active_users"]

    drop_off = max(first_open_users - home_users, 0)
    conversion_rate = calculate_rate(home_users, first_open_users)

    updated_at = datetime.now(
        ZoneInfo(config.timezone)
    ).strftime("%Y-%m-%d %I:%M:%S %p")

    return [
        [
            "App Name",
            "Property ID",
            "Date Range",
            "Funnel Step",
            "Event Name",
            "Screen Condition",
            "Active Users",
            "Event Count",
            "Drop Off",
            "Step Conversion Rate",
            "Updated At",
        ],
        [
            config.app_name,
            config.property_id,
            f"{config.start_date} to {config.end_date}",
            "Step 1 - First Open",
            "first_open",
            "",
            first_open_data["active_users"],
            first_open_data["event_count"],
            "",
            "",
            updated_at,
        ],
        [
            config.app_name,
            config.property_id,
            f"{config.start_date} to {config.end_date}",
            "Step 2 - Home Users",
            "screen_view",
            f"{config.screen_field} contains {config.home_screen_name}",
            home_users_data["active_users"],
            home_users_data["event_count"],
            drop_off,
            conversion_rate,
            updated_at,
        ],
    ]


def main():
    print("Reading GA4 data...")

    first_open_data = get_ga4_step_data(
        event_name="first_open"
    )

    home_users_data = get_ga4_step_data(
        event_name="screen_view",
        screen_name=config.home_screen_name,
    )

    rows = build_rows(
        first_open_data=first_open_data,
        home_users_data=home_users_data,
    )

    write_rows_to_sheet(rows)

    print("Done. GA4 data written to Google Sheet.")
    print(f"App Name: {config.app_name}")
    print(f"Property ID: {config.property_id}")
    print(f"Home Screen: {config.home_screen_name}")
    print(f"Screen Field: {config.screen_field}")


if __name__ == "__main__":
    main()
