"""唯一组合根：仅此处选实现并连接依赖；算法文件内没有服务定位器。"""
from boundary_repair.adapters.integrations import (
    DockerWorkspaceAdapter,
    GitArchiveWorkspaceAdapter,
    FrozenModelAdapter,
    LogicAdapter,
    ProgramAdapter,
)
from boundary_repair.algorithms.controls import PlainControls
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.config import ExperimentConfig
from boundary_repair.pipeline import RepairPipeline
from boundary_repair.ports import WorkspacePort


def build_pipeline(config: ExperimentConfig) -> RepairPipeline:
    """按三个独立开关注入实现，生成全部八种消融组合；不读取密钥、不发网络请求。

    未来接入只需替换适配器构造与相应实现，不需要修改 pipeline.py 的控制流。
    有限布尔语义与单向通用补丁路径已实现；不支持的语义保持 UNKNOWN。
    """
    model = FrozenModelAdapter(config)
    program = ProgramAdapter(config)
    logic = LogicAdapter()
    controls = PlainControls(model, program, config.integration.max_boundaries, config.integration.response_tokens,
                             config.integration.context_files, config.integration.max_context_chars)
    return RepairPipeline(
        specification=(SpecificationRecovery(model, program, logic, config.integration.response_tokens,
                                             config.integration.context_files, config.integration.max_context_chars)
                       if config.modules.partial_specification else controls),
        localization=(ExpressivityLocalization(program, logic, config.integration.max_boundaries)
                      if config.modules.expressivity_localization else controls),
        synthesis=(ScopeSynthesis(model, program, logic, config.integration.response_tokens, config.integration.max_context_chars)
                   if config.modules.scope_synthesis else controls),
    )


def build_workspace(config: ExperimentConfig) -> WorkspacePort:
    """创建工作区适配器；支持 Docker 与显式本地 Git archive，bwrap 不静默回落到宿主执行。"""
    if config.integration.workspace_mode == "git_archive":
        return GitArchiveWorkspaceAdapter(config)
    if config.isolation != "docker":
        from boundary_repair.domain.errors import UnsupportedConfiguration
        raise UnsupportedConfiguration("bwrap execution is not implemented; use Docker or explicit local git_archive")
    return DockerWorkspaceAdapter(config)
