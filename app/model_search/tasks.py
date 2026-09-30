"""串行推理队列及可取消、可恢复查看的任务状态。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import queue
import threading
import time
import uuid

from inference import SearchError, native_path
from storage import atomic_json, check_id, read_json


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, folder, kind, label):
        self.id = uuid.uuid4().hex
        self.path = native_path(folder) / (self.id + '.json')
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.state = {'id': self.id, 'kind': kind, 'label': label, 'status': 'queued',
                      'stage': 'queued', 'message': '等待处理', 'done': 0, 'total': 0,
                      'created_at': time.time(), 'success': 0, 'failed': 0, 'failures': []}
        self.update()

    def update(self, **fields):
        with self.lock:
            self.state.update(fields, updated_at=time.time())
            atomic_json(self.path, self.state)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()


class Tasks:
    def __init__(self, folder):
        self.folder = native_path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.queue = queue.Queue(maxsize=8)
        self.jobs = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._work, name='local-shape-worker', daemon=True)
        self.thread.start()

    def submit(self, kind, label, fn, cleanup=None):
        with self.lock:
            if self.stopping.is_set() or self.queue.full():
                raise SearchError('任务队列已满，请等待当前任务完成', 'queue_full', 429)
            # 只在已结束任务中淘汰内存状态，磁盘仍可查看。
            ended = [key for key, job in self.jobs.items()
                     if job.snapshot()['status'] in ('done', 'error', 'cancelled')]
            for key in ended[:-32]:
                self.jobs.pop(key, None)
            for path in self.folder.glob('*.json'):
                if path.stem not in self.jobs and time.time() - path.stat().st_mtime > 7 * 86400:
                    path.unlink(missing_ok=True)
            job = Job(self.folder, kind, label)
            self.jobs[job.id] = job
            self.queue.put_nowait((job, fn, cleanup))
            return job.snapshot()

    def get(self, tid):
        check_id(tid)
        with self.lock:
            job = self.jobs.get(tid)
        if job:
            return job.snapshot()
        state = read_json(self.folder / (tid + '.json'))
        if not state:
            raise SearchError('任务不存在或已过期', 'not_found', 404)
        if state['status'] in ('queued', 'running'):
            state.update(status='error', message='原服务已退出，请重试；已有建库缓存可复用', code='interrupted')
        return state

    def cancel(self, tid):
        check_id(tid)
        with self.lock:
            job = self.jobs.get(tid)
        if not job:
            raise SearchError('该任务不属于当前服务', 'not_found', 404)
        if job.snapshot()['status'] in ('queued', 'running'):
            job.cancelled.set()
            job.update(message='正在取消，等待当前模型处理结束')
        return job.snapshot()

    def active(self):
        with self.lock:
            return any(job.snapshot()['status'] in ('queued', 'running') for job in self.jobs.values())

    def stop(self):
        self.stopping.set()
        with self.lock:
            for job in self.jobs.values():
                job.cancelled.set()

    def _work(self):
        while not self.stopping.is_set():
            try:
                job, fn, cleanup = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job.check_cancelled()
                job.update(status='running')
                result = fn(job)
                job.update(status='done', stage='done', message='处理完成', result=result)
            except Cancelled:
                job.update(status='cancelled', message='任务已取消，旧索引未改变')
            except Exception as exc:
                job.update(status='error', message=str(exc), code=getattr(exc, 'code', 'pipeline_error'))
            finally:
                if cleanup:
                    try:
                        cleanup()
                    except OSError:
                        pass
                self.queue.task_done()
