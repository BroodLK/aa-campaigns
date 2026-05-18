"""Shared configuration for the Django-ESI OpenAPI 3.1 client."""

from __future__ import annotations
import logging
import time
from hashlib import md5
from typing import Any
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from email.utils import parsedate_to_datetime
from aiopenapi3 import ContentTypeError
from httpx import Response
from esi import app_settings
from esi.openapi_clients import ESIClientProvider, EsiOperation
from esi.exceptions import (
    ESIBucketLimitException,
    ESIErrorLimitException,
    HTTPClientError,
    HTTPNotModified,
)
from esi.rate_limiting import interval_to_seconds

from . import __title__, __version__, __github_url__, __esi_compatibility_date__

logger = logging.getLogger(__name__)

DEFAULT_OPERATIONS = [
    "PostUniverseNames",
    "PostUniverseIds",
    "GetCharactersCharacterIdCorporationhistory",
    "GetCorporationsCorporationId",
    "GetCorporationsCorporationIdAlliancehistory",
    "GetAlliancesAllianceId",
    "GetSovereigntyMap",
    "GetKillmailsKillmailIdKillmailHash",
]


esi = ESIClientProvider(
    compatibility_date=__esi_compatibility_date__,
    ua_appname=__title__,
    ua_version=__version__,
    ua_url=__github_url__,
    operations=DEFAULT_OPERATIONS,
)


def to_plain(value):
    """Recursively convert Pydantic models returned by the OpenAPI client to plain Python types."""
    from datetime import datetime, date

    if hasattr(value, "model_dump"):
        return to_plain(value.model_dump())
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain(val) for key, val in value.items()}
    # Convert datetime objects to ISO format strings for consistency
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def parse_expires(headers: dict | None):
    """Extract a timezone-aware datetime from HTTP Expires headers (if present)."""
    if not headers:
        return None
    value = headers.get("Expires")
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def call_result(
    operation,
    use_etag: bool = True,
    force_refresh: bool = False,
    use_cache: bool = True,
    **kwargs,
):
    """Execute an OpenAPI operation.result() call and return (data, expires_at)."""
    op = _prepare_operation(operation, **kwargs)
    data, response = _execute_operation(
        operation,
        op,
        kwargs,
        list_result=False,
        use_etag=use_etag,
        force_refresh=force_refresh,
        use_cache=use_cache,
    )
    expires_at = parse_expires(response.headers) if response else None
    return data, expires_at


def call_results(
    operation,
    use_etag: bool = True,
    force_refresh: bool = False,
    use_cache: bool = True,
    **kwargs,
):
    """Execute operation.results() and return (list_data, expires_at) with plain types."""
    op = _prepare_operation(operation, **kwargs)
    data, response = _execute_operation(
        operation,
        op,
        kwargs,
        list_result=True,
        use_etag=use_etag,
        force_refresh=force_refresh,
        use_cache=use_cache,
    )
    expires_at = parse_expires(response.headers) if response else None
    return data, expires_at


class ESIHandler:
    """Handler for ESI operations with OpenAPI client semantics."""

    @classmethod
    def result(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        cls,
        operation: EsiOperation,
        use_etag: bool = True,
        return_response: bool = False,
        force_refresh: bool = False,
        use_cache: bool = True,
        **extra,
    ) -> tuple[Any, Response] | Any:
        """Retrieve the result of an ESI operation, handling common exceptions."""
        op = _prepare_operation(operation, **extra)
        data, response = _execute_operation(
            operation,
            op,
            extra,
            list_result=False,
            use_etag=use_etag,
            force_refresh=force_refresh,
            use_cache=use_cache,
        )
        return (data, response) if return_response else data

    @classmethod
    def results(
        cls,
        operation: EsiOperation,
        use_etag: bool = True,
        return_response: bool = False,
        force_refresh: bool = False,
        use_cache: bool = True,
        **extra,
    ) -> tuple[Any, Response] | Any:
        """Retrieve list results of an ESI operation."""
        op = _prepare_operation(operation, **extra)
        data, response = _execute_operation(
            operation,
            op,
            extra,
            list_result=True,
            use_etag=use_etag,
            force_refresh=force_refresh,
            use_cache=use_cache,
        )
        return (data, response) if return_response else data

    @classmethod
    def post_universe_names(cls, ids: list[int], use_etag: bool = False) -> list[dict] | None:
        logger.debug("Getting universe names for %s IDs", len(ids))
        return cls.result(
            operation=esi.client.Universe.PostUniverseNames(body=ids),
            use_etag=use_etag,
        )

    @classmethod
    def post_universe_ids(cls, names: list[str], use_etag: bool = True) -> list[dict] | None:
        logger.debug("Getting universe IDs for %s names", len(names))
        return cls.result(
            operation=esi.client.Universe.PostUniverseIds(body=names),
            use_etag=use_etag,
        )

    @classmethod
    def get_corporations_corporation_id(
        cls, corporation_id: int, use_etag: bool = True
    ) -> dict[str, Any] | None:
        logger.debug("Getting corporation information for corporation ID: %s", corporation_id)
        return cls.result(
            operation=esi.client.Corporation.GetCorporationsCorporationId(
                corporation_id=corporation_id
            ),
            use_etag=use_etag,
        )

    @classmethod
    def get_alliances_alliance_id(
        cls, alliance_id: int, use_etag: bool = True
    ) -> dict[str, Any] | None:
        logger.debug("Getting alliance information for alliance ID: %s", alliance_id)
        return cls.result(
            operation=esi.client.Alliance.GetAlliancesAllianceId(
                alliance_id=alliance_id
            ),
            use_etag=use_etag,
        )

    @classmethod
    def get_killmail(
        cls, killmail_id: int, killmail_hash: str, use_etag: bool = True, force_refresh: bool = False
    ) -> dict[str, Any] | None:
        logger.debug("Getting killmail %s from ESI", killmail_id)
        return cls.result(
            operation=esi.client.Killmails.GetKillmailsKillmailIdKillmailHash(
                killmail_id=killmail_id, killmail_hash=killmail_hash
            ),
            use_etag=use_etag,
            force_refresh=force_refresh,
        )


