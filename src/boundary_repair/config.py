"""本方法独立配置：不读取、不合并、不改写现有 baseline JSON 或 code/.env。"""
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import cast

from boundary_repair.domain.errors import ConfigurationError
from boundary_repair.domain.runtime import BudgetLimits, SearchPolicy


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """只保存环境变量名，不保存敏感值；名称须按用户现有 .env 显式匹配。"""
    name_env: str
    endpoint_env: str
    api_key_env: str
    label: str


@dataclass(frozen=True, slots=True)
class ModuleSelection:
    """三个独立消融开关；False 要替换为对照模块，而不是直接跳过阶段。"""
    partial_specification: bool = True
    expressivity_localization: bool = True
    scope_synthesis: bool = True


@dataclass(frozen=True, slots=True)
class IntegrationSettings:
    """Explicit deployment options; fixture/local modes are never enabled implicitly."""
    model_mode: str = "http"
    fixture_file: Path | None = None
    workspace_mode: str = "docker"
    repository_manifest: Path | None = None
    parser_module: Path | None = None
    parser_mode: str = "typescript"
    max_files: int = 5000
    max_file_bytes: int = 512000
    max_context_chars: int = 100000
    context_files: int = 8
    max_boundaries: int = 40
    response_tokens: int = 8000
    http_timeout: int = 180
    response_format: str = "json_schema"
    asset_attempts: int = 3
    asset_retry_delay: int = 1
    allow_local_http: bool = False
    max_asset_bytes: int = 10000000
    max_assets: int = 8
    max_workspace_bytes: int = 1000000000


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """解析后的配置；路径已按项目根解析，不依赖 CWD 或 .git。"""
    schema_version: int
    project_root: Path
    dataset: Path
    dataset_source: Path
    results_root: Path
    env_file: Path
    target: str
    isolation: str
    seed: int
    model: ModelSettings
    budget: BudgetLimits
    policy: SearchPolicy
    modules: ModuleSelection
    image_manifest: Path | None = None
    harness_python: Path | None = None
    harness_revision: str | None = None
    node_candidates: tuple[str, ...] = ()
    integration: IntegrationSettings = field(default_factory=IntegrationSettings)


def _table(value: object, allowed: set[str], location: str) -> dict[str, object]:
    """将 JSON 对象校验为已知字段映射；未知键立即报错，防止配置拼写静默失效。"""
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ConfigurationError(f"{location} 必须是对象")
    result = cast(dict[str, object], value)
    unknown = result.keys() - allowed
    if unknown:
        raise ConfigurationError(f"{location} 包含未知字段：{sorted(unknown)}")
    return result


def _text(data: dict[str, object], key: str) -> str:
    """取必需的非空文本；错误只输出字段名，不回显潜在敏感值。"""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} 必须是非空字符串")
    return value


def _integer(data: dict[str, object], key: str, minimum: int = 1) -> int:
    """取整数并检查下界；bool 不被当成整数预算接受。"""
    value = data.get(key)
    if type(value) is not int or value < minimum:
        raise ConfigurationError(f"{key} 必须是 >= {minimum} 的整数")
    return value


def _flag(data: dict[str, object], key: str) -> bool:
    """取显式布尔配置；字符串 false 不被按真值接受。"""
    value = data.get(key)
    if type(value) is not bool:
        raise ConfigurationError(f"{key} 必须是布尔值")
    return value


