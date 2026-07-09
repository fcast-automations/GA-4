import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    Cohort,
    CohortSpec,
    CohortsRange,
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    OrderBy,
    RunReportRequest,
)
from googleapiclient.discovery import build

from config import SCOPES, load_config


config = load_config()


@dataclass
class AppConfig:
    app_name: str
    property_id: str
    home_screen_name: str
    screen_field: str
    firebase_project_id: str
    firebase_project_name: str
    firebase_app_id: str
    time_capping_parameter: str
    daily_notification_parameters: str
    iap_screen_parameter: str


def get_credentials():
    service_account_info = json.loads(config.service_account_json)
    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


credentials = get_credentials()
beta_client = BetaAnalyticsDataClient(credentials=credentials)
analytics_admin_session = None
package_name_cache: dict[str, str] = {}

MAX_GOOGLE_SHEETS_CELL_CHARS = 49000

OLD_REPORT_SHEET_NAMES = {
    "GA4 Funnel Summary",
    "GA4 Funnel Details",
    "GA4 User Session Summary",
    "GA4 Retention Details",
    "GA4 Audience Segments",
    "GA4 Personalized User Experience",
    "GA4 Remote Configuration",
    "Firebase AB Time Capping",
    "Firebase AB IAP Screen",
    "GA4 Notification Events",
    "Firebase Notification Delivery",
    "Firebase Daily Notifications",
}


def trim_cell_value(value, max_chars: int = MAX_GOOGLE_SHEETS_CELL_CHARS):
    if value is None:
        return ""

    text = str(value)
    if len(text) <= max_chars:
        return text

    return text[: max_chars - 40] + " ... [trimmed to fit Google Sheets cell limit]"


def sanitize_rows_for_google_sheets(rows: list[list]) -> list[list]:
    return [[trim_cell_value(value) for value in row] for row in rows]


def get_sheets_service():
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def ensure_sheet_exists(service, sheet_name: str):
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=config.spreadsheet_id,
        fields="sheets.properties(title)",
    ).execute()

    existing = {
        sheet.get("properties", {}).get("title", "")
        for sheet in spreadsheet.get("sheets", [])
    }

    if sheet_name in existing:
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=config.spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()


def cleanup_old_report_sheets(service):
    if not config.cleanup_old_tabs:
        return

    spreadsheet = service.spreadsheets().get(
        spreadsheetId=config.spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()

    protected = {config.apps_config_sheet, config.merged_sheet}
    requests = []
    names = []

    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        title = props.get("title", "")

        if title in OLD_REPORT_SHEET_NAMES and title not in protected:
            requests.append({"deleteSheet": {"sheetId": props["sheetId"]}})
            names.append(title)

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=config.spreadsheet_id,
            body={"requests": requests},
        ).execute()
        print("Deleted old report tabs: " + ", ".join(names))


def write_sheet(sheet_name: str, rows: list[list]):
    service = get_sheets_service()
    ensure_sheet_exists(service, sheet_name)
    cleanup_old_report_sheets(service)

    service.spreadsheets().values().clear(
        spreadsheetId=config.spreadsheet_id,
        range=f"{sheet_name}!A:ZZ",
        body={},
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": sanitize_rows_for_google_sheets(rows)},
    ).execute()


def get_apps_config_headers() -> list[str]:
    return [
        "Enabled",
        "App Name",
        "Property ID",
        "Home Screen Name",
        "Screen Field",
        "Firebase Project ID",
        "Firebase Project Name",
        "Firebase App ID",
        "Time Capping Parameter",
        "Daily Notification Parameters",
        "IAP Screen Parameter",
    ]


def ensure_apps_config_headers(service, values: list[list]):
    expected_headers = get_apps_config_headers()
    current_headers = values[0] if values else []

    if current_headers[: len(expected_headers)] == expected_headers:
        return

    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A1:K1",
        valueInputOption="USER_ENTERED",
        body={"values": [expected_headers]},
    ).execute()


def create_apps_config_template(service):
    ensure_sheet_exists(service, config.apps_config_sheet)
    service.spreadsheets().values().update(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [get_apps_config_headers()]},
    ).execute()


