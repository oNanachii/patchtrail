"""One explicit single-process bounded scheduler shared by every identity."""
from concurrent.futures import ThreadPoolExecutor
import threading


class ResourcePool:
    def __init__(self, capacity=6):
        self.lock = threading.RLock()
        self.capacity = capacity
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="patchtrail")
        self.services = []
        self.pending = 0

    def active_runs(self):
        return sum(service.store.count_runs(active=True) for service in self.services)

    def submit(self, operation):
        from .service import ServiceError
        with self.lock:
            if self.pending >= self.capacity:
                raise ServiceError("queue_full", "The shared execution queue is full", 429)
            self.pending += 1
            def execute():
                try:
                    return operation()
                finally:
                    with self.lock:
                        self.pending -= 1
            try:
                return self.executor.submit(execute)
            except BaseException:
                self.pending -= 1
                raise

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
