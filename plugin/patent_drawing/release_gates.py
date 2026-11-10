"""Private acceptance evaluator. Never takes acceptance from SDK or model JSON.

Receipts are operator-controlled files under a declared evidence root. Their
supporting references, exact candidate and case scope must all remain readable.
"""
import hashlib,json,re
from pathlib import Path

CORE={'TC-001','TC-003','TC-005'}


def _v038_case_gate(index,receipt,root):
 summary=json.loads(_read(receipt['case_summary_ref'],root))
 case='TC-001'if index in {3,4}else'TC-005'if index in {5,6}else'TC-003'
 if summary.get('case_id')!=case or summary.get('original_scope_preserved')is not True:raise ValueError('original case scope not preserved')
 plan=json.loads(_read(summary['required_plan_ref'],root));manifest=json.loads(_read(summary['manifest_ref'],root))
 required=[v['id']for v in plan['views']];actual=[v['id']for v in manifest['figures']]
 expected_count=3 if case=='TC-001' else 4
 if (len(required)!=expected_count or len(set(required))!=len(required)or set(actual)!=set(required)
     or len(actual)!=len(required)or manifest.get('complete_set')is False or manifest.get('missing_ids')
     or manifest.get('deliverable_status')=='incomplete_draft'):
  raise ValueError('complete original figure set required; partial delivery cannot close case')
 needed={3:{'spatial_purpose','four_bodies','eight_contacts','source_fidelity'},
         4:{'three_views','10_to_110','all_bases_unchanged','unaffected_view_unchanged','outside_roi_unchanged','atomic_commit'},
         5:{'partition_walls','pin_ownership','rib_hole_contact','no_unsupported_ear','cable_side_opening','conductive_path','dependent_views_same_structure'},
         6:{'set_linked_edit','all_bases_unchanged','atomic_commit'},
         7:{'name_only','technical_limits','linked_names','unaffected_views_unchanged'}}[index]
 checks=summary.get('core_checks',{})
 if any(checks.get(k)!='PASS'for k in needed):raise ValueError('core actual case check FAIL/UNKNOWN/missing')
 if index in {4,6}and summary.get('linked_image_calls')!=0:raise ValueError('display-only linked edit must make zero image calls')

def _read(ref,root):
 p=Path(ref['path']);base=Path(root)
 if not base.is_absolute() or any(x.is_symlink() for x in [base,*base.parents]) or '..' in p.parts:raise ValueError('untrusted evidence path')
 base=base.resolve(strict=True)
 resolved=p.resolve(strict=True)
 if not p.is_absolute() or not resolved.is_relative_to(base) or any(x.is_symlink() for x in [p,*p.parents]) or p.stat().st_mode&0o022 or not p.is_file() or p.stat().st_size>8*1024*1024:raise ValueError('untrusted receipt location')
 raw=p.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('receipt hash mismatch')
 return raw


def evaluate_release(evidence:dict,scope:dict)->dict:
 gates={};root=scope.get('evidence_root');uid=scope.get('candidate_uid')
 version=re.search(r':(\d+)\.(\d+)\.(\d+)@',str(uid))
 v038=scope.get('acceptance_contract')=='patent-v038-1'or(version and tuple(map(int,version.groups()))>=(0,3,8))
 for i in range(1,13):
  gate='G'+str(i);status='UNKNOWN';missing=[];receipt=None
  try:
   if not root or not uid or gate not in evidence:raise ValueError('private receipt missing')
   receipt=json.loads(_read(evidence[gate],root))
   if receipt.get('gate')!=gate or receipt.get('candidate_uid')!=uid:raise ValueError('candidate or gate mismatch')
   if receipt.get('origin')!='operator_verified' or not receipt.get('reviewer'):raise ValueError('trusted reviewer missing')
   refs=receipt.get('evidence_refs',[])
   if not refs:raise ValueError('supporting actual evidence missing')
   for ref in refs:_read(ref,root)
   if i in {3,4,5,6,7,8,9,10,11}:
    needed={'TC-001'} if i in {3,4} else {'TC-005'} if i in {5,6} else {'TC-003'} if i==7 else CORE
    if not needed<=set(receipt.get('original_cases',[])):raise ValueError('original core case coverage missing')
   if i==9 and (receipt.get('role')!='patent_professional' or receipt.get('critical_or_important_open')!=0):raise ValueError('professional scope/acceptance missing')
   if i==11 and (receipt.get('role')!='user' or receipt.get('accepted') is not True):raise ValueError('final user acceptance missing')
   if i==10 and receipt.get('scope') not in {'disclosed_material_draft_assistance','complete_application'}:raise ValueError('material boundary missing')
   if v038 and i in {3,4,5,6,7}:_v038_case_gate(i,receipt,root)
   status=receipt['status'] if receipt.get('status') in {'PASS','FAIL','BLOCKED','UNKNOWN'} else 'UNKNOWN'
  except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:missing.append(str(error))
  gates[gate]={'status':status,'missing':missing,'receipt':evidence.get(gate)}
 engineering=all(gates[g]['status']=='PASS' for g in ['G1','G2'])
 final=all(g['status']=='PASS' for g in gates.values()) and CORE<=set(scope.get('core_cases',[]))
 complete=False
 if final:
  try:complete=json.loads(_read(evidence['G10'],root)).get('scope')=='complete_application'
  except (OSError,ValueError,KeyError):pass
 return {'gates':gates,'engineering_frozen':engineering,'product_final':final,'filing_ready':final and complete,
  'decision':'FINAL' if final else 'NOT_FINAL','published':False}
