"""CN legal drawing checks vs conditional drafting recommendations.

This is a bounded implementation of cited rules, never a filing certificate.
Missing specification/visual evidence stays UNKNOWN, even when schema passes.
"""
import re
import hashlib
from pathlib import Path
from functools import lru_cache

GUIDE = 'https://www.cnipa.gov.cn/art/2023/12/21/art_526_189193.html'
RULES = 'https://www.cnipa.gov.cn/art/2023/12/21/art_3317_189352.html'
LAW = 'https://www.cnipa.gov.cn/art/2020/11/23/art_97_155167.html'
AMENDMENT = 'https://www.cnipa.gov.cn/art/2025/11/14/art_99_202568.html'
POLICY_VERSION = 'CN-drawings-20261107-r1'


@lru_cache(maxsize=1)
def catalog_sha256():
    return hashlib.sha256((Path(__file__).resolve().parents[1] / '_assets/cn_drawing_rules.json').read_bytes()).hexdigest()

PROFILES = {
    'mechanical': [('assembly', '表达部件及连接关系'), ('section', '内部结构被遮挡时'), ('detail', '关键配合不清楚时'), ('state', '涉及运动或装配状态时')],
    'electrical': [('block_diagram', '系统组成'), ('circuit', '电路连接为技术要点时'), ('waveform', '已有时序/波形数据时')],
    'computer_method': [('flowchart', '方法步骤及判断条件'), ('block_diagram', '执行结构有来源时'), ('sequence', '多角色时序为技术要点时')],
    'communication': [('topology', '通信实体和连接'), ('sequence', '消息顺序和两端动作'), ('flowchart', '各角色处理逻辑')],
    'manufacturing_process': [('process_flow', '步骤和工艺条件'), ('equipment', '设备结构有来源时'), ('state', '工序前后状态有来源时')],
    'chemical_material': [('structure', '有依据的材料取向、层间关系或传递方向对理解方案必要时'), ('process_flow', '有制备步骤时'), ('structure_formula', '有确定化学结构时'), ('experimental_evidence', '仅使用可追溯实际数据')],
    'bio_medical': [('process_flow', '实验/制备流程有来源时'), ('experimental_evidence', '只使用原始显微/图谱/实验记录'), ('assembly', '器械结构为技术要点时')],
    'semiconductor': [('layer_section', '有层结构和材料依据时'), ('process_flow', '制造步骤'), ('layout', '平面布局有来源时')],
    'unknown': [('needs_type_confirmation', '先确认技术内容，不能固定套用机械三视图')],
}


def finding(rule, verdict, message, category='legal', locator='', url=GUIDE):
    return {'rule': rule, 'verdict': verdict, 'message': message, 'category': category,
            'source_url': url, 'locator': locator, 'policy_version': POLICY_VERSION, 'policy_catalog_sha256': catalog_sha256()}


def recommend_views(technical_types):
    if not technical_types: technical_types = ['unknown']
    result = []
    for technology in technical_types:
        if technology not in PROFILES:
            raise ValueError('unsupported technical type')
        for kind, condition in PROFILES[technology]:
            if kind not in {x['type'] for x in result}:
                result.append({'type': kind, 'condition': condition, 'technical_type': technology,
                    'category': 'best_practice', 'legal_minimum_count': False,
                    'basis': 'Source-bound drafting recommendation; not a statutory fixed set of views'})
    return result


TECHNICAL_TERMS = {
    'mechanical': ['弹簧', '托板', '安装销', '接地母线', '机械结构', '机械装置', '机械互锁', '机械联锁',
                   '支承', '齿轮', '传动机构', '销孔'],
    'electrical': ['电路', '电压', '电流', '电机控制'],
    'computer_method': ['队列', '去重', '服务端', '算法', '数据处理', '计算机方法', '编码器', '语义表示'],
    'communication': ['基站', '无线', '通信协议'],
    'manufacturing_process': ['加工工序', '加工方法', '制造工艺', '喷镀覆膜', '珩磨'],
    'chemical_material': ['化合物', '聚合物', '催化剂', '面内导热', '鳞片状碳材料', '热扩散膜', '导热膜',
                          '非调质钢', '合金成分', '合金配方'],
    'bio_medical': ['细胞', '电泳', '蛋白'],
    'semiconductor': ['半导体', '光刻', '晶圆', '再分布结构'],
}


def suggest_types(text):
    return [key for key, terms in TECHNICAL_TERMS.items() if any(term in text for term in terms)] or ['unknown']


