import calendar
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import md5
from typing import MutableSequence, Sequence

from snuba import settings
from snuba.clickhouse.http import JSONRowEncoder
from snuba.consumer import KafkaMessageMetadata
from snuba.datasets.events_processor_base import InsertEvent
from snuba.datasets.factory import enforce_table_writer, get_dataset
from snuba.datasets.storages.factory import STORAGES
from snuba.processor import InsertBatch, ProcessedMessage
from snuba.redis import redis_client
from snuba.utils.metrics.backends.dummy import DummyMetricsBackend
from snuba.writer import BatchWriterEncoderWrapper, WriterTableRow
from tests.fixtures import raw_event


def _setup() -> None:
    assert (
        settings.TESTING
    ), "settings.TESTING is False, try `SNUBA_SETTINGS=test` or `make test`"

    from snuba.migrations.runner import Runner

    Runner().run_all(force=True)

    redis_client.flushdb()


def _teardown() -> None:
    from snuba.web.views import truncate_storage

    for storage_key in STORAGES:
        truncate_storage(STORAGES[storage_key])
    redis_client.flushdb()


class BaseTest(object):
    def setup_method(self) -> None:
        _setup()

    def teardown_method(self) -> None:
        _teardown()


class BaseStorageTest(object):
    def setup_method(self, test_method, storage_name: str) -> None:
        _setup()
        self.storage_name = storage_name

    def teardown_method(self) -> None:
        _teardown()


class BaseDatasetTest(object):
    def setup_method(self, test_method, dataset_name: str) -> None:
        _setup()
        self.dataset_name = dataset_name
        self.dataset = get_dataset(dataset_name)
        from snuba.web.views import truncate_dataset

        truncate_dataset(self.dataset)

    def write_processed_messages(self, messages: Sequence[ProcessedMessage]) -> None:
        rows: MutableSequence[WriterTableRow] = []
        for message in messages:
            assert isinstance(message, InsertBatch)
            rows.extend(message.rows)
        self.write_rows(rows)

    def write_rows(self, rows: Sequence[WriterTableRow]) -> None:
        BatchWriterEncoderWrapper(
            enforce_table_writer(self.dataset).get_batch_writer(
                metrics=DummyMetricsBackend(strict=True)
            ),
            JSONRowEncoder(),
        ).write(rows)

    def teardown_method(self) -> None:
        _teardown()


class BaseEventsTest(BaseDatasetTest):
    def setup_method(self, test_method, dataset_name="events"):
        super(BaseEventsTest, self).setup_method(test_method, dataset_name)
        self.table = enforce_table_writer(self.dataset).get_schema().get_table_name()
        self.event = self.__get_event()

    def __get_event(self) -> InsertEvent:
        timestamp = datetime.utcnow()

        data = {
            "datetime": (timestamp - timedelta(seconds=2)).strftime(
                settings.PAYLOAD_DATETIME_FORMAT,
            ),
            "received": int(
                calendar.timegm((timestamp - timedelta(seconds=1)).timetuple())
            ),
            **deepcopy(raw_event),
        }

        unique = "%s:%s" % (str(data["project"]), data["id"])
        primary_hash = md5(unique.encode("utf-8")).hexdigest()

        return InsertEvent(
            {
                "event_id": data["id"],
                "group_id": int(primary_hash[:16], 16),
                "primary_hash": primary_hash,
                "project_id": data["project"],
                "message": data["message"],
                "platform": data["platform"],
                "datetime": data["datetime"],
                "data": data,
                "organization_id": data["organization_id"],
                "retention_days": settings.DEFAULT_RETENTION_DAYS,
            }
        )

    def create_event_row_for_date(
        self, dt: datetime, retention_days=settings.DEFAULT_RETENTION_DAYS
    ):
        return {
            "event_id": uuid.uuid4().hex,
            "project_id": 1,
            "group_id": 1,
            "deleted": 0,
            "timestamp": dt,
            "retention_days": retention_days,
        }

    def write_events(self, events: Sequence[InsertEvent]) -> None:
        processor = (
            enforce_table_writer(self.dataset).get_stream_loader().get_processor()
        )

        processed_messages = []
        for i, event in enumerate(events):
            processed_message = processor.process_message(
                (2, "insert", event, {}), KafkaMessageMetadata(i, 0, datetime.now())
            )
            assert processed_message is not None
            processed_messages.append(processed_message)

        self.write_processed_messages(processed_messages)


class BaseApiTest(BaseEventsTest):
    def setup_method(self, test_method, dataset_name="events"):
        super().setup_method(test_method, dataset_name)
        from snuba.web.views import application

        assert application.testing is True
        application.config["PROPAGATE_EXCEPTIONS"] = False
        self.app = application.test_client()
