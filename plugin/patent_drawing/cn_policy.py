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
    'chemical_material': [('process_flow', '有制备步骤时'), ('structure_formula', '有确定化学结构时'), ('experimental_evidence', '仅使用可追溯实际数据')],
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


def suggest_types(text):
    # A suggestion only; explicit profile remains visible and can be corrected.
    groups = {'mechanical': ['弹簧', '托板', '安装销', '接地母线', '机械'],
              'electrical': ['电路', '电压', '电流'], 'computer_method': ['队列', '去重', '服务端', '算法'],
              'communication': ['基站', '无线', '通信协议'], 'manufacturing_process': ['工序', '加工方法'],
              'chemical_material': ['化合物', '聚合物', '催化剂'], 'bio_medical': ['细胞', '电泳', '蛋白'],
              'semiconductor': ['半导体', '光刻', '晶圆']}
    selected = [k for k, terms in groups.items() if any(t in text for t in terms)]
    return selected or ['unknown']


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
