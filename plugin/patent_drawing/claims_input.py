"""Keep case prose and figures outside the claims-only planning requests."""
import hashlib
import re
from collections import defaultdict

from .claims_relations import RELATION_CONTRACT, _find_span, part_id, reviewed_relations
from .claims_flow import verified_flow


NUMBER = re.compile(r"(?m)^(?:\*\*)?([1-9][0-9]*)[.．、](?:\*\*)?[ \t]*")
REFERENCE = re.compile(r"权利要求[ \t]*([1-9][0-9]*(?:[ \t]*(?:[-–—至、，,]|或|和|及)[ \t]*[1-9][0-9]*)*)")
VIEW_TYPES = {'overall', 'detail', 'section', 'structure', 'flow', 'system', 'topology',
              'sequence', 'relationship', 'evidence'}
FIGURE_NUMBER = re.compile(r'^图\s*[0-9]+[.．、:：\s]*')
REFERENCE_TOKEN = re.compile(r'[0-9]+|[-–—至]')
IMPACTS = {'required', 'geometry', 'optional', 'review'}
CLAIMS_VIEW_CITATION_LIMIT = 96


def _references(claim):
    numbers = []
    for references in REFERENCE.findall(claim):
        ends = REFERENCE_TOKEN.findall(references)
        for index, token in enumerate(ends):
            if token.isdigit():
                numbers.append(int(token))
            elif 0 < index < len(ends) - 1:
                start, stop = int(ends[index - 1]), int(ends[index + 1])
                if not 0 < start <= stop <= 150:
                    raise ValueError('从属权项范围无效。')
                numbers.extend(range(start + 1, stop))
    return list(dict.fromkeys(numbers))


def read_claims_text(text):
    if not isinstance(text, str) or len(text) > 64000 or '\0' in text:
        raise ValueError('权利要求正文为空、超限或含无效字符。')
    text = text.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    text = re.sub(r'^(?:#{1,6}\s+)?权利要求书(?:正文)?[ \t]*\n+', '', text)
    text = re.sub(r'(?m)^\*\*([1-9][0-9]*\.)\*\*', r'\1', text)
    matches = list(NUMBER.finditer(text))
    if (not matches or matches[0].start() or len(matches) > 150
            or re.search(r'(?m)^(?:[ \t]*#|[ \t]*!\[|[ \t]*(?:说明书|技术交底书|摘要|附图说明)[ \t]*$)', text)):
        raise ValueError('请提交边界明确、连续编号的权利要求正文。')
    ranges = {}
    for i, match in enumerate(matches, 1):
        end = matches[i].start() if i < len(matches) else len(text)
        claim = text[match.end():end].strip()
        if int(match[1]) != i or not claim:
            raise ValueError(f'第 {i} 项权利要求缺失、为空或未连续编号。')
        if any(n >= i for n in _references(claim)):
            raise ValueError(f'第 {i} 项的从属引用不是在前权项。')
        ranges[i] = (len(text[:match.start()].encode()), len(text[:end].rstrip().encode()))
    return text, ranges


