from __future__ import annotations

import os


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def table_name(env_key: str, default: str) -> str:
    """Resolve a physical DB table name.

    Each table has a hardcoded default that matches the Aryan/shared schema.
    Set the corresponding TABLE_* env var to override a single table name.
    """
    explicit = str(os.getenv(env_key) or "").strip()
    return explicit if explicit else default


# ── Table names (Aryan/shared schema as default) ──────────────────────────────
# Tables that exist in cviance_db_aryan.sql use that name as the default.
# Tables with no Aryan equivalent keep their own name.

TABLE_CANDIDATES             = table_name("TABLE_CANDIDATES",             "candidates")
TABLE_JOBS                   = table_name("TABLE_JOBS",                   "jobs")
TABLE_CREDENTIALS            = table_name("TABLE_CREDENTIALS",            "platform_accounts")
TABLE_APPLICATIONS           = table_name("TABLE_APPLICATIONS",           "job_applications")
TABLE_PLATFORM_RUNS          = table_name("TABLE_PLATFORM_RUNS",          "platform_runs")           # no Aryan equiv
TABLE_UNANSWERED_QUESTIONS   = table_name("TABLE_UNANSWERED_QUESTIONS",   "application_qa")
TABLE_FORM_ANSWERS           = table_name("TABLE_FORM_ANSWERS",           "candidate_qa")
TABLE_RUN_LOGS               = table_name("TABLE_RUN_LOGS",               "application_logs")
TABLE_APPLICATION_SCREENSHOTS = table_name("TABLE_APPLICATION_SCREENSHOTS", "application_screenshots")  # no Aryan equiv
TABLE_JOB_HARVESTER_CONFIGS  = table_name("TABLE_JOB_HARVESTER_CONFIGS",  "harvester_configs")
TABLE_JOB_HARVESTER_RUNS     = table_name("TABLE_JOB_HARVESTER_RUNS",     "harvester_runs")
TABLE_JOB_HARVESTER_ITEMS    = table_name("TABLE_JOB_HARVESTER_ITEMS",    "harvester_items")


USING_DYNAMIC_TABLE_NAMES = any(
    os.getenv(key)
    for key in (
        "TABLE_CANDIDATES",
        "TABLE_JOBS",
        "TABLE_CREDENTIALS",
        "TABLE_APPLICATIONS",
        "TABLE_PLATFORM_RUNS",
        "TABLE_UNANSWERED_QUESTIONS",
        "TABLE_FORM_ANSWERS",
        "TABLE_RUN_LOGS",
        "TABLE_APPLICATION_SCREENSHOTS",
        "TABLE_JOB_HARVESTER_CONFIGS",
        "TABLE_JOB_HARVESTER_RUNS",
        "TABLE_JOB_HARVESTER_ITEMS",
    )
)