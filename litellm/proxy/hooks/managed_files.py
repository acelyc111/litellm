# What is this?
## This hook is used to check for LiteLLM managed files in the request body, and replace them with model-specific file id

import asyncio
import base64
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union, cast

from litellm import Router, verbose_logger
from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.prompt_templates.common_utils import extract_file_data
from litellm.proxy._types import CallTypes, UserAPIKeyAuth
from litellm.types.llms.openai import (
    AllMessageValues,
    ChatCompletionFileObject,
    CreateFileRequest,
    OpenAIFileObject,
    OpenAIFilesPurpose,
)
from litellm.types.utils import LLMResponseTypes, SpecialEnums

if TYPE_CHECKING:
    from opentelemetry.trace import Span as _Span

    from litellm.proxy.utils import InternalUsageCache as _InternalUsageCache
    from litellm.proxy.utils import PrismaClient as _PrismaClient

    Span = Union[_Span, Any]
    InternalUsageCache = _InternalUsageCache
    PrismaClient = _PrismaClient
else:
    Span = Any
    InternalUsageCache = Any
    PrismaClient = Any


class BaseFileEndpoints(ABC):
    @abstractmethod
    async def afile_retrieve(
        self,
        file_id: str,
        litellm_parent_otel_span: Optional[Span],
    ) -> OpenAIFileObject:
        pass

    @abstractmethod
    async def afile_list(
        self, custom_llm_provider: str, **data: dict
    ) -> List[OpenAIFileObject]:
        pass

    @abstractmethod
    async def afile_delete(
        self, custom_llm_provider: str, file_id: str, **data: dict
    ) -> OpenAIFileObject:
        pass