def claims_system(routing, *, relation_contract=2, compact_recovery=False):
    from .figure_routing import FAMILIES, semantic_instructions

    if relation_contract >= 11:
        selected = '\n'.join(FAMILIES[name]['instruction'] for name in routing['selected']) if routing else ''
        endpoints = ('s/o为两端的逐字名称，各用含该名称的短引，与引文完全相同时写null'
                     if relation_contract >= 12 else 's/o为两端名字，与相应引文完全相同时写null')
        guard = ('守卫优先用本支完整谓词作literal，items=[]。拆分时保留原连词和否定。共用前提与本支触发分开，'
                 '父项前提沿实际依赖链引用，独立权项各自锚定。'
                 if relation_contract >= 12 else
                 '条件优先完整引用一个literal，items=[]。拆分时保留所有谓词、原连词及否定；作用域和触发条件分别核对。')
        branches = ('双路独立引用各自输入、声明、共同汇合及后续动作。分别保留接纳、拒绝、待查的触发和出口。'
                    '恢复条款分别保留每个明确结果，不推断优先级。'
                    if relation_contract >= 12 else
                    '双路分别写输入、汇合和后续动作。保留接纳、拒绝、待查、独立恢复及各自出口，不推断优先级。')
        guard_schema = ('h:0只表示无条件，h:null表示条件缺证。守卫树每个节点（含all根）填op、source和items。'
                        'source为从1开始的引证编号。all根引同一作用域的完整合取，'
                        '有完整本支谓词时优先作逐字literal。'
                        if relation_contract >= 13 else
                        'h为守卫树，每节点 op、source引证编号、items子项；原文无条件写0，无法确定写null。')
        specific = ('提示、参考发送、有效反馈各自写message，保留原文角色、方向、明确通道、逐次配对与旧轮排除。'
                    '相同处置的择一异常用一个完整守卫，连同后续必要条件，分别保留实际动作和独立退出。')
        choices = routing.get('semantic_choices') if routing and relation_contract >= 14 else None
        if choices is not None:
            branches = '每个独立动作和出口均按正文保留。分别引用本支端点、动作和完整守卫。'
            specific = ('保留正文中的方向、必要前提和从属限定。明示收发及反馈用message。'
                        '分流结果仅用于选择规划重点。'
                        + semantic_instructions(choices))
        if routing and routing.get('luna_question_revision', 1) >= 2:
            specific += ('按逐字引文核对原文要求表达的方向、范围和作用。关系图候选仅提示另设视图的可能性。'
                         '如现有图能完整表达，将关系与引文放入对应图；确有剩余表达任务时单独成图。')
        if relation_contract >= 16:
            from .figure_routing import RELATIONSHIP_TOPICS, EXPRESSION_CHOICES

            labels = {option['value']: option['description'] for option in EXPRESSION_CHOICES}
            tasks = (routing or {}).get('relationship_tasks', {})
            specific += (
                'relationship_coverage 用 {t,v,e} 逐项说明关系落在哪张实际图。t 填关系类别，'
                'v 为图序号，e 为该图 features 中足以支撑对象、方向、边界和必要条件的逐字引证编号数组。'
                '每项必要关系须有实际图位和引文；无证或图位未定时保留待核。'
                '定性方向、边界连续性或两个域的空间对应有独立表达任务时，合并为一张关系示意图。'
                '步骤名、连接名或公式文字在图中出现不等于几何对应已表达。实体支撑和区域分界可由同一剖面或'
                '方法图充分承载时，合并到这些图。可按原文纠正上游分类。'
                + ''.join(f'{name}为{topic}，' + (
                    f'上游核对{labels[tasks[name]["expression"]]}，'
                    if name in tasks and tasks[name]['state'] != 'absent' else '')
                    for name, (topic, _) in RELATIONSHIP_TOPICS.items()))
        if relation_contract >= 17:
            specific += (
                '先完成必要图位及关系覆盖映射。区域分区和条件处置优先落入既有流程图；实体区域边界、连续支撑及局部结构尺寸'
                '限定优先落入能表达同一对象的既有结构或剖面图。迁移完整引文、关系视图和图间链接后合并重复图。'
                '同一处理过程的后续分支应分别标明原文中的过程端点、实际动作与本支守卫。'
                '仅对有独立技术限定的权项分别列关系和流程边；沿实际从属链复用相同关系，图内引证与覆盖保留。'
            )
        if relation_contract == RELATION_CONTRACT:
            specific += (f'每图最多 {CLAIMS_VIEW_CITATION_LIMIT} 条逐字逻辑引文；同一权项同一原文位置复用引文编号。'
                         '保留必要的短引、角色和守卫。')
        if compact_recovery:
            specific += (
                '压缩续做。生成一份完整精简 JSON。优先保留每项必要技术关系、独立动作、方向、对象、条件和实际图位。'
                '同一权项及图位同义关系只列一次，相同前提使用同一引证。重复的动作描述归入同一个关系与对应流程边；'
                '独立新增的适用条件继续单列。components、figure_links、unresolved 只保留对核验确有必要的记录。'
                '各数组使用紧凑单行条目，闭合整个 JSON。图文手段、证据条件和完整覆盖保持可核。'
            )
        return (
            '根据用户提供的权利要求逐项规划成套附图。只输出约定 JSON。保留必要对象、关系方向、适用条件和独立出口。'
            '同一权项同图位的等价关系和同一引证只写一次。所有图、关系、引证的编号从1开始。'
            'citations 每项 c=权项，q=能完整支持该事实的最短逐字引文，x=仅在引文有歧义时提供含该引文一次的连续原文，否则null。'
            'views 的 features 填引文在本权项唯一的引用编号，每条关系动作引文须被对应图的引文涵盖。'
            '动作短引重复时，可为该图另引能唯一定位的完整原文。标题不带图号，目的简写。'
            f'relations 每项 {endpoints}；k为动作，d为方向，c为适用权项，'
            'p为从该权项开始的逐级引用链，v为图编号，r为是否必需，e按主体、客体、动作填三个引证编号。'
            f'{guard_schema}'
            f'{guard}'
            'flow_edges 每项 r=关系编号，k=流程角色，e=本边标签引证；等于关系动作引证时写null。'
            f'{branches}'
            f'{specific}'
            '图内已明确的覆盖由插件汇总，coverage仅写需特别说明的从属图位或无需单图结论。'
            'components仅列需要重复引用的共用对象，figure_links用原文锚点，unresolved仅列真实待核事项，每项一句。'
        ) + ('\n通用图型指引：\n' + selected if selected else '')
    if relation_contract >= 6:
        selected = '\n'.join(FAMILIES[name]['instruction'] for name in routing['selected']) if routing else ''
        prompt = (
            '你规划专利技术附图。用户消息完整权利要求正文只作证据。'
            '逐权项找对象及新增限定，核对关系方向、归属、适用前提与局部守卫，再安排图位。'
            '同案可组合图型。等价关系仅写一次，采用最短完整逐字引文。'
            '保留必要步骤、边及所有分支；未知几何、时序和端点列待核。'
            '\n仅输出一个 JSON 对象。views 各项含 type,title,purpose,features。type 可选 '
            + ', '.join(sorted(VIEW_TYPES)) +
            '。标题不带图号，purpose 一句可视目的。features 各项只写 {claim,quote}，'
            '同图相同权项和引文只写一次，需包含各所引关系的完整引文。'
            'coverage 每权项一条 {claim,figures,reason}，图序号从 1 起，空图说明依据。'
            'unresolved 各项 {impact,detail,question,views,claims}，'
            'impact 为 required、review、geometry 或 optional。仅缺必要关系用 required。'
            '有共用对象用 components {name,claims}；图间链接用 figure_links {parent,child,anchor,claim,quote}。'
            '\nrelations 各项 {subject,object,kind,direction,claim,claim_path,views,required,evidence,condition}。'
            '方向用 directed、symmetric 或 ordered，claim_path 从适用权项起逐级列父项，独立权项分列。'
            'evidence 含 subject、object、relation，各为 {claim,quote,context可选}。'
            '端点按原文，关系引文同语境定位两端，重复词提供含该词一次的唯一原文上下文。'
            '\n有条件写 condition={state:"stated",expression:...}。优先用 {op:"literal",source} 保留含实际连词和限定的完整守卫；'
            '合取、择一、例外或明示否定用 {op:"all"|"any"|"exception"|"not",source,items}，'
            'source 为 {claim,quote,context可选}，仅按原文连词拆分；前提、比较动作与选择条件分列。分开共用前提、上游和本支守卫，'
            '保留原文否定、范围和优先关系。无条件写 {state:"none_in_source",operator:"none",terms:[]}；缺证写 state=unknown。'
            '\nflow_edges 各项 {relation,role,label}，relation 为从 1 起的 relations 序号，role 为 branch、message、transition 或 exit，'
            'label 为 {claim,quote,context可选}，引用实际动作或出口。分支和退出保留原文守卫。'
            '流程从输入至计算、更新、去向逐边表达；有正常、旁路、禁止输出时分开，并按状态原声明绑定。'
            '双路分别写输入、汇合和后续去向。参考与反馈各写动作、端点及原文明确的通道和配对；'
            '可达搜索、不可达退出、预算耗尽保留各自条件。'
            '结构或系统的同名成员按各自权项绑定不同所有者，完整引用归属或隔离关系。'
            '可复用图位，保留从属权项的新限定。不确定的项只写一个可定位的疑点和问题。'
        )
        if relation_contract >= 10:
            return prompt + (
                '\n逐边复核：同一权项、图位、端点、方向、动作及守卫的等价关系只列一次。'
                '每个 context 只用于消歧，须是含本字段 quote 一次的连续原文。'
                '角色引证可用同权项双方前提，动作各引自己的原文。'
                '分别列出提示、参考发送与测量反馈的 message，数据及计算用 transition。'
                '保留配对标识、有效性、旧轮排除和每次参考记录的完整字段；通道按实际动作原文。'
                '显式字段关联也单列关系和步骤。'
                '并行路径分别写输入、共同汇合点和后续动作。'
                '按同一权项与图位分开适用对象、触发守卫、局部动作和出口。'
                '身份、前序、命中、恢复、正负出口、待查、选择区域和可选流转各保留原文及对应边。'
                '对同一处置的择一异常保留一条完整守卫，连同后续必要条件引用一次；'
                '分别关联每个实际动作，保留不可达、预算耗尽和禁止输出的独立出口。'
                '按原文保留接纳谓词、排除约束和各分支极性。'
            ) + ('\n通用图型指引：\n' + selected if selected else '')
        if relation_contract >= 7:
            prompt += (
                '\n逐边核对后再输出：有多个图时，每个列出的图都要收录该边的完整关系引文；'
                '优先只在证据所在的一张图表达该边。图间锚点必须是对应权项引文中的逐字短语。'
                '同组动作的共用前提优先复用完整 literal，引用各动作自己适用的条件或例外。'
                '“对于某对象”是适用范围，后续命中、确认阶段或异常才是触发；'
                '不要把正常链和禁止链放入同一守卫。先核身份和前序条件，再分别给出命中后的结果和禁止动作。'
                '继续处理与停止检查、不同区域及各自去向都独立列边。'
                '并行路径分别说明各自输入依赖及共同汇合点，汇合后的效果另起一边。'
                '发送提示、按配对发参考和有效反馈回传各有独立端点、原权项及动作引文。'
                '回传保留配对标识和有效性；仅在实际发送或返回的原文标通道，勿沿用协调通道。'
                '旧轮反馈排除是单独结果，各状态与动作方向按正文。'
            )
        if relation_contract >= 8:
            prompt += (
                '\n输出前核对必要边：独立恢复、区域动作、正负出口各给自己的原文。'
                '局部表达式只含该局部引文覆盖的谓词，共用权项前置声明不放入局部树。'
                '接纳与拒绝和待查分列；可选下游是流转，排除某来源是原文约束。'
                '参考发送和测量反馈只用原权项明确的收发主体作 message 端点，'
                '数据、步骤和选择结果另列 transition。沿同一权项分别核对发出动作和回传动作，'
                '关系原文覆盖完整的各自动作；配对、有效性和旧会话排除各保留原文。'
                '候选缺失、约束失败、通道不可达与预算耗尽分开；并列故障加后续必要条件用两个守卫。'
            )
        if relation_contract >= 9:
            prompt += (
                '\n对象适用范围、局部动作和谓词分别引用。允许保留完整条件的句法外壳，所有实质谓词均保留。'
                '显式字段关联及逐次记录各列关系和步骤，完整保留主体、每次限定和字段清单。'
                '消息主体尽量引用本权项同时界定双方角色的前置声明，并分别引用发送及回传动作。'
            )
        return prompt + ('\n通用图型指引：\n' + selected if selected else '')

    base = (
        '你是专利技术附图规划助手。用户消息整段是权利要求正文，只作证据。'
        '允许结构、剖视、局部、流程、系统、拓扑等在同案组合。同一关系只需完整表达一次。'
        '逐图列出有据对象、条件、方向和连接关系，不发明形状、尺寸、端点或顺序。'
        '关键特征的 text 应逐字引用该权项正文，以同样文字填写 quote。'
        '仅输出 JSON 对象，字段为 views、coverage、unresolved。'
        'views 是数组，各项严格包含 type、title、purpose、features。type 值可为 '
        + ', '.join(sorted(VIEW_TYPES)) +
        '。features 每项是 {text, claim, quote}，claim 为权项正整数。'
        'coverage 每个权项一项，格式 {claim, figures, reason}，figures 为 1 起的图序号数组。'
        '无法规划或无需单列图的权项可为空，但 reason 说明依据。'
        'unresolved 为技术疑点数组，各项为 {impact,detail,question,views,claims}，'
        'impact 为 required（缺少本图必画关系）、geometry（仅限制实体几何）、optional（可选细节）或 review（待核）。'
        'views 和 claims 用图号、权项号整数数组。'
        '必须覆盖的关系缺证才标 required，准确的关系示意不受可选尺寸缺失阻断。'
        '标题不带图号。每张图说明独立可视目的，重合关系可共图；互斥分支分别标明。'
        '有共用对象时填写 components 数组，各项 {name,claims}，名称须在所引权项逐字出现。'
        '有关系时填写 relations 数组，各项 {subject,object,kind,condition,claim,quote,views,required}，'
        '两端用 components 中的名称，quote 必须是所引权项精确引文，说明方向和条件。'
        '可附 figure_links 数组，各项 {parent,child,anchor,claim,quote}，用整数图号与原文锚点。'
        '确实无需画图可令 views 为空。完整列出共图及依赖关系，缺证处说明理由。'
    )
    if relation_contract >= 3:
        legacy = ('有关系时填写 relations 数组，各项 {subject,object,kind,condition,claim,quote,views,required}，'
                  '两端用 components 中的名称，quote 必须是所引权项精确引文，说明方向和条件。')
        base = base.replace('仅输出 JSON 对象，字段为 views、coverage、unresolved。',
                            '仅输出 JSON 对象，含 views、coverage、unresolved、relations，可附 components 和 figure_links。')
        base = base.replace(legacy, (
            'relations 逐条写可绘制的关系，每项含 subject,object,kind,direction,claim,claim_path,views,required,evidence,condition。'
            'subject 与 object 使用原文名称，kind 表明具体关系。direction 为 directed（主体至客体）、'
            'symmetric（对称）或 ordered（主体在先）。claim_path 由适用权项起，逐级列出同一从属支路所需父项，不可拼接择一项。'
            'views 列图序号。required 仅在关系对理解技术方案不可缺时为 true，其他为 false。'
            'evidence 含 subject、object、relation 三个对象，各自为 {claim,quote}，quote 从相应权项逐字复制。'
            '关系引文所在句须能逐字定位两端，避免拼接各自处于独立陈述的对象。每个所列图的 features 至少有一条同权项的引用完整包含该关系引文。'
            '短词多次出现时，加 context 字段，逐字引用只出现一次且包含该词一次的上下文。'
            'condition 写 {state,operator,terms,description}。有条件用 state=stated，'
            'terms 每项 {claim,quote,polarity}，独立逐字引用条件，可加 context。'
            'polarity 取 positive、negative（留原文否定）或 exception（留原文例外）；'
            'operator 取 single、all、any 或 exception，分别表示一项、合取、择一或主条件和例外。'
            '无条件写 state=none_in_source,operator=none,terms=[]；无法核实写 state=unknown。'
            'description 可解释条件，但证据只能来自逐字 quote。保留重要缺证关系及其图号和权项，不推定不存在。'
        ))
    if relation_contract >= 4:
        first = base.index('condition 写 {state,operator')
        last = base.index('保留重要缺证关系及其图号和权项', first)
        base = base[:first] + '条件按下述新版契约。' + base[last:]
        base = base.replace('可附 components 和 figure_links', '可附 components、figure_links 和 flow_edges')
        base += (
            '\n本版条件按原文含义输出。stated 时使用 condition={state:"stated",expression:...}。'
            '确实无条件用 {state:"none_in_source",operator:"none",terms:[]}，待确认用 state=unknown。'
            '单一完整原文条件用 {op:"literal",source:{claim,quote,context可选}}，'
            '保留“无事件”“不可达”“未知”等原有谓词和状态，不再指定 polarity。'
            '复合条件优先整段逐字作为一个 literal；分解时用 {op:"all"|"any"|"exception"|"not",source,items}，'
            'source 是包含所有子项和实际连词的一段原文，items 是嵌套条件树。切忌把或合并为且，或在原文没有否定时补 NOT。'
            '若有流程或交互，在顶层加 flow_edges 数组，各项 {relation,role,label}。'
            'relation 是 relations 的 1 起序号，role 为 branch、message、transition 或 exit，'
            'label={claim,quote,context可选} 是对应的原文状态或事件。'
            '每个出口、转移或方向单列有向关系，branch 和 exit 必须带实际原文守卫。'
            '如果出现明确的三种以上输出模式，为每种模式绑定自己的守卫、输出和状态去向。'
            '保留正常窗口、有效基础输入下的旁路及保护或失效下输出禁止的区别。'
            '循环、重置、事件优先级只按权项。对收发交互分别建立每条参考和反馈边，'
            '按各权项绑定发送方、接收方、通道及匹配条件，不将反馈并入发射箭头。'
            '对有限预算搜索分别画通道可达时的搜索，不可达时的退出，预算耗尽时的失败，避免混并权项。'
            '不同所属对象的同名成员须明确各自归属。原文明确不同所有者的隔离关系可使用原有同名，'
            '保留整个归属与隔离原句作为 relation 引文，插件据此给两个通用实例分配独立标识。'
            '反馈和参考各自引用能区分信号方向的原文，状态名以声明这些状态的权项引文定位。'
            '重复信号名或状态名须给唯一原文上下文。'
            '对可用于图的关系引用简洁完整的原文，保持图内已有 features 与 relations 一致。'
        )
    if relation_contract >= 5:
        base += (
            '\n按同一结果枝分别定位适用前提、上游触发及出口局部守卫。'
            '跨分句时保留各逐字条件及对应动作的准确语境。对手段经过的中间步骤逐条连边，'
            '引用原声明、变量传递、输入输出和效果，不把共用前提写成无条件。'
            '区域、状态的两条分支分别核条件和效果，避免把上游前提与其他分支的守卫合并。'
            '输出主流程时，从输入到计算、更新、结果和后续去向逐边列出。'
            '独立终止、保留待查或禁止输出各有出口，保留原文的或、且及优先条件。'
            '声明了输出状态名时，用其权项内逐字标签指向相应输出边，'
            '包含终止型状态，附属状态不得改变主链守卫。'
            '存在多个独立方法或系统时，对每个权项分别提供同名队列或成员的所有权与切换／隔离原文。'
            '分列各路径的输入依赖、各自输出、汇合后的去向和可选步骤。'
            '按作用域分列参考发送、反馈回传、序列与并行能力及旧关联排除，'
            '每个消息标签含实际动作，通道仅在该动作的明示原文中引用。'
            '相同实施方式可以共图，独立权项仍各有端点和关系；逐个核查依赖权项的新限定。'
            '重复端点加包含其动作的唯一上下文。缺失的节点、方向、条件或适用权项保留为可定位的待核项。'
        )
    if not routing:
        return base
    return base + '\n适用的通用图型指引：\n' + '\n'.join(
        FAMILIES[name]['instruction'] for name in routing['selected']
    )