def _path(value: str, root: Path) -> Path:
    """解析本机路径；拒绝在 POSIX 上将 Windows 绝对路径误当相对目录。"""
    if os.name != "nt" and PureWindowsPath(value).drive:
        raise ConfigurationError("Windows 路径不能在当前 POSIX 环境中解析")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def load_config(path: Path, project_root: Path | None = None) -> ExperimentConfig:
    """JSON → 强类型配置；仅 project_root 相对配置文件，其余路径相对项目根。

    不继承未知 baseline schema，不读取 .env，不自动查询 Git，不设置 safe.directory。
    三个已有配置的预算语义已显式抄录到本包独立 preset；实际文件内容尚未核对。
    """
    data = _table(json.loads(path.read_text(encoding="utf-8-sig")), {
        "schema_version", "project_root", "dataset", "dataset_source", "results_root",
        "env_file", "target", "isolation", "seed", "model", "budget", "policy", "modules",
        "image_manifest", "harness_python", "harness_revision", "node_candidates", "integration",
    }, "config")
    if _integer(data, "schema_version") != 1:
        raise ConfigurationError("不支持的 schema_version")
    target = _text(data, "target")
    isolation = _text(data, "isolation")
    if target not in {"local", "server"} or isolation not in {"docker", "bwrap"}:
        raise ConfigurationError("target/isolation 值不支持")
    if target == "server" and os.name == "nt":
        raise ConfigurationError("server preset 请在 Linux 服务器读取；Windows 使用 local")
    if target == "server" and isolation != "docker":
        raise ConfigurationError("正式服务器实验必须使用 Docker")
    root = (project_root.resolve() if project_root is not None
            else _path(_text(data, "project_root"), path.resolve().parent))
    budget = _table(data.get("budget"), {
        "timeout_seconds", "max_model_calls", "max_output_tokens",
        "max_patch_candidates", "max_ideas",
    }, "budget")
    policy = _table(data.get("policy"), {
        "require_witness", "prefer_greedy_patch", "allow_multiple_sampling",
        "temperature", "final_submissions",
    }, "policy")
    model = _table(data.get("model"), {"name_env", "endpoint_env", "api_key_env", "label"}, "model")
    modules = _table(data.get("modules"), {
        "partial_specification", "expressivity_localization", "scope_synthesis",
    }, "modules")
    temperature = policy.get("temperature")
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2):
        raise ConfigurationError("temperature 必须是 [0, 2] 内有限数值")
    if _integer(policy, "final_submissions") != 1:
        raise ConfigurationError("本架构每题只能提交一个最终 patch")
    optional_paths: dict[str, Path | None] = {}
    for key in ("image_manifest", "harness_python"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ConfigurationError(f"{key} 必须是路径或 null")
        optional_paths[key] = None if value is None else _path(value, root)
    revision = data.get("harness_revision")
    if revision is not None and (not isinstance(revision, str) or not revision.strip()):
        raise ConfigurationError("harness_revision 必须是版本字符串或 null")
    nodes = data.get("node_candidates", [])
    if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
        raise ConfigurationError("node_candidates 必须是字符串列表")
    integration = parse_integration(data.get("integration", {}), root, target)
    return ExperimentConfig(
        schema_version=1, project_root=root,
        dataset=_path(_text(data, "dataset"), root),
        dataset_source=_path(_text(data, "dataset_source"), root),
        results_root=_path(_text(data, "results_root"), root),
        env_file=_path(_text(data, "env_file"), root), target=target, isolation=isolation,
        seed=_integer(data, "seed", 0),
        model=ModelSettings(
            name_env=_text(model, "name_env"), endpoint_env=_text(model, "endpoint_env"),
            api_key_env=_text(model, "api_key_env"), label=_text(model, "label"),
        ),
        budget=BudgetLimits(**{k: _integer(budget, k) for k in {
            "timeout_seconds", "max_model_calls", "max_output_tokens",
            "max_patch_candidates", "max_ideas",
        }}),
        policy=SearchPolicy(
            require_witness=_flag(policy, "require_witness"),
            prefer_greedy_patch=_flag(policy, "prefer_greedy_patch"),
            allow_multiple_sampling=_flag(policy, "allow_multiple_sampling"),
            temperature=float(temperature), final_submissions=1,
        ),
        modules=ModuleSelection(**{k: _flag(modules, k) for k in {
            "partial_specification", "expressivity_localization", "scope_synthesis",
        }}),
        image_manifest=optional_paths["image_manifest"],
        harness_python=optional_paths["harness_python"],
        harness_revision=revision, node_candidates=tuple(nodes), integration=integration,
    )


def parse_integration(value: object, root: Path, target: str) -> IntegrationSettings:
    """Validate optional deployment settings without reading secrets or initializing services."""
    from dataclasses import fields
    data = _table(value, {f.name for f in fields(IntegrationSettings)}, "integration")
    defaults = IntegrationSettings()
    parsed: dict[str, object] = {}
    path_names = {"fixture_file", "repository_manifest", "parser_module"}
    for item in fields(IntegrationSettings):
        key = item.name
        raw = data.get(key, getattr(defaults, key))
        if key in path_names:
            if raw is not None and (not isinstance(raw, str) or not raw):
                raise ConfigurationError(f"{key} must be a path or null")
            parsed[key] = _path(raw, root) if raw is not None else None
        elif key == "allow_local_http":
            if type(raw) is not bool:
                raise ConfigurationError("allow_local_http must be boolean")
            parsed[key] = raw
        elif isinstance(getattr(defaults, key), int):
            if type(raw) is not int or raw <= 0:
                raise ConfigurationError(f"{key} must be positive integer")
            parsed[key] = raw
        else:
            if not isinstance(raw, str) or not raw:
                raise ConfigurationError(f"{key} must be text")
            parsed[key] = raw
    settings = IntegrationSettings(**parsed)
    if settings.response_format not in {'json_schema', 'json_object'}:
        raise ConfigurationError('unsupported_response_format')
    if settings.asset_attempts > 5:
        raise ConfigurationError('asset_attempts must be <= 5')
    if settings.model_mode not in {"http", "fixture"} or settings.parser_mode not in {"typescript", "text"}:
        raise ConfigurationError("unsupported model/parser mode")
    if settings.workspace_mode not in {"docker", "git_archive"}:
        raise ConfigurationError("unsupported workspace mode")
    if target == "server" and (settings.workspace_mode != "docker" or settings.model_mode != "http"):
        raise ConfigurationError("server runs require Docker and a real HTTP model")
    if settings.model_mode == "fixture" and settings.fixture_file is None:
        raise ConfigurationError("fixture mode requires fixture_file")
    return settings
