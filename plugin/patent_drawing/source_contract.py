"""Evidence locations and view obligations; citation validity is not truth."""
import re


def issue(rule, verdict, message):
    return {'rule': rule, 'verdict': verdict, 'message': message}


def compact(s):
    return re.sub(r'\s+', '', s)


COMMUNICATION_CHAIN = (
    '监测控制单元', '计算机总线', '通信模块', '网络交换机', '中央交换机', '工业计算机',
)
MODE_COMPONENTS = ('调制模式判定', 'PWM输出')
BEAM_MESSAGES = ('范围指示', '参考信号', '测量反馈')

_LOCAL_DATA_EXCHANGE = re.compile(r'通信模块通过计算机总线与监测控制单元交换数据')
_DATA_RETURN = re.compile(r'运行数据上送工业计算机[，,]工业计算机命令沿相应路径返回(?:监测控制)?单元')
_SELECTED_DATA_PATH = re.compile(r'通信模块(?:可以)?经网络交换机接入中央交换机[，,]再与工业计算机相连')
_EACH_COLUMN_EXCHANGE = re.compile(r'每(?:一)?列(?:的)?' + _LOCAL_DATA_EXCHANGE.pattern)
_MULTI_COLUMN_PATH = re.compile(r'多列(?:的)?' + _SELECTED_DATA_PATH.pattern)
_PWM_MODE_INFLUENCE = re.compile(
    r'保留电流反馈和调制模式判定构成的运行闭环[；;](?:同步[/／或、]非同步)PWM的具体判定参数需要标定'
    r'|调制模式判定(?:结果)?(?:决定|确定|选择|输入|控制)(?:同步[/／或、]非同步)?PWM(?:输出|模式)?'
)
_BEAM_ANGLE = re.compile(r'发射设备定义相对于自身天线面板的角度范围[，,]其中包含至少两个发射波束方向')
_BEAM_RANGE_MESSAGE = re.compile(r'(?:并把该|发射设备将该)范围指示发送给接收设备')
_BEAM_SELECT = re.compile(r'接收设备根据该范围选择接收波束候选')
_BEAM_MEASURE = re.compile(
    r'发射设备发送参考信号[，,]接收设备使用选定波束进行接收和测量[，,]再向发射设备回传测量反馈'
)
_SYNC_VIEW_CLAUSES = {
    'sync_acknowledgment': (
        r'将修改写入本地待同步队列[，,]记录任务ID[、,，]操作ID(?:[、,，]和?|和)本地序号',
        r'本地序号(?:只|仅)在本终端内递增',
        r'服务端按操作ID去重[；;，,]重复请求返回既有结果[，,]不重复应用',
        r'终端收到确认后才移除对应队列项',
    ),
    'sync_incremental': (
        r'终端拉取服务器修订号之后的变更并应用',
        r'本地未获确认的队列不得被覆盖',
    ),
    'sync_field_conflict': (
        r'并发修改同一字段时保留两个版本并提示用户选择',
    ),
    'sync_account_isolation': (
        r'账号切换时(?:必须|应当|应)隔离各账号的队列',
    ),
}
_CABINET_VIEW_CLAUSES = {
    'cabinet_functional_zones': (
        r'柜内设置供电列和连接列[，,]连接列装有与负载对应的监测控制单元',
        r'主供电经保护单元送至相应(?:监测控制)?单元与负载',
        _EACH_COLUMN_EXCHANGE,
    ),
    'cabinet_auxiliary_rail': (
        r'通信模块内设供电板[，,]向监测控制单元提供辅助供电',
        r'主母线[、，,]辅助供电轨及通信链路分别标识',
    ),
}
_CABINET_RAIL_PATH = (
    r'供电板(?:的输出端)?经(?:由)?辅助供电轨向监测控制单元(?:的辅助电源输入端)?(?:提供辅助供电|供电)',
)


