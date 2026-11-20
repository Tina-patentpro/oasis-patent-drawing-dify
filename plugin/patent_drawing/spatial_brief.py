"""Source-bound visibility obligations for physical image candidates."""
import re

from .operation_store import digest
from .source_contract import compact, validate_facts

PANEL_BEAM_ORIGIN = 'panel-beam-common-origin'
MATERIAL_CONTACT = 'material-flake-contact'
MATERIAL_HEAT_PATH = 'material-in-plane-heat-path'
MATERIAL_THICKNESS = 'material-thickness-direction'
MATERIAL_HEAT_LAYOUT = (
    '在画布左侧画一排长轴横向的相邻鳞片符号。每对相邻符号共用一段陡斜接合线，接合处无白缝。'
    '在符号中部画一根不间断的水平双头箭，两个箭尖分别朝左、朝右。'
    '直箭杆从最左侧符号内部连续穿过两个接合处的内侧，到达最右侧符号内部。'
    '双头箭杆的中点就是左右路径的共同示意起点；此处不留空，也不加竖线、点或箭头。'
    '保留两段接合线，使它们和横向箭杆有清楚的交叉。'
    '在画布右侧留白区单独画正交方向轴。横轴代表膜面，竖轴代表厚度。'
    '两轴旁留空，轴名在审阅线迹后单独标注，底图不画字。'
    '“局部热量”和“面内热扩散”写在图说；鳞片网络区不画文字、引线或竖向箭。'
    '符号及其画幅位置只作定性排版，未知数量、实际形貌、尺寸和性能均不确定。'
)
MATERIAL_HEAT_FEATURES = {
    MATERIAL_CONTACT: '在实际底图上分别指出两个相邻鳞片符号的接合位置；长轴总体沿膜面。符号数量、轮廓及接触形式仅作定性示意。',
    MATERIAL_HEAT_PATH: '从同一个局部示意点发出左右两支连续箭线，每支实际穿过至少一处邻片接合线。'
                        '将箭线画在鳞片符号中部，不在网络上方、下方或外侧另画面内热流箭。'
                        '局部热量写入图说，不画竖向输入箭、加热器或真实施热位置。',
    MATERIAL_THICKNESS: '在鳞片网络外单独标出彼此正交的膜面方向与厚度方向。'
                        '厚度轴只作方向对照，和热流箭线完全分开，不标尺寸或实测值。',
}
_FLAKE_CONTACT = r'使(?:邻接|相邻)鳞片(?:彼此|相互)接触'
_FLAKE_ORIENTATION_TAIL = r'(?:并|且)使其长轴(?:主要)?沿膜面(?:方向)?取向'
_FLAKE_ORIENTATION = re.compile(
    r'(?:(?:选用|采用|使用)(?:由[^，,]{1,48}组成的)?鳞片状碳材料制成导热膜[，,])?'
    rf'{_FLAKE_CONTACT}[，,]?{_FLAKE_ORIENTATION_TAIL}'
)
_FLAKE_ORIENTATION_ON_PAGE = re.compile(
    rf'(?:^|[，,。；;！？!?])({_FLAKE_CONTACT})[，,]?({_FLAKE_ORIENTATION_TAIL})(?=$|[。；;！？!?])'
)
_FILM_HEAT = re.compile(r'利用鳞片面内导热特性与(?:彼此|相互)接触形成连续传热网络[，,]?(?:把|将)局部热量沿膜面扩散[，,]?(?:而不是|而非)主要依赖厚度方向导热')
PANEL_BEAM_TERMS = ('天线面板', '角度范围', '波束方向')
PANEL_RANGE_CLAIM = re.compile(
    r'发射(?:设备|端)(?:定义|确定|设置|给出)(?:了)?(?:一个)?相对(?:于)?'
    r'(?:自身|本机|其)?天线面板的角度范围[，,]?(?:其中|该范围|其)?(?:内)?'
    r'(?:包含|包括|含有)(?:至少|不少于)(?:两个|二个|2个|两种)发射波束方向'
    # The source may cite the following range-indication action in the same sentence.
    # Keep full-clause matching so a conditional or contradictory tail stays unresolved.
    r'(?:[，,](?:并且|并|同时|随后)(?:把|将)(?:该|上述|所述)(?:角度)?范围(?:的)?'
    r'(?:指示|信息)?(?:发送|传递|传输|指示)给?接收(?:设备|端))?'
)
EXPLICIT_DRAWING_REQUEST = re.compile(
    r'flare|gpt.image|/v1/images|位图|栅格|文生图|图像生成模型|不用矢量|不使用矢量|禁止矢量'
    r'|标号|编号|引出线|三维|立体|剖视|按比例', re.IGNORECASE
)
PANEL_OWNER_NAMES = {'发射设备', '发射端'}
PANEL_OWNER_GEOMETRY = re.compile(r'发射(?:设备|端)(?:的)?(?:本体|外形|轮廓|结构|安装)')
PANEL_BEAM_LAYOUT = (
    '用横向白底画布。左侧约三分之一内画一个小的空白面板矩形，矩形外的右侧留白用于画四条线。'
    '四条线都从矩形右边中部同一点向右、向矩形外展开；矩形内部完全空白。'
    '四条线的起端必须实际接触矩形右边的同一小接合点，连接处无白缝。接合点约为线宽，仅表示绘图关系。'
    '两支内箭的尾线也要画到这个点，不为区分箭线而将箭尾右移。'
    '离开共同点后，从上到下可分别看出上边界、上内箭、下内箭、下边界。'
    '最上与最下两线无箭尖；中间两线各在自己的箭尖处结束，箭尖之后无线段。'
    '面板的边不能兼作范围边界，范围外侧不用大矩形围住。画幅位置和比例仅用于排版。'
)
PANEL_BEAM_BASE_INK = (
    '只画空白矩形、两条无箭尖边界和两支内部波束箭线。范围外缘如需闭合，只可用一条无刻度浅弧连接边界末端。'
    '不要在底图中添加文字、标号、引出线、额外箭线或其他轮廓。术语和解释写在图说中。'
    '任务标题、部件名称及来源原句仅供理解，不作为图中文字。'
)