def claims_plan(text, raw, *, reviewed=False, relation_contract=2, response_sha256=None, relationship_tasks=None, _reviewed_views=False):
    text, ranges = read_claims_text(text)
    if reviewed:
        if relation_contract >= 11:
            from .claims_wire import derive_coverage, expand_wire_plan

            expanded = expand_wire_plan(raw, contract=relation_contract)
            derive_coverage(expanded, text.encode(), ranges)
            coverage = (raw, relationship_tasks or {}) if relation_contract >= 16 else None
            result = _reviewed_plan(text, ranges, expanded, relation_contract, response_sha256, coverage)
            result['wire_evidence'] = {
                'response_sha256': response_sha256,
                'citation_count': len(raw['citations']),
                'view_indices': [row['features'] for row in raw['views']],
                'relation_indices': [row['e'] for row in raw['relations']],
                'flow_indices': [row['e'] for row in raw['flow_edges']],
            }
            for issue in result.get('view_limit_issues', []):
                issue['wire_citation_indices'] = raw['views'][issue['number'] - 1]['features']
            if relation_contract >= 13:
                result['wire_evidence']['guard_issues'] = [
                    {'source_index': row['source_index'], 'candidate_id': row['candidate_id'],
                     'errors': row['wire_guard']['errors'], 'status': row['wire_guard']['status']}
                    for row in result['relation_candidates'] if 'wire_guard' in row
                ]
            return result
        return _reviewed_plan(text, ranges, raw, relation_contract, response_sha256)
    if not isinstance(raw, dict) or set(raw) != {'views', 'coverage', 'unresolved'}:
        raise ValueError('权利要求图单缺少有效字段。')
    if not all(isinstance(value, list) for value in raw.values()) or len(raw['views']) > 32:
        raise ValueError('权利要求图单结构或图数无效。')
    blob = text.encode()
    views, titles = [], set()
    max_view_features = CLAIMS_VIEW_CITATION_LIMIT if relation_contract == RELATION_CONTRACT else 60
    isolate_limit = _reviewed_views and relation_contract == RELATION_CONTRACT
    for i, view in enumerate(raw['views'], 1):
        if (not isinstance(view, dict) or set(view) != {'type', 'title', 'purpose', 'features'}
                or not isinstance(view['type'], str) or view['type'] not in VIEW_TYPES
                or not all(isinstance(view[k], str) and view[k].strip() for k in ('title', 'purpose'))
                or view['title'] in titles
                or not isinstance(view['features'], list) or not view['features']
                or not isolate_limit and len(view['features']) > max_view_features):
            raise ValueError(f'图 {i} 的图型、标题或特征清单无效。')
        titles.add(view['title'])
        features = []
        locations = {}
        rejected_features = []
        for feat in view['features']:
            if isolate_limit and isinstance(feat, dict) and set(feat)=={'invalid_citation_index'}:
                rejected_features.append({'reason':'citation_index_out_of_range','reference':feat})
                continue
            allowed = {'text', 'claim', 'quote'} | ({'context'} if isolate_limit else set())
            if (not isinstance(feat, dict) or set(feat) - allowed or not {'text','claim','quote'} <= set(feat)
                    or type(feat['claim']) is not int or feat['claim'] not in ranges
                    or not isinstance(feat['quote'], str) or not feat['quote'].strip()
                    or feat['text'] != feat['quote']):
                raise ValueError(f'图 {i} 的特征未使用原文证据。')
            start, end = ranges[feat['claim']]
            quote = feat['quote'].encode()
            offset = blob.find(quote, start, end)
            if offset < 0:
                if isolate_limit:
                    rejected_features.append({'reason':'citation_not_found','reference':feat})
                    continue
                raise ValueError(f'图 {i} 的证据不在权利要求 {feat["claim"]} 中。')
            feature = {'text':feat['text'],'claim':feat['claim'],'quote':feat['quote'],
                       'start':offset,'end':offset+len(quote)}
            if isolate_limit:
                anchored, _ = _find_span(blob, ranges, feat)
                if anchored:
                    feature.update(start=anchored['start'], end=anchored['end'])
                ambiguous = not anchored and blob.find(quote, offset + 1, end) >= 0
                feature['location_ambiguous'] = ambiguous
                location = (feat['claim'], feature['start'], feature['end'], ambiguous)
                locations.setdefault(location, feature)
            else:
                features.append(feature)
        if isolate_limit:
            features = list(locations.values())
            if rejected_features and _logical_citation_count(features) <= max_view_features:
                raise ValueError(f'图 {i} 有不可核对的引文。')
        views.append({**view,'id':f'fig{i}','number':i,'features':features,
                      **({'rejected_features':rejected_features} if rejected_features else {})})
    coverage = raw['coverage']
    if (len(coverage) != len(ranges)
            or any(not isinstance(row, dict) or set(row) != {'claim', 'figures', 'reason'}
                   or type(row['claim']) is not int or row['claim'] not in ranges
                   or not isinstance(row['figures'], list)
                   or any(type(n) is not int or not 1 <= n <= len(views) for n in row['figures'])
                   or not isinstance(row['reason'], str) or not row['reason'].strip()
                   for row in coverage)
            or {row['claim'] for row in coverage} != set(ranges)):
        raise ValueError('每一权项必须有有效的附图覆盖或处置理由。')
    if any(not isinstance(entry, str) or not entry.strip() for entry in raw['unresolved']):
        raise ValueError('请将疑点列为可核对的文字。')
    if not views:
        status = 'partial' if raw['unresolved'] else 'no_drawing_recommended'
    else:
        status = 'partial' if raw['unresolved'] else 'plan_ready'
    return {'schema_version': 'claims-drawing-plan-1', 'source_mode': 'claims_only',
            'claims_sha256': hashlib.sha256(blob).hexdigest(), 'status': status,
            'coverage': coverage, 'unresolved': raw['unresolved'], 'views': views}