def read_apps_config() -> list[AppConfig]:
    service = get_sheets_service()
    ensure_sheet_exists(service, config.apps_config_sheet)

    response = service.spreadsheets().values().get(
        spreadsheetId=config.spreadsheet_id,
        range=f"{config.apps_config_sheet}!A:K",
    ).execute()

    values = response.get("values", [])

    if len(values) <= 1:
        create_apps_config_template(service)
        raise SystemExit("Apps Config sheet was empty. Template created. Fill apps and run again.")

    ensure_apps_config_headers(service, values)

    apps: list[AppConfig] = []

    for index, row in enumerate(values[1:], start=2):
        enabled = row[0].strip().upper() if len(row) > 0 else ""
        app_name = row[1].strip() if len(row) > 1 else ""
        property_id = row[2].strip() if len(row) > 2 else ""

        if enabled not in {"TRUE", "YES", "1", "Y"}:
            continue

        if not app_name or not property_id:
            print(f"Skipping row {index}: App Name or Property ID is missing.")
            continue

        apps.append(
            AppConfig(
                app_name=app_name,
                property_id=property_id,
                home_screen_name=(row[3].strip() if len(row) > 3 and row[3].strip() else config.default_home_screen_name),
                screen_field=(row[4].strip() if len(row) > 4 and row[4].strip() else config.default_screen_field),
                firebase_project_id=(row[5].strip() if len(row) > 5 else ""),
                firebase_project_name=(row[6].strip() if len(row) > 6 else ""),
                firebase_app_id=(row[7].strip() if len(row) > 7 else ""),
                time_capping_parameter=(row[8].strip() if len(row) > 8 else ""),
                daily_notification_parameters=(row[9].strip() if len(row) > 9 else ""),
                iap_screen_parameter=(row[10].strip() if len(row) > 10 else ""),
            )
        )

    if not apps:
        raise SystemExit("No enabled apps found in Apps Config sheet.")

    return apps


def resolve_ga4_date(value: str) -> str:
    value = str(value).strip()
    today = datetime.now(ZoneInfo(config.timezone)).date()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value

    if value.lower() == "today":
        return today.isoformat()

    if value.lower() == "yesterday":
        return (today - timedelta(days=1)).isoformat()

    match = re.fullmatch(r"(\d+)daysAgo", value, re.IGNORECASE)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat()

    return value


def get_report_dates() -> list[str]:
    start = datetime.fromisoformat(resolve_ga4_date(config.start_date)).date()
    end = datetime.fromisoformat(resolve_ga4_date(config.end_date)).date()

    if start > end:
        raise ValueError(f"START_DATE must be on or before END_DATE. Current: {start} to {end}")

    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def ga4_date_to_iso(value: str) -> str:
    value = str(value).strip()
    if re.fullmatch(r"\d{8}", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


def split_csv(value: str) -> list[str]:
    seen = set()
    result = []

    for item in str(value or "").split(","):
        item = item.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)

    return result


def to_number(value):
    if value in {None, ""}:
        return 0
    try:
        number = float(value)
        return int(number) if number.is_integer() else round(number, 2)
    except Exception:
        return value


def to_float(value) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def percent(value) -> str:
    try:
        number = float(value)
        if number <= 1:
            number *= 100
        return f"{round(number, 2)}%"
    except Exception:
        return ""


def rate(numerator, denominator) -> str:
    denominator = to_float(denominator)
    if denominator == 0:
        return "0%"
    return f"{round((to_float(numerator) / denominator) * 100, 2)}%"


def format_seconds(seconds_value) -> str:
    total = int(round(to_float(seconds_value)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60

    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def classify_api_error(error) -> tuple[str, str]:
    text = str(error)
    lower = text.lower()

    if any(term in lower for term in ["service_disabled", "has not been enabled", "api disabled", "api not enabled"]):
        return "API NOT ENABLED", text
    if any(term in lower for term in ["403", "permission denied", "access denied", "insufficient permissions"]):
        return "NO ACCESS", text
    if any(term in lower for term in ["404", "not found", "invalid property"]):
        return "INVALID PROPERTY ID", text
    return "ERROR", text


def get_analytics_admin_session():
    global analytics_admin_session
    if analytics_admin_session is None:
        analytics_admin_session = AuthorizedSession(credentials)
    return analytics_admin_session


def fetch_ga4_package_name(app: AppConfig) -> str:
    if not config.fetch_package_name:
        return ""

    cache_key = f"{app.property_id}|{app.firebase_app_id}"
    if cache_key in package_name_cache:
        return package_name_cache[cache_key]

    try:
        url = f"{config.ga4_admin_api_base}/properties/{app.property_id}/dataStreams"
        params = {"pageSize": 200}
        streams = []

        while True:
            response = get_analytics_admin_session().get(url, params=params, timeout=30)
            if response.status_code >= 400:
                raise RuntimeError(f"GA4 Admin API error {response.status_code}: {response.text}")

            payload = response.json()
            streams.extend(payload.get("dataStreams", []) or [])

            token = payload.get("nextPageToken", "")
            if not token:
                break
            params["pageToken"] = token

        android_streams = []
        for stream in streams:
            android = stream.get("androidAppStreamData", {}) or {}
            package_name = str(android.get("packageName", "")).strip()
            firebase_app_id = str(android.get("firebaseAppId", "")).strip()
            if package_name:
                android_streams.append((package_name, firebase_app_id))

        if app.firebase_app_id:
            for package_name, firebase_app_id in android_streams:
                if firebase_app_id == app.firebase_app_id:
                    package_name_cache[cache_key] = package_name
                    return package_name

        package_names = []
        seen = set()
        for package_name, _ in android_streams:
            if package_name not in seen:
                package_names.append(package_name)
                seen.add(package_name)

        result = ", ".join(package_names)
        package_name_cache[cache_key] = result
        return result

    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"PACKAGE NAME {status} for {app.app_name} / {app.property_id}: {error_text}")
        package_name_cache[cache_key] = ""
        return ""


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


def in_list_filter(field_name: str, values: list[str]) -> FilterExpression:
    return FilterExpression(
        filter=Filter(
            field_name=field_name,
            in_list_filter=Filter.InListFilter(
                values=values,
                case_sensitive=False,
            ),
        )
    )


def and_filter(expressions: list[FilterExpression]) -> FilterExpression:
    return FilterExpression(and_group=FilterExpressionList(expressions=expressions))


def date_order() -> OrderBy:
    return OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))