def select_source_bound_rendering(bundle, plan, user_request):
    """Select checked drawing geometry for a fully cited qualitative relation."""
    before = digest(plan)
    changes = []
    if (bundle.get('figure_presence') == 'absent' and 'communication' in plan.get('technical_types', [])
            and plan.get('application_stage', 'draft') == 'draft' and not plan.get('scene')
            and not plan.get('specification_labels') and not plan.get('specification_labels_complete')
            and not EXPLICIT_DRAWING_REQUEST.search(user_request)):
        facts = {fact['id']: fact for fact in plan.get('facts', [])}
        views = plan['views']
        parts = {part['id']: part['name'] for part in plan['parts']}
        parents = {pid for view in views for pid in view.get('depends_on', [])}
        for view in views:
            fact_ids = view.get('required_facts', [])
            if (view.get('engine') not in {'image', 'vector'}
                    or view.get('requested_type') != 'relationship' or view.get('delivered_type') != 'relationship'
                    or len(fact_ids) != 1 or view['id'] in parents
                    or any(view.get(k) for k in ('depends_on', 'annotations', 'labels', 'source_region_id',
                                                'reference_selection', 'parent_view', 'expression_mode'))):
                continue
            fact = facts.get(fact_ids[0])
            if not fact or fact['status'] != 'confirmed' or validate_facts(bundle, [fact]):
                continue
            if not any(PANEL_RANGE_CLAIM.fullmatch(clause) for ref in fact['evidence']
                       for clause in re.split(r'[。；;！？!?]', compact(ref['quote']))):
                continue
            part_ids = view.get('parts', [])
            roles = [[pid for pid in part_ids if term in parts.get(pid, '')] for term in PANEL_BEAM_TERMS]
            if any(len(ids) != 1 for ids in roles):
                continue
            panel, angle_range, beam = (ids[0] for ids in roles)
            if len({panel, angle_range, beam}) != 3:
                continue
            context_parts = [pid for pid in part_ids if pid not in {panel, angle_range, beam}]
            # A transmitter may own the panel without requiring a second drawn contour.
            if context_parts and (
                len(context_parts) != 1 or parts.get(context_parts[0]) not in PANEL_OWNER_NAMES
                or view.get('diagram')
                or PANEL_OWNER_GEOMETRY.search(' '.join((user_request, view.get('purpose', ''), view.get('instruction', ''))))
            ):
                continue
            old_engine = view['engine']
            view['engine'] = 'vector'
            view['diagram'] = panel_range_diagram(panel, angle_range, beam)
            view['instruction'] = (
                '用同一面板边缘绘图点连接两条无箭尖边界和两支范围内的波束方向示意箭。'
                '角度、长度和接点均为非比例绘图约定。术语见图说。'
            )
            panel_description = '发射设备的天线面板' if context_parts else '天线面板'
            view['purpose'] = view.get('purpose', '') + (
                f' 矩形表示{panel_description}，两条无箭尖边界表示相对角度范围，两支内箭示意不同波束方向。'
                '共点为非比例绘图约定，不表示实际相位中心、角度或尺寸。'
            )
            changes.append({'view_id': view['id'], 'from_engine': old_engine, 'to_engine': 'vector',
                            'source_fact_ids': list(fact_ids), 'layout': 'panel-relative-beam-range'})
            if context_parts:
                changes[-1]['contextual_part_ids'] = context_parts
    return {'input_sha256': before, 'output_sha256': digest(plan), 'changes': changes}


