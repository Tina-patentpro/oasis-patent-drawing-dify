"""Source-bound visibility obligations for physical image candidates."""
from .source_contract import validate_facts

RULES=[
 ('channel-contour',['通道','外壁','分隔壁','导线','轮廓','弯折'],['通道','外壁','分隔壁'],
  '从通道开口侧选视角，清楚画出外壁、分隔壁和连续通道边界，轮廓随导线弯折；避免用画出弯线代替通道，不新增无源盖板遮住内部。'),
 ('pin-hole-fit',['安装销','可变形肋','PCB','镀覆','接地孔'],['安装销','销','孔'],
  '清楚区分实体安装销、销上可变形肋、PCB镀覆接地孔；不得把孔当作销，不添加螺栓/耳片/套筒代替销。总图遮挡时保留对应局部空间视图，不透视补造未知内部，不确定数量/尺寸。'),
 ('cable-side-opening',['电缆侧','开口','焊接','屏蔽层','排流线','或','PCB'],['电缆侧','开口','屏蔽'],
  '电缆侧预留焊接开口应显示对屏蔽层或排流线焊接位置的可达关系；开口不得移至PCB侧。或不改为同时必须或互斥，不添加未经披露的盖板/紧固件/焊料形状。'),
]


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
    for ident,triggers,local,description in RULES:
        scoped=selected_scope[ident] if selected else any(t in context for t in local)
        if all(t in text for t in triggers)and(overview or scoped):
            fids=[f['id']for f in valid if any(t in e.get('quote','')for e in f.get('evidence',[])for t in triggers)]
            features.append({'id':ident,'required_visibility':description,'source_fact_ids':fids,'verdict':'UNKNOWN'})
    return {'view_id':view['id'],'features':features,'visual_status':'UNKNOWN',
            'limitations':'清单仅为请求义务，不证明图中已实现；实际图件及跨图身份需独立视觉/专业核查。未知数量、尺寸及隐藏几何不得补造；只有锚点人工核准后才可标注改号。'}
