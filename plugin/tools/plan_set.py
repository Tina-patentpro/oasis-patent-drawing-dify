from patent_drawing import safe_tool
from patent_drawing.text_client import OpenAITextClient
from patent_drawing.errors import DrawingError,ToolInvokeError


class PlanDrawingSet(safe_tool.SafeDrawingTool):
    def _invoke(self,tool_parameters):
        try:
            result=OpenAITextClient(self.runtime.credentials.get('openai_api_key')).plan(tool_parameters.get('system_prompt'),tool_parameters.get('user_prompt'))
            message=self.create_json_message(result)
        except DrawingError as e:raise ToolInvokeError(str(e))from None
        except Exception:raise ToolInvokeError('[planner_processing_failed] 规划响应处理失败；未自动重试')from None
        yield message
