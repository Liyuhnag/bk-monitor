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

from django.core.management.base import BaseCommand, CommandError

from bkmonitor.strategy.history import CleanStrategyHistoryParams, clean_strategy_history


class Command(BaseCommand):
    help = (
        "清理指定天数之前的策略变更历史。"
        "保留最新一条成功的 create/update 快照；策略不存在时额外保留最新一条 delete。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            required=True,
            help="保留最近 N 天的历史，删除更早且不在保留规则内的记录",
        )
        parser.add_argument(
            "--strategy_ids",
            type=str,
            default="",
            help="仅清理指定策略，半角逗号分隔；为空则清理全部",
        )
        parser.add_argument(
            "--batch_size",
            type=int,
            default=1000,
            help="分批删除大小，默认 1000",
        )
        parser.add_argument(
            "--dry_run",
            action="store_true",
            help="仅统计将删除数量，不实际删除",
        )

    def handle(self, *args, **options):
        strategy_ids = self._parse_strategy_ids(options["strategy_ids"])
        try:
            params = CleanStrategyHistoryParams(
                days=options["days"],
                strategy_ids=strategy_ids,
                batch_size=options["batch_size"],
                dry_run=options["dry_run"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"clean strategy history start: days={params.days}, before={params.before}, "
            f"strategy_ids={params.strategy_ids}, batch_size={params.batch_size}, dry_run={params.dry_run}"
        )
        deleted = clean_strategy_history(params)
        action = "would delete" if params.dry_run else "deleted"
        self.stdout.write(self.style.SUCCESS(f"clean strategy history done: {action} {deleted} records"))

    @staticmethod
    def _parse_strategy_ids(raw: str) -> list[int] | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            strategy_ids = [int(item.strip()) for item in raw.split(",") if item.strip()]
        except ValueError as exc:
            raise CommandError("--strategy_ids must be comma-separated integers") from exc
        if not strategy_ids:
            raise CommandError("--strategy_ids must be comma-separated integers")
        return strategy_ids
