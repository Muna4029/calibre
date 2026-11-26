#!/usr/bin/env python


__license__ = 'GPL v3'
__copyright__ = '2015, Kovid Goyal <kovid at kovidgoyal.net>'

import sys
from threading import Thread

from calibre.utils.monotonic import monotonic
from polyglot.queue import Full, Queue


class Worker(Thread):

    daemon = True

    def __init__(self, log, notify_server, num, request_queue, result_queue):
        self.request_queue, self.result_queue = request_queue, result_queue
        self.notify_server = notify_server
        self.log = log
        self.working = False
        Thread.__init__(self, name=f'ServerWorker{num}')

    def run(self):
        while True:
            x = self.request_queue.get()
            if x is None:
                break
            job_id, func = x
            self.working = True
            try:
                result = func()
            except Exception:
                # handle_error will capture minimal exception info and push non-blocking
                try:
                    self.handle_error(job_id)
                except Exception:
                    # Ensure worker doesn't die here; log and continue
                    self.log.exception('ServerWorker failed to handle error for job %r', job_id)
            else:
                try:
                    # use non-blocking put to avoid stalling worker threads if the queue is full
                    self.result_queue.put_nowait((job_id, True, result))
                except Full:
                    # dropping the result is preferable to blocking; log for observability
                    self.log.exception('Result queue full, dropping result for job %r', job_id)
            finally:
                self.working = False
            try:
                self.notify_server()
            except Exception:
                self.log.exception('ServerWorker failed to notify server on job completion')

    def handle_error(self, job_id):
        # Capture only minimal exception information (type name and message) to avoid
        # retaining traceback/frame objects which can hold resources open.
        exc_type, exc_value, _ = sys.exc_info()
        exc_info = (exc_type.__name__ if exc_type else None, str(exc_value))
        try:
            self.result_queue.put_nowait((job_id, False, exc_info))
        except Full:
            # If the result queue is full, log and drop the error info to avoid blocking
            self.log.exception('Result queue full, dropping error info for job %r', job_id)


class ThreadPool:

    def __init__(self, log, notify_server, count=10, queue_size=1000):
        self.request_queue, self.result_queue = Queue(queue_size), Queue(queue_size)
        self.workers = [Worker(log, notify_server, i, self.request_queue, self.result_queue) for i in range(count)]

    def start(self):
        for w in self.workers:
            w.start()

    def put_nowait(self, job_id, func):
        self.request_queue.put_nowait((job_id, func))

    def get_nowait(self):
        return self.result_queue.get_nowait()

    def stop(self, wait_till):
        # Attempt to send a shutdown sentinel for each worker. If the request queue is
        # full, retry (with bounded waits) until either the sentinel is submitted or
        # the overall wait_till deadline is reached. Breaking on the first Full would
        # leave workers without a sentinel and prevent clean shutdowns.
        for w in self.workers:
            end = wait_till
            while True:
                try:
                    self.request_queue.put_nowait(None)
                    break
                except Full:
                    now = monotonic()
                    if now >= end:
                        # No time left to wait for queue space; give up for this worker
                        break
                    # Try a blocking put for a short bounded time to allow the queue to drain
                    try:
                        self.request_queue.put(None, timeout=min(0.1, end - now))
                        break
                    except Full:
                        # Timed out, loop and check remaining time again
                        continue
        for w in self.workers:
            now = monotonic()
            if now >= wait_till:
                break
            w.join(wait_till - now)
        self.workers = [w for w in self.workers if w.is_alive()]

    @property
    def busy(self):
        return sum(int(w.working) for w in self.workers)

    @property
    def idle(self):
        return sum(int(not w.working) for w in self.workers)


class PluginPool:

    def __init__(self, loop, plugins):
        self.workers = []
        self.loop = loop
        for plugin in plugins:
            w = Thread(target=self.run_plugin, args=(plugin,), name=self.plugin_name(plugin))
            w.daemon = True
            w.plugin = plugin
            self.workers.append(w)

    def plugin_name(self, plugin):
        return plugin.__class__.__name__

    def run_plugin(self, plugin):
        try:
            plugin.start(self.loop)
        except Exception:
            self.loop.log.exception('Failed to start plugin:', self.plugin_name(plugin))

    def start(self):
        for w in self.workers:
            w.start()

    def stop(self, wait_till):
        for w in self.workers:
            if w.is_alive():
                try:
                    w.plugin.stop()
                except Exception:
                    self.loop.log.exception('Failed to stop plugin:', self.plugin_name(w.plugin))
        for w in self.workers:
            left = wait_till - monotonic()
            if left > 0:
                w.join(left)
            else:
                break
        self.workers = [w for w in self.workers if w.is_alive()]
