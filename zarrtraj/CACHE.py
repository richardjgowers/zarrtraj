import abc
import threading
from collections import deque


class FrameCache(abc.ABCMeta):

    def __init__(
        self,
        cache_size,
        timestep,
        frames_per_chunk,
        parallel=False,
    ):
        self._cache_size = cache_size
        self._timestep = timestep
        self._frames_per_chunk = frames_per_chunk
        self._parallel = parallel

    def update_frame_seq(self, frame_seq):
        """Call this in the reader's _read_next_timestep
        method on the first read
        """
        self._frame_seq = frame_seq
        self._reader_q = frame_seq.copy()

    @abc.abstractmethod
    def getFrame(self, frame):
        """Call this in the reader's
        _read_next_frame() method
        """
        pass

    @abc.abstractmethod
    def cleanup(self):
        """Call this in the reader's close() method"""
        pass


class AsyncFrameCache(FrameCache, threading.thread):

    def __init__(self):
        super(FrameCache, self).__init__()
        self._unread_frames = deque([])
        self._frame_seq = deque([])
        self._stop_event = threading.Event()
        self._first_read = True
        self._mutex = threading.Lock()
        self._frame_available = threading.Condition(self._mutex)
        # load the first time step

    def getFrame(self, frame):
        if self._first_read:
            self._first_read = False
            self.start()
        with self._frame_available:
            while not self._cache_contains(frame):
                self._frame_available.wait()

            self._load_timestep(frame)
            self._reader_q.pop(0)

    def run(self):
        while self._frame_seq and not self._stop_event:
            frame = self._frame_seq.pop(0)
            key = frame % self._frames_per_chunk

            if self._cache_contains(key):
                continue
            elif self._num_cache_frames < self._max_cache_frames:
                self._get_key(key)
            else:
                with self._mutex:
                    eviction_key = self._predict()
                    self._evict(eviction_key)
                    self._get_key(key)
                    self._frame_available.notify()

    def _stop(self):
        self._stop_event.set()

    def cleanup(self):
        self._stop()

    def _predict(frame_seq, cache, frame_seq_len, index):
        """
        1. Attempt to find a page that is
           never referenced in the future
        2. If not possible, return the page that is referenced
           furthest in the future

        Cache is a list of available chunks

        returns the key of the chunk to be replaced
        chunks have keys based on frame number % chunksize


        """
        res = -1
        farthest = index
        for i in range(len(cache)):
            j = 0
            for j in range(index, frame_seq_len):
                if cache[i] == frame_seq[j]:
                    if j > farthest:
                        farthest = j
                        res = i
                    break
            # If a page is never referenced in future, return it.
            if j == frame_seq_len:
                return i
        # If all of the frames were not in future, return any of them, we return 0. Otherwise we return res.
        return 0 if (res == -1) else res

    @abc.abstractmethod
    def _stop(self):
        pass

    @abc.abstractmethod
    def _cache_contains(self, frame):
        pass
