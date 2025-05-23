import logging
import typing
from typing import Dict, Optional
from pydantic import BaseModel
from sycamore.schema import Schema, SchemaField

from sycamore.transforms.query import OpenSearchQueryExecutor
from sycamore.data import OpenSearchQuery
from sycamore.utils.nested import dotted_lookup

if typing.TYPE_CHECKING:
    from opensearchpy.client.indices import IndicesClient


class OpenSearchSchemaField(SchemaField):
    """DEPRECATED: Migrating to docset.SchemaField."""

    name: str = "default"


class OpenSearchSchema(BaseModel):
    """DEPRECATED: Migrating to docset.Schema."""

    fields: Dict[str, SchemaField]
    """A mapping from field name to field type and example values."""

    def to_schema(self) -> Schema:
        fields = list()
        for field_name, field in self.fields.items():
            fields.append(SchemaField(**{**field.dict(), "name": field_name}))
        return Schema(fields=fields)


logger = logging.getLogger(__name__)


class OpenSearchSchemaFetcher:
    """Responsible for retrieving the schema associated with a given OpenSearch index."""

    # Size of random samples for each field.
    NUM_EXAMPLES = 1000
    NUM_EXAMPLE_VALUES = 5

    def __init__(self, client: "IndicesClient", index: str, query_executor: OpenSearchQueryExecutor) -> None:
        super().__init__()
        self._client = client
        self._index = index
        self._query_executor = query_executor

    def get_schema(self) -> OpenSearchSchema:
        """Return a mapping from schema field name to a tuple (type, examples) where "type"
        is the type of the field and "examples" is a list of example values for that field."""

        schema = self._client.get_field_mapping(fields=["*"], index=self._index)
        query = OpenSearchQuery()

        logger.debug(f"Getting schema for index {self._index}")
        # Fetch example values.
        query["index"] = self._index
        query["query"] = {
            "query": {"match_all": {}},
            "size": self.NUM_EXAMPLES,
            "sort": [{"_script": {"type": "number", "script": {"source": "Math.random()"}}}],
        }
        random_sample = self._query_executor.query(query)["result"]["hits"]["hits"]

        result = OpenSearchSchema(
            fields={
                "text_representation": OpenSearchSchemaField(
                    field_type="<class 'str'>", description="Can be assumed to have all other details"
                )
            }
        )

        # Get type and example values for each field.
        one_schema = next(iter(schema.values()))  # in case index is an alias
        for key in one_schema["mappings"].keys():
            if key.endswith(".keyword"):
                logger.debug(f"  Ignoring redundant exact match .keyword key {key}")
                continue
            if not key.startswith("properties."):
                logger.debug(f"  Ignoring non-properties key {key} as they are likely not from sycamore")
                continue
            try:
                logger.debug(f"  Attempting to get samples for key {key}")
                samples: set[str] = set()
                sample_type: Optional[type] = None
                warnings: Dict[str, bool] = {}
                for sample in random_sample:
                    if len(samples) >= self.NUM_EXAMPLE_VALUES:
                        break
                    sample_value = dotted_lookup(sample["_source"], key)
                    if sample_value is not None:
                        if not sample_type:
                            sample_type = type(sample_value)
                        else:
                            t = type(sample_value)

                            # Need to check for compatibility between the sample type and the previously
                            # seen type, and possibly upgrade the sample type.
                            if sample_type is int and t is float:
                                # Upgrade from int to float.
                                sample_type = t
                            elif sample_type is float and t is int:
                                # No need to change.
                                pass
                            elif sample_type is list and t is not list:
                                # Upgrade from singleton to list.
                                sample_value = [sample_value]
                            elif sample_type is not list and t is list:
                                # Need to upgrade our sample type to match the new list type.
                                sample_type = t
                                samples = {str([x]) for x in samples}
                            elif sample_type != t:
                                # We have an incompatible type, so promote it to string.
                                if key not in warnings:
                                    logger.warning(
                                        "Got multiple sample types for schema field"
                                        + f" {key}: {sample_type} and {t}. Promoting to str."
                                    )
                                    warnings[key] = True
                                sample_type = str
                                samples = {str(x) for x in samples}

                        samples.add(str(sample_value))

                if len(samples) > 0:
                    logger.debug(f"  Got samples for {key} of type {sample_type}")
                    result.fields[key] = OpenSearchSchemaField(field_type=str(sample_type), examples=list(samples))
                else:
                    logger.debug(f"  No samples for {key}; ignoring key")
            except KeyError:
                logger.debug(f"  Error retrieving samples for {key}")
                # This can happen if there are mappings that have no corresponding values.
                # Skip them.
                continue
        return result
