import asyncio
import datetime
import json
import logging
import os
import re
import resource
from typing import Optional

import aiohttp
import requests
import tiktoken
import litellm
import time

from bespokelabs.curator.request_processor.online_request_processor import (
    OnlineRequestProcessor,
    APIRequest,
    StatusTracker,
)
from bespokelabs.curator.request_processor.generic_request import GenericRequest
from bespokelabs.curator.request_processor.generic_response import TokenUsage, GenericResponse

logger = logging.getLogger(__name__)


def get_token_encoding_name(model_name: str) -> str:
    """Get the token encoding name for a given model."""
    if model_name.startswith("gpt-4"):
        return "cl100k_base"
    elif model_name.startswith("gpt-3.5"):
        return "cl100k_base"
    else:
        return "cl100k_base"  # Default to cl100k_base


def api_endpoint_from_url(request_url: str) -> str:
    """Extract the API endpoint from the request URL.
    This is used to determine the number of tokens consumed by the request.
    """

    # OpenAI API
    match = re.search("^https://[^/]+/v\\d+/(.+)$", request_url)
    if match:
        return match[1]

    # for Azure OpenAI deployment urls
    match = re.search(r"^https://[^/]+/openai/deployments/[^/]+/(.+?)(\?|$)", request_url)
    if match:
        return match[1]

    # Catch all for other API endpoints using OpenAI OpenAPI format
    if "chat/completions" in request_url:
        return "chat/completions"
    elif "completions" in request_url:
        return "completions"
    else:
        raise NotImplementedError(f'API endpoint "{request_url}" not implemented in this script')