def _explicit_field_rows(blob, ranges, views, relation_rows):
    patterns = (
        ('explicit_field_binding', re.compile(r'(?<![^，；。：:])(?P<subject>所述[^，；。关联不未无]{1,16})关联(?P<object>[^，；。]{3,220})')),
        ('per_occurrence_record', re.compile(r'(?<![^，；。：:])对每次(?P<subject>[^，；。]{1,16}?)记录(?P<object>[^，；。]{3,180})')),
    )
    recovered = []
    for claim, (start, end) in ranges.items():
        text = blob[start:end].decode()
        for rule, pattern in patterns:
            matches = list(pattern.finditer(text))
            if len(matches) != 1:
                continue
            match, = matches
            quote = match[0]
            if not re.search(r'、|和|及', match['object']):
                continue
            left = start + len(text[:match.start()].encode())
            right = start + len(text[:match.end()].encode())
            supported = [view for view in views if any(
                not feature['location_ambiguous'] and feature['claim'] == claim
                and feature['start'] <= left and right <= feature['end'] for feature in view['features'])]
            if not supported or any(
                isinstance(row, dict) and row.get('claim') == claim
                and isinstance(row.get('evidence'), dict)
                and quote in str(row['evidence'].get('relation', {}).get('quote', ''))
                for row in relation_rows):
                continue
            view = supported[0]
            feature = next(f for f in view['features'] if f['claim']==claim
                           and not f['location_ambiguous'] and f['start'] <= left and right <= f['end'])
            evidence = {key: {'claim':claim, 'quote':match[key], 'context':quote}
                        for key in ('subject','object')}
            evidence['relation'] = {'claim':claim,'quote':quote}
            row = {'claim':claim,'claim_path':[claim],'views':[view['number']],'required':True,
                   'subject':match['subject'],'object':match['object'],'kind':'逐次记录' if rule=='per_occurrence_record' else '字段关联',
                   'direction':'directed','evidence':evidence,
                   'condition':{'state':'none_in_source','operator':'none','terms':[]}}
            recovered.append((row, {'rule':rule,'claim':claim,'view_id':view['id'],'feature_id':feature['id'],
                                    'source':{'claim':claim,'quote':quote,'start':left,'end':right}}))
    return recovered


