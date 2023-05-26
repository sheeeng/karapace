"""
karapace - schema backup

Copyright (c) 2023 Aiven Ltd
See LICENSE for details
"""
from __future__ import annotations

from .backends.reader import BaseBackupReader, BaseItemsBackupReader, ProducerSend, RestoreTopic
from .backends.v3.constants import V3_MARKER
from .backends.writer import BackupWriter, StdOut
from .encoders import encode_key, encode_value
from .errors import BackupError, EmptyPartition, PartitionCountError, StaleConsumerError
from .poll_timeout import PollTimeout
from enum import Enum
from functools import partial
from kafka import KafkaConsumer, KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.consumer.fetcher import ConsumerRecord
from kafka.errors import KafkaError, TopicAlreadyExistsError
from kafka.structs import PartitionMetadata, TopicPartition
from karapace import constants
from karapace.backup.backends.v1 import SchemaBackupV1Reader
from karapace.backup.backends.v2 import AnonymizeAvroWriter, SchemaBackupV2Reader, SchemaBackupV2Writer, V2_MARKER
from karapace.backup.backends.v3.backend import SchemaBackupV3Reader, SchemaBackupV3Writer
from karapace.config import Config
from karapace.kafka_utils import kafka_admin_from_config, kafka_consumer_from_config, kafka_producer_from_config
from karapace.key_format import KeyFormatter
from karapace.schema_reader import new_schema_topic_from_config
from karapace.utils import assert_never
from pathlib import Path
from tenacity import retry, retry_if_exception_type, RetryCallState, stop_after_delay, wait_fixed
from typing import AbstractSet, Callable, Collection, Final, Iterator, Literal, NoReturn, TypeVar

import contextlib
import datetime
import logging
import math
import sys

LOG = logging.getLogger(__name__)

B = TypeVar("B", str, bytes)
F = TypeVar("F")


class BackupVersion(Enum):
    ANONYMIZE_AVRO = -1
    V1 = 1
    V2 = 2
    V3 = 3

    @classmethod
    def identify(cls, path: Path) -> BackupVersion:
        with path.open("rb") as fp:
            header = fp.read(4)
            if header == V3_MARKER:
                return BackupVersion.V3
            if header == V2_MARKER:
                return BackupVersion.V2
        return BackupVersion.V1

    @property
    def reader(self) -> type[BaseBackupReader]:
        if self is BackupVersion.V3:
            return SchemaBackupV3Reader
        if self is BackupVersion.V2 or self is BackupVersion.ANONYMIZE_AVRO:
            return SchemaBackupV2Reader
        if self is BackupVersion.V1:
            return SchemaBackupV1Reader
        assert_never(self)

    @property
    def writer(self) -> type[BackupWriter]:
        if self is BackupVersion.V3:
            return SchemaBackupV3Writer
        if self is BackupVersion.V2:
            return SchemaBackupV2Writer
        if self is BackupVersion.ANONYMIZE_AVRO:
            return AnonymizeAvroWriter
        if self is BackupVersion.V1:
            raise AttributeError("Cannot produce backups for V1")
        assert_never(self)


def __before_sleep(description: str) -> Callable[[RetryCallState], None]:
    """Returns a function to print a user-friendly message before going to sleep in retries.

    :param description: of the action, should compose well with _failed_ and _returned_ as next words.
    :returns: a function that can be used in ``tenacity.retry``'s ``before_sleep`` argument for printing a user-friendly
        message that explains which action failed, that a retry is going to happen, and how to abort if desired.
    """

    def before_sleep(it: RetryCallState) -> None:
        outcome = it.outcome
        if outcome is None:
            result = "did not complete yet"
        elif outcome.failed:
            result = f"failed ({outcome.exception()})"
        else:
            result = f"returned {outcome.result()!r}"
        print(f"{description} {result}, retrying... (Ctrl+C to abort)", file=sys.stderr)

    return before_sleep


def __check_partition_count(topic: str, supplier: Callable[[str], AbstractSet[PartitionMetadata]]) -> None:
    """Checks that the given topic has exactly one partition.

    :param topic: to check.
    :param supplier: of topic partition metadata.
    :raises PartitionCountError: if the topic does not have exactly one partition.
    """
    partition_count = len(supplier(topic))
    if partition_count != 1:
        raise PartitionCountError(
            f"Topic {topic!r} has {partition_count} partitions, but only topics with exactly 1 partition can be backed "
            "up. The schemas topic MUST have exactly 1 partition to ensure perfect ordering of schema updates."
        )


@contextlib.contextmanager
def _admin(config: Config) -> KafkaAdminClient:
    """Creates an automatically closing Kafka admin client.

    :param config: for the client.
    :raises Exception: if client creation fails, concrete exception types are unknown, see Kafka implementation.
    """

    admin = retry(
        before_sleep=__before_sleep("Kafka Admin client creation"),
        reraise=True,
        stop=stop_after_delay(60),  # seconds
        wait=wait_fixed(1),  # seconds
        retry=retry_if_exception_type(KafkaError),
    )(kafka_admin_from_config)(config)

    try:
        yield admin
    finally:
        admin.close()


