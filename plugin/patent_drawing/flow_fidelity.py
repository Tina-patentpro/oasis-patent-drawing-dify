"""Bounded clause-aware tripwires. Ambiguity is UNKNOWN, not semantic proof."""
import re
from .source_contract import compact

_PATTERNS={
 'conflict_gate':r'(?:整批)?(?:无冲突|没有冲突)(?:时)?才(?:能)?(?:应用|更新)',
 'global_sequence':r'(?:全局|跨终端)(?:单调)?(?:序号|排序)',
 'timeout_unexecuted':r'超时(?:即|则|表示|说明)(?:未执行|未处理)',
}


def classify_gate_mentions(text):
    """Classify only supported phrases within their own punctuation clause."""
    out=[];offset=0
    for clause in re.split(r'[。；;，,]',text):
        indices=[i for i,c in enumerate(clause)if not c.isspace()]
        normalized=''.join(clause[i]for i in indices)
        for predicate,pattern in _PATTERNS.items():
            for match in re.finditer(pattern,normalized):
                start=indices[match.start()];end=indices[match.end()-1]+1
                line_start=clause.rfind('\n',0,start)+1
                prefix=compact(clause[line_start:start])
                if not prefix and line_start:
                    previous=compact(clause[:line_start].rstrip('\n').split('\n')[-1])
                    if re.search(r'(?:不设|不设置|不增加|不得增加|禁止增加|不以|不要求|引用[：:]|错误示例[：:]|反例[：:])[“\"「『]*$',previous):prefix=previous
                if re.search(r'(?:并非不|不是不|未尝不|不能不|不得不)',prefix):polarity='ambiguous'
                elif re.search(r'(?:不设|不设置|不增加|不得增加|禁止增加|不以|不要求|不得用作|不用于|不能用于)[^。；;，,]{0,12}$',prefix):polarity='prohibited'
                elif re.search(r'(?:引用|错误示例|反例)[：:]?[^。；;，,]{0,12}$',prefix):polarity='quoted'
                else:polarity='asserted'
                tail=normalized[match.start():match.end()+24]
                scope='field'if'字段'in tail and'整批'not in tail else'batch'
                out.append({'predicate':predicate,'polarity':polarity,'scope':scope,'span':[offset+start,offset+end]})
        offset+=len(clause)+1
    return out


def check_logic(bundle,plan):
    source=' '.join(p.get('text','')for p in bundle['pages']);source_mentions=classify_gate_mentions(source);out=[]
    texts=[('fact:'+f['id'],f.get('text',''))for f in plan.get('facts',[])if f.get('status')=='confirmed']
    for v in plan.get('views',[]):
        d=v.get('diagram')or{}
        texts.extend((v['id']+':'+n.get('id',str(i)),n.get('text',''))for i,n in enumerate(d.get('nodes',[])))
    for locator,text in texts:
        for mention in classify_gate_mentions(text):
            polarity=mention['polarity'];predicate=mention['predicate']
            if polarity=='prohibited':continue
            verdict='UNKNOWN'if polarity in{'quoted','ambiguous'}else'FAIL';message=None
            if verdict=='UNKNOWN':message='否定或引用范围未明确，需核对实际执行条件；不能据此判语义通过。'
            elif predicate=='conflict_gate':
                supported=any(x['predicate']==predicate and x['polarity']=='asserted'and(x['scope']=='batch'or mention['scope']=='field')for x in source_mentions)
                if not supported:message='交底未支持将无冲突增加为应用拉取更新的前提；分开表达字段冲突处理与拉取保护。'
            elif predicate=='global_sequence'and'只在本终端内递增'in compact(source):message='交底的本地序号仅在本终端递增，不能扩大为跨终端或全局顺序。'
            elif predicate=='timeout_unexecuted'and re.search(r'超时不.{0,15}未执行',compact(source)):message='交底禁止从超时推断服务端未执行。'
            if message:out.append({'rule':'SOURCE-FLOW-LIMITATION','verdict':verdict,'message':locator+'：'+message,'span':mention['span']})
    return out