def domain_drawing_obligations(bundle, technical_types):
    """Locate source-backed features that affect view choice and later review."""
    obligations = {
        'mechanical': [
            ('interlock_reversed_positions', ('第一控制杆', '第二控制杆', '阻挡', '让行', '反转'),
             '用状态示意表达第一锁定位置和第二锁定位置。图内分别标出两个控制杆的阻挡/断开和让行/可闭合。'
             '第一位置阻挡第一控制杆、让行第二控制杆，第二位置反转。传感开关关系另作补充。'),
            ('interlock_both_open', ('中间位置', '两个控制杆', '双断'),
             '在独立状态图或可区分的同图分区显示中间位置，图内明确两个控制杆受阻并保持双断。'),
            ('interlock_reversed_travel', ('阻挡边缘抵住第一控制杆', '第二接触边缘与第二控制杆运动方向对齐', '反转'),
             '两锁定位置优先分图，用矩形示意两杆、折线示意边缘，并分别将可见标号引到对应形状。'
             '令闭合方向朝图上方，阻挡边缘接触受阻杆顶边内侧，且不可进入杆体。'
             '让行杆上方路径留空，对应接触边缘与该路径平行且不进入路径内部。'
             '保持来源所示方向，接触边缘长度及行程未知时不补造。第二位置交换这两项关系。'),
            ('interlock_middle_travel', ('中间位置', '阻挡边缘两端', '阻止两个控制杆上升'),
             '中间位置用独立矢量图或同排独立分区。阻挡边缘的可见线段必须在两个杆顶边内侧各有接触，'
             '线段离开接触点后不得穿入任何杆体，否则被填充覆盖后只剩杆侧接触。'
             '对两杆和阻挡边缘各用一个实际引线标号，不补造尺寸或行程。'),
        ],
        'semiconductor': [
            ('rdl_conductor_and_insulator', ('再分布', '导电图案', '绝缘层'),
             '剖面分开表达再分布结构中的导电图案和绝缘层。把导电柱及贯穿电极连到实际对应的导电部位。'),
            ('distinct_underfill_interface', ('封装基板', '中介结构', '底部填充'),
             '区分芯片接合界面和封装基板与中介结构之间的界面。来源若为可选底填，用单独视图或视图中的可选实施方式表达。'),
            ('visible_conductive_chain', ('贯穿电极', '导电图案', '导电柱', '芯片连接端子'),
             '分层剖面画出贯穿电极、导电图案、导电柱、芯片连接端子及其已披露接触次序。'
             '用这四项名称登记稳定部件；矢量剖面为各自实际形状设置可读标号和引线，编号沿用本图单已核编号。'
             '导电柱侧部的绝缘须与导电柱区分。只在登记表出现的标号不满足图面要求。'),
            ('package_process_order', ('形成', '再分布结构', '设置导电柱', '热压接合'),
             '另作矢量工艺流程图，实际箭头依次连接形成再分布结构、设置导电柱、热压接合。'
             '可在设置导电柱的节点写侧部绝缘关系，不假设温度、压力、时间或绝缘层的沉积先后。'),
        ],
        'chemical_material': [
            ('in_plane_heat_network', ('鳞片', '接触', '面内', '厚度方向'),
             '用定性图表达鳞片的膜面取向、邻片接触和连续热流。原文并列的取向与接触共同支持面内热扩散，'
             '关系框图采用并列汇合，不以箭头暗示两者的制备先后。以独立方向标识对照厚度方向，不把它画成实际主热流。'
             '不使用假定导热率、尺寸或层数。'),
        ],
        'communication': [
            ('panel_relative_beam_range', ('天线面板', '角度范围', '波束方向'),
             '定性角范围图以横平竖直的正对画布矩形作为面板符号，不用斜视投影。'
             '面板边缘上一处可见的非比例示意点为共同起点。'
             '两条边界和至少两支内部波束箭头从该点连续发出；该点不是实测相位中心，不添加角值或坐标定义。'
             '图面只保留轮廓、边界和内部箭线，术语放入图说，底图不画文字或引出线。'
             '设备之间的指示、选束、测量和反馈仍用单独的关系线图表达。'),
        ],
    }
    pages = [(p['id'], re.sub(r'\s+', '', p.get('text', ''))) for p in bundle['pages']]
    result = []
    for technical_type in technical_types:
        for ident, terms, requirement in obligations.get(technical_type, ()):
            page = next((pid for pid, text in pages if all(term in text for term in terms)), None)
            if page:
                result.append({'id': ident, 'page_id': page, 'requirement': requirement})
    from .source_contract import source_view_evidence

    requirements = {
        'sync_acknowledgment': '用实际节点及箭线表达终端作用域、本地入队、上传去重和收到确认才移除队列项。',
        'sync_incremental': '独立图表达按服务器修订号拉取和应用，保留未获确认的本地队列。',
        'sync_field_conflict': '独立图表达同字段冲突时保留两个版本并提示用户选择。',
        'sync_account_isolation': '独立图表达各账号与各自队列的分界，不增画切换实施或注销处理。',
        'cabinet_functional_zones': '独立功能示意表达柜内供电列、连接列和连接列内的监测控制单元，以及供电、通信功能关系。',
        'cabinet_auxiliary_rail': '仅在供电轨两端及方向有来源时绘制接线；已核供电关系可归入工程局部。',
        'cabinet_rail_path': '按已确认的供电板、辅助供电轨及监测控制单元端点画出同向两段供电关系。',
    }
    for ident, evidence in source_view_evidence(bundle, technical_types).items():
        result.append({'id': ident, 'page_id': evidence[0]['page_id'], 'evidence': evidence,
                       'requirement': requirements[ident]})
    return result


