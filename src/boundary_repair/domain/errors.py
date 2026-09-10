"""显式区分未实现、配置错误、预算耗尽与证据问题。"""


class BoundaryRepairError(Exception):
    """本项目可向 CLI 汇报的受控错误；不能等同于评测失败。"""


class ImplementationRequired(BoundaryRepairError, NotImplementedError):
    """占位函数必须抛出此异常；严禁用空结果冒充算法成功。"""

    def __init__(self, component: str) -> None:
        """输入稳定的组件标识；保存标识用于结果落盘，不写入敏感上下文。"""
        self.component = component
        super().__init__(f"尚未实现：{component}。参见 docs/IMPLEMENTATION.md。")


class ConfigurationError(BoundaryRepairError):
    """配置字段缺失、值不合法或运行环境不匹配。"""


class DatasetFormatError(BoundaryRepairError):
    """数据结构不支持；禁止猜测未知附件字段或静默忽略损坏记录。"""


class BudgetExceeded(BoundaryRepairError):
    """本任务资源预算耗尽；不能触发额外的修复尝试。"""


class EvidenceConflict(BoundaryRepairError):
    """解释空间矛盾；不能利用空集蕴含生成伪 MUST。"""


class NoAdmissiblePatch(BoundaryRepairError):
    """没有符合声明作用域的补丁；与占位未实现、程序异常分别记录。"""


class ValidationError(BoundaryRepairError):
    """Untrusted data, stale source, or a structured response violates its contract."""


class ExternalServiceError(BoundaryRepairError):
    """Sanitized HTTP/process failure; credentials and response bodies are not exposed."""


class UnsupportedConfiguration(ConfigurationError):
    """An explicit runtime mode is outside the supported, tested implementation."""
