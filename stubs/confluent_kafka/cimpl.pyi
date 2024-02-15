from confluent_kafka.admin._metadata import ClusterMetadata
from typing import Any, Callable, Final

OFFSET_BEGINNING: Final = ...
OFFSET_END: Final = ...

class KafkaError:
    _NOENT: int
    _AUTHENTICATION: int
    _UNKNOWN_TOPIC: int
    _UNKNOWN_PARTITION: int
    _TIMED_OUT: int
    _STATE: int
    UNKNOWN_TOPIC_OR_PART: int

    def code(self) -> int: ...
    def str(self) -> str: ...

class KafkaException(Exception):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args: tuple[KafkaError]

class NewTopic:
    def __init__(
        self,
        topic: str,
        num_partitions: int = -1,
        replication_factor: int = -1,
        replica_assignment: list | None = None,
        config: dict[str, str] | None = None,
    ) -> None:
        self.topic: str

class TopicPartition:
    def __init__(
        self,
        topic: str,
        partition: int = -1,
        offset: int = -1001,
        metadata: str | None = None,
        leader_epoch: int | None = None,
    ) -> None:
        self.topic: str
        self.partition: int
        self.offset: int
        self.metadata: str | None
        self.leader_epoch: int | None
        self.error: KafkaError | None

class Message:
    def offset(self) -> int: ...
    def timestamp(self) -> tuple[int, int]: ...
    def key(self) -> str | bytes | None: ...
    def value(self) -> str | bytes | None: ...
    def topic(self) -> str: ...
    def partition(self) -> int: ...
    def headers(self) -> list[tuple[str, bytes]] | None: ...
    def error(self) -> KafkaError | None: ...

class Producer:
    def produce(
        self,
        topic: str,
        value: str | bytes | None = None,
        key: str | bytes | None = None,
        partition: int = -1,
        on_delivery: Callable[[KafkaError, Message], Any] | None = None,
        timestamp: int | None = -1,
        headers: dict[str | None, bytes | None] | list[tuple[str | None, bytes | None]] | None = None,
    ) -> None: ...
    def flush(self, timeout: float = -1) -> None: ...
    def list_topics(self, topic: str | None = None, timeout: float = -1) -> ClusterMetadata: ...
    def poll(self, timeout: float = -1) -> int: ...

class Consumer:
    def subscribe(
        self,
        topics: list[str],
        on_assign: Callable[[Consumer, list[TopicPartition]], None] | None = None,
        on_revoke: Callable[[Consumer, list[TopicPartition]], None] | None = None,
    ) -> None: ...
    def get_watermark_offsets(
        self, partition: TopicPartition, timeout: float | None = None, cached: bool = False
    ) -> tuple[int, int] | None: ...
    def close(self) -> None: ...
    def list_topics(self, topic: str | None = None, timeout: float = -1) -> ClusterMetadata: ...
    def consume(self, num_messages: int = 1, timeout: float = -1) -> list[Message]: ...
    def poll(self, timeout: float = -1) -> Message | None: ...
    def assign(self, partitions: list[TopicPartition]) -> None: ...
    def commit(
        self, message: Message | None = None, offsets: list[TopicPartition] | None = None, asynchronous: bool = True
    ) -> list[TopicPartition] | None: ...
    def committed(self, partitions: list[TopicPartition], timeout: float = -1) -> list[TopicPartition]: ...
    def unsubscribe(self) -> None: ...
    def assignment(self) -> list[TopicPartition]: ...
    def seek(self, partition: TopicPartition) -> None: ...

TIMESTAMP_CREATE_TIME: Final = ...
TIMESTAMP_NOT_AVAILABLE: Final = ...
TIMESTAMP_LOG_APPEND_TIME: Final = ...