class OpenAIOnlineRequestProcessor(OnlineRequestProcessor):
    """OpenAI-specific implementation of the OnlineRequestProcessor.

    Handles API requests to OpenAI's chat completion endpoints with rate limiting,
    token counting, and error handling specific to OpenAI's API.

    Note:
        - Supports both OpenAI and Azure OpenAI endpoints
        - Automatically detects and respects API rate limits
        - Handles token counting using tiktoken
        - Supports structured output via JSON schema
    """

    def __init__(
        self,
        model: str = "gpt-4",
        api_key: str = os.getenv("OPENAI_API_KEY"),
        url: str = "https://api.openai.com/v1/chat/completions",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
    ):
        super().__init__(
            model=model,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
        )
        self.url = url
        self.api_key = api_key
        self.token_encoding = tiktoken.get_encoding(get_token_encoding_name(model))

        # Set resource limits for file descriptors
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(
            resource.RLIMIT_NOFILE, (min(hard, 10 * 3000), hard)
        )  # default to 3000 rpm

    def get_rate_limits(self) -> dict:
        """Get rate limits from OpenAI API headers.

        Returns:
            dict: Contains 'max_requests_per_minute' and 'max_tokens_per_minute'

        Note:
            - Makes a dummy request to get actual rate limits
            - Falls back to default values if headers are missing
            - Supports both OpenAI and Azure endpoints
        """
        response = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": []},
        )

        rpm = int(response.headers.get("x-ratelimit-limit-requests", 0))
        tpm = int(response.headers.get("x-ratelimit-limit-tokens", 0))

        if not rpm or not tpm:
            logger.warning("Failed to get rate limits from OpenAI API, using default values")
            rpm = 30_000
            tpm = 150_000_000

        logger.info(f"Automatically set max_requests_per_minute to {rpm}")
        logger.info(f"Automatically set max_tokens_per_minute to {tpm}")

        return {
            "max_requests_per_minute": rpm,
            "max_tokens_per_minute": tpm,
        }

    def estimate_output_tokens(self) -> int:
        """Estimate number of tokens in the response.

        Returns:
            int: Estimated number of output tokens

        Note:
            Default implementation returns a conservative estimate.
            Override this method for more accurate model-specific estimates.
        """
        try:
            return litellm.get_max_tokens(model=self.model) // 4
        except Exception:
            return 0

    def estimate_total_tokens(self, messages: list) -> int:
        """Estimate total tokens for a request using OpenAI's token counting rules.

        Args:
            messages (list): List of message dictionaries with role and content

        Returns:
            int: Estimated total tokens including message formatting tokens

        Note:
            Includes:
            - 4 tokens per message for formatting
            - Role/name tokens
            - Content tokens
            - 2 tokens for assistant reply priming
        """
        num_tokens = 0
        for message in messages:
            num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
            for key, value in message.items():
                try:
                    num_tokens += len(self.token_encoding.encode(str(value)))
                except TypeError:
                    logger.warning(
                        f"Failed to encode value {value} with tiktoken. Assuming 1 token per 4 chars."
                    )
                    num_tokens += len(str(value)) // 4
                if key == "name":  # if there's a name, the role is omitted
                    num_tokens -= 1  # role is always required and always 1 token

        num_tokens += 2  # every reply is primed with <im_start>assistant
        output_tokens = self.estimate_output_tokens()
        return num_tokens + output_tokens

    def create_api_specific_request(self, generic_request: GenericRequest) -> dict:
        """Create an OpenAI-specific request from a generic request.

        Args:
            generic_request (GenericRequest): Generic request object

        Returns:
            dict: OpenAI API-compatible request dictionary

        Note:
            - Handles JSON schema response format if specified
            - Applies optional parameters (temperature, top_p, etc.)
            - Maintains compatibility with both chat and completion endpoints
        """
        request = {
            "model": generic_request.model,
            "messages": generic_request.messages,
        }
        if generic_request.response_format:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "output_schema",
                    "schema": generic_request.response_format,
                },
            }

        if self.temperature is not None:
            request["temperature"] = self.temperature

        if self.top_p is not None:
            request["top_p"] = self.top_p

        if self.presence_penalty is not None:
            request["presence_penalty"] = self.presence_penalty

        if self.frequency_penalty is not None:
            request["frequency_penalty"] = self.frequency_penalty

        return request

    async def process_single_request(
        self,
        request: APIRequest,
        session: aiohttp.ClientSession,
        retry_queue: asyncio.Queue,
        save_filepath: str,
        status_tracker: StatusTracker,
    ) -> None:
        """Process a single OpenAI API request with error handling and retry logic.

        Args:
            request (APIRequest): The request to process
            session (aiohttp.ClientSession): Async HTTP session
            retry_queue (asyncio.Queue): Queue for failed requests to retry
            save_filepath (str): Path to save response
            status_tracker (StatusTracker): Tracks request status and rate limits

        Note:
            - Handles rate limit errors with exponential backoff
            - Tracks token usage and costs
            - Saves responses immediately to avoid data loss
            - Updates status tracker for monitoring
        """
        try:
            api_endpoint = api_endpoint_from_url(self.url)
            request_header = {"Authorization": f"Bearer {self.api_key}"}
            if "/deployments" in self.url:  # Azure deployment
                request_header = {"api-key": f"{self.api_key}"}

            async with session.post(
                self.url,
                headers=request_header,
                json=request.api_specific_request,
                timeout=60.0,
            ) as response:
                response_json = await response.json()

                if "error" in response_json:
                    status_tracker.num_api_errors += 1
                    error = response_json["error"]
                    if "rate limit" in error.get("message", "").lower():
                        status_tracker.time_of_last_rate_limit_error = time.time()
                        status_tracker.num_rate_limit_errors += 1
                        status_tracker.num_api_errors -= 1
                    raise Exception(f"API error: {error}")

                if response.status != 200:
                    raise Exception(
                        f"API request failed with status {response.status}: {response_json}"
                    )

                response_message = response_json["choices"][0]["message"]["content"]
                usage = response_json["usage"]
                token_usage = TokenUsage(
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                )

                # Calculate cost using litellm
                cost = litellm.completion_cost(completion_response=response_json)

                # Create and save response immediately
                generic_response = GenericResponse(
                    response_message=response_message,
                    response_errors=None,
                    raw_request=request.api_specific_request,
                    raw_response=response_json,
                    generic_request=request.generic_request,
                    created_at=request.created_at,
                    finished_at=datetime.datetime.now(),
                    token_usage=token_usage,
                    response_cost=cost,
                )

                await self.append_generic_response(generic_response, save_filepath)
                status_tracker.num_tasks_in_progress -= 1
                status_tracker.num_tasks_succeeded += 1
                status_tracker.pbar.update(1)

        except Exception as e:
            logger.error(f"Error in API request: {e}")
            status_tracker.num_other_errors += 1
            request.result.append(e)

            if request.attempts_left > 0:
                request.attempts_left -= 1
                retry_queue.put_nowait(request)
            else:
                generic_response = GenericResponse(
                    response_message=None,
                    response_errors=[str(e) for e in request.result],
                    raw_request=request.api_specific_request,
                    raw_response=None,
                    generic_request=request.generic_request,
                    created_at=request.created_at,
                    finished_at=datetime.datetime.now(),
                )
                await self.append_generic_response(generic_response, save_filepath)
                status_tracker.num_tasks_in_progress -= 1
                status_tracker.num_tasks_failed += 1
