"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2025 Tencent. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

from copy import deepcopy
from unittest import mock

import pytest

from bkmonitor.as_code.parse import sync_grafana_dashboards


def _make_dashboard(title: str, with_input: bool = False) -> dict:
    dashboard = {"title": title, "id": 1, "panels": []}
    if with_input:
        dashboard["__inputs"] = [
            {
                "name": "DS_BKMONITOR",
                "type": "datasource",
                "pluginId": "bkmonitor-timeseries-datasource",
            }
        ]
    return dashboard


@pytest.fixture
def patch_grafana_apis():
    """Mock sync_grafana_dashboards 依赖的 Grafana / org API。"""
    with (
        mock.patch("bkmonitor.as_code.parse.get_or_create_org", return_value={"id": 1}) as get_org,
        mock.patch("bkmonitor.as_code.parse.api.grafana.search_folder_or_dashboard") as search_folder,
        mock.patch("bkmonitor.as_code.parse.api.grafana.get_all_data_source") as get_ds,
        mock.patch("bkmonitor.as_code.parse.api.grafana.create_folder") as create_folder,
        mock.patch("bkmonitor.as_code.parse.api.grafana.import_dashboard") as import_dashboard,
    ):
        search_folder.return_value = {"data": [{"title": "existed", "id": 10}]}
        get_ds.return_value = {
            "data": [
                {"type": "bkmonitor-timeseries-datasource", "uid": "uid-ts"},
                {"type": "prometheus", "uid": "uid-prom"},
            ]
        }
        create_folder.return_value = {"result": True, "data": {"id": 20}, "message": ""}
        import_dashboard.return_value = {"result": True, "data": {}}
        yield {
            "get_org": get_org,
            "search_folder": search_folder,
            "get_ds": get_ds,
            "create_folder": create_folder,
            "import_dashboard": import_dashboard,
        }


def test_sync_grafana_dashboards_empty_skips_api(patch_grafana_apis):
    sync_grafana_dashboards(bk_biz_id=2, dashboards={})

    patch_grafana_apis["get_org"].assert_not_called()
    patch_grafana_apis["import_dashboard"].assert_not_called()


def test_sync_grafana_dashboards_only_datasources_yaml_skips_import(patch_grafana_apis):
    sync_grafana_dashboards(
        bk_biz_id=2,
        dashboards={"datasources.yaml": {"DS_BKMONITOR": "uid-ts"}},
    )

    patch_grafana_apis["get_org"].assert_called_once()
    patch_grafana_apis["import_dashboard"].assert_not_called()
    patch_grafana_apis["create_folder"].assert_not_called()


def test_sync_grafana_dashboards_imports_all_concurrently(patch_grafana_apis):
    dashboards = {
        "a.json": _make_dashboard("dash-a"),
        "folder/b.json": _make_dashboard("dash-b"),
        "folder/c.json": _make_dashboard("dash-c"),
    }

    sync_grafana_dashboards(bk_biz_id=2, dashboards=deepcopy(dashboards))

    assert patch_grafana_apis["import_dashboard"].call_count == 3
    folder_ids = {
        call.kwargs["folderId"] for call in patch_grafana_apis["import_dashboard"].call_args_list
    }
    assert 0 in folder_ids  # root
    assert 20 in folder_ids  # newly created folder
    # 同一 folder 只创建一次
    patch_grafana_apis["create_folder"].assert_called_once_with(org_id=1, title="folder")


def test_sync_grafana_dashboards_reuses_existing_folder(patch_grafana_apis):
    sync_grafana_dashboards(
        bk_biz_id=2,
        dashboards={"existed/host.json": _make_dashboard("host")},
    )

    patch_grafana_apis["create_folder"].assert_not_called()
    patch_grafana_apis["import_dashboard"].assert_called_once()
    assert patch_grafana_apis["import_dashboard"].call_args.kwargs["folderId"] == 10


def test_sync_grafana_dashboards_resolves_datasource_inputs(patch_grafana_apis):
    sync_grafana_dashboards(
        bk_biz_id=2,
        dashboards={
            "datasources.yaml": {"DS_BKMONITOR": "uid-ts"},
            "host.json": _make_dashboard("host", with_input=True),
        },
    )

    call_kwargs = patch_grafana_apis["import_dashboard"].call_args.kwargs
    assert call_kwargs["inputs"] == [
        {
            "name": "DS_BKMONITOR",
            "type": "datasource",
            "pluginId": "bkmonitor-timeseries-datasource",
            "value": "uid-ts",
        }
    ]
    assert "id" not in call_kwargs["dashboard"]


def test_sync_grafana_dashboards_aggregates_import_errors(patch_grafana_apis):
    def _import_side_effect(**kwargs):
        title = kwargs["dashboard"]["title"]
        if title == "bad":
            raise RuntimeError("boom")
        return {"result": True, "data": {}}

    patch_grafana_apis["import_dashboard"].side_effect = _import_side_effect

    with pytest.raises(ValueError, match=r"folder/bad\.json: boom") as exc_info:
        sync_grafana_dashboards(
            bk_biz_id=2,
            dashboards={
                "ok.json": _make_dashboard("ok"),
                "folder/bad.json": _make_dashboard("bad"),
            },
        )

    assert "ok.json" not in str(exc_info.value)
    assert patch_grafana_apis["import_dashboard"].call_count == 2


def test_sync_grafana_dashboards_unknown_datasource_uid(patch_grafana_apis):
    with pytest.raises(ValueError, match=r"datasource\(missing-uid\) is not exist"):
        sync_grafana_dashboards(
            bk_biz_id=2,
            dashboards={
                "datasources.yaml": {"DS_BKMONITOR": "missing-uid"},
                "host.json": _make_dashboard("host"),
            },
        )

    patch_grafana_apis["import_dashboard"].assert_not_called()
