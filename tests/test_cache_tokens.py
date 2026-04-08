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

    async def test_cache_tokens_sum_into_total_input(self):
        """total_input_tokens includes cache_read + cache_creation + base input_tokens."""
        from switchboard.dispatch.engine import _update_usage

        task = self._make_task()
        result = self._make_result(input_tokens=100, cache_read=50, cache_creation=20)

        mock_get = AsyncMock(return_value=task)
        mock_update = AsyncMock()

        with patch("switchboard.dispatch.engine.db.get_task", mock_get), \
             patch("switchboard.dispatch.engine.db.update_task", mock_update):
            await _update_usage("proj/task-1", result)

        call_kwargs = mock_update.call_args[1]
        # 100 + 50 + 20 = 170
        assert call_kwargs["total_input_tokens"] == 170

    async def test_cache_tokens_accumulate_across_attempts(self):
        """Cache tokens accumulate (sum) across multiple calls, like other token fields."""
        from switchboard.dispatch.engine import _update_usage

        # Task already has 30 cache_read from a prior attempt
        task = self._make_task(total_input=200, cache_read=30, cache_creation=5)
        result = self._make_result(input_tokens=100, cache_read=40, cache_creation=10)

        mock_get = AsyncMock(return_value=task)
        mock_update = AsyncMock()

        with patch("switchboard.dispatch.engine.db.get_task", mock_get), \
             patch("switchboard.dispatch.engine.db.update_task", mock_update):
            await _update_usage("proj/task-1", result)

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["total_cache_read_tokens"] == 70   # 30 + 40
        assert call_kwargs["total_cache_creation_tokens"] == 15  # 5 + 10

    async def test_missing_cache_fields_default_to_zero(self):
        """Missing cache fields in usage don't crash; they default to 0."""
        from switchboard.dispatch.engine import _update_usage

        task = self._make_task()
        result = MagicMock()
        result.total_cost_usd = 0.0
        result.usage = {"input_tokens": 50, "output_tokens": 25}  # no cache fields

        mock_get = AsyncMock(return_value=task)
        mock_update = AsyncMock()

        with patch("switchboard.dispatch.engine.db.get_task", mock_get), \
             patch("switchboard.dispatch.engine.db.update_task", mock_update):
            await _update_usage("proj/task-1", result)

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["total_cache_read_tokens"] == 0
        assert call_kwargs["total_cache_creation_tokens"] == 0
        assert call_kwargs["total_input_tokens"] == 50

    async def test_null_usage_results_in_zero_cache_tokens(self):
        """When result.usage is None/falsy, cache tokens are 0."""
        from switchboard.dispatch.engine import _update_usage

        task = self._make_task()
        result = MagicMock()
        result.total_cost_usd = 0.0
        result.usage = None

        mock_get = AsyncMock(return_value=task)
        mock_update = AsyncMock()

        with patch("switchboard.dispatch.engine.db.get_task", mock_get), \
             patch("switchboard.dispatch.engine.db.update_task", mock_update):
            await _update_usage("proj/task-1", result)

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["total_cache_read_tokens"] == 0
        assert call_kwargs["total_cache_creation_tokens"] == 0

    async def test_null_existing_cache_tokens_treated_as_zero(self):
        """If task has NULL for cache token columns (pre-migration rows), treat as 0."""
        from switchboard.dispatch.engine import _update_usage

        # Simulate a pre-migration row where columns are NULL
        task = self._make_task()
        task["total_cache_read_tokens"] = None
        task["total_cache_creation_tokens"] = None

        result = self._make_result(input_tokens=100, cache_read=30, cache_creation=10)

        mock_get = AsyncMock(return_value=task)
        mock_update = AsyncMock()

        with patch("switchboard.dispatch.engine.db.get_task", mock_get), \
             patch("switchboard.dispatch.engine.db.update_task", mock_update):
            await _update_usage("proj/task-1", result)

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["total_cache_read_tokens"] == 30
        assert call_kwargs["total_cache_creation_tokens"] == 10
