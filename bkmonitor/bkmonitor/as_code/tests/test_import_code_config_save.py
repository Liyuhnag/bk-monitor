"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2025 Tencent. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

from unittest import mock

from django.utils import timezone

from bkmonitor.as_code.parse import import_code_config
from bkmonitor.models import ActionPlugin


def _empty_qs():
    qs = mock.MagicMock()
    qs.only.return_value = []
    qs.filter.return_value = qs
    qs.exclude.return_value = qs
    qs.values_list.return_value = []
    qs.__iter__ = mock.Mock(return_value=iter([]))
    return qs


class _FakeNoticeModel:
    objects = mock.MagicMock()

    def __init__(self, pk: int):
        self.pk = pk
        self.save = mock.MagicMock()


class _FakeActionModel:
    objects = mock.MagicMock()

    def __init__(self, pk: int):
        self.pk = pk
        self.save = mock.MagicMock()


def test_import_code_config_updates_as_code_metadata_after_serializer_save():
    """Serializer.save() 后应通过 update 补写 path/app/hash/snippet，而不是 instance.save()。"""
    _FakeNoticeModel.objects.reset_mock()
    _FakeActionModel.objects.reset_mock()

    notice_instance = _FakeNoticeModel(11)
    action_instance = _FakeActionModel(22)

    notice_slz = mock.MagicMock()
    notice_slz.save.return_value = notice_instance
    action_slz = mock.MagicMock()
    action_slz.save.return_value = action_instance

    notice_records = [
        {
            "path": "ops.yaml",
            "obj": notice_slz,
            "hash": "notice-hash",
            "snippet": "notice-snippet",
            "schema_error": None,
            "parse_error": None,
            "validate_error": None,
        }
    ]
    action_records = [
        {
            "path": "job.yaml",
            "obj": action_slz,
            "hash": "action-hash",
            "snippet": "action-snippet",
            "schema_error": None,
            "parse_error": None,
            "validate_error": None,
        }
    ]

    now = timezone.now()
    with (
        mock.patch("bkmonitor.as_code.parse.convert_duty_rules", return_value=[]),
        mock.patch("bkmonitor.as_code.parse.convert_notices", return_value=notice_records),
        mock.patch("bkmonitor.as_code.parse.convert_actions", return_value=action_records),
        mock.patch("bkmonitor.as_code.parse.convert_rules", return_value=[]),
        mock.patch("bkmonitor.as_code.parse.convert_assign_groups", return_value=[]),
        mock.patch("bkmonitor.as_code.parse.DutyRule.objects") as duty_objects,
        mock.patch("bkmonitor.as_code.parse.UserGroup.objects", _empty_qs()),
        mock.patch("bkmonitor.as_code.parse.ActionConfig.objects", _empty_qs()),
        mock.patch("bkmonitor.as_code.parse.ActionPlugin.objects") as plugin_objects,
        mock.patch("bkmonitor.as_code.parse.StrategyModel.objects", _empty_qs()),
        mock.patch("bkmonitor.as_code.parse.AlertAssignGroup.objects", _empty_qs()),
        mock.patch("bkmonitor.as_code.parse.timezone.now", return_value=now),
    ):
        duty_objects.filter.return_value.only.return_value = []
        plugin_objects.get.side_effect = ActionPlugin.DoesNotExist

        result = import_code_config(
            bk_biz_id=2,
            app="app1",
            configs={
                "notice/ops.yaml": "name: ops\n",
                "action/job.yaml": "name: job\n",
            },
            incremental=True,
        )

    assert result is None
    notice_slz.save.assert_called_once_with()
    action_slz.save.assert_called_once_with()
    _FakeNoticeModel.objects.filter.assert_called_once_with(id=11)
    _FakeActionModel.objects.filter.assert_called_once_with(id=22)
    _FakeNoticeModel.objects.filter.return_value.update.assert_called_once_with(
        path="ops.yaml",
        app="app1",
        hash="notice-hash",
        snippet="notice-snippet",
        update_time=now,
    )
    _FakeActionModel.objects.filter.return_value.update.assert_called_once_with(
        path="job.yaml",
        app="app1",
        hash="action-hash",
        snippet="action-snippet",
        update_time=now,
    )
    notice_instance.save.assert_not_called()
    action_instance.save.assert_not_called()