def _prepare_operation(operation, **kwargs):
    spec_backoff_seconds = getattr(settings, "ESI_SPEC_BACKOFF_SECONDS", 60)
    op = _resolve_operation(operation, spec_backoff_seconds)
    return _bind_operation(op, **kwargs)


def _execute_operation(
    operation,
    op,
    kwargs,
    *,
    list_result: bool,
    use_etag: bool,
    force_refresh: bool,
    use_cache: bool,
):
    rate_limit_threshold = getattr(settings, "ESI_RATE_LIMIT_SOFT_THRESHOLD", 100)
    max_backoff_retries = getattr(settings, "ESI_RATE_LIMIT_MAX_RETRIES", 3)
    spec_backoff_seconds = getattr(settings, "ESI_SPEC_BACKOFF_SECONDS", 60)
    attempts = 0
    spec_refreshed = False
    try:
        while True:
            try:
                if list_result:
                    data, response = op.results(
                        return_response=True,
                        use_etag=use_etag,
                        force_refresh=force_refresh,
                        use_cache=use_cache,
                    )
                else:
                    data, response = op.result(
                        return_response=True,
                        use_etag=use_etag,
                        force_refresh=force_refresh,
                        use_cache=use_cache,
                    )
                _log_rate_limit_remaining(response.headers)
                _maybe_backoff_on_rate_limit(response.headers, rate_limit_threshold)
                return to_plain(data), response
            except HTTPNotModified:
                logger.debug(
                    "ESI returned 304 Not Modified for %s",
                    getattr(op, "operation", None).operationId
                    if getattr(op, "operation", None)
                    else op,
                )
                return None, None
            except ContentTypeError:
                logger.warning(
                    "ESI returned gibberish (ContentTypeError) for %s",
                    getattr(op, "operation", None).operationId
                    if getattr(op, "operation", None)
                    else op,
                )
                return None, None
            except HTTPClientError as exc:
                logger.error("ESI HTTP error for %s: %s", op, exc)
                return None, None
            except (ESIBucketLimitException, ESIErrorLimitException) as e:
                attempts += 1
                wait_seconds = int(getattr(e, "reset", 0) or 0)
                if wait_seconds <= 0:
                    wait_seconds = 60
                logger.warning("ESI rate limit hit (%s). Backing off for %ss.", e, wait_seconds)
                time.sleep(wait_seconds)
                if attempts >= max_backoff_retries:
                    raise
            except Exception as e:
                if _should_refresh_spec(e, spec_refreshed):
                    spec_refreshed = True
                    _refresh_esi_client()
                    time.sleep(spec_backoff_seconds)
                    op = _bind_operation(
                        _rebind_operation(operation, spec_backoff_seconds), **kwargs
                    )
                    continue
                raise
    except Exception as e:
        logger.error("Error calling ESI operation: %s", e)
        return None, None


def _bind_operation(operation, **kwargs):
    return operation(**kwargs) if kwargs else operation


def _is_operation_factory(operation) -> bool:
    return callable(operation) and not hasattr(operation, "result")


