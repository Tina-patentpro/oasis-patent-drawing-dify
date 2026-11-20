"""Bounded Responses transport; one POST, shared approved provider key."""
import json
import hashlib
import base64
import httpx
from .client import OpenAIImagesClient, _status_error, safe_request_id, safe_usage, project_usage
from .errors import DrawingError
from .file_input import bounded_body, close_http_resources, run_with_deadline

TEXT_MODEL='gpt-6.1-sol'
DEADLINE=300
DEFAULT_PLAN_OUTPUT_TOKENS = 24000
CLAIMS_PLAN_OUTPUT_TOKENS = 36000
CLAIMS_WIRE_OUTPUT_TOKENS = 20000
CLAIMS_WIRE_TARGET_TOKENS = 16000
CLAIMS_EXTENDED_OUTPUT_TOKENS = 25000
CLAIMS_PLAN_DEADLINE = 420


def claims_plan_options(contract, *, output_cap=None):
    if output_cap is not None:
        from .claims_relations import RELATION_CONTRACT

        if contract != RELATION_CONTRACT or output_cap not in (
                CLAIMS_WIRE_OUTPUT_TOKENS, CLAIMS_EXTENDED_OUTPUT_TOKENS):
            raise ValueError('图单输出额度与已登记契约不匹配。')
        return {'max_output_tokens': output_cap, 'claims_wire': True, 'claims_wire_contract': contract}
    if contract >= 13:
        return {'max_output_tokens': CLAIMS_WIRE_OUTPUT_TOKENS, 'claims_wire': True,
                'claims_wire_contract': contract}
    if contract >= 11:
        return {'max_output_tokens': CLAIMS_WIRE_OUTPUT_TOKENS, 'claims_wire': True}
    return {'max_output_tokens': CLAIMS_PLAN_OUTPUT_TOKENS} if contract >= 6 else {}


class OpenAITextClient(OpenAIImagesClient):
    @staticmethod
    def prepare(system,user,images=None,*,max_output_tokens=DEFAULT_PLAN_OUTPUT_TOKENS,claims_wire=False,
                claims_wire_contract=11, mixed_materials=False):
        if (not isinstance(system,str)or not system.strip()or len(system)>16000
            or not isinstance(user,str)or not user.strip()or len(user)>240000
            or len(system)+len(user)>260000):
            raise DrawingError('parameter_error','规划提示为空或超限')
        allowed = ({CLAIMS_WIRE_OUTPUT_TOKENS} if claims_wire else
                   {DEFAULT_PLAN_OUTPUT_TOKENS, CLAIMS_PLAN_OUTPUT_TOKENS})
        if claims_wire and max_output_tokens == CLAIMS_EXTENDED_OUTPUT_TOKENS:
            from .claims_relations import RELATION_CONTRACT

            if claims_wire_contract == RELATION_CONTRACT:
                allowed.add(CLAIMS_EXTENDED_OUTPUT_TOKENS)
        if mixed_materials:
            if claims_wire or images:
                raise DrawingError('parameter_error', '多材料图单仅发送已核定文本和原图定位。')
            allowed = {CLAIMS_WIRE_OUTPUT_TOKENS, CLAIMS_EXTENDED_OUTPUT_TOKENS}
        if type(max_output_tokens) is not int or max_output_tokens not in allowed:
            raise DrawingError('parameter_error','规划输出额度无效')
        response_format = {'type': 'json_object'}
        if claims_wire:
            from .claims_wire import claims_wire_format

            if images:
                raise DrawingError('parameter_error', '仅权利要求正文的图单不可附带模型图片。')
            response_format = claims_wire_format(claims_wire_contract)
        payload={'model':TEXT_MODEL,'input':[{'role':'system','content':system},{'role':'user','content':user}],
                 'text':{'format':response_format},'max_output_tokens':max_output_tokens,'store':False}
        if images:
            from .images import PreparedImage
            if not isinstance(images,list) or len(images)>12 or any(not isinstance(i,PreparedImage)for i in images):
                raise DrawingError('parameter_error','规划页图必须为已核原图，最多12页')
            payload['input'][1]['content']=[{'type':'input_text','text':user}]+[{'type':'input_image','image_url':i.data_url}for i in images]
        return payload

    def plan(self,system,user,images=None,evidence_sink=None,*,max_output_tokens=DEFAULT_PLAN_OUTPUT_TOKENS,
             claims_wire=False, claims_wire_contract=11, mixed_materials=False):
        from .operator_policy import work_audit_headers

        payload=self.prepare(system,user,images,max_output_tokens=max_output_tokens,claims_wire=claims_wire,
                             claims_wire_contract=claims_wire_contract, mixed_materials=mixed_materials)
        timeout = CLAIMS_PLAN_DEADLINE if claims_wire or mixed_materials or max_output_tokens == CLAIMS_PLAN_OUTPUT_TOKENS else DEADLINE
        audit_headers=work_audit_headers(self._transport)
        if evidence_sink:evidence_sink('request',json.dumps(payload,ensure_ascii=False).encode())
        request_id=None
        def fetch(deadline):
            nonlocal request_id
            client=httpx.Client(transport=self._transport,timeout=httpx.Timeout(timeout,connect=20),follow_redirects=False)
            response=None
            try:
                outgoing=client.build_request('POST','https://api.openai.com/v1/responses',json=payload,
                    headers={'Authorization':'Bearer '+self._api_key,'Accept':'application/json',
                             'Accept-Encoding':'identity', **audit_headers})
                response=client.send(outgoing,stream=True);request_id=safe_request_id(response.headers.get('x-request-id'))
                body=bounded_body(response,2*1024*1024,max_seconds=timeout,deadline=deadline)
                if evidence_sink:
                    evidence_sink('receipt', json.dumps({'http_status': response.status_code,
                        'request_id': request_id, 'body_sha256': hashlib.sha256(body).hexdigest()}).encode())
                    evidence_sink('response', body)
                if response.status_code!=200:raise _status_error(response.status_code,body,request_id)
                return body
            finally:close_http_resources(response,client)
        try:body=run_with_deadline(fetch,timeout)
        except DrawingError as error:
            if error.category in {'response_timeout','response_too_large'}:raise DrawingError('result_unknown','规划未取得最终结果；费用状态未知，未自动重试',request_id)from None
            raise
        except httpx.HTTPError:raise DrawingError('result_unknown','规划连接中断；费用状态未知，未自动重试',request_id)from None
        return decode_plan_response(body, request_id, max_output_tokens=max_output_tokens, claims_wire=claims_wire,
                                    claims_wire_contract=claims_wire_contract, mixed_materials=mixed_materials)


