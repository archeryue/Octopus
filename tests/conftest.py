"""Global test configuration — ensure tests use known default settings."""

import os

# Override env vars before any module imports Settings, so tests
# don't pick up values from the user's .env file.
os.environ["OCTOPUS_AUTH_TOKEN"] = "changeme"
