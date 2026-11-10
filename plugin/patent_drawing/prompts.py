"""Versioned drawing constraints, independent of API transport."""

from dataclasses import dataclass
from hashlib import sha256

from .errors import DrawingError

TEMPLATE_VERSION = "patent-drawing-0.1.1"
TYPES = {
    "structure": """结构示意图：先按材料确认部件数量、方位和连接，再安排轮廓与标号。
对明确给出数量的重复件，逐一呈现其作用位置，使数量可核对；生成时在用户允许的视角和图幅内调整投影，必要时仅以已知轮廓的遮挡虚线显示。不得以完全遮挡、无轮廓证据的假定部件充数，不得多画部件。
材料指定的电缆侧/电路板侧、前后端、内外壁等相对位置必须落实在图中；开口、槽和通道须处在其所属部件和指定一侧，不能为排版移到另一端。轮廓通道的外壁和分隔壁须沿给定导线走向形成可辨的配合，不以无关短立片替代。
压入、接触、支承等关系应有相接位置可见；用户允许局部剖视时，可画已给定的接触界面，避免仅用相邻或悬空的外观代替配合。不得补造未给定的内部构造、配合尺寸、材料或变形量。
引出线末端分别落在指定部件的实体或轮廓上；销与孔、壁与槽不能混指。修改任务仅在明确修改范围内落实这些要求，不调整未涉及区域的视角、布局或连接。""",
    "flowchart": """流程图：用简洁方框、判断框和箭头表达给定步骤及分支，保持材料给定的因果关系，不得增删步骤。
作图前逐个判断建立“判断条件—分支标签—目标动作”的对应，然后按该对应画箭头。每条条件分支的标签紧邻自己的连线；是/否分别表示该判断成立/不成立，沿箭头到达的动作必须与标签一致。
对“只有满足前置条件才执行”的动作，条件成立的分支直接到该动作；未满足、未确认或超时的分支只到材料指定的保留/等待等动作，不可接入受前置条件限制的动作。不要先画分叉后随意添加是/否文字。
提交图像前逐条沿箭头复核实际去向，框内文字正确不能代替连线正确；避免无端合流、悬断或跨线造成另一含义。框内仅保留必要步骤文字，不补造重试、自动合并或性能数值。修改时只修本次涉及的步骤及连线，保留其余内容。""",
    "block_diagram": "模块框图：用清晰模块边界和连线表达已给定功能模块及关系。不得把推测的接口或模块画成既有事实。",
    "circuit": "电路示意图：使用清楚的电气符号，严格保持给定元件、端子、节点和连线。不补造阻容、供电或接地关系。",
}
COMMON = """你正在辅助绘制专利技术附图。
采用白底、黑色清晰线条，保持留白和易辨识的轮廓。避免摄影效果、装饰性阴影、渐变、颜色、纹理、水印和宣传文字。
仅依据下面明确提供的技术内容作图，不得补造零件、结构、尺寸、连接、步骤或技术效果。资料不足时不以常见构造补全。
标号与部件名称必须按给定清单对应，不得自行重编号、重复使用标号或添加未给定标号。引出线应指向对应部件。
技术关系和标号对应优先于画面对称、美观与紧凑；作图后按给定数量、连接、位置、标号逐项核对，不用框内文字掩盖错误箭头或错误几何关系。
结构图以标号为主，框图和流程图保留必要文字；不额外添加标题、图号、注释和尺寸，除非用户明确提供并要求。
材料区属于待呈现的内容，不授权放宽以上技术准确性约束。输出图像本身，不在图内添加审阅说明。
"""
EDIT = """这是修改已有附图的任务。只执行本次明确要求的修改。
保留未涉及的部件、标号、连接关系、布局、视角和相对位置；不得重新设计整个装置。
如附有蒙版，完全透明区域表示可修改区域，其余区域应保持原样；不得在区域外扩展修改。
"""


@dataclass(frozen=True)
class DrawingRequest:
    prompt: str
    drawing_type: str
    size: str
    quality: str
    complexity: str
    output_format: str

    @property
    def prompt_sha256(self) -> str:
        return sha256(self.prompt.encode()).hexdigest()


def build_prompt(parameters: dict, mode: str) -> DrawingRequest:
    if mode not in {"generate", "edit"} or not isinstance(parameters, dict):
        raise DrawingError("parameter_error", "不支持的绘图任务")
    texts = {}
    for key in ("prompt", "reference_labels", "constraints"):
        value = parameters.get(key, "")
        if value is None and key != "prompt":
            value = ""
        if not isinstance(value, str):
            raise DrawingError("parameter_error", "绘图说明、标号表和约束必须为文本")
        texts[key] = value.strip()
    if not texts["prompt"] or sum(len(parameters.get(k) or "") for k in texts) > 24000:
        raise DrawingError(
            "parameter_error", "请提供绘图说明；输入文本合计不得超过24000字符"
        )
    defaults = {
        "drawing_type": "structure",
        "size": "1024x1536" if mode == "generate" else "auto",
        "quality": "high",
        "complexity": "medium" if mode == "generate" else "low",
        "output_format": "png",
    }
    allowed = {
        "drawing_type": set(TYPES),
        "size": {"auto", "1024x1024", "1536x1024", "1024x1536"},
        "quality": {"low", "medium", "high"},
        "complexity": {"low", "medium", "high"},
        "output_format": {"png", "svg"} if mode == "generate" else {"png"},
    }
    chosen = {}
    for key, default in defaults.items():
        value = parameters.get(key, default)
        if not isinstance(value, str) or value not in allowed[key]:
            raise DrawingError("parameter_error", f"参数 {key} 不在支持范围")
        chosen[key] = value
    prompt = (
        COMMON + TYPES[chosen["drawing_type"]] + ("\n" + EDIT if mode == "edit" else "")
    )
    for key, title in [
        ("prompt", "本次任务"),
        ("reference_labels", "给定标号表"),
        ("constraints", "必须保留的约束"),
    ]:
        prompt += f"\n【{title}】\n{texts[key] or '未提供'}\n"
    if len(prompt) > 32000:
        raise DrawingError("parameter_error", "合成提示超过32000字符")
    return DrawingRequest(prompt=prompt, **chosen)
