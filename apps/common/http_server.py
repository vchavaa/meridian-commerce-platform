from http.server import ThreadingHTTPServer
from threading import BoundedSemaphore


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    max_workers = 64

    def __init__(self, server_address, request_handler_class):
        self._worker_slots = BoundedSemaphore(self.max_workers)
        super().__init__(server_address, request_handler_class)

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            try:
                try:
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                except OSError:
                    pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except (OSError, RuntimeError):
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()