def _positive_location(page, source, indices, normalized, pattern):
    for match in re.finditer(pattern, normalized):
        prefix = re.split(r'[。；;，,！!？?]', normalized[:match.start()])[-1]
        suffix = re.split(r'[。；;，,！!？?]', normalized[match.end():], maxsplit=1)[0]
        if (re.search(r'不是|并非|未|不|待|禁止|反例|示例|假设|尚|仅在', prefix[-24:])
                or re.search(r'^(?:是否)?(?:仍|尚)?(?:待核|待确认|未确认|未确定|是错误示例)', suffix)):
            continue
        begin, end = indices[match.start()], indices[match.end() - 1] + 1
        return {'page_id': page['id'], 'source_sha256': page['source_sha256'],
                'span': [begin, end], 'quote': source[begin:end]}
    return None


def _visible_source_pages(bundle):
    visible = {source['sha256'] for source in bundle.get('sources', ())
               if source.get('visibility', 'generation_visible') == 'generation_visible'}
    for page in bundle.get('pages', ()):
        if page.get('source_sha256') in visible:
            source = page.get('text', '')
            indices = [index for index, char in enumerate(source) if not char.isspace()]
            yield page, source, indices, ''.join(source[index] for index in indices)


def source_view_evidence(bundle, technical_types):
    """Locate all source clauses needed by a fixed qualitative view."""
    types = set(technical_types)
    clauses = {}
    if 'computer_method' in types:
        clauses.update(_SYNC_VIEW_CLAUSES)
    if 'electrical' in types:
        clauses.update(_CABINET_VIEW_CLAUSES)
        clauses['cabinet_rail_path'] = _CABINET_RAIL_PATH
    pages = list(_visible_source_pages(bundle))
    located = {}
    for role, patterns in clauses.items():
        evidence = [next((locator for page in pages if (locator := _positive_location(*page, pattern))), None)
                    for pattern in patterns]
        if all(evidence):
            located[role] = evidence
    return located


def drawing_relationships(bundle, technical_types, *, bind_instances=False):
    """Derive scoped qualitative relationships from positive, visible source clauses."""
    types = set(technical_types)
    if not types & {'electrical', 'communication'}:
        return []

    rows = []
    for page, text, indices, normalized in _visible_source_pages(bundle):
        def locate(pattern):
            return _positive_location(page, text, indices, normalized, pattern)

        exchange, return_path, selection = (locate(pattern) for pattern in (
            _LOCAL_DATA_EXCHANGE, _DATA_RETURN, _SELECTED_DATA_PATH))
        if exchange:
            chain = COMMUNICATION_CHAIN if return_path and selection else COMMUNICATION_CHAIN[:3]
            per_column = locate(_EACH_COLUMN_EXCHANGE) if bind_instances else None
            convergence = locate(_MULTI_COLUMN_PATH) if per_column and len(chain) > 3 else None
            for index, (source, target) in enumerate(zip(chain, chain[1:]), 1):
                evidence = [exchange] if index < 3 else [exchange, return_path, selection]
                row = {'id': f'communication_link_{index}', 'kind': 'communication',
                       'direction': 'both', 'source_name': source, 'target_name': target,
                       'evidence': evidence}
                if index < 3 and per_column:
                    row['repeated_roles'] = [source, target]
                    evidence.append(per_column)
                elif index == 3 and convergence:
                    row['repeated_roles'] = [source]
                    evidence.extend([per_column, convergence])
                rows.append(row)
        if 'electrical' in types and (evidence := locate(_PWM_MODE_INFLUENCE)):
            rows.append({'id': 'pwm_mode_influence', 'kind': 'pwm',
                         'direction': 'forward', 'source_name': MODE_COMPONENTS[0],
                         'target_name': MODE_COMPONENTS[1], 'evidence': [evidence]})
        if 'communication' in types:
            evidence = [locate(pattern) for pattern in (_BEAM_ANGLE, _BEAM_RANGE_MESSAGE, _BEAM_SELECT, _BEAM_MEASURE)]
            if all(evidence):
                rows.append({'id': 'beam_transfer_views', 'kind': 'beam', 'evidence': evidence})
    # The same relation can appear on several pages; preserve all locators.
    merged = {}
    for row in rows:
        if previous := merged.get(row['id']):
            previous['evidence'].extend(row['evidence'])
            if 'repeated_roles' in row:
                previous['repeated_roles'] = row['repeated_roles']
        else:
            merged[row['id']] = row
    return list(merged.values())


