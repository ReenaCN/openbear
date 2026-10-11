"""Same-protocol model fallback for recommendation runs only.

The Agent loop owns one backend. This adapter presents an ordered chain as that
backend: each physical request goes to the current model; an availability
failure (quota/auth/missing model/exhausted transient errors) switches the
chain to the next model and the same request is retried there. Opaque native
state produced by an earlier model (encrypted reasoning, signatures, native
items) is stripped before another model sees the transcript.
"""
from __future__ import annotations

import copy
import hashlib
import json

from app.llm.base import OpenBearLLMError
from app.llm.error_classify import (
    AUTH,
    AUTH_PERMANENT,
    BILLING,
    MODEL_NOT_FOUND,
    OVERLOADED,
    RATE_LIMIT,
    SERVER_ERROR,
    TIMEOUT,
    UNKNOWN,
)

# Errors that a different model may not share. Our own request faults (format,
# context overflow) would fail identically on the next model.
SWITCH_IMMEDIATELY = frozenset({BILLING, AUTH, AUTH_PERMANENT, MODEL_NOT_FOUND})
SWITCH_AFTER_RETRIES = frozenset({RATE_LIMIT, OVERLOADED, TIMEOUT, SERVER_ERROR, UNKNOWN})


class FallbackChain:
    def __init__(self, entries, *, retries_before_switch=2, on_switch=None):
        if not entries:
            raise ValueError('fallback chain requires at least one model')
        protocols = {str(getattr(e['backend'], 'protocol', '') or '').lower() for e in entries}
        if len(protocols) != 1:
            raise ValueError('fallback chain must use one protocol')
        self.entries = entries
        self.index = 0
        self.failures = 0
        self.retries_before_switch = max(0, int(retries_before_switch))
        self.on_switch = on_switch
        self.used = [entries[0]['label']]
        self.switches = []
        # Opaque state (signatures / native items) each model produced itself.
        self.owned: dict[str, set[str]] = {}

    @property
    def current(self):
        return self.entries[self.index]

    async def note_success(self):
        self.failures = 0

    async def note_failure(self, error):
        """Return True when the chain switched to another model."""
        reason = getattr(error, 'reason', '') or UNKNOWN
        if reason in SWITCH_IMMEDIATELY:
            switch = True
        elif reason in SWITCH_AFTER_RETRIES:
            self.failures += 1
            switch = self.failures > self.retries_before_switch
        else:
            switch = False
        if not switch or self.index + 1 >= len(self.entries):
            return False
        previous = self.current['label']
        self.index += 1
        self.failures = 0
        self.used.append(self.current['label'])
        self.switches.append({'from': previous, 'to': self.current['label'], 'reason': reason})
        if self.on_switch is not None:
            await self.on_switch(previous, self.current['label'], reason)
        return True