@retry(
    before_sleep=__before_sleep("Schemas topic creation"),
    reraise=True,
    stop=stop_after_delay(60),  # seconds
    wait=wait_fixed(1),  # seconds
    retry=retry_if_exception_type(KafkaError),
)
def _maybe_create_topic(config: Config, name: str, backup_version: BackupVersion) -> None:
    if backup_version in {BackupVersion.V1, BackupVersion.V2}:
        topic = new_schema_topic_from_config(config)

        if topic.name != name:
            LOG.warning(
                "Not creating topic, because the name %r from the config and the name %r from the CLI differ.",
                topic.name,
                name,
            )
            return
    else:
        topic = NewTopic(
            name=name,
            num_partitions=1,
            replication_factor=config["replication_factor"],
            topic_configs={"cleanup.policy": "compact"},
        )

    with _admin(config) as admin:
        try:
            admin.create_topics([topic], timeout_ms=constants.TOPIC_CREATION_TIMEOUT_MS)
        except TopicAlreadyExistsError:
            LOG.debug("Topic %r already exists", topic.name)
            return

        LOG.info(
            "Created topic %r (partition count: %s, replication factor: %s, config: %s)",
            topic.name,
            topic.num_partitions,
            topic.replication_factor,
            topic.topic_configs,
        )
        return


@contextlib.contextmanager
def _consumer(config: Config, topic: str) -> Iterator[KafkaConsumer]:
    """Creates an automatically closing Kafka consumer client.

    :param config: for the client.
    :param topic: to consume from.
    :raises PartitionCountError: if the topic does not have exactly one partition.
    :raises Exception: if client creation fails, concrete exception types are unknown, see Kafka implementation.
    """

    with kafka_consumer_from_config(config, topic) as consumer:
        __check_partition_count(topic, consumer.partitions_for_topic)
        yield consumer


@contextlib.contextmanager
def _producer(config: Config, topic: str) -> Iterator[KafkaProducer]:
    """Creates an automatically closing Kafka producer client.

    :param config: for the client.
    :param topic: to produce to.
    :raises PartitionCountError: if the topic does not have exactly one partition.
    :raises Exception: if client creation fails, concrete exception types are unknown, see Kafka implementation.
    """
    with kafka_producer_from_config(config) as producer:
        __check_partition_count(topic, producer.partitions_for)
        yield producer


def _normalize_location(input_location: str) -> Path | StdOut:
    if input_location in ("", "-"):
        return "-"
    return Path(input_location).absolute()


def _consume_records(
    consumer: KafkaConsumer,
    topic_partition: TopicPartition,
    poll_timeout: PollTimeout,
) -> Iterator[ConsumerRecord]:
    start_offset: int = consumer.beginning_offsets([topic_partition])[topic_partition]
    end_offset: int = consumer.end_offsets([topic_partition])[topic_partition]
    last_offset = start_offset

    LOG.info(
        "Reading from topic-partition %s:%s (offset %s to %s).",
        topic_partition.topic,
        topic_partition.partition,
        start_offset,
        end_offset,
    )

    if start_offset >= end_offset:
        raise EmptyPartition

    end_offset -= 1  # high watermark to actual end offset

    while True:
        records: Collection[ConsumerRecord] = consumer.poll(poll_timeout.milliseconds).get(topic_partition, [])
        if len(records) == 0:
            raise StaleConsumerError(topic_partition, start_offset, end_offset, last_offset, poll_timeout)
        for record in records:
            yield record
        last_offset = record.offset  # pylint: disable=undefined-loop-variable
        if last_offset >= end_offset:
            break


def _write_partition(
    path: Path | StdOut,
    backend: BackupWriter[B, F],
    consumer: KafkaConsumer,
    topic_partition: TopicPartition,
    poll_timeout: PollTimeout,
    allow_overwrite: bool,
) -> F:
    file_path = backend.start_partition(
        path=path,
        topic_name=topic_partition.topic,
        index=topic_partition.partition,
    )

    with backend.safe_writer(file_path, allow_overwrite) as buffer:
        for record in _consume_records(consumer, topic_partition, poll_timeout):
            backend.store_record(buffer, record)

    filename = file_path.name if isinstance(file_path, Path) else file_path
    return backend.finalize_partition(
        index=topic_partition.partition,
        filename=filename,
    )


def _handle_restore_topic(
    instruction: RestoreTopic,
    config: Config,
    backup_version: BackupVersion,
) -> None:
    _maybe_create_topic(
        config=config,
        name=instruction.name,
        backup_version=backup_version,
    )


def _raise_backup_error(exception: Exception) -> NoReturn:
    raise BackupError("Error while producing restored messages") from exception


def _handle_producer_send(
    instruction: ProducerSend,
    producer: KafkaProducer,
) -> None:
    LOG.debug(
        "Sending kafka msg key: %r, value: %r",
        instruction.key,
        instruction.value,
    )
    producer.send(
        instruction.topic_name,
        key=instruction.key,
        value=instruction.value,
        partition=instruction.partition_index,
        headers=[(key.decode() if key is not None else None, value) for key, value in instruction.headers],
        timestamp_ms=instruction.timestamp,
    ).add_errback(_raise_backup_error)


