"""
Fetches secrets from AWS Secrets Manager and caches them in-process.

All secrets live under a single JSON secret named 'bus-delay-tracker/credentials':
  {
    "google_maps_api_key": "..."
  }

AWS credentials are never stored here — they come from the boto3 credential
chain in priority order:
  1. IAM role attached to the instance/task (best, zero config)
  2. ~/.aws/credentials set by `aws configure` (good for local dev)
  3. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars (last resort)
"""

import json
import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

SECRET_NAME = "bus-delay-tracker/credentials"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache

    region = os.environ.get("AWS_REGION", "ca-central-1")
    client = boto3.client("secretsmanager", region_name=region)
    try:
        value = client.get_secret_value(SecretId=SECRET_NAME)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        raise RuntimeError(
            f"Could not retrieve secret '{SECRET_NAME}' from Secrets Manager "
            f"({code}). Ensure the secret exists and your IAM identity has "
            f"secretsmanager:GetSecretValue permission."
        ) from e
    except BotoCoreError as e:
        raise RuntimeError(f"AWS error loading secrets: {e}") from e

    _cache = json.loads(value["SecretString"])
    logger.debug("Secrets loaded from AWS Secrets Manager.")
    return _cache


def get(key: str) -> str:
    """Return a single secret value by key."""
    secrets = _load()
    if key not in secrets:
        raise KeyError(
            f"Key '{key}' not found in secret '{SECRET_NAME}'. "
            f"Available keys: {list(secrets.keys())}"
        )
    return secrets[key]
