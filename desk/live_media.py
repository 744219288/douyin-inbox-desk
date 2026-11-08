"""Resolve incomplete live media without waiting for the inbox sweep."""
import threading
import time

from .recovery import request, messages
from .stranger import _field as f, StrangerError


class LiveMediaResolver:
    def __init__(self, auth, deliver):
        self.auth, self.deliver = auth, deliver
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.pending = {}
        auth._desk_stranger_stop = self.stop_event
        auth._desk_stranger_interval = 2

    def enqueue(self, event):
        if event.get('is_outgoing') or event.get('media_kind') not in ('image', 'sticker') or event.get('media_url'):
            return
        short = event.get('conversation_short_id')
        if not short or short == '0':
            return
        with self.lock:
            key = (event['conversation_id'], event['message_id'])
            self.pending.setdefault(key, (dict(event), 0, time.monotonic() + .5))
        self.wake.set()

    def resolve(self, selected):
        event = selected[0][1][0]
        cid, short = event['conversation_id'], event['conversation_short_id']
        query = f(1, cid.encode()) + f(2, 1) + f(3, int(short)) + f(4, 1) + f(5, 0) + f(6, 50)
        body = request(self.auth, 301, query, '/v1/message/get_by_conversation')
        targets = {key for key, _ in selected}
        completed = set()
        for item in messages(body.get(1, []), self.auth.get_uid()):
            key = (item['conversation_id'], item['message_id'])
            if key in targets and not item.get('is_outgoing') and item.get('media_url') and item['conversation_short_id'] == short:
                if self.deliver(item) is not False:
                    completed.add(key)
        return completed

    def run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            with self.lock:
                due = [(key, value) for key, value in self.pending.items() if value[2] <= time.monotonic()]
            if not due:
                self.wake.wait(.5)
                continue
            cid = due[0][0][0]
            selected = [(key, value) for key, value in due if key[0] == cid]
            completed, cooldown = set(), 0
            try:
                completed = self.resolve(selected)
            except StrangerError as exc:
                cooldown = 120 if exc.rate_limited else 5
            except Exception:
                cooldown = 5
            with self.lock:
                for key, (event, attempts, _) in selected:
                    if key in completed or attempts >= 5:
                        self.pending.pop(key, None)  # Background recovery remains the fallback.
                    else:
                        self.pending[key] = (event, attempts + 1, time.monotonic() + max(cooldown, min(2 ** (attempts + 1), 30)))
            if cooldown and self.stop_event.wait(cooldown):
                return

    def start(self):
        threading.Thread(target=self.run, name='live-media', daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
