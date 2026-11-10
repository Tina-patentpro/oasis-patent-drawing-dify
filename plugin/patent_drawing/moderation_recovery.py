"""Private evidence qualification. Provider categories never identify trigger words."""
import hashlib,json,re
from .operation_store import digest


def diagnose_rejection(status:int,body:bytes,request_id:str|None)->dict:
 stage='unknown';categories=[];code=None
 try:
  e=json.loads(body).get('error',{});details=e.get('moderation_details',{})
  if details.get('moderation_stage') in {'input','output'}:stage=details['moderation_stage']
  known={'violence','sexual','self-harm','hate','harassment','illicit','sexual/minors','violence/graphic'}
  categories=[x if x in known else 'unknown_category' for x in details.get('categories',[]) if isinstance(x,str)][:20]
  code=e.get('code') if e.get('code') in {'moderation_blocked','content_policy_violation','image_generation_user_error'} else None
 except (ValueError,AttributeError,TypeError):pass
 return {'state':'provider_rejected' if 400<=status<500 and status!=408 else 'result_unknown',
  'http_status':status,'request_id':request_id if isinstance(request_id,str) and re.fullmatch(r'req_[A-Za-z0-9_-]{1,160}',request_id) else None,
  'moderation_stage':stage,'categories':categories,'code':code,'keyword_cause':'UNKNOWN','automatic_retry':False}


def recovery_eligibility(original:dict,candidate:dict,evidence:list[dict])->dict:
 missing=[];valid=[]
 if original.get('state')!='provider_rejected':missing.append('original_not_resolved_rejection')
 if candidate.get('lineage_sha256')!=original.get('lineage_sha256'):missing.append('lineage_mismatch')
 for e in evidence:
  if not isinstance(e,dict):continue
  raw=e.get('blob');kind=e.get('kind')
  if kind not in {'official_feedback','source_clarification'} or not isinstance(raw,bytes) or not raw or len(raw)>4*1024*1024:continue
  if hashlib.sha256(raw).hexdigest()!=e.get('sha256') or e.get('withdrawn') is not False or e.get('reviewed') is not True or e.get('new_information') is not True:continue
  if e.get('previous_operation_id')!=original.get('operation_id') or e.get('source_sha256')!=candidate.get('source_sha256'):continue
  if kind=='source_clarification':
   if (not original.get('facts_sha256') or not candidate.get('facts_sha256') or candidate['facts_sha256']==original['facts_sha256'] or candidate.get('source_sha256')==original.get('source_sha256')):continue
   if e['sha256'] not in candidate.get('source_components',[]) or e['sha256'] in original.get('source_components',[]):continue
  valid.append({k:e[k] for k in ('kind','sha256','previous_operation_id','source_sha256')})
 if not valid:missing.append('qualifying_new_evidence_missing')
 return {'eligible':not missing,'missing':missing,'evidence_sha256':digest(valid) if valid else None,
  'previous_operation_id':original.get('operation_id'),'candidate_sha256':candidate.get('request_sha256'),
  'source_sha256':candidate.get('source_sha256'),'lineage_sha256':candidate.get('lineage_sha256')}
