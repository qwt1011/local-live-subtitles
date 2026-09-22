"""PCM 缓冲：按音频时间轴切片、丢弃已提交部分。

存在的理由是服务端需要持有"唯一流状态"（ARCHITECTURE_REVIEW.md 第 4 节不变式 2），
而这个状态的核心就是"还没被提交的那段音频"。客户端不再做任何分段决策。

位置一律用**音频时间轴秒**表示，从捕获开始算起。
"""

from collections import deque

import numpy as np


class PcmBuffer:
    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self._frames = deque()
        self._samples = 0
        self._cache = None
        # 缓冲区第一个采样点对应的音频位置（秒）
        self.start = 0.0

    @property
    def seconds(self):
        return self._samples / float(self.sample_rate)

    @property
    def end(self):
        return self.start + self.seconds

    @property
    def empty(self):
        return self._samples == 0

    def push(self, pcm):
        frame = np.asarray(pcm, dtype=np.float32)
        if frame.size == 0:
            return
        self._frames.append(frame)
        self._samples += frame.size
        self._cache = None

    def to_array(self):
        if self._cache is None:
            if not self._frames:
                self._cache = np.zeros(0, dtype=np.float32)
            elif len(self._frames) == 1:
                self._cache = self._frames[0]
            else:
                self._cache = np.concatenate(list(self._frames))
        return self._cache

    def _index(self, position):
        return int(round((position - self.start) * self.sample_rate))

    def slice(self, start, end):
        """按音频位置切片；超出可用范围的部分自动截断。"""
        begin = max(0, self._index(start))
        finish = min(self._samples, self._index(end))
        if finish <= begin:
            return np.zeros(0, dtype=np.float32)
        return self.to_array()[begin:finish]

    def drop_before(self, position):
        """丢弃 position 之前的音频，并把 start 推进到 position。"""
        cut = self._index(position)
        if cut <= 0:
            return
        cut = min(cut, self._samples)
        remaining = cut
        while remaining > 0 and self._frames:
            head = self._frames[0]
            if len(head) <= remaining:
                remaining -= len(head)
                self._frames.popleft()
            else:
                self._frames[0] = head[remaining:]
                remaining = 0
        self._samples -= cut
        self.start = position
        self._cache = None

    def reset(self):
        self._frames.clear()
        self._samples = 0
        self._cache = None
        self.start = 0.0
