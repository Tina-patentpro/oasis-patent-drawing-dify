"""Protect the public SDK entry point, including its pre-invocation conversion."""

from dify_plugin import Tool

from .errors import ToolInvokeError


class SafeDrawingTool(Tool):
    def invoke(self, tool_parameters):
        try:
            yield from super().invoke(tool_parameters)
        except ToolInvokeError:
            raise
        except Exception:
            raise ToolInvokeError(
                "[parameter_error] 工具输入解析失败；请检查图片文件与参数类型"
            ) from None
