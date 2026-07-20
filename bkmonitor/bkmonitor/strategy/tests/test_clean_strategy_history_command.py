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

from datetime import datetime, timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from bkmonitor.models import StrategyHistoryModel, StrategyModel

CMD = "clean_strategy_history"


class TestCleanStrategyHistoryCommandArgs:
    def test_requires_days(self):
        with pytest.raises(CommandError):
            call_command(CMD)

    @pytest.mark.parametrize("days", [0, -1])
    def test_rejects_non_positive_days(self, days):
        with pytest.raises(CommandError, match="days must be a positive integer"):
            call_command(CMD, days=days)

    @pytest.mark.parametrize("batch_size", [0, -1])
    def test_rejects_non_positive_batch_size(self, batch_size):
        with pytest.raises(CommandError, match="batch_size must be a positive integer"):
            call_command(CMD, days=1, batch_size=batch_size)

    @pytest.mark.parametrize("strategy_ids", ["abc", "1,x,2", ","])
    def test_rejects_invalid_strategy_ids(self, strategy_ids):
        with pytest.raises(CommandError, match="--strategy_ids must be comma-separated integers"):
            call_command(CMD, days=1, strategy_ids=strategy_ids)

    def test_passes_parsed_options_to_clean_strategy_history(self):
        stdout = StringIO()
        with mock.patch(
            "bkmonitor.management.commands.clean_strategy_history.clean_strategy_history",
            return_value=3,
        ) as clean_mock:
            call_command(
                CMD,
                days=30,
                strategy_ids="1, 2,3",
                batch_size=100,
                dry_run=True,
                stdout=stdout,
            )

        params = clean_mock.call_args.args[0]
        assert params.days == 30
        assert params.strategy_ids == [1, 2, 3]
        assert params.batch_size == 100
        assert params.dry_run is True
        output = stdout.getvalue()
        assert "dry_run=True" in output
        assert "would delete 3 records" in output

    def test_empty_strategy_ids_means_all_strategies(self):
        with mock.patch(
            "bkmonitor.management.commands.clean_strategy_history.clean_strategy_history",
            return_value=0,
        ) as clean_mock:
            call_command(CMD, days=7, strategy_ids="")

        params = clean_mock.call_args.args[0]
        assert params.strategy_ids is None
        assert params.dry_run is False

    def test_reports_deleted_count_when_not_dry_run(self):
        stdout = StringIO()
        with mock.patch(
            "bkmonitor.management.commands.clean_strategy_history.clean_strategy_history",
            return_value=5,
        ):
            call_command(CMD, days=7, stdout=stdout)

        assert "deleted 5 records" in stdout.getvalue()
        assert "would delete" not in stdout.getvalue()


@pytest.mark.django_db(databases=("default", "monitor_api"))
class TestCleanStrategyHistoryCommandIntegration:
    def test_dry_run_does_not_delete_records(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 7, 19, 12, 0, 0))
        monkeypatch.setattr("bkmonitor.strategy.history.timezone.now", lambda: now)

        strategy = StrategyModel.objects.create(
            bk_biz_id=2,
            name="cmd-dry-run",
            scenario="os",
            type=StrategyModel.StrategyType.Monitor,
        )
        old_time = now - timedelta(days=31)
        old_ids = []
        for hour in (3, 2, 1):
            history = StrategyHistoryModel.objects.create(
                strategy_id=strategy.id,
                create_user="admin",
                operate="update",
                status=True,
                content={"id": strategy.id},
            )
            StrategyHistoryModel.objects.filter(id=history.id).update(create_time=old_time - timedelta(hours=hour))
            old_ids.append(history.id)

        stdout = StringIO()
        call_command(CMD, days=30, dry_run=True, stdout=stdout)

        assert "would delete 2 records" in stdout.getvalue()
        assert set(StrategyHistoryModel.objects.filter(id__in=old_ids).values_list("id", flat=True)) == set(old_ids)