def panel_range_diagram(panel, angle_range, beam):
    origin = [450, 400]
    primitives = [{'type': 'rect', 'part': panel, 'box': [130, 255, 450, 545], 'stroke_width': 4}]
    for part, endpoints, arrow in (
            (angle_range, ([1090, 145], [1090, 655]), False),
            (beam, ([1010, 310], [1010, 490]), True)):
        primitives.extend({'type': 'polyline', 'part': part, 'points': [origin, endpoint],
                           'stroke_width': 4, 'arrow': arrow} for endpoint in endpoints)
    return {'width': 1200, 'height': 800, 'primitives': primitives, 'nodes': [], 'edges': []}


def panel_beam_reference():
    """Unlabelled layout reference from the same four-ray vector convention."""
    from .annotations import regular_font
    from .diagrams import render_diagram
    from .images import prepare_reference

    diagram = panel_range_diagram('panel', 'range', 'beam')
    for primitive in diagram['primitives']:
        primitive.pop('part')
    rendered = render_diagram({'parts': []}, {'parts': [], 'diagram': diagram},
                              font_factory=regular_font)
    from io import BytesIO
    from PIL import Image
    from .annotations import png

    with Image.open(BytesIO(rendered['png'])) as image:
        return prepare_reference(png(image.resize((1536, 1024), Image.Resampling.LANCZOS)))


RULES=[
 ('channel-contour',['通道','外壁','分隔壁','导线','轮廓','弯折'],['通道','外壁','分隔壁'],
  '从通道开口侧选视角，清楚画出外壁、分隔壁和连续通道边界，轮廓随导线弯折；避免用画出弯线代替通道，不新增无源盖板遮住内部。'),
 ('pin-hole-fit',['安装销','可变形肋','PCB','镀覆','接地孔'],['安装销','销','孔'],
  '清楚区分实体安装销、销上可变形肋、PCB镀覆接地孔；不得把孔当作销，不添加螺栓/耳片/套筒代替销。总图遮挡时保留对应局部空间视图，不透视补造未知内部，不确定数量/尺寸。'),
 ('cable-side-opening',['电缆侧','开口','焊接','屏蔽层','排流线','或','PCB'],['电缆侧','开口','屏蔽'],
  '电缆侧预留焊接开口应显示对屏蔽层或排流线焊接位置的可达关系；开口不得移至PCB侧。或不改为同时必须或互斥，不添加未经披露的盖板/紧固件/焊料形状。'),
]


