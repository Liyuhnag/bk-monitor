"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2025 Tencent. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

from datetime import datetime, timedelta

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bkmonitor.models import StrategyHistoryModel, StrategyModel
from bkmonitor.strategy.history import (
    CleanStrategyHistoryParams,
    _collect_keep_history_ids,
    _delete_queryset_in_batches,
    _iter_candidate_strategy_id_chunks,
    clean_strategy_history,
)


def _create_strategy(name: str) -> StrategyModel:
    return StrategyModel.objects.create(
        bk_biz_id=2,
        name=name,
        scenario="os",
        type=StrategyModel.StrategyType.Monitor,
    )


def _create_history(
    strategy_id: int,
    create_time: datetime,
    *,
    operate: str = "update",
    status: bool = True,
    content: dict | None = None,
    message: str = "",
) -> StrategyHistoryModel:
    history = StrategyHistoryModel.objects.create(
        strategy_id=strategy_id,
        create_user="admin",
        operate=operate,
        status=status,
        content=content if content is not None else {"id": strategy_id},
        message=message,
    )
    StrategyHistoryModel.objects.filter(id=history.id).update(create_time=create_time)
    history.create_time = create_time
    return history


class TestCleanStrategyHistoryParams:
    @pytest.mark.parametrize("days", [0, -1, True, 1.5, "1", None])
    def test_rejects_invalid_days(self, days):
        with pytest.raises(ValueError, match="days must be a positive integer"):
            CleanStrategyHistoryParams(days=days)

    @pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, "1", None])
    def test_rejects_invalid_batch_size(self, batch_size):
        with pytest.raises(ValueError, match="batch_size must be a positive integer"):
            CleanStrategyHistoryParams(days=1, batch_size=batch_size)

    @pytest.mark.parametrize("dry_run", [0, 1, "false", None])
    def test_rejects_non_boolean_dry_run(self, dry_run):
        with pytest.raises(ValueError, match="dry_run must be a bool"):
            CleanStrategyHistoryParams(days=1, dry_run=dry_run)

    @pytest.mark.parametrize("strategy_ids", [[], (), {1}, [True], [1.5], ["1"], [None]])
    def test_rejects_invalid_strategy_ids(self, strategy_ids):
        with pytest.raises(ValueError, match="strategy_ids must be a non-empty list of int"):
            CleanStrategyHistoryParams(days=1, strategy_ids=strategy_ids)

    def test_accepts_defaults_and_positive_integer_strategy_ids(self):
        params = CleanStrategyHistoryParams(days=7, strategy_ids=[1, 2])

        assert params.batch_size == 1000
        assert params.dry_run is False

    def test_before_is_frozen_at_init(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        later = now + timedelta(seconds=30)
        current = {"value": now}

        monkeypatch.setattr(
            "bkmonitor.strategy.history.timezone.now",
            lambda: current["value"],
        )

        params = CleanStrategyHistoryParams(days=7)
        expected = now - timedelta(days=7)
        assert params.before == expected

        current["value"] = later
        assert params.before == expected


@pytest.mark.django_db(databases=("default", "monitor_api"))
class TestCollectKeepHistoryIds:
    def test_empty_strategy_ids_do_not_query_database(self, django_assert_num_queries):
        with django_assert_num_queries(0, connection=connections["monitor_api"]):
            assert _collect_keep_history_ids([]) == set()

    def test_existing_strategy_keeps_only_latest_successful_snapshot(self):
        strategy = _create_strategy("existing")
        base_time = timezone.now() - timedelta(days=10)
        old_success = _create_history(strategy.id, base_time, operate="create", status=True)
        latest_success = _create_history(strategy.id, base_time + timedelta(hours=1), status=True)
        _create_history(
            strategy.id,
            base_time + timedelta(hours=2),
            status=False,
            message="update failed",
        )
        # 线上 delete 默认 status=False；已存在策略不保留 delete
        _create_history(strategy.id, base_time + timedelta(hours=3), operate="delete", status=False)

        assert _collect_keep_history_ids([strategy.id]) == {latest_success.id}
        assert old_success.id != latest_success.id

    def test_existing_strategy_with_only_create_keeps_create_snapshot(self):
        strategy = _create_strategy("create-only")
        create_history = _create_history(
            strategy.id,
            timezone.now() - timedelta(days=10),
            operate="create",
            status=True,
        )

        assert _collect_keep_history_ids([strategy.id]) == {create_history.id}

    def test_deleted_strategy_keeps_latest_successful_snapshot_and_delete(self):
        strategy_id = 900001
        base_time = timezone.now() - timedelta(days=10)
        latest_success = _create_history(strategy_id, base_time, operate="update", status=True)
        _create_history(
            strategy_id,
            base_time + timedelta(hours=1),
            operate="update",
            status=False,
            message="update failed",
        )
        # 线上 delete_by_strategy_ids：status 默认 False
        _create_history(strategy_id, base_time, operate="delete", status=False)
        latest_delete = _create_history(strategy_id, base_time + timedelta(hours=2), operate="delete", status=False)

        assert _collect_keep_history_ids([strategy_id]) == {latest_success.id, latest_delete.id}

    def test_empty_content_is_not_a_recoverable_snapshot(self):
        strategy = _create_strategy("empty-content")
        base_time = timezone.now() - timedelta(days=10)
        valid_snapshot = _create_history(strategy.id, base_time, operate="create", status=True)
        _create_history(
            strategy.id,
            base_time + timedelta(hours=1),
            operate="update",
            status=True,
            content={},
        )

        assert _collect_keep_history_ids([strategy.id]) == {valid_snapshot.id}

    def test_legacy_bulk_update_success_with_empty_message_is_kept(self):
        """存量批量更新写入缺陷兼容：成功但未写 status=True（message=""），应作为可恢复快照保留。"""
        strategy = _create_strategy("legacy-bulk-update")
        bulk_success = _create_history(
            strategy.id,
            timezone.now() - timedelta(days=10),
            operate="update",
            status=False,
            message="",
        )

        assert _collect_keep_history_ids([strategy.id]) == {bulk_success.id}

    def test_latest_bulk_update_snapshot_is_kept(self):
        """新写入的 bulk_update(status=True) 应作为可恢复快照保留。"""
        strategy = _create_strategy("bulk-update-keep")
        base_time = timezone.now() - timedelta(days=10)
        _create_history(strategy.id, base_time, operate="update", status=True)
        latest_bulk = _create_history(
            strategy.id,
            base_time + timedelta(hours=1),
            operate="bulk_update",
            status=True,
        )

        assert _collect_keep_history_ids([strategy.id]) == {latest_bulk.id}

    def test_newer_bulk_update_overrides_legacy_empty_message_snapshot(self):
        """更新的 bulk_update 成功快照应覆盖更早的存量 message="" 记录。"""
        strategy = _create_strategy("bulk-overrides-legacy")
        base_time = timezone.now() - timedelta(days=10)
        _create_history(
            strategy.id,
            base_time,
            operate="update",
            status=False,
            message="",
        )
        latest_bulk = _create_history(
            strategy.id,
            base_time + timedelta(hours=1),
            operate="bulk_update",
            status=True,
        )

        assert _collect_keep_history_ids([strategy.id]) == {latest_bulk.id}

    def test_failed_update_with_message_is_not_kept(self):
        strategy = _create_strategy("failed-update")
        _create_history(
            strategy.id,
            timezone.now() - timedelta(days=10),
            operate="update",
            status=False,
            message="update failed",
        )

        assert _collect_keep_history_ids([strategy.id]) == set()

    def test_failed_update_does_not_override_older_recoverable_snapshot(self):
        strategy = _create_strategy("failed-after-success")
        base_time = timezone.now() - timedelta(days=10)
        older_success = _create_history(strategy.id, base_time, operate="update", status=True)
        _create_history(
            strategy.id,
            base_time + timedelta(hours=1),
            operate="update",
            status=False,
            message="update failed",
        )

        assert _collect_keep_history_ids([strategy.id]) == {older_success.id}

    def test_same_create_time_uses_greater_id_as_latest(self):
        strategy = _create_strategy("same-time")
        create_time = timezone.now() - timedelta(days=10)
        first = _create_history(strategy.id, create_time)
        second = _create_history(strategy.id, create_time)

        assert second.id > first.id
        assert _collect_keep_history_ids([strategy.id]) == {second.id}

    def test_mixed_strategy_batch_uses_three_queries(self, django_assert_num_queries):
        existing = _create_strategy("query-count")
        deleted_id = 900002
        create_time = timezone.now() - timedelta(days=10)
        existing_update = _create_history(existing.id, create_time)
        deleted_update = _create_history(deleted_id, create_time, operate="update")
        deleted_delete = _create_history(deleted_id, create_time, operate="delete")

        with django_assert_num_queries(3, connection=connections["monitor_api"]):
            keep_ids = _collect_keep_history_ids([existing.id, deleted_id])

        assert keep_ids == {existing_update.id, deleted_update.id, deleted_delete.id}

    def test_latest_history_query_uses_mysql57_compatible_scalar_subquery(self):
        strategy = _create_strategy("mysql57-compatible")
        create_time = timezone.now() - timedelta(days=10)
        latest_update = _create_history(strategy.id, create_time)

        with CaptureQueriesContext(connections["monitor_api"]) as queries:
            keep_ids = _collect_keep_history_ids([strategy.id])

        sql = " ".join(query["sql"] for query in queries.captured_queries).upper()
        assert keep_ids == {latest_update.id}
        assert "ROW_NUMBER" not in sql
        assert " OVER " not in sql
        assert "LIMIT 1" in sql


@pytest.mark.django_db(databases=("default", "monitor_api"))
class TestIterCandidateStrategyIdChunks:
    def test_includes_zero_strategy_id_from_failed_create(self):
        before = timezone.now() - timedelta(days=7)
        _create_history(0, before - timedelta(seconds=1), operate="create", status=False)

        assert list(_iter_candidate_strategy_id_chunks(before)) == [[0]]

    def test_returns_sorted_distinct_old_strategy_ids_in_chunks(self):
        before = timezone.now() - timedelta(days=7)
        old_time = before - timedelta(seconds=1)
        recent_time = before + timedelta(seconds=1)
        _create_history(103, old_time)
        _create_history(101, old_time)
        _create_history(101, old_time, operate="delete")
        _create_history(102, old_time)
        _create_history(104, recent_time)

        chunks = list(_iter_candidate_strategy_id_chunks(before, chunk_size=2))

        assert chunks == [[101, 102], [103]]

    def test_applies_strategy_id_filter(self):
        before = timezone.now() - timedelta(days=7)
        old_time = before - timedelta(seconds=1)
        for strategy_id in (201, 202, 203):
            _create_history(strategy_id, old_time)

        chunks = list(_iter_candidate_strategy_id_chunks(before, strategy_ids=[203, 201], chunk_size=1))

        assert chunks == [[201], [203]]

    def test_returns_no_chunks_when_all_histories_are_on_or_after_cutoff(self):
        before = timezone.now() - timedelta(days=7)
        _create_history(301, before)
        _create_history(302, before + timedelta(seconds=1))

        assert list(_iter_candidate_strategy_id_chunks(before)) == []


@pytest.mark.django_db(databases=("default", "monitor_api"))
class TestDeleteQuerysetInBatches:
    def test_deletes_all_matching_rows_with_partial_last_batch(self):
        create_time = timezone.now() - timedelta(days=10)
        delete_ids = [_create_history(401, create_time).id for _ in range(5)]
        retained = _create_history(402, create_time)
        queryset = StrategyHistoryModel.objects.filter(id__in=delete_ids)

        deleted = _delete_queryset_in_batches(queryset, batch_size=2)

        assert deleted == 5
        assert not StrategyHistoryModel.objects.filter(id__in=delete_ids).exists()
        assert StrategyHistoryModel.objects.filter(id=retained.id).exists()


@pytest.mark.django_db(databases=("default", "monitor_api"))
class TestCleanStrategyHistory:
    def test_uses_fixed_cutoff_during_entire_cleanup(self, monkeypatch):
        start_time = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        now_values = iter((start_time, start_time + timedelta(days=2)))
        now_calls = []

        def moving_now():
            now_calls.append(1)
            return next(now_values)

        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", moving_now)
        strategy = _create_strategy("fixed-cutoff")
        _create_history(strategy.id, start_time - timedelta(days=31), status=True)
        not_expired_at_start = _create_history(
            strategy.id,
            start_time - timedelta(days=29),
            status=False,
            message="update failed",
        )

        deleted = clean_strategy_history(CleanStrategyHistoryParams(days=30))

        assert deleted == 0
        assert StrategyHistoryModel.objects.filter(id=not_expired_at_start.id).exists()
        assert len(now_calls) == 1

    def test_cleans_old_histories_and_preserves_records_required_by_each_strategy_state(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", lambda: now)
        old_time = now - timedelta(days=31)
        recent_time = now - timedelta(days=1)

        existing = _create_strategy("integration-existing")
        _create_history(existing.id, old_time - timedelta(hours=3), operate="update", status=True)
        kept_existing = _create_history(existing.id, old_time - timedelta(hours=2), operate="update", status=True)
        _create_history(
            existing.id,
            old_time - timedelta(hours=1),
            operate="update",
            status=False,
            message="update failed",
        )
        _create_history(existing.id, old_time, operate="delete", status=False)
        recent_existing = _create_history(
            existing.id,
            recent_time,
            operate="update",
            status=False,
            message="update failed",
        )

        deleted_id = 900003
        kept_deleted_update = _create_history(deleted_id, old_time - timedelta(hours=4), operate="update", status=True)
        _create_history(
            deleted_id,
            old_time - timedelta(hours=3),
            operate="update",
            status=False,
            message="update failed",
        )
        _create_history(deleted_id, old_time - timedelta(hours=2), operate="delete", status=False)
        kept_deleted_delete = _create_history(deleted_id, old_time - timedelta(hours=1), operate="delete", status=False)
        _create_history(
            deleted_id,
            old_time,
            operate="create",
            status=False,
            message="create failed",
        )
        recent_deleted = _create_history(
            deleted_id,
            recent_time,
            operate="create",
            status=False,
            message="create failed",
        )

        deleted = clean_strategy_history(CleanStrategyHistoryParams(days=30, batch_size=2))

        assert deleted == 6
        assert set(StrategyHistoryModel.objects.values_list("id", flat=True)) == {
            kept_existing.id,
            kept_deleted_update.id,
            kept_deleted_delete.id,
            recent_existing.id,
            recent_deleted.id,
        }

    def test_latest_record_outside_cleanup_window_still_controls_which_old_record_is_kept(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", lambda: now)
        strategy = _create_strategy("recent-latest")
        old_update = _create_history(strategy.id, now - timedelta(days=31), operate="update", status=True)
        recent_update = _create_history(strategy.id, now - timedelta(days=1), operate="update", status=True)

        deleted = clean_strategy_history(CleanStrategyHistoryParams(days=30))

        assert deleted == 1
        assert not StrategyHistoryModel.objects.filter(id=old_update.id).exists()
        assert StrategyHistoryModel.objects.filter(id=recent_update.id).exists()

    def test_dry_run_returns_count_without_deleting(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", lambda: now)
        strategy = _create_strategy("dry-run")
        old_ids = [
            _create_history(strategy.id, now - timedelta(days=31, hours=hour), status=True).id for hour in (3, 2, 1)
        ]

        would_delete = clean_strategy_history(CleanStrategyHistoryParams(days=30, dry_run=True))

        assert would_delete == 2
        assert set(StrategyHistoryModel.objects.filter(id__in=old_ids).values_list("id", flat=True)) == set(old_ids)

    def test_strategy_ids_limit_cleanup_scope(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", lambda: now)
        selected = _create_strategy("selected")
        unselected = _create_strategy("unselected")
        old_time = now - timedelta(days=31)
        selected_old = _create_history(selected.id, old_time - timedelta(hours=1))
        selected_latest = _create_history(selected.id, old_time)
        unselected_old = _create_history(unselected.id, old_time - timedelta(hours=1))
        unselected_latest = _create_history(unselected.id, old_time)

        deleted = clean_strategy_history(CleanStrategyHistoryParams(days=30, strategy_ids=[selected.id], batch_size=1))

        assert deleted == 1
        assert not StrategyHistoryModel.objects.filter(id=selected_old.id).exists()
        assert StrategyHistoryModel.objects.filter(id=selected_latest.id).exists()
        assert StrategyHistoryModel.objects.filter(id__in=[unselected_old.id, unselected_latest.id]).count() == 2

    def test_returns_zero_when_there_are_no_old_histories(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", lambda: now)
        _create_history(900004, now - timedelta(days=1))

        assert clean_strategy_history(CleanStrategyHistoryParams(days=30)) == 0

    def test_full_cleanup_covers_bulk_update_legacy_and_deleted_strategies(self, monkeypatch):
        """
        综合场景：同时覆盖
        - bulk_update(status=True) 作为可恢复快照
        - 存量 message="" 批量成功兼容
        - 已删除策略保留最新快照 + 最新 delete
        - 窗口外最新可恢复快照决定窗口内旧记录是否可删
        - strategy_ids 未限定时多策略一并清理
        """
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", lambda: now)
        old_time = now - timedelta(days=31)
        recent_time = now - timedelta(days=1)

        # 已存在策略：全局最新可恢复在窗口内(recent bulk_update)，窗口内旧快照全部可删
        existing = _create_strategy("full-existing")
        old_existing_update = _create_history(existing.id, old_time - timedelta(hours=3), operate="update", status=True)
        old_existing_bulk = _create_history(
            existing.id,
            old_time - timedelta(hours=2),
            operate="bulk_update",
            status=True,
        )
        old_existing_fail = _create_history(
            existing.id,
            old_time - timedelta(hours=1),
            operate="update",
            status=False,
            message="update failed",
        )
        recent_existing = _create_history(
            existing.id,
            recent_time,
            operate="bulk_update",
            status=True,
        )

        # 仅有存量批量缺陷记录的策略：message="" 应保留，失败记录可删
        legacy_only = _create_strategy("full-legacy")
        kept_legacy = _create_history(
            legacy_only.id,
            old_time - timedelta(hours=1),
            operate="update",
            status=False,
            message="",
        )
        old_legacy_fail = _create_history(
            legacy_only.id,
            old_time,
            operate="update",
            status=False,
            message="update failed",
        )

        # 已删除策略：保留最新 bulk_update 快照 + 最新 delete
        deleted_id = 900005
        old_deleted_update = _create_history(deleted_id, old_time - timedelta(hours=4), operate="update", status=True)
        old_deleted_legacy = _create_history(
            deleted_id,
            old_time - timedelta(hours=3),
            operate="update",
            status=False,
            message="",
        )
        kept_deleted_snapshot = _create_history(
            deleted_id,
            old_time - timedelta(hours=2),
            operate="bulk_update",
            status=True,
        )
        old_deleted_delete = _create_history(deleted_id, old_time - timedelta(hours=1), operate="delete", status=False)
        kept_deleted_delete = _create_history(
            deleted_id,
            old_time,
            operate="delete",
            status=False,
        )
        recent_deleted_fail = _create_history(
            deleted_id,
            recent_time,
            operate="update",
            status=False,
            message="update failed",
        )

        deleted = clean_strategy_history(CleanStrategyHistoryParams(days=30, batch_size=2))

        # existing 删 3 + legacy 删 1 + deleted 删 3
        assert deleted == 7
        assert set(StrategyHistoryModel.objects.values_list("id", flat=True)) == {
            recent_existing.id,
            kept_legacy.id,
            kept_deleted_snapshot.id,
            kept_deleted_delete.id,
            recent_deleted_fail.id,
        }
        assert not StrategyHistoryModel.objects.filter(
            id__in=[
                old_existing_update.id,
                old_existing_bulk.id,
                old_existing_fail.id,
                old_legacy_fail.id,
                old_deleted_update.id,
                old_deleted_legacy.id,
                old_deleted_delete.id,
            ]
        ).exists()
