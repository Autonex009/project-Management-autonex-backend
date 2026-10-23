
"""
Thread-safe in-memory TTL caching service for Project and Employee Portal views.
Zero Redis dependency. Safe for AnyIO / multi-worker thread pools.
Complies with skilss.md Law 3 & Law 10 (Memory Leak and Connection Pool protection).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class InMemoryTTLCache:
    """Thread-safe in-memory cache with configurable TTL and max-size eviction.
    Protects the 14-connection Supabase pool by serving burst views from RAM.
    """

    def __init__(self, ttl_seconds: float = 60.0, max_size: int = 1000) -> None:
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._cache: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Optional[Any]:
        """Fetch item if present and within TTL."""
        now = time.time()
        with self._lock:
            if key in self._cache:
                timestamp, value = self._cache[key]
                if now - timestamp < self.ttl:
                    return value
                # Expired - remove immediately
                del self._cache[key]
        return None

    def set(self, key: Any, value: Any) -> None:
        """Store item with current timestamp, self-pruning expired or excess entries."""
        now = time.time()
        with self._lock:
            # Self-prune expired keys if cache reaches or exceeds max_size
            if len(self._cache) >= self.max_size:
                expired = [k for k, (ts, _) in self._cache.items() if now - ts >= self.ttl]
                for k in expired:
                    del self._cache[k]
                # If still over capacity after pruning expired, pop oldest (FIFO)
                while len(self._cache) >= self.max_size:
                    self._cache.pop(next(iter(self._cache)), None)

            self._cache[key] = (now, value)

    def invalidate(self, key: Optional[Any] = None) -> None:
        """Invalidate cache for a specific key, or clear all entries if key is None."""
        with self._lock:
            if key is not None:
                self._cache.pop(key, None)
            else:
                self._cache.clear()

    def size(self) -> int:
        """Return the current number of cached entries."""
        with self._lock:
            return len(self._cache)


# Global singleton instance for Employee Portal "My Projects" endpoint
my_projects_cache = InMemoryTTLCache(ttl_seconds=60.0, max_size=1000)

# L1 In-Memory Caches for Project Metadata & Active Allocations
project_name_l1_cache = InMemoryTTLCache(ttl_seconds=300.0, max_size=2000)
allocation_l1_cache = InMemoryTTLCache(ttl_seconds=60.0, max_size=1000)
employee_profile_l1_cache = InMemoryTTLCache(ttl_seconds=300.0, max_size=1000)

_redis_client: Optional[Any] = None
_redis_initialized: bool = False
_redis_lock = threading.Lock()


def get_redis_client() -> Optional[Any]:
    """Thread-safe lazy initialization of Redis client with circuit breaker fallback."""
    global _redis_client, _redis_initialized
    if _redis_initialized:
        return _redis_client
    with _redis_lock:
        if _redis_initialized:
            return _redis_client
        import os
        from dotenv import load_dotenv
        load_dotenv()
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                import redis
                client = redis.from_url(redis_url, socket_timeout=2.0, decode_responses=True)
                client.ping()
                _redis_client = client
                logger.info("project_cache: Connected to Redis L2 layer successfully.")
            except Exception as exc:
                logger.warning("project_cache: Redis connection failed (%s), running in L1 RAM mode only.", exc)
                _redis_client = None
        else:
            _redis_client = None
        _redis_initialized = True
        return _redis_client


def get_project_names_batch(db: Any, project_ids: list[int | str]) -> dict[int, str]:
    """Dual-tier L1 (RAM) + L2 (Redis) + L3 (PostgreSQL) lookup for project names.
    Guarantees zero N+1 queries and protects the 14-slot Supabase connection pool.
    """
    if not project_ids:
        return {}

    # Defensive parsing: convert strings, deduplicate, filter out non-integers
    valid_pids: list[int] = []
    seen = set()
    for p in project_ids:
        try:
            pid_int = int(p)
            if pid_int not in seen:
                seen.add(pid_int)
                valid_pids.append(pid_int)
        except (ValueError, TypeError):
            continue

    if not valid_pids:
        return {}

    missing_ids: list[int] = []
    result: dict[int, str] = {}

    # 1. Tier 1: Check L1 RAM (< 0.001 ms)
    for pid in valid_pids:
        cached_name = project_name_l1_cache.get(pid)
        if cached_name:
            result[pid] = cached_name
        else:
            missing_ids.append(pid)

    if not missing_ids:
        return result

    # 2. Tier 2: Check L2 Redis (~0.8 ms)
    redis_client = get_redis_client()
    if redis_client:
        try:
            raw_vals = redis_client.hmget("cache:project_names", [str(pid) for pid in missing_ids])
            still_missing = []
            for pid, val in zip(missing_ids, raw_vals):
                if val:
                    result[pid] = val
                    project_name_l1_cache.set(pid, val)
                else:
                    still_missing.append(pid)
            missing_ids = still_missing
        except Exception as exc:
            logger.debug("project_cache: Redis L2 read failed, falling back to L3: %s", exc)

    # 3. Tier 3: PostgreSQL Database Fallback (Single batch query for remaining missing IDs only)
    if missing_ids:
        try:
            import app.models.parent_project  # Required to prevent SQLAlchemy mapper Initialization errors
            import app.models.sub_project
            from app.models.project import DailySheet

            rows = db.query(DailySheet.id, DailySheet.name).filter(DailySheet.id.in_(missing_ids)).all()
            fetched_map = {r[0]: r[1] for r in rows}

            # Backfill L1 RAM and L2 Redis
            for pid, name in fetched_map.items():
                result[pid] = name
                project_name_l1_cache.set(pid, name)

            if redis_client and fetched_map:
                try:
                    redis_client.hset("cache:project_names", mapping={str(k): v for k, v in fetched_map.items()})
                    redis_client.expire("cache:project_names", 86400)  # 24 Hours TTL
                except Exception as exc:
                    logger.debug("project_cache: Redis L2 write failed: %s", exc)
        except Exception as exc:
            logger.warning("project_cache: Error querying project names from DB: %s", exc)

    return result


def get_active_allocations_cached(db: Any, employee_id: int) -> set[int]:
    """Dual-tier L1 (RAM) + L2 (Redis) + L3 (PostgreSQL) lookup for employee active project IDs.
    Returns set of sub_project_ids the employee is officially allocated to.
    """
    # 1. Tier 1: Check L1 RAM (< 0.001 ms)
    cached_allocs = allocation_l1_cache.get(employee_id)
    if cached_allocs is not None:
        return cached_allocs

    # 2. Tier 2: Check L2 Redis (~0.8 ms)
    redis_client = get_redis_client()
    cache_key = f"alloc:active:{employee_id}"
    if redis_client:
        try:
            members = redis_client.smembers(cache_key)
            if members:
                # Handle sentinel for unassigned/bench employees with zero allocations
                if "__empty__" in members:
                    alloc_set = set()
                else:
                    alloc_set = {int(m) for m in members if m.isdigit()}
                allocation_l1_cache.set(employee_id, alloc_set)
                return alloc_set
        except Exception as exc:
            logger.debug("project_cache: Redis allocation read failed: %s", exc)

    # 3. Tier 3: PostgreSQL Database Fallback (Single index scan on ix_allocations_employee_active)
    from app.models.allocation import Allocation
    rows = (
        db.query(Allocation.sub_project_id)
        .filter(Allocation.employee_id == employee_id, Allocation.is_active == True)
        .all()
    )
    alloc_set = {r[0] for r in rows if r[0] is not None}

    # Backfill L1 and L2
    allocation_l1_cache.set(employee_id, alloc_set)
    if redis_client:
        try:
            redis_client.delete(cache_key)
            if alloc_set:
                redis_client.sadd(cache_key, *[str(pid) for pid in alloc_set])
            else:
                redis_client.sadd(cache_key, "__empty__")
            redis_client.expire(cache_key, 600)  # 10 Minutes TTL
        except Exception as exc:
            logger.debug("project_cache: Redis allocation write failed: %s", exc)

    return alloc_set


def invalidate_allocation_cache(employee_id: Optional[int] = None) -> None:
    """Invalidate allocation cache in L1 and L2 when an employee is allocated/unallocated."""
    allocation_l1_cache.invalidate(employee_id)
    redis_client = get_redis_client()
    if redis_client:
        try:
            if employee_id is not None:
                redis_client.delete(f"alloc:active:{employee_id}")
            else:
                for k in redis_client.scan_iter("alloc:active:*"):
                    redis_client.delete(k)
        except Exception as exc:
            logger.debug("project_cache: Redis allocation invalidation failed: %s", exc)


def invalidate_my_projects_cache(employee_id: Optional[int] = None) -> None:
    """Invalidate cached 'My Projects' data for a specific employee or all employees."""
    my_projects_cache.invalidate(employee_id)
    invalidate_allocation_cache(employee_id)
    if employee_id is not None:
        logger.info("Invalidated my_projects_cache and allocation_cache for employee_id=%s", employee_id)
    else:
        logger.info("Cleared all my_projects_cache and allocation_cache")


# L1 In-Memory Cache for Projects Dropdown list (1 minute TTL)
dropdown_l1_cache = InMemoryTTLCache(ttl_seconds=60.0, max_size=10)



def get_cached_projects_dropdown(db: Optional[Any] = None) -> list[dict]:
    """Fetch full active unarchived project dropdown items using Dual-Tier L1 (RAM) + L2 (Redis).
    Avoids hitting PostgreSQL for dropdown lists during active user sessions.
    """
    # 1. Tier 1: Check L1 RAM (< 0.001 ms)
    cached = dropdown_l1_cache.get("all")
    if cached is not None:
        return cached

    # 2. Tier 2: Check L2 Redis (~0.8 ms)
    redis_client = get_redis_client()
    cache_key = "cache:projects_dropdown"
    if redis_client:
        try:
            raw_data = redis_client.get(cache_key)
            if raw_data:
                import json
                items = json.loads(raw_data)
                dropdown_l1_cache.set("all", items)
                return items
        except Exception as exc:
            logger.debug("project_cache: Redis dropdown read failed: %s", exc)

    # 3. Tier 3: PostgreSQL Database Fallback
    need_close = False
    session = db
    if session is None:
        from app.db.database import SessionLocal
        session = SessionLocal()
        need_close = True

    try:
        import app.models.parent_project
        import app.models.sub_project
        from sqlalchemy import func
        from app.models.project import DailySheet as Project

        ARCHIVED_STATUSES = {"completed", "on-hold", "cancelled"}
        rows = (
            session.query(
                Project.id,
                Project.name,
                Project.required_manpower,
                Project.required_expertise,
                Project.project_status,
            )
            .filter(
                (Project.project_status == None) |
                (~func.lower(Project.project_status).in_(ARCHIVED_STATUSES))
            )
            .order_by(Project.name.asc())
            .all()
        )
        items = [
            {
                "id": r[0],
                "name": r[1],
                "required_manpower": r[2] or 0,
                "required_expertise": r[3] or [],
                "project_status": r[4] or "active",
            }
            for r in rows
        ]

        # Backfill L1 RAM and L2 Redis
        dropdown_l1_cache.set("all", items)
        if redis_client:
            try:
                import json
                redis_client.set(cache_key, json.dumps(items), ex=3600)  # 1 Hour TTL
            except Exception as exc:
                logger.debug("project_cache: Redis dropdown write failed: %s", exc)

        return items
    finally:
        if need_close and session is not None:
            session.close()


def invalidate_projects_dropdown_cache() -> None:
    """Invalidate project dropdown cache in L1 and L2 when projects change."""
    dropdown_l1_cache.invalidate()
    redis_client = get_redis_client()
    if redis_client:
        try:
            redis_client.delete("cache:projects_dropdown")
        except Exception as exc:
            logger.debug("project_cache: Redis dropdown invalidation failed: %s", exc)

