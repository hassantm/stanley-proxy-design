"""
db.py — asyncpg pool lifecycle and non-throwing exchange inserts.
"""
import logging
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

_INSERT_SQL = """
    INSERT INTO exchanges (session_id, agent_id, role, content, model, source)
    VALUES ($1, $2, $3, $4, $5, 'proxy')
"""


class ExchangeLogger:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
    ) -> "ExchangeLogger":
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def probe(self) -> bool:
        """Run SELECT 1 to verify DB connectivity. Logs a warning on failure."""
        try:
            await self._pool.fetchval("SELECT 1")
            return True
        except Exception:
            logger.warning("DB startup probe failed — will retry on first insert", exc_info=True)
            return False

    async def log_exchange(
        self,
        *,
        session_id: str,
        agent_id: Optional[str],
        role: str,
        content: str,
        model: Optional[str],
    ) -> None:
        """Insert one row into exchanges. Never raises — logs to stderr on failure."""
        try:
            await self._pool.execute(
                _INSERT_SQL,
                session_id,
                agent_id,
                role,
                content,
                model,
            )
        except Exception:
            logger.exception(
                "Failed to insert exchange row (session=%s role=%s)", session_id, role
            )

    async def log_turns(
        self,
        *,
        session_id: str,
        agent_id: Optional[str],
        model: Optional[str],
        turns: list[tuple[str, str]],
    ) -> None:
        """Insert multiple (role, content) turns. Each insert is independent."""
        for role, content in turns:
            await self.log_exchange(
                session_id=session_id,
                agent_id=agent_id,
                role=role,
                content=content,
                model=model,
            )
