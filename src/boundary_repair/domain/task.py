"""生成侧允许读取的数据。TaskInput 故意没有 gold/test patch 或评分字段。"""
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SourceKind(StrEnum):
    """证据来源；图片观察、issue 要求和修复前代码事实不能互相冒充。"""
    ISSUE_TEXT = "issue_text"
    ISSUE_IMAGE = "issue_image"
    BASE_CODE = "base_code"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """可审计来源；locator 是原文片段/图像锚点/源码定位，不是推理结论。"""
    source_id: str
    kind: SourceKind
    locator: str


@dataclass(frozen=True, slots=True)
class IssueAsset:
    """原 issue 附件引用；禁止混入 image_assets.test_patch 或修复后参考图。"""
    uri: str
    source_id: str
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class TaskInput:
    """单任务不可变输入；repo 是项目名，不是 baseline 仓库路径。"""
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    assets: tuple[IssueAsset, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """源码范围；path 为仓库相对 POSIX 路径，行号从 1 开始且包含 end_line。"""
    path: str
    start_line: int
    end_line: int
    content_sha256: str
    start_byte: int | None = None
    end_byte: int | None = None
    node_kind: str = ""
    symbol: str = ""
    node_count: int = 0


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """隔离的修复前工作树；tree_sha256 由工作区导出计算，物化补丁前重新校验。"""
    root: Path
    base_commit: str
    tree_sha256: str


@dataclass(frozen=True, slots=True)
class ProgramIndex:
    """语义前端输出；只保存修复前符号和位置，不在领域层暴露 Babel 对象。"""
    symbols: tuple[str, ...]
    locations: tuple[SourceSpan, ...]
    unsupported_constructs: tuple[str, ...] = ()
    read_interfaces: tuple[tuple[SourceSpan, tuple[str, ...]], ...] = ()
