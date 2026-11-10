from patent_drawing import safe_tool
from patent_drawing.set_tools import invoke_set


class PackageDrawingSet(safe_tool.SafeDrawingTool):
    def _invoke(self,tool_parameters):
        yield from invoke_set(self,tool_parameters,'package')
