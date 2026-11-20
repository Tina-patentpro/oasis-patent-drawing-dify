"""Track candidate evidence and enforce the ordered supplement policy."""
import hashlib
import re

from .material_sections import TYPES, read_spans
from .operation_store import digest


TOPICS = {
    'structure': '结构、连接、位置和分区',
    'condition': '必要条件、异常守卫及退出',
    'direction': '对象端点和传递方向',
    'sequence': '流程、时序及先后步骤',
    'marks': '图中部件标号和图面标注',
    'views': '必要图位、剖面或局部视图',
    'dimensions': '可从来源核实的尺寸和相对尺度',
}
PATTERNS = {
    'structure': r'连接|支承|安装|端部|分区|结构|固定|配合|层|孔|槽',
    'condition': r'仅在|条件|当.{1,45}时|失配|拒绝|失败|否则|终止|继续|退出',
    'direction': r'向|发送|接收|反馈|转入|传输|方向',
    'sequence': r'步骤|随后|先后|依次|处理|切换|完成后|确认|移除',
    'marks': r'标号|编号|标记|标注|部件[0-9]+',
    'views': r'附图|剖视|剖面|细节图|局部图|图\s*[0-9一二三四五六七八九十]+',
    'dimensions': r'长度|厚度|直径|间隙|尺度|尺寸|宽度|高度|直角',
}
_DIRECTION = re.compile(r'(?P<src>[\w\u4e00-\u9fff]{2,28})向(?P<dst>[\w\u4e00-\u9fff]{2,28})(?:发送|传输|返回)')
_SPAN = re.compile(r'[^\n\r。；]+[。；]?')


def _topics(text):
    return [key for key, pattern in PATTERNS.items() if re.search(pattern, text)]


def source_rows(pages, catalog):
    """Produce verbatim evidence pieces. Keep claims together across pages."""
    result = {kind: [] for kind in TYPES}
    by_id = {page['id']:page for page in pages}
    for section in catalog['sections']:
        kind = section['type']
        if kind not in result:
            continue
        spans = section['spans']
        if kind == 'claims':
            entries = [spans]
        else:
            entries = []
            for span in spans:
                raw = by_id[span['page_id']]['text']
                for match in _SPAN.finditer(raw, span['start'], span['end']):
                    quote = match[0]
                    if len(quote.strip()) < 3:
                        continue
                    entries.append([{**span, 'start':match.start(), 'end':match.end(),
                        'quote_sha256':hashlib.sha256(quote.encode()).hexdigest()}])
        for refs in entries:
            quote = read_spans(pages, refs)
            record = {'type':kind, 'section_id':section['id'], 'spans':refs, 'quote':quote, 'topics':_topics(quote)}
            result[kind].append({'id':'e'+digest(record)[:16], **record})
    return result


def initial_evidence(rows, answers):
    return {'accepted':[{**row, 'decision':'foundation'} for row in rows], 'rejected':[],
            'gaps':[key for key in TOPICS if answers.get(key) in ('missing','uncertain')],
            'scope_reviews':[]}


def _possible_reversal(text, prior):
    matches = list(_DIRECTION.finditer(text))
    if not matches:
        return False
    for match in matches:
        src, dst = match['src'], match['dst']
        for old in _DIRECTION.finditer(prior):
            if (old['src'].endswith(dst) or dst.endswith(old['src'])) and (
                    old['dst'].endswith(src) or src.endswith(old['dst'])):
                return True
    return False


def next_candidates(rows, evidence):
    if not evidence['gaps']:
        return []
    pending = []
    accepted = evidence['accepted']
    for row in rows:
        gaps = [item for item in evidence['gaps'] if item in row['topics']]
        reversal = [item['id'] for item in accepted if _possible_reversal(row['quote'], item['quote'])]
        if not gaps and not reversal:
            continue
        compared = reversal or [p['id'] for p in accepted if set(row['topics']) & set(p['topics'])]
        pending.append({**row, 'gaps':gaps, 'compared':compared, 'reversal_candidates':reversal})
    return pending


def apply_supplements(evidence, rows, decisions):
    accepted = list(evidence['accepted'])
    rejected = list(evidence['rejected'])
    covered = set()
    for row in rows:
        decision = decisions[row['id']]
        gap = decision.removeprefix('supplement_')
        if row['reversal_candidates']:
            # Exact opposing actor/receiver pairs require scope review. A confirmed
            # same-scope conflict never becomes an eligible evidence row.
            outcome = 'conflict' if decision == 'conflict' else 'uncertain'
        elif decision.startswith('supplement_') and gap in row['gaps'] and gap in evidence['gaps']:
            accepted.append({**row, 'decision':decision, 'fills':gap})
            covered.add(gap)
            continue
        elif decision in {'duplicate','conflict','irrelevant','uncertain'}:
            outcome = decision
        else:
            raise ValueError('补缺判断超出本级已登记的选项。')
        rejected.append({**row, 'decision':outcome})
    return {**evidence, 'accepted':accepted, 'rejected':rejected,
            'gaps':[item for item in evidence['gaps'] if item not in covered]}


def planner_context(pages, evidence, figures):
    citations = []
    known = set()
    for row in evidence['accepted']:
        quote = read_spans(pages, row['spans'])
        if quote != row['quote']:
            raise ValueError('已选材料原文失配。')
        if row['id'] in known:
            continue
        known.add(row['id'])
        citations.append({key:row[key] for key in ('id','type','section_id','spans','quote','decision')})
    return {'citations':citations, 'open_topics':evidence['gaps'], 'original_figures':figures}
