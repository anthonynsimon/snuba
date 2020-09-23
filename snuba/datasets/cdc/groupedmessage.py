from snuba.datasets.dataset import Dataset
from snuba.datasets.entities import EntityKey
from snuba.datasets.entities.factory import get_entity


class GroupedMessageDataset(Dataset):
    def __init__(self) -> None:
        groupedmessages_entity = get_entity(EntityKey.GROUPEDMESSAGES)
        super().__init__(default_entity=groupedmessages_entity)