def row_to_dict(response_row, dimension_headers: list[str], metric_headers: list[str]) -> dict:
    data = {}

    for index, value in enumerate(response_row.dimension_values):
        if index < len(dimension_headers):
            data[dimension_headers[index]] = value.value

    for index, value in enumerate(response_row.metric_values):
        if index < len(metric_headers):
            data[metric_headers[index]] = value.value

    return data


def parse_response_rows(response) -> list[dict]:
    dimension_headers = [header.name for header in response.dimension_headers]
    metric_headers = [header.name for header in response.metric_headers]
    return [row_to_dict(row, dimension_headers, metric_headers) for row in response.rows]


def run_daily_metrics_report(app: AppConfig) -> dict[str, dict]:
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
        dimensions=[Dimension(name="date")],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="averageSessionDuration"),
            Metric(name="userEngagementDuration"),
            Metric(name="engagementRate"),
            Metric(name="eventCount"),
            Metric(name="totalRevenue"),
        ],
        order_bys=[date_order()],
        keep_empty_rows=True,
        limit=100000,
    )

    response = beta_client.run_report(request)
    by_date = {}

    for row in parse_response_rows(response):
        report_date = ga4_date_to_iso(row.get("date", ""))
        active_users = to_number(row.get("activeUsers", 0))
        sessions = to_number(row.get("sessions", 0))
        session_seconds = to_float(row.get("averageSessionDuration", 0))
        engagement_seconds = to_float(row.get("userEngagementDuration", 0))

        by_date[report_date] = {
            "Active Users": active_users,
            "New Users": to_number(row.get("newUsers", 0)),
            "Sessions": sessions,
            "Engaged Sessions": to_number(row.get("engagedSessions", 0)),
            "Avg Session Duration Seconds": round(session_seconds, 2),
            "Avg Session Duration": format_seconds(session_seconds),
            "Total Engagement Seconds": round(engagement_seconds, 2),
            "Total Engagement Time": format_seconds(engagement_seconds),
            "Sessions Per Active User": round(to_float(sessions) / to_float(active_users), 2) if to_float(active_users) else 0,
            "Engagement Rate": percent(row.get("engagementRate", 0)),
            "Total Event Count": to_number(row.get("eventCount", 0)),
            "Total Revenue": round(to_float(row.get("totalRevenue", 0)), 2),
        }

    return by_date


def run_event_report(app: AppConfig, event_names: list[str]) -> dict[tuple[str, str], dict]:
    if not event_names:
        return {}

    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
        dimensions=[Dimension(name="date"), Dimension(name="eventName")],
        metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
        dimension_filter=in_list_filter("eventName", event_names),
        order_bys=[date_order()],
        keep_empty_rows=False,
        limit=100000,
    )

    response = beta_client.run_report(request)
    result = {}

    for row in parse_response_rows(response):
        report_date = ga4_date_to_iso(row.get("date", ""))
        event_name = row.get("eventName", "")
        result[(report_date, event_name)] = {
            "active_users": to_number(row.get("activeUsers", 0)),
            "event_count": to_number(row.get("eventCount", 0)),
        }

    return result


