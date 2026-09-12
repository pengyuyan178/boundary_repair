"""原始数据只在此处解码；显式 allowlist 投影后才传入生成模块。"""
import json
from pathlib import Path
from typing import cast

from boundary_repair.domain.errors import DatasetFormatError
from boundary_repair.domain.task import IssueAsset, TaskInput


def project_task(record: dict[str, object]) -> TaskInput:
    """原始行 → TaskInput；绝不透传 raw record、hints、patch、test_patch 或评分字段。

    支持 image_assets 为对象或 JSON 字符串；只取 problem_statement URL 列表。
    缺失附件字段表示没有结构化引用，算法仍可分析原 issue 文本；未知附件形状报错。
    此函数仅做数据隔离，不下载图片、不执行图片内容、不读取 patch 图片。
    """
    required = ("instance_id", "repo", "base_commit", "problem_statement")
    if any(not isinstance(record.get(k), str) or not str(record[k]).strip() for k in required):
        raise DatasetFormatError("任务缺少 instance_id/repo/base_commit/problem_statement 文本")
    assets = record.get("image_assets", {})
    if isinstance(assets, str):
        try:
            assets = json.loads(assets)
        except json.JSONDecodeError as exc:
            raise DatasetFormatError("image_assets JSON 无法解码") from exc
    if assets is None:
        assets = {}
    if not isinstance(assets, dict):
        raise DatasetFormatError("image_assets 必须是对象或对象的 JSON 字符串")
    urls = assets.get("problem_statement", [])
    if not isinstance(urls, list) or not all(isinstance(u, str) and u for u in urls):
        raise DatasetFormatError("image_assets.problem_statement 必须是非空 URL 字符串列表")
    unique = tuple(dict.fromkeys(urls))
    return TaskInput(
        instance_id=cast(str, record["instance_id"]), repo=cast(str, record["repo"]),
        base_commit=cast(str, record["base_commit"]),
        problem_statement=cast(str, record["problem_statement"]),
        assets=tuple(
            IssueAsset(uri=url, source_id=f"issue-image-{i}") for i, url in enumerate(unique)
        ),
    )


def load_tasks(path: Path) -> tuple[TaskInput, ...]:
    """JSON 数组、instance_id 映射或 JSONL → 去重校验后的不可变任务序列。

    映射键必须和行内 instance_id 一致；未知 wrapper 不猜测。JSONL 按扩展名识别，
    不因解析失败就换一种格式。原始行仅活在此边界内；这是 API 隔离，不是进程沙箱。
    """
    try:
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
                    if line.strip()]
        else:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, list):
                rows = raw
            elif isinstance(raw, dict):
                rows = []
                for key, value in raw.items():
                    if not isinstance(value, dict) or value.get("instance_id") != key:
                        raise DatasetFormatError("任务映射键与 instance_id 不匹配，或是未知 wrapper")
                    rows.append(value)
            else:
                raise DatasetFormatError("数据顶层须为任务数组或 instance_id 映射")
    except json.JSONDecodeError as exc:
        raise DatasetFormatError("数据不是合法 JSON/JSONL") from exc
    tasks: list[TaskInput] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DatasetFormatError("数据行必须是对象")
        task = project_task(row)
        if task.instance_id in seen:
            raise DatasetFormatError("存在重复 instance_id")
        seen.add(task.instance_id)
        tasks.append(task)
    return tuple(tasks)


def load_generation_tasks(path: Path) -> tuple[TaskInput, ...]:
    """Load an answer-free task list with exact task and issue-asset field allowlists."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise DatasetFormatError("generation_tasks_must_be_nonempty_list")
    tasks = []
    seen = set()
    fields = {"instance_id", "repo", "base_commit", "problem_statement", "assets"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise DatasetFormatError("generation_task_fields_not_allowlisted")
        if any(not isinstance(row[k], str) or not row[k].strip() for k in fields - {"assets"}):
            raise DatasetFormatError("invalid_generation_task_identity")
        if not isinstance(row["assets"], list):
            raise DatasetFormatError("generation_assets_must_be_list")
        assets = []
        for asset in row["assets"]:
            if not isinstance(asset, dict) or set(asset) != {"uri", "source_id", "media_type"}:
                raise DatasetFormatError("generation_asset_fields_not_allowlisted")
            if any(not isinstance(asset[k], str) or not asset[k] for k in ("uri", "source_id")):
                raise DatasetFormatError("invalid_generation_issue_asset")
            if asset["media_type"] is not None and not isinstance(asset["media_type"], str):
                raise DatasetFormatError("invalid_generation_asset_media_type")
            assets.append(IssueAsset(**asset))
        if row["instance_id"] in seen:
            raise DatasetFormatError("duplicate_generation_task")
        seen.add(row["instance_id"])
        tasks.append(TaskInput(**{k: row[k] for k in fields - {"assets"}}, assets=tuple(assets)))
    return tuple(tasks)


def select_tasks(
    tasks: tuple[TaskInput, ...], *, repo: str | None = None,
    instance_ids: tuple[str, ...] = (), limit: int | None = None,
) -> tuple[TaskInput, ...]:
    """稳定筛选，不抽样调参；显式实例不存在或与 repo 筛选冲突时立即报错。"""
    available = {task.instance_id for task in tasks if repo is None or task.repo == repo}
    if set(instance_ids) - available:
        raise DatasetFormatError("指定实例不存在，或与 repo 筛选条件冲突")
    selected = tuple(task for task in tasks if task.instance_id in available
                     and (not instance_ids or task.instance_id in instance_ids))
    if limit is not None and limit <= 0:
        raise DatasetFormatError("limit 必须为正整数")
    return selected if limit is None else selected[:limit]