def check_policy(plan):
    out = []; patent = plan.get('patent_type', 'unknown'); views = plan.get('views', [])
    if patent == 'design':
        return [finding('CN-DESIGN-ROUTE', 'FAIL', '外观设计/GUI需独立图样规则与原图片支持，不能按发明结构图流程交付。', locator='实施细则30、31', url=RULES)]
    if patent not in {'invention', 'utility_model'}:
        out.append(finding('CN-PATENT-TYPE', 'UNKNOWN', '尚未确认发明/实用新型申请类型。', locator='实施细则20', url=RULES))
    recommend_views(plan.get('technical_types', ['unknown']))
    physical = {'assembly', 'spatial', 'structure', 'section', 'detail', 'layer_section', 'circuit', 'equipment', 'layout', 'top'}
    kinds = {v.get('delivered_type', v.get('type')) for v in views}
    if patent == 'utility_model' and not (kinds & physical):
        out.append(finding('CN-UTILITY-STRUCTURE', 'FAIL', '实用新型缺表示产品形状、构造或其结合的结构附图；仅流程/关系框图不足。', locator='实施细则20第5款', url=RULES))
    labels = set();part_labels = {p['id']: p['label'] for p in plan.get('parts', [])}
    for index, v in enumerate(views, 1):
        kind = v.get('delivered_type', v.get('type'))
        marks = v.get('labels', [part_labels[x] for x in v.get('parts', []) if x in part_labels])
        labels.update(marks)
        if kind in physical and any(not re.fullmatch(r'[0-9]+(?:[a-zA-Z]|[.-][0-9]+)?', str(x)) for x in marks):
            out.append(finding('CN-MARK-NUMERIC', 'FAIL', f'图{index}结构标记应以阿拉伯数字编号；剖切符号与流程文字另行处理。', locator='第一部分第一章4.3'))
        if v.get('has_words') and not v.get('necessary_words'):
            out.append(finding('CN-NECESSARY-WORDS', 'UNKNOWN', f'图{index}存在尚未确认必要性的词语；说明性长注释移入附图说明。', locator='实施细则21', url=RULES))
        if v.get('photo') and (patent == 'utility_model' or v.get('photo_purpose') not in {'microscopy', 'metallography', 'electrophoresis'}):
            out.append(finding('CN-PHOTO', 'FAIL', f'图{index}照片用途不符合当前已支持例外；需按适用法域及专利类型另审。', locator='第一部分第一章4.3、第二章7.3'))
        if v.get('color') and not v.get('color_reason'):
            out.append(finding('CN-COLOR-NECESSITY', 'UNKNOWN', f'图{index}彩色必要性待核，不能一律禁色或直接认定合格。', locator='第一部分第一章4.3'))
        if kind in {'waveform', 'experimental_evidence'} and not v.get('data_provenance'):
            out.append(finding('BP-NO-FABRICATED-DATA', 'FAIL', f'图{index}缺数据来源，禁止生成虚构测量曲线、显微图或图谱。', category='best_practice', locator='真实性/来源约束', url=LAW))
        if plan.get('application_stage') == 'filed' and v.get('new_feature') and not v.get('filed_support'):
            out.append(finding('CN-NO-ADDED-MATTER', 'FAIL', f'图{index}申请后新增内容缺原申请依据；发明人后续确认不代原始支持。', locator='专利法33', url=LAW))
    spec_labels = plan.get('specification_labels')
    if spec_labels is None or (spec_labels==[] and not plan.get('specification_labels_complete',False)):
        out.append(finding('CN-MARK-SPEC', 'UNKNOWN', '未提供完整说明书标记索引，不能判定图文标记双向一致已通过。', locator='实施细则21', url=RULES))
    elif labels != set(spec_labels):
        out.append(finding('CN-MARK-SPEC', 'FAIL', '说明书与附图标记不双向对应。', locator='实施细则21', url=RULES))
    if not plan.get('visual_review_receipt'):
        out.append(finding('CN-REDUCTION-CLARITY', 'UNKNOWN', '须查看实际输出及缩至2/3的可读性；仅JSON合法不能判清晰。', locator='第一部分第一章4.3'))
    return out


def parse_technical_types(value):
    if not value or value=='auto':return None
    if not isinstance(value,str):raise ValueError('technical type must be explicit text')
    rows=list(dict.fromkeys(value.split(',')))
    if not rows or len(rows)>8 or any(x not in PROFILES or x=='unknown'for x in rows):raise ValueError('unsupported technical type')
    return rows