def run_home_screen_report(app: AppConfig) -> dict[str, dict]:
    request = RunReportRequest(
        property=f"properties/{app.property_id}",
        date_ranges=[DateRange(start_date=config.start_date, end_date=config.end_date)],
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
        dimension_filter=and_filter(
            [
                exact_filter("eventName", "screen_view"),
                contains_filter(app.screen_field, app.home_screen_name),
            ]
        ),
        order_bys=[date_order()],
        keep_empty_rows=True,
        limit=100000,
    )

    response = beta_client.run_report(request)
    result = {}

    for row in parse_response_rows(response):
        report_date = ga4_date_to_iso(row.get("date", ""))
        result[report_date] = {
            "active_users": to_number(row.get("activeUsers", 0)),
            "event_count": to_number(row.get("eventCount", 0)),
        }

    return result


def parse_cohort_day(value: str) -> int:
    value = str(value).strip()
    if value == "":
        return 0
    try:
        return int(value)
    except ValueError:
        digits = re.sub(r"\D", "", value)
        return int(digits) if digits else 0


def chunked(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def run_retention_report(app: AppConfig, report_dates: list[str]) -> dict[str, dict]:
    if config.retention_days <= 0:
        return {}

    retention_by_date = {
        report_date: {
            "Cohort Total Users": 0,
            "D1 Active Users": 0,
            "D1 Retention": "0%",
            "D7 Active Users": 0,
            "D7 Retention": "0%",
        }
        for report_date in report_dates
    }

    # GA4 limits very large cohort requests, so keep each API call compact.
    for dates_chunk in chunked(report_dates, 12):
        request = RunReportRequest(
            property=f"properties/{app.property_id}",
            dimensions=[Dimension(name="cohort"), Dimension(name="cohortNthDay")],
            metrics=[Metric(name="cohortActiveUsers"), Metric(name="cohortTotalUsers")],
            cohort_spec=CohortSpec(
                cohorts=[
                    Cohort(
                        name=report_date,
                        dimension="firstSessionDate",
                        date_range=DateRange(start_date=report_date, end_date=report_date),
                    )
                    for report_date in dates_chunk
                ],
                cohorts_range=CohortsRange(
                    granularity=CohortsRange.Granularity.DAILY,
                    start_offset=0,
                    end_offset=config.retention_days,
                ),
            ),
            keep_empty_rows=True,
            limit=100000,
        )

        response = beta_client.run_report(request)

        for row in parse_response_rows(response):
            report_date = row.get("cohort", "")
            day = parse_cohort_day(row.get("cohortNthDay", "0"))
            active_users = to_number(row.get("cohortActiveUsers", 0))
            total_users = to_number(row.get("cohortTotalUsers", 0))

            if report_date not in retention_by_date:
                continue

            if to_float(total_users) > to_float(retention_by_date[report_date]["Cohort Total Users"]):
                retention_by_date[report_date]["Cohort Total Users"] = total_users

            if day == 1:
                retention_by_date[report_date]["D1 Active Users"] = active_users
                retention_by_date[report_date]["D1 Retention"] = rate(active_users, total_users)
            elif day == 7:
                retention_by_date[report_date]["D7 Active Users"] = active_users
                retention_by_date[report_date]["D7 Retention"] = rate(active_users, total_users)

    return retention_by_date


def remove_empty_and_duplicate_columns(rows: list[list]) -> list[list]:
    if not rows or not rows[0]:
        return rows

    header = [str(value).strip() for value in rows[0]]
    protected = {"Package Name", "Date"}
    keep_indexes = []
    seen_signatures: dict[tuple[str, ...], str] = {}

    for index, column_name in enumerate(header):
        values = tuple(str(row[index]).strip() if index < len(row) else "" for row in rows[1:])

        if column_name in protected:
            keep_indexes.append(index)
            continue

        if all(value == "" for value in values):
            continue

        if values in seen_signatures:
            print(f"Removed duplicate-data column: {column_name} = {seen_signatures[values]}")
            continue

        seen_signatures[values] = column_name
        keep_indexes.append(index)

    return [[row[index] if index < len(row) else "" for index in keep_indexes] for row in rows]


def build_header(event_names: list[str]) -> list[str]:
    header = [
        "Package Name",
        "Date",
        "Active Users",
        "New Users",
        "Sessions",
        "Engaged Sessions",
        "Avg Session Duration Seconds",
        "Avg Session Duration",
        "Total Engagement Seconds",
        "Total Engagement Time",
        "Sessions Per Active User",
        "Engagement Rate",
        "Total Event Count",
        "Total Revenue",
        "First Open Users",
        "Home Screen Users",
        "Home Screen Views",
        "Funnel Drop Off",
        "Funnel Conversion Rate",
        "Cohort Total Users",
        "D1 Active Users",
        "D1 Retention",
        "D7 Active Users",
        "D7 Retention",
    ]

    for event_name in event_names:
        if event_name == "first_open":
            continue
        header.append(f"{event_name} Event Count")

    return header


def build_rows_for_app(app: AppConfig, report_dates: list[str], package_name: str, event_names: list[str]) -> list[list]:
    print(f"Processing: {app.app_name} / {app.property_id} / {report_dates[0]} to {report_dates[-1]}")

    daily_metrics = {}
    event_data = {}
    home_data = {}
    retention_data = {}

    try:
        daily_metrics = run_daily_metrics_report(app)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"DAILY METRICS {status} for {app.app_name}: {error_text}")

    try:
        event_data = run_event_report(app, event_names)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"EVENTS {status} for {app.app_name}: {error_text}")

    try:
        home_data = run_home_screen_report(app)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"HOME SCREEN {status} for {app.app_name}: {error_text}")

    try:
        retention_data = run_retention_report(app, report_dates)
    except Exception as error:
        status, error_text = classify_api_error(error)
        print(f"RETENTION {status} for {app.app_name}: {error_text}")

    rows = []

    for report_date in report_dates:
        metrics = daily_metrics.get(report_date, {})
        first_open_users = event_data.get((report_date, "first_open"), {}).get("active_users", 0)
        home_users = home_data.get(report_date, {}).get("active_users", 0)
        home_views = home_data.get(report_date, {}).get("event_count", 0)
        drop_off = to_float(first_open_users) - to_float(home_users)
        if drop_off < 0:
            drop_off = 0

        row_values = {
            "Package Name": package_name,
            "Date": report_date,
            "Active Users": metrics.get("Active Users", 0),
            "New Users": metrics.get("New Users", 0),
            "Sessions": metrics.get("Sessions", 0),
            "Engaged Sessions": metrics.get("Engaged Sessions", 0),
            "Avg Session Duration Seconds": metrics.get("Avg Session Duration Seconds", 0),
            "Avg Session Duration": metrics.get("Avg Session Duration", "0m 0s"),
            "Total Engagement Seconds": metrics.get("Total Engagement Seconds", 0),
            "Total Engagement Time": metrics.get("Total Engagement Time", "0m 0s"),
            "Sessions Per Active User": metrics.get("Sessions Per Active User", 0),
            "Engagement Rate": metrics.get("Engagement Rate", "0%"),
            "Total Event Count": metrics.get("Total Event Count", 0),
            "Total Revenue": metrics.get("Total Revenue", 0),
            "First Open Users": first_open_users,
            "Home Screen Users": home_users,
            "Home Screen Views": home_views,
            "Funnel Drop Off": int(drop_off),
            "Funnel Conversion Rate": rate(home_users, first_open_users),
        }
        row_values.update(retention_data.get(report_date, {}))

        for event_name in event_names:
            if event_name == "first_open":
                continue
            row_values[f"{event_name} Event Count"] = event_data.get((report_date, event_name), {}).get("event_count", 0)

        rows.append(row_values)

    header = build_header(event_names)
    return [[row.get(column, "") for column in header] for row in rows]