class _PROXY_LiteLLMManagedFiles(CustomLogger):
    # Class variables or attributes
    def __init__(
        self, internal_usage_cache: InternalUsageCache, prisma_client: PrismaClient
    ):
        self.internal_usage_cache = internal_usage_cache
        self.prisma_client = prisma_client

    async def store_unified_file_id(
        self,
        file_id: str,
        file_object: OpenAIFileObject,
        litellm_parent_otel_span: Optional[Span],
    ) -> None:
        key = f"litellm_proxy/{file_id}"
        verbose_logger.info(
            f"Storing LiteLLM Managed File object with id={file_id} in cache"
        )
        await self.internal_usage_cache.async_set_cache(
            key=key,
            value=file_object,
            litellm_parent_otel_span=litellm_parent_otel_span,
        )

    async def get_unified_file_id(
        self, file_id: str, litellm_parent_otel_span: Optional[Span] = None
    ) -> Optional[OpenAIFileObject]:
        key = f"litellm_proxy/{file_id}"
        return await self.internal_usage_cache.async_get_cache(
            key=key,
            litellm_parent_otel_span=litellm_parent_otel_span,
        )

    async def delete_unified_file_id(
        self, file_id: str, litellm_parent_otel_span: Optional[Span] = None
    ) -> OpenAIFileObject:
        key = f"litellm_proxy/{file_id}"
        ## get old value
        old_value = await self.internal_usage_cache.async_get_cache(
            key=key,
            litellm_parent_otel_span=litellm_parent_otel_span,
        )
        if old_value is None or not isinstance(old_value, OpenAIFileObject):
            raise Exception(f"LiteLLM Managed File object with id={file_id} not found")
        ## delete old value
        await self.internal_usage_cache.async_set_cache(
            key=key,
            value=None,
            litellm_parent_otel_span=litellm_parent_otel_span,
        )
        return old_value

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: Dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "pass_through_endpoint",
            "rerank",
        ],
    ) -> Union[Exception, str, Dict, None]:
        """
        - Detect litellm_proxy/ file_id
        - add dictionary of mappings of litellm_proxy/ file_id -> provider_file_id => {litellm_proxy/file_id: {"model_id": id, "file_id": provider_file_id}}
        """
        if call_type == CallTypes.completion.value:
            messages = data.get("messages")
            if messages:
                file_ids = (
                    self.get_file_ids_and_decode_b64_to_unified_uid_from_messages(
                        messages
                    )
                )
                if file_ids:
                    model_file_id_mapping = await self.get_model_file_id_mapping(
                        file_ids, user_api_key_dict.parent_otel_span
                    )

                    data["model_file_id_mapping"] = model_file_id_mapping

        return data

    def get_file_ids_and_decode_b64_to_unified_uid_from_messages(
        self, messages: List[AllMessageValues]
    ) -> List[str]:
        """
        Gets file ids from messages
        """
        file_ids = []
        for message in messages:
            if message.get("role") == "user":
                content = message.get("content")
                if content:
                    if isinstance(content, str):
                        continue
                    for c in content:
                        if c["type"] == "file":
                            file_object = cast(ChatCompletionFileObject, c)
                            file_object_file_field = file_object["file"]
                            file_id = file_object_file_field.get("file_id")
                            if file_id:
                                file_ids.append(
                                    _PROXY_LiteLLMManagedFiles._convert_b64_uid_to_unified_uid(
                                        file_id
                                    )
                                )
                                file_object_file_field[
                                    "file_id"
                                ] = _PROXY_LiteLLMManagedFiles._convert_b64_uid_to_unified_uid(
                                    file_id
                                )
        return file_ids

    @staticmethod
    def _convert_b64_uid_to_unified_uid(b64_uid: str) -> str:
        is_base64_unified_file_id = (
            _PROXY_LiteLLMManagedFiles._is_base64_encoded_unified_file_id(b64_uid)
        )
        if is_base64_unified_file_id:
            return is_base64_unified_file_id
        else:
            return b64_uid

    @staticmethod
    def _is_base64_encoded_unified_file_id(b64_uid: str) -> Union[str, Literal[False]]:
        # Add padding back if needed
        padded = b64_uid + "=" * (-len(b64_uid) % 4)
        # Decode from base64
        try:
            decoded = base64.urlsafe_b64decode(padded).decode()
            if decoded.startswith(SpecialEnums.LITELM_MANAGED_FILE_ID_PREFIX.value):
                return decoded
            else:
                return False
        except Exception:
            return False

    def convert_b64_uid_to_unified_uid(self, b64_uid: str) -> str:
        is_base64_unified_file_id = self._is_base64_encoded_unified_file_id(b64_uid)
        if is_base64_unified_file_id:
            return is_base64_unified_file_id
        else:
            return b64_uid

    async def get_model_file_id_mapping(
        self, file_ids: List[str], litellm_parent_otel_span: Span
    ) -> dict:
        """
        Get model-specific file IDs for a list of proxy file IDs.
        Returns a dictionary mapping litellm_proxy/ file_id -> model_id -> model_file_id

        1. Get all the litellm_proxy/ file_ids from the messages
        2. For each file_id, search for cache keys matching the pattern file_id:*
        3. Return a dictionary of mappings of litellm_proxy/ file_id -> model_id -> model_file_id

        Example:
        {
            "litellm_proxy/file_id": {
                "model_id": "model_file_id"
            }
        }
        """

        file_id_mapping: Dict[str, Dict[str, str]] = {}
        litellm_managed_file_ids = []

        for file_id in file_ids:
            ## CHECK IF FILE ID IS MANAGED BY LITELM
            is_base64_unified_file_id = self._is_base64_encoded_unified_file_id(file_id)

            if is_base64_unified_file_id:
                litellm_managed_file_ids.append(is_base64_unified_file_id)
            elif file_id.startswith(SpecialEnums.LITELM_MANAGED_FILE_ID_PREFIX.value):
                litellm_managed_file_ids.append(file_id)

        if litellm_managed_file_ids:
            # Get all cache keys matching the pattern file_id:*
            for file_id in litellm_managed_file_ids:
                # Search for any cache key starting with this file_id
                cached_values = cast(
                    Dict[str, str],
                    await self.internal_usage_cache.async_get_cache(
                        key=file_id, litellm_parent_otel_span=litellm_parent_otel_span
                    ),
                )
                if cached_values:
                    file_id_mapping[file_id] = cached_values

        return file_id_mapping

    async def async_post_call_success_hook(
        self,
        data: Dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: LLMResponseTypes,
    ) -> Any:
        if isinstance(response, OpenAIFileObject):
            asyncio.create_task(
                self.store_unified_file_id(
                    response.id, response, user_api_key_dict.parent_otel_span
                )
            )

        return None

    @staticmethod
    async def return_unified_file_id(
        file_objects: List[OpenAIFileObject],
        create_file_request: CreateFileRequest,
        purpose: OpenAIFilesPurpose,
        internal_usage_cache: InternalUsageCache,
        litellm_parent_otel_span: Span,
    ) -> OpenAIFileObject:
        ## GET THE FILE TYPE FROM THE CREATE FILE REQUEST
        file_data = extract_file_data(create_file_request["file"])

        file_type = file_data["content_type"]

        unified_file_id = SpecialEnums.LITELLM_MANAGED_FILE_COMPLETE_STR.value.format(
            file_type, str(uuid.uuid4())
        )

        # Convert to URL-safe base64 and strip padding
        base64_unified_file_id = (
            base64.urlsafe_b64encode(unified_file_id.encode()).decode().rstrip("=")
        )

        ## CREATE RESPONSE OBJECT

        response = OpenAIFileObject(
            id=base64_unified_file_id,
            object="file",
            purpose=cast(OpenAIFilesPurpose, purpose),
            created_at=file_objects[0].created_at,
            bytes=file_objects[0].bytes,
            filename=file_objects[0].filename,
            status="uploaded",
        )

        ## STORE RESPONSE IN DB + CACHE
        stored_values: Dict[str, str] = {}
        for file_object in file_objects:
            model_id = file_object._hidden_params.get("model_id")
            if model_id is None:
                verbose_logger.warning(
                    f"Skipping file_object: {file_object} because model_id in hidden_params={file_object._hidden_params} is None"
                )
                continue
            file_id = file_object.id
            stored_values[model_id] = file_id
        await internal_usage_cache.async_set_cache(
            key=unified_file_id,
            value=stored_values,
            litellm_parent_otel_span=litellm_parent_otel_span,
        )

        return response

    async def afile_retrieve(
        self, file_id: str, litellm_parent_otel_span: Optional[Span]
    ) -> OpenAIFileObject:
        stored_file_object = await self.get_unified_file_id(
            file_id, litellm_parent_otel_span
        )
        if stored_file_object:
            return stored_file_object
        else:
            raise Exception(f"LiteLLM Managed File object with id={file_id} not found")

    async def afile_list(
        self,
        purpose: Optional[OpenAIFilesPurpose],
        litellm_parent_otel_span: Optional[Span],
        **data: Dict,
    ) -> List[OpenAIFileObject]:
        return []

    async def afile_delete(
        self,
        file_id: str,
        litellm_parent_otel_span: Optional[Span],
        llm_router: Router,
        **data: Dict,
    ) -> OpenAIFileObject:
        file_id = self.convert_b64_uid_to_unified_uid(file_id)
        model_file_id_mapping = await self.get_model_file_id_mapping(
            [file_id], litellm_parent_otel_span
        )
        specific_model_file_id_mapping = model_file_id_mapping.get(file_id)
        if specific_model_file_id_mapping:
            for model_id, file_id in specific_model_file_id_mapping.items():
                await llm_router.afile_delete(model=model_id, file_id=file_id, **data)  # type: ignore

        stored_file_object = await self.delete_unified_file_id(
            file_id, litellm_parent_otel_span
        )
        if stored_file_object:
            return stored_file_object
        else:
            raise Exception(f"LiteLLM Managed File object with id={file_id} not found")

    async def afile_content(
        self,
        file_id: str,
        litellm_parent_otel_span: Optional[Span],
        llm_router: Router,
        **data: Dict,
    ) -> str:
        """
        Get the content of a file from first model that has it
        """
        initial_file_id = file_id
        unified_file_id = self.convert_b64_uid_to_unified_uid(file_id)
        model_file_id_mapping = await self.get_model_file_id_mapping(
            [unified_file_id], litellm_parent_otel_span
        )
        specific_model_file_id_mapping = model_file_id_mapping.get(unified_file_id)
        if specific_model_file_id_mapping:
            exception_dict = {}
            for model_id, file_id in specific_model_file_id_mapping.items():
                try:
                    return await llm_router.afile_content(model=model_id, file_id=file_id, **data)  # type: ignore
                except Exception as e:
                    exception_dict[model_id] = str(e)
            raise Exception(
                f"LiteLLM Managed File object with id={initial_file_id} not found. Checked model id's: {specific_model_file_id_mapping.keys()}. Errors: {exception_dict}"
            )
        else:
            raise Exception(
                f"LiteLLM Managed File object with id={initial_file_id} not found"
            )