class FallbackBackend:
    """Duck-typed backend: delegates to the chain's current model."""

    def __init__(self, chain: FallbackChain):
        self.chain = chain
        first = chain.entries[0]['backend']
        self.protocol = str(getattr(first, 'protocol', '') or '')
        self._base = getattr(first, '_base', '')

    def __getattr__(self, name):
        # Estimation/payload helpers follow the active model.
        return getattr(self.chain.current['backend'], name)

    @property
    def active_entry(self):
        """Model that served the most recent request (for per-step accounting)."""
        return self.chain.current

    @staticmethod
    def _item_print(item):
        return 'item:' + hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _fingerprints(self, message):
        prints = set()
        if message.get('signature'):
            prints.add('sig:' + str(message['signature']))
        for item in message.get('native_output_items') or []:
            if isinstance(item, dict):
                prints.add(self._item_print(item))
        return prints

    def _record(self, label, event):
        owned = self.chain.owned.setdefault(label, set())
        if event.kind == 'reasoning' and event.signature:
            owned.add('sig:' + str(event.signature))
        elif event.kind == 'native_output_item':
            for item in event.native_output_items or []:
                if isinstance(item, dict):
                    owned.add(self._item_print(item))

    def _foreign_stripped(self, messages, label):
        """Strip only another model's opaque state; keep this model's own chain."""
        owned = self.chain.owned.get(label, set())
        out, foreign_tool_turn = [], False
        for message in messages:
            prints = self._fingerprints(message)
            if prints and not prints <= owned:
                message = dict(message)
                for key in ('native_output_items', 'reasoning', 'signature'):
                    message.pop(key, None)
                if message.get('role') == 'assistant':
                    foreign_tool_turn = bool(message.get('tool_calls'))
            elif message.get('role') == 'assistant':
                foreign_tool_turn = False
            out.append(message)
        return out, foreign_tool_turn

    def _prepare(self, messages, opts):
        entry = self.chain.current
        foreign_tool_turn = False
        if self.chain.index:
            messages, foreign_tool_turn = self._foreign_stripped(messages, entry['label'])
        opts = dict(opts)
        opts['model'] = entry['model_id']
        levels = entry.get('thinking_levels') or []
        level = opts.get('think_level')
        if levels and level not in levels:
            opts['think_level'] = entry.get('default_thinking') or levels[0]
        elif not levels:
            opts['think_level'] = 'off'
        if foreign_tool_turn and self.protocol.lower() == 'anthropic':
            # The open tool turn came from another model; its signed thinking cannot
            # be replayed, and Anthropic then requires thinking off for this step.
            opts['think_level'] = 'off'
        if entry.get('max_tokens'):
            opts['max_tokens'] = min(int(opts.get('max_tokens') or entry['max_tokens']), int(entry['max_tokens']))
        return messages, opts

    async def execute_physical_attempts(self, request, **callbacks):
        """Runtime entry: settle each request before choosing a fallback.

        The chain remains on the serving entry until its accounting callback
        completes, so Web pricing uses that entry, never the next one's price.
        """
        from app.runtime.model_call import PreparedRequest, execute_attempt, _invoke
        observer = callbacks.pop('on_event', None)
        while True:
            entry = self.chain.current
            messages, options = self._prepare(request.messages, request.options)
            produced = False

            async def observe(event, partial, outcome):
                nonlocal produced
                if event.kind in ('content', 'reasoning', 'tool_call', 'native_output_item', 'encrypted_reasoning'):
                    produced = True
                self._record(entry['label'], event)
                if observer is not None:
                    await _invoke(observer, event, partial, outcome)

            physical = PreparedRequest(entry['backend'], messages, options, request.metadata)
            outcome = await execute_attempt(physical, on_event=observe, **callbacks)
            if outcome.error is None:
                await self.chain.note_success()
                return outcome
            if produced or not await self.chain.note_failure(outcome.error):
                return outcome

    async def stream(self, messages, **opts):
        while True:
            entry = self.chain.current
            prepared, options = self._prepare(messages, opts)
            switched = False
            produced = False
            events = entry['backend'].stream(prepared, **options)
            try:
                async for event in events:
                    if event.kind == 'error':
                        failed = OpenBearLLMError.from_stream_event(event, protocol=self.protocol)
                        if not produced and await self.chain.note_failure(failed):
                            switched = True
                            break
                        yield event
                        return
                    if event.kind in ('content', 'reasoning', 'tool_call'):
                        produced = True
                    self._record(entry['label'], event)
                    yield event
            except OpenBearLLMError as error:
                if produced or not await self.chain.note_failure(error):
                    raise
                switched = True
            finally:
                close = getattr(events, 'aclose', None)
                if close is not None:
                    await close()
            if not switched:
                await self.chain.note_success()
                return
            # Switched before any output: retry this same request on the next model.

    async def complete(self, messages, **opts):
        while True:
            prepared, options = self._prepare(messages, opts)
            try:
                result = await self.chain.current['backend'].complete(prepared, **options)
            except OpenBearLLMError as error:
                if await self.chain.note_failure(error):
                    continue
                raise
            await self.chain.note_success()
            return result


def chain_entries(config, llm_factory, labels):
    """Resolve enabled same-protocol models; drop unknown ones, keep order."""
    out = []
    protocol = ''
    for label in labels:
        resolved = config.models.resolve(label)
        if not resolved:
            continue
        provider, model = resolved
        if protocol and provider.protocol != protocol:
            continue
        protocol = protocol or provider.protocol
        backend, model_id, max_tokens = llm_factory.backend_for(label)
        out.append({'label': label, 'backend': backend, 'model_id': model_id, 'max_tokens': max_tokens,
                    'thinking_levels': list(model.thinking_levels or []),
                    'default_thinking': model.default_thinking_level or '',
                    'cost': dict(model.cost or {}), 'protocol': provider.protocol})
    return out


def protocol_groups(config):
    groups = {}
    for provider_key, provider in config.models.providers.items():
        if not provider.enabled:
            continue
        for model in provider.models:
            groups.setdefault(provider.protocol, []).append({
                'key': f'{provider_key}/{model.id}', 'label': model.name or f'{provider_key}/{model.id}',
                'provider': provider_key, 'thinkingLevels': list(model.thinking_levels or []),
                'cost': copy.deepcopy(dict(model.cost or {}))})
    return groups
