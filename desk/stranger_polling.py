"""Bounded snapshot polling, with no replies to the initial inbox backlog."""
import threading
import time


class StrangerPoller:
    def __init__(self, fetch, deliver, report, interval=60, history_fetch=None, deliver_batch=None, recover_on_start=False, recovery_since=0, manual_fetch=None):
        self.fetch, self.deliver, self.report = fetch, deliver, report
        self.interval = interval
        self.history_fetch = history_fetch
        self.deliver_batch = deliver_batch
        self.recover_on_start = recover_on_start
        self.recovery_since = recovery_since
        self.stop_event = threading.Event()
        self.seen = set()
        self.baseline_time = None
        self.initialized = False
        self.manual_fetch = manual_fetch
        self.refresh_lock = threading.Lock()
        self.wake = threading.Event()
        self.refresh = {'status':'idle'}
        self.retry_after = 0
        self.max_histories = None
        self.should_yield = lambda: False

    def refresh_state(self):
        with self.refresh_lock:
            return dict(self.refresh)

    def request_refresh(self):
        with self.refresh_lock:
            if self.stop_event.is_set() or not self.manual_fetch:
                raise ValueError('陌生人读取未启动，请先连接账号')
            if self.refresh['status'] not in ('queued','running'):
                self.refresh = {'status':'queued','requested_at':time.time()}
                self.wake.set()
            return dict(self.refresh)

    def poll_once(self, fetch=None):
        snapshot = (fetch or self.fetch)()
        if self.stop_event.is_set():
            return
        if snapshot['truncated']:
            if fetch:
                raise RuntimeError('Incomplete refresh snapshot')
            # Do not establish an incomplete baseline or auto-reply when the
            # list is saturated; oldest conversations may be outside snapshot.
            self.report('limited', '陌生人会话达到拉取上限，自动接待暂停，请到抖音处理积压')
            return
        events = snapshot['events']
        if not self.initialized:
            self.seen = set() if self.recover_on_start else {(e['conversation_id'], e['message_id']) for e in events}
            self.baseline_time = self.recovery_since if self.recover_on_start else snapshot.get('server_time_ms') or int(time.time() * 1000)
            self.initialized = True
            if not self.recover_on_start:
                self.report('ready', f'陌生人监听已启动；跳过 {len(events)} 条已有消息，等待新私信')
                return
            self.report('recovering', f'正在检查 {len(events)} 个会话是否漏回；会核对已发送记录')
        delivered = 0
        skipped = 0
        histories = 0
        # Each history request shares the platform's rate limit. Service the
        # newest conversations first instead of delaying them behind old ones.
        for event in sorted(events, key=lambda item: item['created_at_ms'], reverse=True):
            key = (event['conversation_id'], event['message_id'])
            if key in self.seen:
                continue
            if event['created_at_ms'] <= self.baseline_time:
                self.seen.add(key)
                skipped += 1
                continue
            if self.stop_event.is_set():
                return
            if histories and (self.should_yield() or (not fetch and self.max_histories and histories >= self.max_histories)):
                break
            self.report('recovering', '正在核对未回复消息的历史记录；为避免限频需等待请求间隔')
            history = self.history_fetch(event['conversation_short_id']) if self.history_fetch else [event]
            histories += 1
            if not any((item['conversation_id'],item['message_id']) == key for item in history):
                raise RuntimeError('Incomplete stranger history')
            if any(item['conversation_id'] != event['conversation_id'] for item in history):
                raise RuntimeError('Mismatched stranger history')
            if self.stop_event.is_set():
                return
            outgoing = [item['created_at_ms'] for item in history if item.get('is_outgoing')]
            if any(stamp <= 0 for stamp in outgoing):
                # Without the owner's ordering boundary we cannot safely infer
                # which customer messages still need a reply.
                raise RuntimeError('Owner history timestamp is missing')
            cutoff = max([self.baseline_time] + outgoing)
            pending = []
            for item in history:
                item_key = (item['conversation_id'], item['message_id'])
                if item['created_at_ms'] <= self.baseline_time or item_key in self.seen:
                    continue
                # Already answered messages still belong in the transcript.
                # Keep display eligibility separate from reply eligibility.
                if not item.get('is_outgoing') and item['created_at_ms'] <= cutoff:
                    item = {**item, 'history_only': True}
                pending.append(item)
            pending.sort(key=lambda item: (item['created_at_ms'], int(item['message_id']) if item['message_id'].isdigit() else 0))
            if self.deliver_batch:
                accepted = self.deliver_batch(pending) is not False
            else:
                accepted = all(self.deliver(item) is not False for item in pending)
            if not accepted:
                continue
            self.seen.update((item['conversation_id'],item['message_id']) for item in history)
            incomplete_media = [item for item in history if item.get('media_kind') in ('image', 'sticker')
                                and not item.get('media_url') and item['created_at_ms'] > self.baseline_time]
            if incomplete_media:
                # Live notifications can precede the available CDN resource.
                # Allow a later history read to enrich the same stored row.
                self.seen.discard(key)
                for item in incomplete_media:
                    self.seen.discard((item['conversation_id'], item['message_id']))
            delivered += len(pending)
        # The service persists deduplication; this cache only limits repeats
        # within one connection. Preserve a bound for long-lived sessions.
        if len(self.seen) > 10000:
            self.seen = {(e['conversation_id'], e['message_id']) for e in events}
        self.report('recovering' if snapshot.get('partial') else 'ready',
                    ('正在逐页补查历史 · ' if snapshot.get('partial') else '漏回检查正常 · ') + f'每轮至少间隔 {self.interval} 秒'
                    + (f' · 本轮接收 {delivered} 条' if delivered else '')
                    + (f' · 跳过 {skipped} 条旧消息或时间未知消息' if skipped else ''))
        return {'conversations':snapshot.get('count',len(events)),'received':delivered}

    def run(self):
        failures = 0
        while not self.stop_event.is_set():
            remaining = max(0,self.retry_after-time.monotonic())
            if remaining and self.stop_event.wait(remaining):
                return
            with self.refresh_lock:
                manual = self.refresh['status'] == 'queued'
                if manual:
                    self.refresh['status'] = 'running'
            delay = self.interval
            try:
                result = self.poll_once(self.manual_fetch if manual else None)
                failures = 0
                if manual:
                    with self.refresh_lock:
                        self.refresh.update(status='cancelled' if self.stop_event.is_set() else 'completed',finished_at=time.time(),**(result or {}))
            except Exception as exc:
                failures += 1
                delay = min(self.interval * 2 ** min(failures, 4), 900)
                from .stranger import StrangerError
                if isinstance(exc, StrangerError) and exc.rate_limited:
                    delay = max(120, delay)
                    self.report('rate_limited', f'平台限频（{exc.code}），暂停补拉 {delay} 秒；请勿反复重连')
                elif isinstance(exc, StrangerError) and exc.code is not None:
                    self.report('error', f'离线私信补查被拒绝（{exc.code}），{delay} 秒后重试')
                else:
                    self.report('error', f'离线私信补查未完成，{delay} 秒后重试；实时连接状态单独显示')
                self.retry_after = time.monotonic()+delay
                if manual:
                    with self.refresh_lock:
                        self.refresh.update(status='failed',finished_at=time.time(),retry_seconds=delay)
            self.wake.wait(delay)
            self.wake.clear()

    def start(self):
        threading.Thread(target=self.run, name='stranger-inbox', daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
