"""Estimate the actual plan, before paid image dispatch. None is explicit unlimited."""
def estimate_dispatches(plan,current_records=None,base_specs=None):
    records=current_records or{};count=0;required=[]
    for view in plan.get('views',[]):
        if view['engine']!='image':continue
        prior=records.get(view['id'])
        if prior and base_specs and prior.get('base_spec_sha256')==base_specs.get(view['id']):continue
        count+=1;required.append(view['id'])
    return {'image':count,'text':0,'required_views':required}

def estimate_group(plans):
    cases={name:estimate_dispatches(p)['image']for name,p in plans.items()}
    return {'image':sum(cases.values()),'by_case':cases}

def budget_decision(estimate,approved):
    if approved is not None and(type(approved)is not int or approved<0):raise ValueError('invalid approved budget')
    needed=estimate['image'];return {'allowed':approved is None or needed<=approved,'required':needed,'approved':approved,'unlimited':approved is None}
