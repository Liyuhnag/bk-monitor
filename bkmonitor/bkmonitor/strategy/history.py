# -*- coding: utf-8 -*-
"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2025 Tencent. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from bkmonitor.models import StrategyHistoryModel, StrategyModel

STRATEGY_ID_CHUNK_SIZE = 500


@dataclass
class CleanStrategyHistoryParams:
    """策略变更历史清理参数"""

    days: int
    strategy_ids: list[int] | None = None
    batch_size: int = 1000
    dry_run: bool = False
    # 创建时冻结，保证日志打印的截止时间与实际删除过滤一致
    before: datetime = field(init=False)

    def __post_init__(self):
        if not isinstance(self.days, int) or isinstance(self.days, bool) or self.days <= 0:
            raise ValueError("days must be a positive integer")
        if not isinstance(self.batch_size, int) or isinstance(self.batch_size, bool) or self.batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(self.dry_run, bool):
            raise ValueError("dry_run must be a bool")
        if self.strategy_ids is not None:
            if not isinstance(self.strategy_ids, list) or not self.strategy_ids:
                raise ValueError("strategy_ids must be a non-empty list of int")
            for strategy_id in self.strategy_ids:
                if not isinstance(strategy_id, int) or isinstance(strategy_id, bool):
                    raise ValueError("strategy_ids must be a non-empty list of int")
        self.before = timezone.now() - timedelta(days=self.days)


def _collect_latest_history_ids(
    queryset: QuerySet,
    partition_by: tuple[str, ...],
    order_by: tuple[str, ...] = ("-create_time", "-id"),
) -> set[int]:
    """使用相关子查询批量获取每个分组中指定排序下的第一条历史 ID。"""
    group_filters = {field: OuterRef(field) for field in partition_by}
    latest_id = (
        queryset.order_by()
        .filter(**group_filters)
        .order_by(*order_by)
        .values("id")[:1]
    )
    return set(queryset.order_by().filter(id=Subquery(latest_id)).values_list("id", flat=True))


def _collect_keep_history_ids(strategy_ids: list[int]) -> set[int]:
    """
    计算需要保留的历史记录 ID。

    - 所有策略：保留最新一条可恢复的 create/update 快照（content 非空，且 status=True 或 message=""）
      message="" 用于兼容存量批量更新成功记录（历史上未写 status=True）
    - 策略不存在：额外保留最新一条 delete（线上 delete 默认 status=False，故不按 status 过滤）
    """
    if not strategy_ids:
        return set()
    # 用来区分策略是否存在
    existing_ids = set(StrategyModel.objects.filter(id__in=strategy_ids).values_list("id", flat=True))
    deleted_ids = set(strategy_ids) - existing_ids

    recoverable_snapshots = (
        StrategyHistoryModel.objects.filter(
            strategy_id__in=strategy_ids,
            operate__in=("create", "update", "bulk_update"),
        )
        .filter(Q(status=True) | Q(message=""))
        .exclude(content={})
        .exclude(content__isnull=True)
    )
    keep_ids = _collect_latest_history_ids(
        recoverable_snapshots,
        partition_by=("strategy_id",),
    )
    keep_ids.update(
        _collect_latest_history_ids(
            StrategyHistoryModel.objects.filter(
                strategy_id__in=deleted_ids,
                operate="delete",
            ),
            partition_by=("strategy_id",),
        )
    )
    return keep_ids


def _iter_candidate_strategy_id_chunks(
    before: datetime,
    strategy_ids: list[int] | None = None,
    chunk_size: int = STRATEGY_ID_CHUNK_SIZE,
) -> Iterator[list[int]]:
    """分页产出截止时间前有历史的 strategy_id，避免一次 distinct 全量进内存。"""
    # 新建策略失败时，历史记录可能保留 strategy_id=0，因此初始游标需要从 -1 开始。
    last_strategy_id = -1
    while True:
        queryset = StrategyHistoryModel.objects.filter(create_time__lt=before, strategy_id__gt=last_strategy_id)
        if strategy_ids is not None:
            queryset = queryset.filter(strategy_id__in=strategy_ids)
        chunk = list(queryset.order_by("strategy_id").values_list("strategy_id", flat=True).distinct()[:chunk_size])
        if not chunk:
            break
        yield chunk
        last_strategy_id = chunk[-1]


def _delete_queryset_in_batches(queryset: QuerySet, batch_size: int) -> int:
    deleted = 0
    while True:
        history_ids = list(queryset.values_list("id", flat=True)[:batch_size])
        if not history_ids:
            break
        deleted_count, _ = StrategyHistoryModel.objects.filter(id__in=history_ids).delete()
        deleted += deleted_count
    return deleted


def clean_strategy_history(params: CleanStrategyHistoryParams) -> int:
    """
    清理指定天数之前的策略变更历史。

    先按截止时间圈定可清理范围，再保留可恢复快照：
    - 所有策略：保留最新一条可恢复的 create/update 快照（status=True 或 message=""）
    - 策略不存在：额外保留最新一条 delete
    其余可清理范围内的记录删除。

    :param params: 清理参数
    :return: 删除数量；dry_run 时返回将删除数量
    """
    before = params.before
    deleted = 0
    for strategy_id_chunk in _iter_candidate_strategy_id_chunks(before, params.strategy_ids):
        keep_ids = _collect_keep_history_ids(strategy_id_chunk)
        queryset = StrategyHistoryModel.objects.filter(
            create_time__lt=before,
            strategy_id__in=strategy_id_chunk,
        )
        if keep_ids:
            queryset = queryset.exclude(id__in=keep_ids)

        if params.dry_run:
            deleted += queryset.count()
            continue
        deleted += _delete_queryset_in_batches(queryset, params.batch_size)
    return deleted
