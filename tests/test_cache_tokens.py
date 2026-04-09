"""Tests for cache token accumulation in _update_usage (dispatch/engine.py)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestUpdateUsageCacheTokens:
    """Verify _update_usage correctly accumulates cache token fields."""

    def _make_result(self, input_tokens=0, output_tokens=0,
                     cache_read=0, cache_creation=0, cost=0.0):
        """Build a mock SDK ResultMessage."""
        result = MagicMock()
        result.total_cost_usd = cost
        result.usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        }
        return result

    def _make_task(self, total_input=0, total_output=0, total_cost=0.0,
                   cache_read=0, cache_creation=0):
        """Build a mock task dict as returned by db.get_task."""
        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": total_cost,
            "total_cache_read_tokens": cache_read,
            "total_cache_creation_tokens": cache_creation,
        }

    async def test_cache_read_tokens_accumulated(self):
        """cache_read_input_tokens is stored in total_cache_read_tokens."""
        from switchboard.dispatch.engine import _update_usage

        task = self._make_task()
        result = self._make_result(input_tokens=100, cache_read=40, cache_creation=10)

        mock_get = AsyncMock(return_value=task)
        mock_update = AsyncMock()

        with patch("switchboard.dispatch.engine.db.get_task", mock_get), \
             patch("switchboard.dispatch.engine.db.update_task", mock_update):
            await _update_usage("proj/task-1", result)

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["total_cache_read_tokens"] == 40
        assert call_kwargs["total_cache_creation_tokens"] == 10