def main():
    print("Reading app list from Apps Config sheet...")
    apps = read_apps_config()
    report_dates = get_report_dates()

    print(f"Total enabled apps found: {len(apps)}")
    print(f"Report date range: {report_dates[0]} to {report_dates[-1]}")

    notification_events = split_csv(config.notification_event_names)
    key_events = split_csv(config.key_event_names)
    event_names = split_csv(",".join(["first_open"] + notification_events + key_events))

    header = build_header(event_names)
    rows = [header]

    for app in apps:
        package_name = fetch_ga4_package_name(app)
        if package_name:
            print(f"Package name found for {app.app_name}: {package_name}")
        else:
            print(f"Package name not found for {app.app_name}; final Package Name cell will be blank.")

        rows.extend(build_rows_for_app(app, report_dates, package_name, event_names))

    rows = remove_empty_and_duplicate_columns(rows)
    write_sheet(config.merged_sheet, rows)

    print("Done. Only the merged report was updated.")
    print(f"Merged Sheet: {config.merged_sheet}")
    print(f"Rows written: {len(rows) - 1}")
    print(
        "Expected GA4 Data API calls: "
        f"about {len(apps) * (3 + ((len(report_dates) + 11) // 12))} "
        "instead of apps × dates × reports."
    )


if __name__ == "__main__":
    main()
