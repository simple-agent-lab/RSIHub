"""Host-owned resource policy; candidate output cannot override these limits."""

from dataclasses import asdict, dataclass

MIB = 1024 * 1024


@dataclass(frozen=True)
class ResourcePolicy:
    schema_version: int = 1
    default_memory_mb: int = 1024
    default_pids: int = 128
    max_file_bytes: int = 32 * MIB
    max_tree_bytes: int = 64 * MIB
    max_tree_entries: int = 4096
    max_tree_depth: int = 32
    tmp_mb: int = 128
    output_inodes: int = 8192
    log_bytes: int = MIB
    log_files: int = 1
    log_tail_lines: int = 1000
    docker_command_timeout_s: float = 15
    cleanup_timeout_s: float = 15
    supervisor_grace_s: float = 90
    poll_interval_s: float = 0.02
    model_body_bytes: int = 16 * MIB
    model_request_timeout_s: float = 120
    model_transport_grace_s: float = 5

    @property
    def max_output_mb(self) -> int:
        return self.max_tree_bytes // MIB

    @property
    def archive_bytes(self) -> int:
        # Reserve tar headers and padding for the bounded entry count.
        return self.max_tree_bytes + self.max_tree_entries * 2048

    def receipt(self) -> dict[str, int | float]:
        return asdict(self)


POLICY = ResourcePolicy()