def decode_plan_response(body, request_id, *, max_output_tokens=DEFAULT_PLAN_OUTPUT_TOKENS, claims_wire=False,
                         claims_wire_contract=11, mixed_materials=False):
    request_id = safe_request_id(request_id)
    try:
        result=json.loads(body)
        incomplete = result.get('incomplete_details')
        if (result.get('status') == 'incomplete' and isinstance(incomplete, dict)
                and incomplete.get('reason') == 'max_output_tokens'):
            raise DrawingError('planner_output_truncated',
                               '规划返回截断，已保存原响应及请求回执。',
                               request_id, upstream_code='max_output_tokens')
        if result.get('status')!='completed':raise DrawingError('planner_incomplete','规划响应未完成；未执行绘图，未自动重试',request_id)
        content=[c for o in result.get('output',[])if o.get('type')=='message'for c in o.get('content',[])]
        if any(c.get('type')=='refusal'for c in content):raise DrawingError('content_refused','模型拒绝规划此材料；未执行绘图',request_id)
        text=''.join(c.get('text','')for c in content if c.get('type')=='output_text')
        max_text_bytes = 512 * 1024 if max_output_tokens == CLAIMS_PLAN_OUTPUT_TOKENS else 240000
        if not text or len(text.encode()) > max_text_bytes:
            raise DrawingError('planner_invalid_output','规划未返回有效长度的JSON文本',request_id)
        if claims_wire or mixed_materials:
            observed = (safe_usage(result.get('usage')) or {}).get('output_tokens')
            if observed is not None and observed > max_output_tokens:
                raise DrawingError('planner_invalid_output', '规划用量超过绑定的输出上限。', request_id)
        if claims_wire:
            from .claims_wire import load_wire_plan

            try:
                load_wire_plan(text, contract=claims_wire_contract)
            except (ValueError, RecursionError):
                raise DrawingError('planner_invalid_output',
                                   '紧凑图单结构或引用不合格，已保存原响应及请求回执。', request_id) from None
        return {'success':True,'text':text,'model':TEXT_MODEL,'request_id':request_id,'usage':safe_usage(result.get('usage')),'usage_projection':project_usage(result.get('usage')),'raw_response':result,'raw_response_base64':base64.b64encode(body).decode(),'professional_review_status':'not_reviewed'}
    except DrawingError as error:
        error.http_status=200;error._response_body=body
        raise
    except (ValueError,TypeError,AttributeError):raise DrawingError('planner_invalid_output','规划响应无法解析；未执行绘图',request_id,http_status=200,response_body=body)from None

# Pure preflight alias remains available when tests inject a client factory.
prepare_plan_payload = OpenAITextClient.prepare
