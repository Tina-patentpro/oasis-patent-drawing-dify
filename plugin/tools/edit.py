from patent_drawing import safe_tool
from patent_drawing.invocation import invoke


class EditPatentDrawing(safe_tool.SafeDrawingTool):
    def _invoke(self, tool_parameters):
        yield from invoke(self, tool_parameters, "edit")