def _logical_citation_count(features):
    return sum(not feature['location_ambiguous'] for feature in features)


def _citation_display(features):
    """Reference nested, uniquely anchored quotes without changing logical evidence."""
    aliases = []
    for feature in features:
        if feature['location_ambiguous']:
            continue
        containers = [anchor for anchor in features
                      if not anchor['location_ambiguous'] and anchor['claim'] == feature['claim']
                      and anchor['start'] <= feature['start'] < feature['end'] <= anchor['end']
                      and (anchor['start'], anchor['end']) != (feature['start'], feature['end'])]
        if containers:
            root = min(containers, key=lambda item: (item['start'] - item['end'], item['start']))
            aliases.append({'feature_id':feature['id'], 'anchor_id':root['id'],
                            'start':feature['start']-root['start'], 'end':feature['end']-root['start']})
    nested = {a['feature_id'] for a in aliases}
    count = _logical_citation_count(features)
    return {'logical_count':count, 'anchor_ids':[f['id'] for f in features
                                                if not f['location_ambiguous'] and f['id'] not in nested],
            'aliases':aliases,
            **({'pending_count':len(features)-count} if count < len(features) else {})}


def _reviewed_plan(text, ranges, raw, relation_contract, response_sha256, coverage=None):
    required = {'views', 'coverage', 'unresolved'}
    extras = {'components', 'relations', 'figure_links'}
    if relation_contract >= 4:
        extras.add('flow_edges')
    if (not isinstance(raw, dict) or not required <= raw.keys()
            or raw.keys() - required - extras
            or not all(isinstance(group, list) for group in raw.values())):
        raise ValueError('权利要求图单缺少有效字段。')
    partial = {'coverage': [dict(row) if isinstance(row, dict) else row for row in raw['coverage']]}
    partial['views'] = [{**v, 'title': f'图{i} {v["title"]}'} if isinstance(v, dict)
                        and isinstance(v.get('title'), str) else v
                        for i, v in enumerate(raw['views'], 1)]
    is_current = relation_contract == RELATION_CONTRACT
    if relation_contract >= 6:
        for view in partial['views']:
            if not isinstance(view, dict) or not isinstance(view.get('features'), list):
                continue
            features = {}
            invalid_indices = []
            allowed = {'claim','quote','text'} | ({'context'} if is_current else set())
            for feature in view['features']:
                if is_current and isinstance(feature, dict) and set(feature)=={'invalid_citation_index'}:
                    invalid_indices.append(feature)
                    continue
                if (not isinstance(feature, dict) or set(feature) - allowed
                        or not {'claim','quote'} <= set(feature)
                        or type(feature['claim']) is not int or not isinstance(feature['quote'], str)
                        or feature.get('text', feature['quote']) != feature['quote']
                        or 'context' in feature and not isinstance(feature['context'], str)):
                    raise ValueError('图中特征必须使用所引权项的逐字原文。')
                key = (feature['claim'],feature['quote'],feature.get('context'))
                features[key] = {**feature, 'text':feature['quote']}
            view['features'] = [*features.values(), *invalid_indices]
    partial['unresolved'] = []
    plan = claims_plan(text, partial, relation_contract=relation_contract, _reviewed_views=is_current)
    blob = text.encode()
    clauses = {number: blob[a:b].decode() for number, (a, b) in ranges.items()}
    parents = {number: _references(clause) for number, clause in clauses.items()}
    view_remap, unique, signatures = {}, [], {}
    limitations = []
    oversized = {view['number']:view for view in plan['views']
                 if is_current and _logical_citation_count(view['features']) > CLAIMS_VIEW_CITATION_LIMIT}
    for view in plan['views']:
        if view['number'] in oversized:
            continue
        title = FIGURE_NUMBER.sub('', view['title'], count=1)
        title = FIGURE_NUMBER.sub('', title, count=1).strip()
        signature = (view['type'], title, view['purpose'],
                     frozenset((f['claim'], f['start'], f['end']) for f in view['features']))
        number = None if oversized else signatures.get(signature)
        if number is None:
            number = view['number'] if oversized else len(unique) + 1
            signatures[signature] = number
            features = []
            for feature in view['features']:
                until = ranges[feature['claim']][1]
                repeated = feature.get('location_ambiguous',
                                       blob.find(feature['quote'].encode(), feature['start'] + 1, until) >= 0)
                identifier = f'c{feature["claim"]}-{feature["start"]}-{feature["end"]}'
                if is_current and repeated and any(
                    other['claim'] == feature['claim'] and other['start'] == feature['start']
                    and other['end'] == feature['end'] and not other.get('location_ambiguous')
                    for other in view['features']
                ):
                    identifier += '-pending'
                features.append({**feature,'id':identifier,'location_ambiguous':repeated})
                if repeated:
                    limitations.append({'impact': 'review', 'claims': [feature['claim']],
                                        'view_ids': [f'fig{number}'],
                                        'detail': '引文在同一权项内出现多次。',
                                        'question': '哪次出现对应当前图中的对象或关系？'})
            confirmed = {**view, 'id': f'fig{number}', 'number': number, 'title': title, 'features': features}
            if is_current:
                display = _citation_display(features)
                if display['aliases'] or 'pending_count' in display:
                    confirmed['citation_display'] = display
            unique.append(confirmed)
        view_remap[view['number']] = number
    limitations.extend(_limitation(item, ranges, view_remap) for item in raw['unresolved'])

    def paths_from(claim, evidence_claims):
        pending = [(claim, [claim])]
        visited = set()
        found = []
        while pending:
            current, path = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            if current in evidence_claims:
                found.append(path)
            pending.extend((parent, path + [parent]) for parent in parents[current])
        return found

    verified_views = {view['number']:view for view in unique}
    for row in plan['coverage']:
        pending_figures = [i for i in row['figures'] if i in oversized]
        row['figures'] = list(dict.fromkeys(view_remap[i] for i in row['figures'] if i in view_remap))
        row['paths'] = []
        for n in row['figures']:
            features = verified_views[n]['features']
            by_claim = defaultdict(list)
            for evidence in features:
                if not evidence['location_ambiguous']:
                    by_claim[evidence['claim']].append(evidence['id'])
            paths = paths_from(row['claim'], by_claim)
            direct = [p for p in paths if len(p) == 1]
            for path in (direct or paths or [[row['claim']]]):
                destination = by_claim.get(path[-1], [])
                kind = 'direct' if destination and len(path) == 1 else ('inherited' if destination else 'unverified')
                row['paths'].append({'view_id': f'fig{n}', 'kind': kind, 'claim_path': path,
                                     'feature_ids': destination, 'limitation_verified': kind == 'direct'})
        if pending_figures:
            row['pending_figures'] = list(dict.fromkeys(pending_figures))
            for number in row['pending_figures']:
                source_claims = {f['claim'] for f in oversized[number]['features'] if not f['location_ambiguous']}
                for path in paths_from(row['claim'], source_claims) or [[row['claim']]]:
                    row['paths'].append({'view_id':f'fig{number}', 'kind':'unverified', 'claim_path':path,
                                         'feature_ids':[], 'limitation_verified':False})
        kinds = {p['kind'] for p in row['paths']}
        row['disposition'] = ('expressed' if kinds == {'direct'} else 'partial' if kinds - {'unverified'}
                              else 'unverified' if kinds else 'no_separate_view')
    components = _components(raw.get('components', []), clauses, unique)
    candidates = {}
    relation_rows = list(raw.get('relations', []))
    flow_rows = list(raw.get('flow_edges', []))
    recovered = _explicit_field_rows(blob, ranges, unique, relation_rows) if relation_contract >= 9 else []
    for row, receipt in recovered:
        row['views'] = [next(original for original, number in view_remap.items() if number==row['views'][0])]
        relation_rows.append(row)
        flow_rows.append({'relation':len(relation_rows), 'role':'transition', 'label':row['evidence']['relation']})
    if relation_contract >= 3:
        relations, relation_candidates, relation_issues = reviewed_relations(
            relation_rows, blob, ranges, parents, view_remap, unique, response_sha256,
            contract=relation_contract, held_numbers=oversized)
        candidates['relation_candidates'] = relation_candidates
        if relation_contract >= 9:
            derivations = []
            offset = len(raw.get('relations', []))
            for index, (_, receipt) in enumerate(recovered, offset):
                candidate = relation_candidates[index]
                candidate.update(origin='source_derived')
                entry = {**receipt, 'candidate_id':candidate['candidate_id'], 'status':candidate['status']}
                derivations.append(entry)
                for relation in relations:
                    if relation['id']==candidate['candidate_id']:
                        relation.update(origin='source_derived', derivation=entry)
            candidates['source_derivations'] = derivations
        limitations.extend(relation_issues)
        if unique and not relations and not relation_candidates:
            limitations.append(_limitation('当前视图没有可核关系，请核对必要关系及图号。', {}, {}))
        by_id = {part['id']: part for part in components}
        for relation in relations:
            for role in ('subject', 'object'):
                ident = relation[role + '_id']
                part = by_id.setdefault(ident, {'id': ident, 'name': relation[role], 'claims': [], 'view_ids': []})
                part['claims'] = sorted(set(part['claims']) | {relation['evidence'][role]['claim']})
                part['view_ids'] = list(dict.fromkeys([*part['view_ids'], *relation['view_ids']]))
        components = list(by_id.values())
        if relation_contract >= 4:
            instances = {}
            for relation in relations:
                for instance in relation.get('instances', []):
                    item = instances.setdefault(instance['id'], {**instance, 'citations': []})
                    if instance['citation'] not in item['citations']:
                        item['citations'].append(instance['citation'])
                    component = by_id.setdefault(instance['id'], {
                        'id': instance['id'], 'name': instance['name'], 'claims': [], 'view_ids': [],
                    })
                    component['name'] = instance['name']
                    component['claims'] = sorted(set(component['claims']) | {relation['claim']})
                    component['view_ids'] = list(dict.fromkeys([*component['view_ids'], *relation['view_ids']]))
            edges, flow_candidates, flow_issues = verified_flow(
                flow_rows, relations, relation_candidates, blob, ranges, parents, response_sha256,
                contract=relation_contract)
            limitations.extend(flow_issues)
            if relation_contract >= 5:
                owner_proofs = {r['claim'] for r in relations if r.get('instances')
                                and r['evidence']['relation']['claim'] == r['claim']}
                for claim, text in clauses.items():
                    if claim not in owner_proofs and re.search(
                            r'切换账号时[^。；]{0,60}(?:隔离各账号的[^。；]{1,30}|(?:各|不同)账号的[^。；]{1,30}隔离)', text):
                        limitations.append({
                            'code': 'owner_scope_missing', 'impact': 'required', 'claims': [claim],
                            'view_ids': [v['id'] for v in unique if any(f['claim']==claim for f in v['features'])],
                            'detail': '该权项缺少各账号及各自队列的独立归属关系。',
                            'question': '请核对该权项中切换条件和两个队列各自的所有者。'})
            if relation_contract == RELATION_CONTRACT:
                from .claims_flow import consolidate_flow_placements

                migrations = consolidate_flow_placements(relations, edges, unique)
                if migrations:
                    candidates['flow_placement_migrations'] = migrations
            candidates.update(instances=list(instances.values()), flow_edges=edges, flow_candidates=flow_candidates)
            components = list(by_id.values())
    else:
        relations = _relations(raw.get('relations', []), clauses, components, view_remap, limitations)
    links = _links(raw.get('figure_links', []), clauses, view_remap, limitations, contract=relation_contract)
    if coverage:
        from .claims_wire import relationship_coverage

        wire, tasks = coverage
        candidates['relationship_review'], issues = relationship_coverage(
            wire, tasks, blob, ranges, view_remap, unique, held_numbers=oversized)
        limitations.extend(issues)
    if oversized:
        held = []
        for number, view in oversized.items():
            count, fid = _logical_citation_count(view['features']), view['id']
            claims = sorted({f['claim'] for f in view['features']})
            held.append({'view_id':fid,'number':number,'title':view['title'],
                         'reason':'figure_citation_limit','logical_count':count,'limit':CLAIMS_VIEW_CITATION_LIMIT,
                         'claims':claims,'response_sha256':response_sha256,'features':view['features'],
                         **({'rejected_features':view['rejected_features']} if 'rejected_features' in view else {}),
                         'related_view_ids':list(dict.fromkeys(
                             f'fig{other}' for item in candidates.get('relation_candidates', [])
                             if fid in item.get('pending_view_ids', []) for other in item['raw']['views']
                             if other != number and (other in view_remap or other in oversized))),
                         'candidate_indices':[index for index,item in enumerate(raw.get('relations', []))
                                              if isinstance(item, dict) and isinstance(item.get('views'), list)
                                              and number in item['views']],
                         'figure_links':[link for link in raw.get('figure_links', [])
                                         if isinstance(link, dict) and number in (link.get('parent'),link.get('child'))]})
            limitations.append({'code':'figure_citation_limit','impact':'required','claims':claims,'view_ids':[fid],
                                'detail':f'图 {number} 有 {count} 条引文，超过 {CLAIMS_VIEW_CITATION_LIMIT}，原图位待核。',
                                'question':'请核对该图的原文和必要引用。'})
        candidates['view_limit_issues'] = held
    blocked = (any(item['impact'] in {'required', 'review'} for item in limitations)
               or any(row['disposition'] in {'partial', 'unverified'} for row in plan['coverage']))
    status = 'partial' if blocked else 'plan_ready' if unique else 'no_drawing_recommended'
    return {**plan, **candidates, 'schema_version': f'claims-drawing-plan-{relation_contract}', 'status': status,
            'claim_dependencies': [{'claim': number, 'parent_claims': refs} for number, refs in parents.items()],
            'views': unique, 'components': components, 'relations': relations, 'figure_links': links,
            'limitations': limitations, 'unresolved': [item['detail'] for item in limitations]}