_DENIED_RESTRICTION = re.compile(
    r'(没有证据(?:证明|表明|支持)|未(?:声明|规定|要求|限定|确认|披露|说明)'
    r'|不(?:要求|限定)|不能从原文推定|不得(?:推定|认定)|并非|不是|不)'
    r'(?:二者|两者|两个方案|两种方案|两个方向|相互|必须|一定|其中|的)*$')


def _exclusive_assertions(text, *, source_quote=False):
    """Bounded lexical guard, not a general entailment/semantic approval.

    A source-scoped denial of a restriction does not assert mutual compatibility.
    Ordinary positive/negative restrictions still need a matching source claim.
    """
    text=compact(text);claims=set()
    for occurrence in re.finditer(r'两者择一|择一|互斥|仅允许',text):
        if occurrence.group()=='择一'and not source_quote:continue  # Ordinary user selection is not a new exclusivity trigger.
        prefix=text[:occurrence.start()]
        clause=re.split(r'[，,。；;：:!?！？\n]',prefix)[-1]
        negative=_DENIED_RESTRICTION.search(clause)
        polarity=True
        if negative:
            lead=clause[:negative.start()]
            double=lead.endswith(('不是','并非','并不是','不能','不得','不可','没有'))
            polarity=double
            sentence=re.split(r'[。；;!?！？\n]',prefix)[-1]
            source_scoped=any(word in sentence for word in ('原文','交底','材料','来源'))
            denies_assertion=any(word in negative.group(1)for word in ('声明','规定','要求','限定','确认','披露','说明','证据','推定','认定'))
            if source_scoped and denies_assertion and not double:continue
        term=occurrence.group().replace('两者择一','择一')
        claims.add((term,polarity))
    return claims