def _material_source_facts(bundle, facts):
    quotes_by_page = {}
    contact_ids = set()
    heat_ids = set()
    for fact in facts:
        for evidence in fact['evidence']:
            quotes = quotes_by_page.setdefault(evidence['page_id'], {})
            for clause in re.split(r'[。；;！？!?]', compact(evidence['quote'])):
                quotes.setdefault(clause, set()).add(fact['id'])
                if _FLAKE_ORIENTATION.fullmatch(clause):
                    contact_ids.add(fact['id'])
                if _FILM_HEAT.fullmatch(clause):
                    heat_ids.add(fact['id'])
    # Adjacent excerpts must reconstruct one cited clause on the same source page.
    for page in bundle['pages']:
        quotes = quotes_by_page.get(page['id'])
        if not quotes:
            continue
        for contact, orientation in _FLAKE_ORIENTATION_ON_PAGE.findall(compact(page['text'])):
            if contact in quotes and orientation in quotes:
                contact_ids.update(quotes[contact], quotes[orientation])
    return contact_ids, heat_ids


def beam_fact_ids(bundle, facts):
    """Join split citations only where the source explicitly links the clauses."""
    for fact in facts:
        quote = ' '.join(e['quote'] for e in fact['evidence'])
        if all(term in quote for term in PANEL_BEAM_TERMS):
            return [fact['id']]
    pages = {p['id']: compact(p['text']) for p in bundle['pages']}
    for first in facts:
        for left in first['evidence']:
            lead = compact(left['quote']).rstrip('，,。')
            if not all(term in lead for term in PANEL_BEAM_TERMS[:2]):
                continue
            for second in facts:
                if first['id'] == second['id']:
                    continue
                for right in second['evidence']:
                    follow = compact(right['quote'])
                    if (left['page_id'] == right['page_id'] and PANEL_BEAM_TERMS[2] in follow
                            and follow.startswith(('其中包含', '其中包括', '该范围包含', '该范围包括'))
                            and any(lead + join + follow in pages.get(left['page_id'], '')
                                    for join in ('', '，', ',', '。'))):
                        return sorted((first['id'], second['id']))
    return []


