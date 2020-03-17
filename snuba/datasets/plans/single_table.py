from typing import Sequence

from snuba.datasets.plans.query_plan import (
    QueryPlanExecutionStrategy,
    QueryRunner,
    StorageQueryPlan,
    StorageQueryPlanBuilder,
)
from snuba.datasets.storage import QueryStorageSelector, ReadableStorage
from snuba.query import RawQueryResult
from snuba.query.query_processor import QueryProcessor
from snuba.request import Request


class SimpleQueryPlanExecutionStrategy(QueryPlanExecutionStrategy):
    def execute(self, request: Request, runner: QueryRunner) -> RawQueryResult:
        return runner(request)


class SingleTableQueryPlanBuilder(StorageQueryPlanBuilder):
    """
    Builds the Storage Query Execution Plan for a dataset that is based on
    a single storage.
    """

    def __init__(
        self, storage: ReadableStorage, post_processors: Sequence[QueryProcessor]
    ) -> None:
        # The storage the query is based on
        self.__storage = storage
        # This is a set of query processors that have to be executed on the
        # query after the storage selection but that are defined by the dataset.
        # Query processors defined by a Storage must be executable independently
        # from the context the Storage is used (whether the storage is used by
        # itself or whether it is joined with another storage).
        # In a joined query we would have processors defined by multiple storages.
        # that would have to be executed only once (like Prewhere). That is a
        # candidate to be added here as post process.
        self.__post_processors = post_processors

    def build_plan(self, request: Request) -> StorageQueryPlan:
        request.query.set_data_source(
            self.__storage.get_schemas().get_read_schema().get_data_source()
        )

        return StorageQueryPlan(
            query_processors=[
                *self.__storage.get_query_processors(),
                *self.__post_processors,
            ],
            execution_strategy=SimpleQueryPlanExecutionStrategy(),
        )


class SelectedTableQueryPlanBuilder(StorageQueryPlanBuilder):
    """
    A query plan builder that selects one of multiple storages in the
    dataset.
    """

    def __init__(
        self, selector: QueryStorageSelector, post_processors: Sequence[QueryProcessor]
    ) -> None:
        self.__selector = selector
        self.__post_processor = post_processors

    def build_plan(self, request: Request) -> StorageQueryPlan:
        storage = self.__selector.select_storage(request.query, request.settings)
        return SingleTableQueryPlanBuilder(storage, self.__post_processor).build_plan(
            request,
        )