class SchemaBackup:
    def __init__(
        self,
        config: Config,
        backup_path: str,
        topic_option: str | None = None,
    ) -> None:
        self.config: Final = config
        self.backup_location: Final = _normalize_location(backup_path)
        self.topic_name: Final[str] = topic_option or self.config["topic_name"]
        self.timeout_ms: Final = 1000
        self.timeout_kafka_producer: Final = 5
        self.producer_exception: Exception | None = None

        # Schema key formatter
        self.key_formatter: Final = (
            KeyFormatter()
            if self.topic_name == constants.DEFAULT_SCHEMA_TOPIC or self.config.get("force_key_correction", False)
            else None
        )

    def restore_backup(self) -> None:
        if isinstance(self.backup_location, str):
            raise NotImplementedError("Cannot restore backups from stdin")

        if not self.backup_location.exists():
            raise BackupError("Backup location doesn't exist")

        backup_version = BackupVersion.identify(self.backup_location)
        backend_type = backup_version.reader
        backend = (
            backend_type(
                key_encoder=partial(encode_key, key_formatter=self.key_formatter),
                value_encoder=encode_value,
            )
            if issubclass(backend_type, BaseItemsBackupReader)
            else backend_type()
        )

        LOG.info("Identified backup backend: %s", backend.__class__.__name__)
        LOG.info("Starting backup restore for topic: %r", self.topic_name)

        # We set up an ExitStack context, so that we can enter the producer context only
        # after processing a RestoreTopic instruction.
        with contextlib.ExitStack() as stack:
            producer = None

            for instruction in backend.read(self.backup_location, self.topic_name):
                if isinstance(instruction, RestoreTopic):
                    _handle_restore_topic(instruction, self.config, backup_version=backup_version)
                    producer = stack.enter_context(_producer(self.config, instruction.name))
                elif isinstance(instruction, ProducerSend):
                    if producer is None:
                        raise RuntimeError("Backend has not yet sent RestoreTopic.")
                    _handle_producer_send(instruction, producer)
                else:
                    assert_never(instruction)

    def create(
        self,
        version: Literal[BackupVersion.V3, BackupVersion.V2, BackupVersion.ANONYMIZE_AVRO],
        *,
        poll_timeout: PollTimeout = PollTimeout.default(),
        overwrite: bool = False,
    ) -> None:
        """Creates a backup of the configured topic.

        :param version: Specifies which format version to use for the backup.
        :param poll_timeout: specifies the maximum time to wait for receiving records,
            if not records are received within that time and the target offset has not
            been reached an exception is raised. Defaults to one minute.
        :param overwrite: the output file if it exists.

        :raises Exception: if consumption fails, concrete exception types are unknown,
            see Kafka implementation.
        :raises FileExistsError: if ``overwrite`` is not ``True`` and the file already
            exists, or if the parent directory of the file is not a directory.
        :raises OSError: if writing fails or if the file already exists and is not
            actually a file.
        :raises StaleConsumerError: if no records are received within the given
            ``poll_timeout`` and the target offset has not been reached yet.
        """
        if version is BackupVersion.V3 and not isinstance(self.backup_location, Path):
            raise RuntimeError("Backup format version 3 does not support writing to stdout.")

        start_time = datetime.datetime.now(datetime.timezone.utc)
        backend = version.writer()

        with backend.prepare_location(
            self.topic_name,
            self.backup_location,
        ) as backup_location:
            LOG.info(
                "Started backup in format %s of topic '%s'.",
                version.name,
                self.topic_name,
            )

            # Note: It's expected that we at some point want to introduce handling of
            # multi-partition topics here. The backend interface is built with that in
            # mind, so that .store_metadata() accepts a sequence of data files.
            with _consumer(self.config, self.topic_name) as consumer:
                (partition,) = consumer.partitions_for_topic(self.topic_name)
                topic_partition = TopicPartition(self.topic_name, partition)

                try:
                    data_file = _write_partition(
                        path=backup_location,
                        backend=backend,
                        consumer=consumer,
                        topic_partition=topic_partition,
                        poll_timeout=poll_timeout,
                        allow_overwrite=overwrite,
                    )
                except EmptyPartition:
                    LOG.warning(
                        "Topic partition '%s' is empty, nothing to back up.",
                        topic_partition,
                    )
                    return

            end_time = datetime.datetime.now(datetime.timezone.utc)
            backend.store_metadata(
                path=backup_location,
                topic_name=self.topic_name,
                topic_id=None,
                started_at=start_time,
                finished_at=end_time,
                data_files=[data_file],
            )

        LOG.info(
            "Finished backup in format %s of '%s' to %s after %s seconds.",
            version.name,
            self.topic_name,
            ("stdout" if not isinstance(self.backup_location, Path) else self.backup_location),
            math.ceil((end_time - start_time).total_seconds()),
        )