def _mapped(values, mapping):
    if not isinstance(values, list) or not all(type(i) is int and i in mapping for i in values):
        return []
    return list(dict.fromkeys(mapping[i] for i in values))


def _limitation(item, ranges, views):
    fallback = {'impact': 'review', 'claims': [], 'view_ids': [], 'question': '需要核对哪一项权利要求或表达边界？'}
    if isinstance(item, str) and item.strip():
        return {**fallback, 'detail': item}
    if (not isinstance(item, dict) or not isinstance(item.get('detail'), str)
            or not item['detail'].strip()):
        return {**fallback, 'detail': '待核项格式需修订。'}
    refs = _mapped(item.get('claims'), {i: i for i in ranges})
    fig = _mapped(item.get('views'), {i: f'fig{n}' for i, n in views.items()})
    impact = item.get('impact')
    if not isinstance(impact, str) or impact not in IMPACTS or not (refs or fig):
        impact = 'review'
    return {**fallback, 'impact': impact, 'claims': refs, 'view_ids': fig, 'detail': item['detail'],
            'question': item['question'] if isinstance(item.get('question'), str) and item['question'].strip() else fallback['question']}


def _components(rows, clauses, views):
    names = defaultdict(set)
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get('name'), str) and row['name'].strip():
            label = row['name'].strip()
            for i in _mapped(row.get('claims'), {n: n for n in clauses}):
                if label in clauses[i]:
                    names[label].add(i)
    return [{'id': part_id(label), 'name': label,
             'claims': sorted(claims), 'view_ids': [v['id'] for v in views
             if any(label in f['quote'] and f['claim'] in claims for f in v['features'])]}
            for label, claims in sorted(names.items())]