def requirements(bundle,plan,view):
    valid=[f for f in plan.get('facts',[])if f.get('status')=='confirmed'and not any(x['verdict']=='FAIL'for x in validate_facts(bundle,[f]))]
    text=' '.join(e.get('quote','')for f in valid for e in f.get('evidence',[]))
    selected=' '.join(p['name']for p in plan.get('parts',[])if p['id']in view.get('parts',[]))
    context=view.get('instruction','')+' '+view.get('purpose','')+' '+selected
    overview=(view.get('requested_type')=='assembly' or view.get('purpose')=='assembly'
              or (not view.get('depends_on') and any(t in context for t in ['空间总图','总体结构','整体装配'])))
    selected_scope={
        'channel-contour':any(t in selected for t in ['通道','外壁','分隔壁']),
        'pin-hole-fit':any(t in selected for t in ['安装销','可变形肋','接地孔']),
        'cable-side-opening':any(t in selected for t in ['焊接开口','预留开口']),
    }
    features=[]
    if (bundle.get('figure_presence') == 'absent' and view.get('engine') == 'image'
            and view.get('requested_type') == view.get('delivered_type') == 'relationship'
            and 'chemical_material' in plan.get('technical_types', [])):
        selected_ids = set(view.get('required_facts', []))
        contact_ids, heat_ids = _material_source_facts(bundle, [f for f in valid if f['id'] in selected_ids])
        if contact_ids and heat_ids:
            for ident, ids in ((MATERIAL_CONTACT, contact_ids),
                               (MATERIAL_HEAT_PATH, contact_ids | heat_ids),
                               (MATERIAL_THICKNESS, heat_ids)):
                features.append({'id': ident, 'required_visibility': MATERIAL_HEAT_FEATURES[ident],
                                 'source_fact_ids': sorted(ids), 'verdict': 'UNKNOWN'})
    for ident,triggers,local,description in RULES:
        scoped=selected_scope[ident] if selected else any(t in context for t in local)
        if all(t in text for t in triggers)and(overview or scoped):
            fids=[f['id']for f in valid if any(t in e.get('quote','')for e in f.get('evidence',[])for t in triggers)]
            features.append({'id':ident,'required_visibility':description,'source_fact_ids':fids,'verdict':'UNKNOWN'})
    if (bundle.get('figure_presence') == 'absent' and view.get('engine') == 'image'
            and view.get('requested_type') == 'relationship' and 'communication' in plan.get('technical_types', [])):
        selected_ids = set(view.get('required_facts', []))
        relevant = beam_fact_ids(bundle, [f for f in valid if f['id'] in selected_ids])
        if not relevant and '波束' in context and '范围' in context:
            # Legacy evidence can match a single complete fact. Split facts
            # must both be selected by this view.
            relevant = next((beam_fact_ids(bundle, [f]) for f in valid if beam_fact_ids(bundle, [f])), [])
        if relevant:
            features.append({
                'id': PANEL_BEAM_ORIGIN, 'source_fact_ids': relevant, 'verdict': 'UNKNOWN',
                'required_visibility': (
                    PANEL_BEAM_LAYOUT
                    + '面板只画一块闭合、正对画布的二维矩形。上边和下边水平、左边和右边竖直，四角是直角。'
                    '不要倾斜矩形或画成平行四边形、梯形、斜视投影及带厚度的板。'
                    '在朝向波束的一侧矩形边缘画一个可见的共同起点。'
                    '两条边界和两条带箭尖的波束线均从同一点出发，各线连续清晰；范围边界轻微弯曲时仍要保持与同一点相接。'
                    '边界线不带箭尖，两支箭的箭尖严格落在两条边界之间。'
                    '不要从面板上下两处起线，不要让箭尾悬空。'
                    '这个点只是非比例的绘图基准，不表示真实相位中心、坐标原点、角值或设备尺寸。'
                    + PANEL_BEAM_BASE_INK),
            })
    return {'view_id':view['id'],'features':features,'visual_status':'UNKNOWN',
            'limitations':'清单仅为请求义务，不证明图中已实现；实际图件及跨图身份需独立视觉/专业核查。未知数量、尺寸及隐藏几何不得补造；只有锚点人工核准后才可标注改号。'}


def bind_mixed_communication_marks(plan, changes):
    """Add visible references where both source-bound beam geometry and actors are known."""
    if plan.get('part_mark_mode') == 'name_only':
        return []
    parts = {p['id']: p for p in plan['parts']}
    recognized = {
        '发射设备': 'transmit', '发射端': 'transmit',
        '接收设备': 'receive', '接收端': 'receive',
    }
    sequence_marks = []
    for view in plan['views']:
        if view['engine'] != 'vector' or view.get('requested_type') not in {'sequence', 'interaction'}:
            continue
        roles = {}
        for pid in view['parts']:
            role = recognized.get(parts[pid]['name'])
            if not role or not parts[pid]['label']:
                continue
            nodes = [node for node in view['diagram'].get('nodes', [])
                     if node['text'].strip() in {parts[pid]['name'], '{{' + pid + '.name}}'}]
            if len(nodes) == 1 and role not in roles:
                roles[role] = (pid, nodes[0])
        if len(roles) == 2:
            sequence_marks.append((view['id'], roles))
    if not sequence_marks:
        return []

    range_views = {row['view_id'] for row in changes if row.get('layout') == 'panel-relative-beam-range'}
    updated = []
    for view in plan['views']:
        if view['id'] not in range_views:
            continue
        primitives = view['diagram']['primitives']
        for index, target, at in [(0, [200, 255], [175, 163]), (1, [930, 209], [970, 132]),
                                   (3, [850, 336], [865, 271])]:
            shape = primitives[index]
            shape['id'] = 'numbered_beam_shape_' + str(index)
            primitives.append({'type': 'label', 'part': shape['part'], 'at': at, 'target': target,
                               'target_shape': shape['id'], 'font_size': 34})
        updated.append(view['id'])
    if updated:
        for fid, roles in sequence_marks:
            for pid, node in roles.values():
                node['text'] = '{{' + pid + '.name}} {{' + pid + '.label}}'
            updated.append(fid)
    return updated
