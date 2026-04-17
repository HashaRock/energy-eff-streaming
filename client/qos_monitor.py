from dataclasses import dataclass, field


@dataclass
class QoSMetrics:
    stall_count: int = 0
    total_stall_duration_s: float = 0.0
    num_segment_requests: int = 0
    num_key_requests: int = 0
    total_bytes_b: int = 0
    playback_duration_s: float = 0.0

    @property
    def rebuffer_ratio(self) -> float:
        if self.playback_duration_s <= 0:
            return 0.0
        return self.total_stall_duration_s / self.playback_duration_s

    @property
    def num_requests(self) -> int:
        return self.num_segment_requests + self.num_key_requests
