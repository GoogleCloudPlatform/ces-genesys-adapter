# Copyright 2025 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time

import google.auth
from google.auth.transport import requests as google_auth_requests
from google.cloud import secretmanager

from . import config

logger = logging.getLogger(__name__)


class Auth:
    def __init__(self):
        self._token_info = {}
        self._lock = asyncio.Lock()
        self._sm_client = None

    async def get_token(self):
        async with self._lock:
            if config.AUTH_TOKEN_SECRET_PATH:
                # Token-based auth
                now_ms = int(time.time() * 1000)
                if not self._token_info or self._token_info.get("expiry", 0) <= now_ms:
                    logger.info("Auth token is missing or expired, fetching new token.")
                    await self._fetch_token_from_secret_manager()
                return self._token_info["access_token"]
            else:
                # ADC-based auth
                creds, _ = google.auth.default()
                auth_req = google_auth_requests.Request()
                creds.refresh(auth_req)
                return creds.token

    async def _fetch_token_from_secret_manager(self):
        if not self._sm_client:
            self._sm_client = secretmanager.SecretManagerServiceClient()

        secret_path = config.AUTH_TOKEN_SECRET_PATH
        if "/versions/" not in secret_path:
            secret_path = f"{secret_path}/versions/latest"

        try:
            logger.info(f"Fetching auth token from secret manager: {secret_path}")
            response = self._sm_client.access_secret_version(name=secret_path)
            payload = response.payload.data.decode("UTF-8")
            token_data = json.loads(payload)

            if "access_token" not in token_data or "expiry" not in token_data:
                raise ValueError("Secret payload is missing 'access_token' or 'expiry'")

            self._token_info = {
                "access_token": token_data["access_token"],
                "expiry": token_data["expiry"],
            }
            logger.info("Successfully loaded auth token from Secret Manager.")
        except Exception as e:
            logger.error(
                f"Failed to load auth token from Secret Manager: {e}", exc_info=True
            )
            # If we fail, clear the token info to force a retry on the next call.
            self._token_info = {}
            raise

    def verify_request(self, request):
        headers = request.headers
        # API Key Verification
        if headers.get("x-api-key") != config.GENESYS_API_KEY:
            logger.warning("API key verification failed.")
            return False

        # Signature Verification (if client secret is configured)
        if config.GENESYS_CLIENT_SECRET:
            try:
                client_secret = config.GENESYS_CLIENT_SECRET.strip()
                secret = base64.b64decode(client_secret)

                signature_header = headers.get("Signature", "")
                signature_input_header = headers.get("Signature-Input", "")

                if not signature_header or not signature_input_header:
                    logger.warning("Signature or Signature-Input headers missing.")
                    return False

                # Parse Signature header
                match = re.search(r"""sig1=:(.*?):""", signature_header)
                if not match:
                    logger.warning("Could not parse signature from Signature header.")
                    return False
                received_signature_b64 = match.group(1)

                # Parse Signature-Input header
                match_input = re.search(
                    r"""sig1=\((.*?)\);(.*)""", signature_input_header
                )
                if not match_input:
                    logger.warning("Could not parse Signature-Input header.")
                    return False

                signed_components_str = match_input.group(1)
                signature_params_str = match_input.group(2)

                signed_component_names = [
                    comp.strip().strip('''"''')
                    for comp in signed_components_str.split(" ")
                ]

                # Construct the canonical signature base
                signature_base_lines = []
                for component_name in signed_component_names:
                    if component_name == "@request-target":
                        signature_base_lines.append(
                            f""""@request-target": {request.path}"""
                        )
                    elif component_name == "@authority":
                        signature_base_lines.append(
                            f""""@authority": {headers.get("host")}"""
                        )
                    else:
                        header_value = headers.get(component_name)
                        if header_value is None:
                            logger.error(
                                "Missing required header for signature: "
                                f"{component_name}"
                            )
                            return False
                        signature_base_lines.append(
                            f""""{component_name.lower()}": {header_value}"""
                        )

                signature_base_lines.append(
                    f'"@signature-params": ({signed_components_str});'
                    f"{signature_params_str}"
                )
                canonical_signature_base = "\n".join(signature_base_lines)

                # Compute the HMAC-SHA256 hash
                computed_hmac = hmac.new(
                    secret, canonical_signature_base.encode("utf-8"), hashlib.sha256
                ).digest()
                computed_signature_b64 = base64.b64encode(computed_hmac).decode("utf-8")

                # Compare signatures
                if not hmac.compare_digest(
                    computed_signature_b64, received_signature_b64
                ):
                    logger.error("Signature verification failed!")
                    return False

            except Exception as e:
                logger.error(
                    f"An error occurred during signature verification: {e}",
                    exc_info=True,
                )
                return False

        return True


auth_provider = Auth()