def _resolve_operation(operation, spec_backoff_seconds: int):
    if not _is_operation_factory(operation):
        return operation
    try:
        return operation()
    except Exception as e:
        if _should_refresh_spec(e, False):
            _refresh_esi_client()
            time.sleep(spec_backoff_seconds)
            return operation()
        raise


def _rebind_operation(operation, spec_backoff_seconds: int):
    if _is_operation_factory(operation):
        return _resolve_operation(operation, spec_backoff_seconds)
    op_meta = getattr(operation, "operation", None)
    if not op_meta:
        return operation
    tag = op_meta.tags[0] if getattr(op_meta, "tags", None) else None
    op_id = getattr(op_meta, "operationId", None)
    if not tag or not op_id:
        return operation
    tag_attr = tag.replace(" ", "_")
    try:
        return getattr(getattr(esi.client, tag_attr), op_id)
    except Exception:
        return operation


def _should_refresh_spec(error: Exception, already_refreshed: bool) -> bool:
    if already_refreshed:
        return False
    return "components" in str(error)


def _refresh_esi_client() -> None:
    _clear_esi_spec_cache()
    esi._client = None
    esi._client_async = None


def _clear_esi_spec_cache() -> None:
    spec_url = f"{app_settings.ESI_API_URL}meta/openapi.json"
    compat_date = str(__esi_compatibility_date__)
    cache_key = f"ESI_API_CACHE_{md5(f'{spec_url}-{compat_date}'.encode()).hexdigest()}"
    cache.delete(cache_key)


def _maybe_backoff_on_rate_limit(headers: dict | None, threshold: int) -> None:
    if not headers or threshold <= 0:
        return

    remaining_value = headers.get("x-ratelimit-remaining")
    if remaining_value is None:
        remaining_value = headers.get("X-RateLimit-Remaining")
    try:
        remaining = int(remaining_value)
    except (TypeError, ValueError):
        return

    if remaining >= threshold:
        return

    group = headers.get("x-ratelimit-group") or headers.get("X-RateLimit-Group")
    limit_header = headers.get("x-ratelimit-limit") or headers.get("X-RateLimit-Limit")
    window_seconds = _parse_window_seconds(limit_header)

    wait_seconds = _get_bucket_ttl_seconds(group)
    if wait_seconds is None:
        wait_seconds = window_seconds or 60

    logger.warning(
        "ESI rate limit remaining low for %s: %s. Backing off for %ss.",
        group or "unknown",
        remaining,
        wait_seconds
    )
    time.sleep(wait_seconds)


def _log_rate_limit_remaining(headers: dict | None) -> None:
    if not headers:
        return

    remaining_value = headers.get("x-ratelimit-remaining")
    if remaining_value is None:
        remaining_value = headers.get("X-RateLimit-Remaining")
    try:
        remaining = int(remaining_value)
    except (TypeError, ValueError):
        return

    group = headers.get("x-ratelimit-group") or headers.get("X-RateLimit-Group") or "unknown"
    limit = headers.get("x-ratelimit-limit") or headers.get("X-RateLimit-Limit") or ""
    if limit:
        logger.info("ESI rate limit remaining: %s %s/%s", group, remaining, limit)
    else:
        logger.info("ESI rate limit remaining: %s %s", group, remaining)


def _parse_window_seconds(limit_header: str | None) -> int | None:
    if not limit_header or "/" not in limit_header:
        return None
    _, window = limit_header.split("/", 1)
    window = window.strip()
    try:
        return interval_to_seconds(window)
    except Exception:
        return None


def _get_bucket_ttl_seconds(group: str | None) -> int | None:
    if not group:
        return None
    try:
        ttl = cache.ttl(f"esi:bucket:{group}")
    except Exception:
        return None
    if ttl is None or ttl < 0:
        return None
    return ttl + 1


"""
Helpers for caching ESI expiry timestamps in Django's cache backend.
"""

from datetime import datetime


def expiry_cache_key(kind: str, identifier) -> str:
    """Generate a namespaced cache key used to store expiry hints."""
    return f"aacampaign:esi_expiry:{kind}:{identifier}"


def get_cached_expiry(key: str) -> datetime | None:
    """Fetch previously stored expiry timestamps and convert them back to datetimes."""
    ts = cache.get(key)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        cache.delete(key)
        return None


def set_cached_expiry(key: str, expires_at: datetime | None) -> None:
    """
    Write a future expiry timestamp (or clear the cache when None).

    The epoch value is stored to avoid timezone serialization issues.
    """
    if not expires_at:
        cache.delete(key)
        return
    now = timezone.now()
    timeout = max(1, int((expires_at - now).total_seconds()))
    cache.set(key, expires_at.timestamp(), timeout)
