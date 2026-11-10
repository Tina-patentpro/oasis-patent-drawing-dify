"""Verify exact historical bytes; an imported 200 is never execution authority."""
import base64
import hashlib
import io
import json
import re

from PIL import Image

from .operation_store import digest

MEMBERS={'request','response','headers','image','approval','technical_review'}


def validate_external_attempt(attempt: dict, blobs: dict[str,bytes], lineage: dict) -> dict:
    issues=[]
    def fail(message):issues.append({'rule':'EXTERNAL-ATTEMPT','verdict':'FAIL','message':message})
    if not isinstance(attempt,dict) or not isinstance(blobs,dict) or set(blobs)!=MEMBERS:
        return {'status':'FAIL','verified_attempt':None,'issues':[{'rule':'EXTERNAL-ATTEMPT','verdict':'FAIL','message':'complete original byte members required'}]}
    for k in MEMBERS:
        raw=blobs[k]
        if not isinstance(raw,bytes) or not raw or len(raw)>48*1024*1024 or hashlib.sha256(raw).hexdigest()!=attempt.get(k+'_sha256'):
            fail('original '+k+' bytes/hash invalid')
    if (attempt.get('kind')!='manual_diagnostic' or attempt.get('model')!='gpt-image-2.6-flare'
            or lineage.get('state')!='provider_rejected'
            or attempt.get('previous_operation_id')!=lineage.get('operation_id')
            or any(not re.fullmatch(r'[a-f0-9]{64}',str(attempt.get(k,''))) or attempt[k]!=lineage.get(k)
                   for k in ('source_sha256','lineage_sha256'))):
        fail('manual diagnostic source/family/prior operation mismatch or unresolved UNKNOWN')
    if issues:return {'status':'FAIL','verified_attempt':None,'issues':issues}
    try:
        def pairs(items):
            out={}
            for k,v in items:
                if k in out:raise ValueError('duplicate JSON key')
                out[k]=v
            return out
        req,resp,headers,approval,review=[json.loads(blobs[k],object_pairs_hook=pairs)for k in ('request','response','headers','approval','technical_review')]
        if req.get('model')!=attempt['model']:fail('raw request model differs')
        rid=attempt.get('request_id')
        if (not isinstance(rid,str) or not re.fullmatch(r'req_[A-Za-z0-9_-]{1,160}',rid)
                or headers.get('status')!=200 or headers.get('headers',{}).get('x-request-id')!=rid):
            fail('actual HTTP200 and request ID required')
        if (approval.get('request_sha256')!=attempt['request_sha256'] or approval.get('automatic_retries')!=0
                or approval.get('production_recovery_authorization')is not False
                or not isinstance(approval.get('scope'),str) or not approval['scope'].strip()):
            fail('original exact one-shot manual authorization required, not general recovery')
        data=resp.get('data')
        if not isinstance(data,list) or len(data)!=1 or base64.b64decode(data[0]['b64_json'],validate=True)!=blobs['image']:
            fail('response image bytes differ')
        with Image.open(io.BytesIO(blobs['image']))as im:
            if im.format!='PNG' or im.width*im.height>8_000_000:fail('valid bounded original PNG required')
            im.verify()
        if review.get('overall')not in {'PASS','FAIL','UNKNOWN'}:fail('historical technical review verdict required')
    except (ValueError,KeyError,TypeError,AttributeError,OSError)as e:
        fail('unreadable original evidence: '+type(e).__name__)
    if issues:return {'status':'FAIL','verified_attempt':None,'issues':issues}
    verified={**attempt,'attempt_sha256':digest(attempt),'supplier_state':'response_saved',
              'semantic_state':review['overall'],'grants_retry':False,'promotes_current':False,
              'official_feedback':False,'scope':'historical_external_attempt_only'}
    return {'status':'PASS','verified_attempt':verified,'issues':[]}
