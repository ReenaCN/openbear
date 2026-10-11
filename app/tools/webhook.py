"""One action-scoped tool. Execution identity and owner come only from the host."""
from __future__ import annotations

import json
from pathlib import Path

from app.tools.base import current_tool_context
from app.runtime.tool_result import ToolOutcome
from app.webhooks.contracts import WebhookError, dumps
from app.webhooks.repository import many, one
from app.webhooks import queries, receipts, waits

ACTIONS=('describe','list','get','create','update','delete','set_enabled','pause','resume','stop','events','retry','wait','report','telemetry')
READONLY={'describe','events','get','report'}
AUTOMATIC={'describe','get','events','wait','report','telemetry'}
GUIDANCE=(Path(__file__).resolve().parents[2]/'prompts'/'guides'/'webhook.md').read_text(encoding='utf-8')


async def dispatch(service,action,params,ctx):
    if action not in ACTIONS or not isinstance(params,dict): raise WebhookError('invalid_action')
    if ctx.receipt_repair and action not in READONLY: raise WebhookError('receipt_repair_read_only',status=403)
    if ctx.webhook_automatic and action not in AUTOMATIC: raise WebhookError('automatic_configuration_forbidden',status=403)
    if ctx.agent_session_uuid and action not in ('describe','events','report','telemetry'): raise WebhookError('agent_action_forbidden',status=403)
    owner=ctx.webhook_owner_id
    if not owner or not ctx.conversation_uuid: raise WebhookError('host_identity_required',status=403)
    for forbidden in ('owner','ownerId','runId','assignmentId','attemptId','source','trusted','confirm'):
        if forbidden in params: raise WebhookError('host_identity_not_a_parameter',status=403)
    a=await one(service.db.conn,'SELECT * FROM webhook_assignments WHERE conversation_uuid=? AND root_turn_uuid=?',(ctx.conversation_uuid,ctx.run_root_turn_uuid))
    if a: ctx.webhook_assignment_id=a['assignment_id']
    if action=='describe':
        return {'actions':list(ACTIONS),'requested':params.get('action'),'guidance':GUIDANCE,'protocol':{
            'wait':{'op':'register | await | cancel','register':{'requestId':'stable ID','endpointId':'owned endpoint','match':{'all':[{'path':'body.orderId','op':'eq','value':'123'}]},'afterSeq':'optional task-start cursor override','timeoutSeconds':'positive seconds or null','collection':'optional batching settings'},'await':{'waitId':'registered ID'}},
            'report':{'receiptId':'stable ID','results':[{'eventId':'owned and delivered event','outcome':'completed | skipped | failed | unknown','summary':'supported claim','evidenceRefs':['tool-action:existing-action-id'],'result':'optional structured result'}]},
            'management':'get before update; expectedRevision/expectedControlRevision + stable requestId; sensitive operations invoke the real user confirmation gate; keys never returned to model',
            'telemetry':'operationId + declared metrics/records; platform metrics are read-only'}}
    if action=='list':
        return await queries.targets(service,owner,params) if params.get('targets') else await service.list(owner,params)
    if action=='get':
        if params.get('current') or ctx.receipt_repair:
            if not a: raise WebhookError('assignment_required',status=403)
            members=await many(service.db.conn,'SELECT event_id,delivered_at_ms FROM webhook_assignment_events WHERE assignment_id=?',(a['assignment_id'],))
            actions=await many(service.db.conn,'SELECT r.* FROM runtime_actions r JOIN webhook_runtime_links l ON r.run_id=l.run_id WHERE l.assignment_id=? ORDER BY r.created_at,r.action_id',(a['assignment_id'],))
            return {'assignmentId':a['assignment_id'],'state':a['state'],'events':members,'executionHistory':actions}
        eid=params.get('endpointId')
        if ctx.webhook_automatic and a and eid!=a['origin_endpoint_id']: raise WebhookError('endpoint_outside_assignment',status=403)
        return await service.get(owner,eid)
    if action=='events':
        eid=params.get('eventId')
        if ctx.webhook_automatic or ctx.receipt_repair:
            if not eid or not a or not await one(service.db.conn,'SELECT event_id FROM webhook_assignment_events WHERE assignment_id=? AND event_id=?',(a['assignment_id'],eid)): raise WebhookError('event_not_owned',status=403)
        return await queries.detail(service,owner,eid) if eid else await queries.events(service,owner,params)
    if action=='wait':
        if not service.bridge: raise WebhookError('runtime_unavailable',status=503)
        aid=await service.bridge.ensure_human_assignment(ctx)
        op=params.get('op','register')
        if op=='register': return await waits.register(service,aid,owner,{k:v for k,v in params.items() if k!='op'})
        if op=='await': return await waits.await_wait(service,aid,params.get('waitId'))
        if op=='cancel': return await waits.cancel(service,aid,params.get('waitId'))
        raise WebhookError('invalid_wait_operation')
    if action=='report':
        return await receipts.report(service,ctx.webhook_assignment_id,ctx.run_id,params)
    if action=='telemetry':
        from app.webhooks.telemetry import observe
        if not a: raise WebhookError('assignment_required',status=403)
        event_id=params.get('eventId')
        if event_id:
            m=await one(service.db.conn,'SELECT e.endpoint_id FROM webhook_assignment_events m JOIN webhook_events e USING(event_id) WHERE m.assignment_id=? AND m.event_id=?',(a['assignment_id'],event_id))
            if not m: raise WebhookError('event_not_owned',status=403)
            eid=m['endpoint_id']
        else: eid=a['origin_endpoint_id']
        return await observe(service,eid,params,source='model',event_id=event_id,assignment_id=a['assignment_id'],action_id=ctx.execution_id)
    eid=params.get('endpointId'); body={k:v for k,v in params.items() if k!='endpointId'}
    if action=='delete' or action=='update' and params.get('operation')=='rotate_key':
        sensitive='rotate_key' if action=='update' else action
        if ctx.web_confirm is None: raise WebhookError('user_confirmation_unavailable',status=403)
        body.pop('operation',None)
        prepared=await service.prepare(owner,eid,sensitive,body,tool_approval=True)
        effects = ({'stopRunning': body.get('stopRunning', False), 'scope': body.get('scope', 'all')}
                   if sensitive == 'delete' else {'graceSeconds': body.get('graceSeconds', 0)})
        answer=await ctx.web_confirm({'_sourceTool':'Webhook','_requiresAuthorization':True,
                                      '_approvalScope':prepared['approvalScope'],'_approvalConstraints':prepared['approvalConstraints'],
                                      'title':'确认Webhook '+sensitive,'body':dumps({'impact':prepared['impact'], **effects}),
                                      'type':'warning','default':False,'confirmText':'执行明确操作','cancelText':'取消','timeoutSeconds':300})
        if not answer.get('confirmed') or answer.get('text') or answer.get('feedback'): return {'cancelled':True,'userFeedback':answer}
        result=await service.control(owner,eid,{'confirmationToken':prepared['confirmationToken'],'requestId':body.get('requestId')},action=sensitive)
        result.pop('credential',None); result['credentialDelivery']='Key omitted from model; view/copy the current Key in the logged-in Web UI'
        return result
    if action=='create':
        result=await service.create(owner,body); result.pop('credential',None)
        result['credentialDelivery']='Key omitted from model; view/copy it again in the logged-in Web UI; legacy Keys require manual rotation'
        return result
    if action=='update': return await service.update(owner,eid,body)
    if action in ('set_enabled','pause','resume','stop'): return await service.control(owner,eid,body,action=action)
    if action=='retry':
        from app.webhooks.recovery import retry
        return await retry(service,owner,params.get('eventId'),body)
    raise WebhookError('invalid_action')


def register_webhook_tool(registry,service):
    async def handler(args):
        try: return dumps(await dispatch(service,args.get('action'),args.get('params',{}),current_tool_context()))
        except WebhookError as exc: return ToolOutcome(dumps(exc.payload),'failed','not_started')
    registry.add('Webhook','Manage authorized external triggers; register/await external events, report owned outcomes, query observations. Call describe before create/update; it returns the action contracts and design guidance. Identity is supplied by runtime; keys never enter model context. Amended receipts keep endpoint/config/control versions fixed; deletion may reduce stopRunning or stop scope and affected resources may finish, never expand. Rotation parameters stay exact.',
                      {'type':'object','properties':{'action':{'type':'string','enum':list(ACTIONS)},'params':{'type':'object'}},'required':['action'],'additionalProperties':False},handler,visibility={'main','agent'},effect_scope='external_event')
