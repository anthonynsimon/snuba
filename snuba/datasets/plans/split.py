import copy
import math

from datetime import timedelta
from typing import NamedTuple, Optional, Sequence


from snuba import state, util
from snuba.datasets.plans.query_plan import (
    QueryPlanExecutionStrategy,
    QueryRunner,
)
from snuba.request import Request
from snuba.web import RawQueryResult

# Every time we find zero results for a given step, expand the search window by
# this factor. Based on the assumption that the initial window is 2 hours, the
# worst case (there are 0 results in the database) would have us making 4
# queries before hitting the 90d limit (2+20+200+2000 hours == 92 days).
STEP_GROWTH = 10


class ColumnSplitSpec(NamedTuple):
    """
    Provides the column names needed to perform column splitting.
    id_column represent the identity of the row.

    """

    id_column: str
    project_column: str
    timestamp_column: str

    def get_min_columns(self) -> Sequence[str]:
        return [self.id_column, self.project_column, self.timestamp_column]


class SplitQueryPlanExecutionStrategy(QueryPlanExecutionStrategy):
    def __init__(
        self,
        split_spec: Optional[ColumnSplitSpec],
        default_strategy: QueryPlanExecutionStrategy,
    ) -> None:
        self.__split_spec = split_spec
        self.__default_strategy = default_strategy

    def execute(self, request: Request, runner: QueryRunner) -> RawQueryResult:
        (use_split,) = state.get_configs([("use_split", 0)])
        query_limit = request.query.get_limit()
        limit = query_limit if query_limit is not None else 0
        remaining_offset = request.query.get_offset()
        orderby = util.to_list(request.query.get_orderby())

        common_conditions = use_split and limit and not request.query.get_groupby()

        if common_conditions:
            total_col_count = len(request.query.get_all_referenced_columns())
            column_split_spec = self.__split_spec
            if column_split_spec:
                copied_query = copy.deepcopy(request.query)
                copied_query.set_selected_columns(column_split_spec.get_min_columns())
                min_col_count = len(copied_query.get_all_referenced_columns())
            else:
                min_col_count = None

            if (
                column_split_spec
                and request.query.get_selected_columns()
                and not request.query.get_aggregations()
                and total_col_count > min_col_count
            ):
                return self.__col_split(request, column_split_spec, runner)
            elif orderby[:1] == ["-timestamp"] and remaining_offset < 1000:
                return self.__time_split(request, runner)

        return self.__default_strategy.execute(request, runner)

    def __time_split(self, request: Request, runner: QueryRunner) -> RawQueryResult:
        """
        If a query is:
            - ORDER BY timestamp DESC
            - has no grouping
            - has an offset/limit
            - has a large time range
        We know we have to reverse-sort the entire set of rows to return the small
        chunk at the end of the time range, so optimistically split the time range
        into smaller increments, and start with the last one, so that we can potentially
        avoid querying the entire range.
        """
        date_align, split_step = state.get_configs(
            [("date_align_seconds", 1), ("split_step", 3600)]  # default 1 hour
        )

        query_limit = request.query.get_limit()
        limit = query_limit if query_limit is not None else 0
        remaining_offset = request.query.get_offset()

        to_date = util.parse_datetime(
            request.extensions["timeseries"]["to_date"], date_align
        )
        from_date = util.parse_datetime(
            request.extensions["timeseries"]["from_date"], date_align
        )

        overall_result = None
        split_end = to_date
        split_start = max(split_end - timedelta(seconds=split_step), from_date)
        total_results = 0
        while split_start < split_end and total_results < limit:
            # The query function may mutate the request body during query
            # evaluation, so we need to copy the body to ensure that the query
            # has not been modified in between this call and the next loop
            # iteration, if needed.
            # XXX: The extra data is carried across from the initial response
            # and never updated.
            split_request = copy.deepcopy(request)
            split_request.extensions["timeseries"][
                "from_date"
            ] = split_start.isoformat()
            split_request.extensions["timeseries"]["to_date"] = split_end.isoformat()
            split_request.query.add_conditions(
                [
                    ("timestamp", ">=", split_start.isoformat()),
                    ("timestamp", "<", split_end.isoformat()),
                ]
            )
            # Because its paged, we have to ask for (limit+offset) results
            # and set offset=0 so we can then trim them ourselves.
            split_request.query.set_offset(0)
            split_request.query.set_limit(limit - total_results + remaining_offset)

            result = runner(split_request)

            if overall_result is None:
                overall_result = result
            else:
                overall_result.result["data"].extend(result.result["data"])

            if remaining_offset > 0 and len(overall_result.result["data"]) > 0:
                to_trim = min(remaining_offset, len(overall_result.result["data"]))
                overall_result.result["data"] = overall_result.result["data"][to_trim:]
                remaining_offset -= to_trim

            total_results = len(overall_result.result["data"])

            if total_results < limit:
                if len(result.result["data"]) == 0:
                    # If we got nothing from the last query, expand the range by a static factor
                    split_step = split_step * STEP_GROWTH
                else:
                    # If we got some results but not all of them, estimate how big the time
                    # range should be for the next query based on how many results we got for
                    # our last query and its time range, and how many we have left to fetch.
                    remaining = limit - total_results
                    split_step = split_step * math.ceil(
                        remaining / float(len(result.result["data"]))
                    )

                # Set the start and end of the next query based on the new range.
                split_end = split_start
                try:
                    split_start = max(
                        split_end - timedelta(seconds=split_step), from_date
                    )
                except OverflowError:
                    split_start = from_date

        return overall_result

    def __col_split(
        self, request: Request, column_split_spec: ColumnSplitSpec, runner: QueryRunner,
    ):
        """
        Split query in 2 steps if a large number of columns is being selected.
            - First query only selects event_id and project_id.
            - Second query selects all fields for only those events.
            - Shrink the date range.
        """
        # The query function may mutate the request body during query
        # evaluation, so we need to copy the body to ensure that the query has
        # not been modified by the time we're ready to run the full query.
        minimal_request = copy.deepcopy(request)
        minimal_request.query.set_selected_columns(column_split_spec.get_min_columns())
        result = runner(minimal_request)
        del minimal_request

        if result.result["data"]:
            request = copy.deepcopy(request)

            event_ids = list(
                set(
                    [
                        event[column_split_spec.id_column]
                        for event in result.result["data"]
                    ]
                )
            )
            request.query.add_conditions(
                [(column_split_spec.id_column, "IN", event_ids)]
            )
            request.query.set_offset(0)
            request.query.set_limit(len(event_ids))

            project_ids = list(
                set(
                    [
                        event[column_split_spec.project_column]
                        for event in result.result["data"]
                    ]
                )
            )
            request.extensions["project"]["project"] = project_ids

            timestamp_field = column_split_spec.timestamp_column
            timestamps = [event[timestamp_field] for event in result.result["data"]]
            request.extensions["timeseries"]["from_date"] = util.parse_datetime(
                min(timestamps)
            ).isoformat()
            # We add 1 second since this gets translated to ('timestamp', '<', to_date)
            # and events are stored with a granularity of 1 second.
            request.extensions["timeseries"]["to_date"] = (
                util.parse_datetime(max(timestamps)) + timedelta(seconds=1)
            ).isoformat()

        return runner(request)