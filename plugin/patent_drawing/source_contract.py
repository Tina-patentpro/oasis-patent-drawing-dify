"""Evidence locations and view obligations; citation validity is not truth."""
import re


def issue(rule, verdict, message):
    return {'rule': rule, 'verdict': verdict, 'message': message}


def compact(s):
    return re.sub(r'\s+', '', s)


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