def _source_span(row, clauses):
    if not isinstance(row, dict) or type(row.get('claim')) is not int:
        return ''
    quote = row.get('quote')
    if isinstance(quote, str) and quote.strip() and quote in clauses.get(row['claim'], ''):
        return quote
    return ''


def _relations(rows, clauses, components, views, limitations):
    by_name = {c['name']: c['id'] for c in components}
    result = []
    for row in rows:
        quote = _source_span(row, clauses)
        if quote and all(isinstance(row.get(k), str) for k in ('subject', 'object', 'kind', 'condition')):
            a, b, condition = row['subject'], row['object'], row['condition']
            view_ids = _mapped(row.get('views'), {i: f'fig{n}' for i, n in views.items()})
            if a in by_name and b in by_name and a in quote and b in quote and (not condition or condition in quote) and view_ids:
                result.append({**row, 'subject_id': by_name[a], 'object_id': by_name[b], 'view_ids': view_ids})
                continue
        limitations.append(_limitation('有候选图间关系缺少可核两端、条件或引文。', {}, {}))
    return result


def _links(rows, clauses, views, limitations, *, contract=6):
    result = []
    for index, row in enumerate(rows):
        quote = _source_span(row, clauses)
        if quote and isinstance(row.get('anchor'), str) and row['anchor'] and row['anchor'] in quote:
            numbers = _mapped([row.get('parent'), row.get('child')], views)
            if len(numbers) == 2:
                result.append({**row, 'parent': f'fig{numbers[0]}', 'child': f'fig{numbers[1]}'})
                continue
        issue = _limitation('有总图和局部图的定位关系需复核。', {}, {})
        if contract >= 7:
            data = row if isinstance(row, dict) else {}
            claim = data.get('claim')
            roles = {role: f'fig{views[data[role]]}' for role in ('parent', 'child')
                     if type(data.get(role)) is int and data[role] in views}
            anchor = data.get('anchor')
            issue.update(code='figure_link_unverified', link_index=index, figure_roles=roles,
                         link_anchor=anchor if isinstance(anchor, str) else '',
                         claims=[claim] if type(claim) is int and claim in clauses else [],
                         view_ids=list(dict.fromkeys(roles.values())))
        limitations.append(issue)
    return result
