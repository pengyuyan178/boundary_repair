"""本地单写者存储：UTF-8、原子 JSON、显式 batch 隔离，不修改已有结果。"""
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path

from boundary_repair.domain.errors import ConfigurationError
from boundary_repair.domain.repair import PatchArtifact
from boundary_repair.domain.runtime import StageEvent
from boundary_repair.ports import StageArtifact


RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
}


def safe_component(value: str) -> str:
    """校验 batch/instance/产物单级名称；拒绝路径穿越、绝对路径和 Windows 保留名。"""
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value)
            or ".." in value or value.endswith(".")
            or value.split(".")[0].upper() in RESERVED_NAMES):
        raise ConfigurationError("目录或文件标识不合法")
    return value


def json_value(value: object) -> object:
    """转换已知数据结构为 JSON 值；不回退到 repr，避免隐式保存密钥/客户端对象。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)
                if not field.metadata.get("secret", False)}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON mappings require string keys")
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported artifact type: {type(value).__name__}")


def write_json(path: Path, value: object) -> None:
    """先完整序列化，再同目录临时文件 + replace；失败不留下半个 JSON 文件。"""
    text = json.dumps(json_value(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path: Path, value: object) -> None:
    """单写者追加一条 JSONL；不宣称支持多进程同时写或断电事务。"""
    text = json.dumps(json_value(value), ensure_ascii=False, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def file_sha256(path: Path) -> str:
    """流式计算非敏感输入文件哈希；调用方不得将 .env 传入本函数。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(package_root: Path) -> str:
    """以包内相对路径、Python 与可信前端源码计算指纹；不要求项目存在 .git。"""
    digest = hashlib.sha256()
    for path in sorted(p for p in package_root.rglob("*")
                       if p.is_file() and p.suffix in {".py", ".cjs"}):
        digest.update(path.relative_to(package_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_sha256(path)))
    return digest.hexdigest()


def prediction_row(patch: PatchArtifact, model_label: str) -> dict[str, str]:
    """补丁 → SWE-bench 三字段 JSONL；校验非空与内容哈希，不添加 resolved 字段。

    正式字段依据官方 Evaluation Guide：instance_id、model_name_or_path、model_patch。
    来源和访问日期见 docs/SOURCES.md；语法正确性由 ProgramAdapter 的实际 AST 后端额外校验。
    """
    digest = hashlib.sha256(patch.unified_diff.encode("utf-8")).hexdigest()
    if not patch.unified_diff.strip() or digest != patch.sha256:
        raise ValueError("patch is empty or SHA256 does not match")
    return {"instance_id": patch.instance_id, "model_name_or_path": model_label,
            "model_patch": patch.unified_diff}


@dataclass(frozen=True, slots=True)
class CaseStore:
    """一个新建案例目录；实现 TracePort，只能写，不读取历史失败作为反馈。"""
    root: Path

    def emit(self, event: StageEvent) -> None:
        """追加阶段状态到 trajectory/stages.jsonl；不包含模型凭证或原始 .env。"""
        append_jsonl(self.root / "trajectory" / "stages.jsonl", event)

    def save(self, name: str, artifact: StageArtifact) -> None:
        """按类型写原始允许输入或中间产物；名称必须是单级安全文件名。"""
        from boundary_repair.domain.task import TaskInput
        directory = "input_context" if isinstance(artifact, TaskInput) else "trajectory"
        write_json(self.root / directory / safe_component(name), artifact)

    def save_inference(self, record: object) -> None:
        """记录生成状态与尚未评分的事实；不把 NOT_IMPLEMENTED 当作修复失败。"""
        write_json(self.root / "result_data" / "inference.json", record)

    def save_patch(self, patch: PatchArtifact) -> None:
        """只保存最终补丁，不应用到用户仓库；x 模式防止覆盖同案例已有 patch。"""
        destination = self.root / "patch" / "final.patch"
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(patch.unified_diff)


@dataclass(frozen=True, slots=True)
class BatchStore:
    """单进程顺序批次；并行执行应通过独立案例和单独汇总器扩展。"""
    root: Path

    @classmethod
    def create(cls, results_root: Path, batch_id: str) -> "BatchStore":
        """原子预留新 batch 目录；同名已存在立即失败，不隐式 resume 或覆盖。"""
        path = results_root / safe_component(batch_id)
        path.mkdir(parents=True, exist_ok=False)
        (path / "cases").mkdir()
        (path / "predictions.jsonl").touch(exist_ok=False)
        (path / "results.jsonl").touch(exist_ok=False)
        return cls(path)

    def create_case(self, instance_id: str) -> CaseStore:
        """创建五类案例子目录；instance_id 不能穿越到其他 batch 或用户项目。"""
        path = self.root / "cases" / safe_component(instance_id)
        path.mkdir(exist_ok=False)
        for name in ("patch", "input_context", "trajectory", "logs", "result_data"):
            (path / name).mkdir()
        return CaseStore(path)

    def record(self, record: object, prediction: dict[str, str] | None = None) -> None:
        """追加生成结果与可选预测；未实现/失败案例没有伪造的空 patch 预测。"""
        append_jsonl(self.root / "results.jsonl", record)
        if prediction is not None:
            append_jsonl(self.root / "predictions.jsonl", prediction)
