"""Configuration constants for NHS-JobWatch."""

import os

BASE_URL = "https://apply.jobs.scot.nhs.uk"
LISTING_ENDPOINT = f"{BASE_URL}/Home/_JobCard"

# Honest User-Agent identifying the tool (no browser impersonation)
USER_AGENT = "NHS-JobWatch/1.0 (personal vacancy monitor)"

# Network access constraints (Decision D-006, D-009, TRD 3.2)
REQUEST_TIMEOUT_SECONDS = 15.0
MIN_REQUEST_INTERVAL_SECONDS = 3.0

# Fixed default database path (relative to repo root)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "jobwatch.db")

# Monitored Scopes and per-scope request caps for baseline sweeps
MONITORED_SCOPES = [
    "Departments=9",
    "Departments=11",
    "what=nurse",
    "what=healthcare support worker",
    "what=health care assistant",
    "what=care assistant",
]

SCOPE_REQUEST_CAPS = {
    "Departments=9": 30,
    "Departments=11": 8,
    "what=nurse": 30,
    "what=healthcare support worker": 10,
    "what=health care assistant": 10,
    "what=care assistant": 10,
}