def validate_facts(bundle, facts):
    pages = {p['id']: p for p in bundle['pages']}; out = []; ids = set()
    if not isinstance(facts, list) or len(facts) > 160:
        raise ValueError('fact capacity exceeded')
    for fact in facts:
        fid = fact.get('id')
        if not isinstance(fid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', fid) or fid in ids:
            raise ValueError('fact identity invalid')
        ids.add(fid)
        if fact.get('status') not in {'confirmed', 'unknown', 'conflict'}:
            raise ValueError('fact status invalid')
        if fact['status'] != 'confirmed':
            out.append(issue('SOURCE-UNKNOWN', 'UNKNOWN', f'{fid}尚未确认或存在冲突。')); continue
        evidence = fact.get('evidence', [])
        valid = bool(evidence)
        for e in evidence:
            page = pages.get(e.get('page_id'))
            quote = e.get('quote', '')
            valid = valid and page is not None and bool(quote) and compact(quote) in compact(page['text'])
            if _exclusive_assertions(fact.get('text','')) - _exclusive_assertions(quote,source_quote=True):
                valid = False
        if not valid:
            out.append(issue('SOURCE-EVIDENCE', 'FAIL', f'{fid}缺有效来源页/原文片段或增加无源排他限定。'))
    return out


def validate_view_plan(facts, instances, views):
    out = []; ids = {v['id'] for v in views}; fids = {f['id'] for f in facts}
    if len(ids) != len(views) or not 1 <= len(views) <= 24:
        raise ValueError('view identity/count invalid')
    for v in views:
        requested = v.get('requested_type'); delivered = v.get('delivered_type')
        if v.get('expression_mode')=='relational_schematic'and not v.get('parts'):
            out.append(issue('VIEW-PARTS','FAIL',f'{v["id"]}关系空间图缺稳定部件ID；无编号底图不能清空部件身份。'))
        if requested != delivered:
            out.append(issue('VIEW-TYPE', 'FAIL', f'{v["id"]}要求{requested}，实际{delivered}；降级不能自动满足原需求。'))
        if delivered in {'section', 'detail'}:
            parent = v.get('parent_view')
            mapping = v.get('cut') if delivered == 'section' else v.get('region')
            # An original supplied section can be reproduced as a source view;
            # a newly derived section must have an actual parent and cut.
            source_section = v.get('source_section') and v.get('engine') == 'source' and bool(v.get('source_region_id'))
            if not source_section and (parent not in ids or not mapping):
                out.append(issue('VIEW-MAPPING', 'FAIL', f'{v["id"]}缺父图及剖切/放大区域映射。'))
        if set(v.get('required_facts', [])) - fids:
            out.append(issue('VIEW-FACTS', 'FAIL', f'{v["id"]}引用不存在的事实。'))
    return out


def validate_source_update(old, new, confirmed_fact_ids=()):
    if old.get('source_sha256') != new.get('source_sha256'):
        raise ValueError('new source version requires replan and fresh approval')
    newer = {f['id']: f for f in new.get('facts', [])}
    for old_fact in old.get('facts', []):
        f = newer.get(old_fact['id'])
        if f is None:
            raise ValueError('source fact/unknown cannot be silently removed')
        if old_fact['status'] != 'confirmed' and f.get('status') == 'confirmed' and old_fact['id'] not in confirmed_fact_ids:
            raise ValueError('unknown promotion requires explicit source/user confirmation')


def validate_detail_bindings(bindings, rendered, allowed_evidence_ids):
    """Validate bidirectional locators against rendered figure identity and regions."""
    from .spatial_guide import box_valid, point_in_box

    findings = []
    seen = set()

    def add(verdict, row, reason):
        findings.append({'rule': 'DETAIL-BINDING', 'verdict': verdict,
                         'view_id': row.get('child_view_id'), 'message': reason})

    for row in bindings:
        parent_id, child_id = row.get('parent_view_id'), row.get('child_view_id')
        locator = row.get('locator')
        parent, child = rendered.get(parent_id), rendered.get(child_id)
        if (not parent or not child or parent_id == child_id or (parent_id, locator) in seen
                or not isinstance(locator, str) or not re.fullmatch(r'[A-Z][0-9]?', locator)
                or row.get('representation') not in {'projection', 'schematic', 'crop'}):
            add('FAIL', row, '局部图身份、索引或表达方式不明确。')
            continue
        seen.add((parent_id, locator))
        features = set(row.get('feature_ids', []))
        for side, figure in [('parent', parent), ('child', child)]:
            region = row.get(side + '_region')
            targets = row.get(side + '_targets', {})
            if (row.get(side + '_artifact_sha256') != figure['sha256']
                    or not box_valid(region, *figure['size'])
                    or not point_in_box(figure.get('locators', {}).get(locator), region)
                    or not features or features != set(targets)
                    or any(not point_in_box(point, region)
                           or not point_in_box(point, figure.get('targets', {}).get(feature))
                           for feature, point in targets.items())):
                add('FAIL', row, '总图或局部图的区域、实际索引、引线落点或图件版本不匹配。')
        evidence = set(row.get('source_evidence_ids', []))
        if not evidence:
            add('UNKNOWN', row, '局部关系缺少来源定位。')
        elif evidence - allowed_evidence_ids:
            add('FAIL', row, '局部关系引用了未获准的来源。')
    status = 'FAIL' if any(row['verdict'] == 'FAIL' for row in findings) else 'UNKNOWN' if findings or not bindings else 'PASS'
    return {'status': status, 'findings': findings}


def detail_caption_updates(views, review, required_children):
    numbers = {view['id']: i for i, view in enumerate(views, 1)}
    updates = {fid: [] for fid in numbers}
    bound = set()
    if review.get('status') == 'PASS':
        for row in sorted(review['bindings'], key=lambda item: item['locator']):
            parent, child, locator = (row[k] for k in ('parent_view_id', 'child_view_id', 'locator'))
            updates[parent].append(f'{locator} 对应图{numbers[child]}。')
            updates[child].append(f'本图对应图{numbers[parent]} 的 {locator}。')
            bound.add(child)
    for fid in required_children - bound:
        updates[fid].append('本图为定性示意，总图位置待核。')
    return {fid: ' '.join(notes) for fid, notes in updates.items() if notes}
